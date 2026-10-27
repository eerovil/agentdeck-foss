"""A command pressed on the deck reaches the engine without a comment (#2324, #2419).

The deck leaves a request file beside the instance's state carrying the exact comment the button
stands for; the next wake turns it into the same control that comment becomes. A ``merge`` is
admitted or refused by ``_gate_release`` exactly as the comment would be, and a ``/claude``
instruction is queued for a worker. There is no GitHub comment behind it, so nothing reacts to one.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, run_wake, store_for

from agentdeck.poller_engine import deck_releases
from agentdeck.poller_engine.ports import EffectKind, LinkedPRs, PrState
from agentdeck.poller_engine.reconcile import CardSnapshot

ACTION_ID = "press-0001abcd"
CONTROL_ID = f"deck:{ACTION_ID}"


def _in_review(extra=None):
    rec = {"status": "In review"}
    rec.update(extra or {})
    return {"issues": {"store#5": rec}}


def _ready_port(**kwargs):
    return FakePort(
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
        **kwargs,
    )


def _ask(store, request_id=ACTION_ID, key="store#5", command="/merge"):
    return deck_releases.write(
        store.paths.state, request_id=request_id, key=key, command=command, by="deck"
    )


def test_a_deck_merge_is_admitted_like_a_merge_comment_and_its_file_is_removed(tmp_path):
    port = _ready_port()
    store = store_for(tmp_path, _in_review())
    path = _ask(store)

    outcome = run_wake(store, port, now=_NOW)

    state = store.read()
    rec = state["issues"]["store#5"]
    assert (rec["mode"], rec["status"]) == ("merge", "In progress")
    assert rec["merge_control"] == CONTROL_ID
    control = state["controls"][CONTROL_ID]
    assert (control["kind"], control["status"], control["author"]) == (
        "merge", "processing", "deck:deck"
    )
    assert outcome.directives == []  # a merge is poll-driven, as for the comment
    # No comment stands behind it, so nothing reacts to one.
    assert EffectKind.REACT not in port.kinds()
    assert not path.exists()


def test_a_deck_merge_is_refused_on_the_issue_like_the_comment(tmp_path):
    port = _ready_port()
    store = store_for(tmp_path, _in_review({"pending": ["8"]}))
    _ask(store)

    outcome = run_wake(store, port, now=_NOW)

    control = store.read()["controls"][CONTROL_ID]
    assert (control["status"], control["rejection"]) == ("rejected", "reject-busy")
    refusal = next(effect for effect in outcome.effects if effect.kind is EffectKind.COMMENT)
    assert "active or queued work" in refusal.value.body
    assert EffectKind.REACT not in port.kinds()


def test_a_request_read_again_is_the_same_control_not_a_second_release(tmp_path):
    port = _ready_port()
    store = store_for(tmp_path, _in_review())
    _ask(store)
    run_wake(store, port, now=_NOW)
    # A wake that died before removing the file, or the same press written twice.
    _ask(store)

    later = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert [cid for cid in store.read()["controls"] if cid.startswith("deck:")] == [CONTROL_ID]
    assert later.report.commands_ingested == 0
    assert not deck_releases.request_path(store.paths.state, ACTION_ID).exists()


def test_a_deck_merge_and_a_merge_comment_meet_the_same_gate(tmp_path):
    """The comment path is unchanged beside it: one admits, the other is refused as busy."""
    port = _ready_port(comments=[comment(3, "/merge")])
    store = store_for(tmp_path, _in_review())
    _ask(store)

    run_wake(store, port, now=_NOW)

    controls = store.read()["controls"]
    statuses = sorted((controls["3"]["status"], controls[CONTROL_ID]["status"]))
    assert statuses == ["processing", "rejected"]


def test_a_request_the_engine_does_not_understand_is_discarded(tmp_path):
    port = _ready_port()
    store = store_for(tmp_path, _in_review())
    folder = deck_releases.directory(store.paths.state)
    folder.mkdir(parents=True)
    (folder / "garbage.json").write_text("{not json")
    # No command, and commands the deck may not ask for: a stop, a verdict that does not say
    # what it read (a well-formed one is a deck command since #2736), an empty `/claude`, and
    # one carrying the engine's own marker.
    for name, command in (
        ("review", None),
        ("stop", "/claude stop"),
        ("reviewed", "/reviewed pass abcdef1"),
        ("empty", "/claude"),
        ("marked", "/claude go <!-- claude-bot -->"),
        ("prose", "please merge"),
        ("long", "/claude " + "x" * deck_releases.MAX_COMMAND),
    ):
        (folder / f"{name}.json").write_text(
            json.dumps({"id": f"{ACTION_ID}-{name}", "kind": "claude", "command": command,
                        "key": "store#5"})
        )

    outcome = run_wake(store, port, now=_NOW)

    assert not any(cid.startswith("deck:") for cid in store.read().get("controls", {}))
    assert list(folder.glob("*.json")) == []
    assert sum("discarded" in line for line in outcome.log) == 8


def test_a_merge_written_before_requests_carried_their_command_still_merges(tmp_path):
    """A request left by the #2324 deck, still waiting when this code deployed."""
    port = _ready_port()
    store = store_for(tmp_path, _in_review())
    folder = deck_releases.directory(store.paths.state)
    folder.mkdir(parents=True)
    (folder / f"{ACTION_ID}.json").write_text(
        json.dumps({"id": ACTION_ID, "kind": "merge", "key": "store#5", "by": "deck"})
    )

    run_wake(store, port, now=_NOW)

    control = store.read()["controls"][CONTROL_ID]
    assert (control["kind"], control["status"]) == ("merge", "processing")


def test_a_deck_instruction_is_queued_for_a_worker_like_a_claude_comment(tmp_path):
    """#2419: Rebase, Retry, Fix and the rest reach a worker with no comment to paste."""
    port = FakePort(snapshots={"store#5": CardSnapshot(status="Blocked")})
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Blocked"}}})
    path = _ask(store, command="/claude Rebase: bring the base in, and finish as usual.")

    outcome = run_wake(store, port, now=_NOW)

    state = store.read()
    control = state["controls"][CONTROL_ID]
    assert (control["kind"], control["payload"], control["author"]) == (
        "claude", "Rebase: bring the base in, and finish as usual.", "deck:deck"
    )
    assert control["status"] in {"queued", "processing"}
    rec = state["issues"]["store#5"]
    assert CONTROL_ID in (rec.get("pending") or []) + (rec.get("processing") or [])
    assert rec["work_authority"]["login"] == "deck:deck"
    assert EffectKind.REACT not in port.kinds()
    assert not path.exists()
    assert any("deck claude request" in line for line in outcome.log)


def test_a_deck_instruction_reopens_a_released_closed_card_and_starts_it(tmp_path):
    """#2748: the merge released the card, so state no longer holds it; the deck's `/claude`
    starts a worker and reopens the issue, as the same comment on GitHub would."""
    port = FakePort()
    store = store_for(tmp_path, {"issues": {}})
    _ask(store, command="/claude It only works half the time.")

    outcome = run_wake(store, port, now=_NOW)

    assert len(outcome.directives) == 1, outcome.log
    assert store.read()["issues"]["store#5"]["reopen_if_closed"]

    # The card's first live read finds the issue closed before the request, and reopens it.
    port.snapshots["store#5"] = CardSnapshot(
        status="Done", open=False, owned=False, closed_at="2026-07-25T11:30:00Z"
    )
    run_wake(store, port, now=_NOW + timedelta(seconds=40))

    assert port.kinds().count(EffectKind.REOPEN_ISSUE) == 1
    rec = store.read()["issues"]["store#5"]
    assert "reopen_if_closed" not in rec
    assert rec["status"] == "In progress", "the reopened card must not be cleaned up as closed"


def test_a_deck_instruction_starts_a_released_labelled_card_on_the_wake_that_reads_it(tmp_path):
    """#2845, the #2841 shape: the merge released the card, the issue is open again and still
    wears the label, so the same wake's sweep finds it "new" and reconciliation releases it once
    more. The request read in that wake must still start the worker then, not a wake later."""
    from agentdeck.poller_engine.ownership import LabelEvent

    port = FakePort(
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("e1", "2026-07-25T11:00:00Z")]},
        snapshots={"store#5": CardSnapshot(status="Done", owned=True)},
    )
    store = store_for(
        tmp_path,
        {
            "issues": {},
            "ownership": {
                "initialized": True,
                "events": {"store#5": "e1"},
                "last_complete_scan_at": "2026-07-25T11:59:00+00:00",
            },
        },
    )
    _ask(store, command="/claude Chat page is still slow.")

    outcome = run_wake(store, port, now=_NOW)

    assert len(outcome.directives) == 1, outcome.log
    assert store.read()["issues"]["store#5"]["status"] == "In progress"


def test_a_deck_instruction_older_than_the_close_reopens_nothing(tmp_path):
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="Done", open=False, owned=False,
                                           closed_at="2026-07-25T12:30:00Z")},
    )
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Done"}}})
    deck_releases.write(
        store.paths.state, request_id=ACTION_ID, key="store#5",
        command="/claude one more thing", by="deck", now=_NOW - timedelta(hours=1),
    )

    run_wake(store, port, now=_NOW)

    assert EffectKind.REOPEN_ISSUE not in port.kinds()


def test_the_reviewers_instruction_never_reopens_a_closed_issue(tmp_path):
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="Done", open=False, owned=False,
                                           closed_at="2026-07-25T11:30:00Z")},
    )
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Done"}}})
    deck_releases.write(
        store.paths.state, request_id=ACTION_ID, key="store#5",
        command="/claude Fix the findings below.", by="claude-reviewer", reviewer=True,
    )

    run_wake(store, port, now=_NOW)

    assert EffectKind.REOPEN_ISSUE not in port.kinds()


def test_a_deck_merge_review_is_gated_like_the_comment(tmp_path):
    port = _ready_port()
    store = store_for(tmp_path, _in_review())
    _ask(store, command="/merge review")

    run_wake(store, port, now=_NOW)

    state = store.read()
    control = state["controls"][CONTROL_ID]
    assert (control["kind"], control["author"]) == ("merge-review", "deck:deck")
    assert control["status"] != "new"
    assert state["issues"]["store#5"].get("mode") == "merge-review"
    assert EffectKind.REACT not in port.kinds()


@pytest.mark.parametrize(
    ("request_id", "key", "command"),
    [
        ("short", "store#5", "/merge"),
        ("../../etc/passwd", "store#5", "/merge"),
        (ACTION_ID, "store", "/merge"),
        (ACTION_ID, "store#0", "/merge"),
        (ACTION_ID, "owner/store#5", "/merge"),
        (ACTION_ID, "store#5", "/claude stop"),
        (ACTION_ID, "store#5", "/reviewed pass abcdef1"),
        (ACTION_ID, "store#5", ""),
        (ACTION_ID, "store#5", "merge"),
    ],
    ids=["short-id", "path-id", "no-number", "zero", "owner-in-key", "stop", "reviewed",
         "empty", "no-slash"],
)
def test_a_request_is_written_only_when_the_engine_would_accept_it(
    tmp_path, request_id, key, command
):
    with pytest.raises(deck_releases.RequestError):
        deck_releases.write(
            tmp_path / "state.json", request_id=request_id, key=key, command=command
        )

    assert not deck_releases.directory(tmp_path / "state.json").exists()


def test_a_request_waits_while_the_card_cannot_be_read(tmp_path):
    """A wake that cannot read GitHub leaves the request waiting rather than losing it."""
    port = _ready_port()
    port.graphql_deferred = True
    store = store_for(tmp_path, _in_review())
    path = _ask(store)

    run_wake(store, port, now=_NOW)
    control = store.read().get("controls", {}).get(CONTROL_ID)
    assert path.exists() and control is None  # the halted wake never reached intake

    port.graphql_deferred = False
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert store.read()["issues"]["store#5"]["mode"] == "merge"


# --- a quiet wake does not hold a deck Merge for the ceiling (#2671) ---------------------

_RECENT = (_NOW - timedelta(seconds=30)).isoformat()


def _quiet_in_review(extra=None):
    state = _in_review(extra)
    state["last_live_wake"] = _RECENT
    return state


def test_a_waiting_deck_merge_turns_a_quiet_wake_into_a_full_one(tmp_path):
    port = _ready_port()
    store = store_for(tmp_path, _quiet_in_review())
    _ask(store)

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert not outcome.quiet
    assert not any("live wake skipped" in line for line in outcome.log)
    assert not any("release deferred" in line for line in outcome.log)
    state = store.read()
    assert state["issues"]["store#5"]["merge_control"] == CONTROL_ID
    assert state["last_live_wake"] == _NOW.isoformat()


def test_a_merge_deferred_by_an_earlier_wake_makes_the_next_wake_full(tmp_path):
    """The ledger half: a release still new forces the live read, whoever queued it."""
    port = _ready_port()
    store = store_for(tmp_path, _quiet_in_review())
    _ask(store)
    folder = deck_releases.directory(store.paths.state)
    with store.transaction() as state:
        deck_releases.ingest(state, folder)
    for path in folder.glob("*.json"):
        path.unlink()

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert not outcome.quiet
    assert store.read()["controls"][CONTROL_ID]["status"] == "processing"


def test_reviewer_findings_start_a_worker_on_a_quiet_wake(tmp_path):
    """#2764: the worker had ended In review, and starting the next one reads the card's pull
    requests live. A quiet wake refused that read, so the findings sat queued until the ceiling
    while the review chat had already closed saying they were sent."""
    port = _ready_port(
        pr_states={"store#7": PrState(ref="store#7", head="aaa", merge_state="CLEAN")}
    )
    store = store_for(tmp_path, _quiet_in_review({"started": True}))
    deck_releases.write(
        store.paths.state, request_id=ACTION_ID, key="store#5",
        command="/claude Fix the findings below.", by="claude-reviewer", reviewer=True,
    )

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert not outcome.quiet
    assert not any("linked PRs unreadable" in line for line in outcome.log), outcome.log
    assert len(outcome.directives) == 1, outcome.log
    rec = store.read()["issues"]["store#5"]
    assert CONTROL_ID in (rec.get("processing") or [])


def test_a_deck_instruction_already_queued_does_not_hold_every_wake_full(tmp_path):
    """Only the waiting file forces the live read: one full wake per request, as a comment's
    webhook gives. A `/claude` deferred in the ledger (a held card, a full worker cap) waits for
    the ceiling like a comment's would, rather than making every wake full."""
    port = _ready_port()
    store = store_for(tmp_path, _quiet_in_review())
    _ask(store, command="/claude rebase this")
    folder = deck_releases.directory(store.paths.state)
    with store.transaction() as state:
        deck_releases.ingest(state, folder)
    for path in folder.glob("*.json"):
        path.unlink()

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.quiet == "webhook-quiet"
    assert port.snapshot_batches == []


def test_a_settled_or_malformed_merge_does_not_force_a_full_wake(tmp_path):
    port = _ready_port()
    store = store_for(
        tmp_path,
        {
            **_quiet_in_review(),
            "controls": {
                "c1": {"id": "c1", "repo": "store", "number": 5, "kind": "merge",
                       "status": "rejected", "created_at": _RECENT},
            },
        },
    )
    deck_releases.directory(store.paths.state).mkdir(parents=True, exist_ok=True)
    (deck_releases.directory(store.paths.state) / "junk.json").write_text("{not json")

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.quiet == "webhook-quiet"


# --- the Claude fallback reviewer's verdict (#2736) ---

_HEAD = "a" * 40
_DIFF = "d" * 64


def _reviewable(tmp_path, *, diff=_DIFF, head=_HEAD):
    """A resting card with a ready, tested pull request and no review yet."""
    from agentdeck.poller_engine import release
    from agentdeck.poller_engine.ports import PrState

    rec = {"status": "In review", "started": True, "generation": "gen1"}
    release.record_tests_pass(rec, head=_HEAD, now=_NOW)
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={"store#5": LinkedPRs(open={"store#7": head})},
        pr_states={"store#7": PrState(ref="store#7", head=head, merge_state="CLEAN")},
        review_diffs={"store#7": diff},
    )
    return port, store_for(tmp_path, {"issues": {"store#5": rec}})


def test_a_deck_verdict_is_taken_like_the_owners_comment(tmp_path):
    from agentdeck.poller_engine import release

    port, store = _reviewable(tmp_path)
    path = deck_releases.write(
        store.paths.state, request_id=ACTION_ID, key="store#5",
        command=f"/reviewed pass {_HEAD} {_DIFF}", by="claude-reviewer", reviewer=True,
    )

    run_wake(store, port, now=_NOW, auto_release=True)

    state = store.read()
    control = state["controls"][CONTROL_ID]
    assert control["kind"] == "reviewed" and control["status"] == "done"
    assert control["payload"] == {
        "verdict": "pass", "head": _HEAD, "digest": _DIFF, "targets": []
    }
    assert control["author"] == "deck:claude-reviewer"
    rec = state["issues"]["store#5"]
    assert release.review_pass(rec)["diffs"] == {"store#7": _DIFF}
    assert rec["mode"] == "merge"  # the same release a comment's pass leads to
    assert not path.exists()


@pytest.mark.parametrize(
    ("diff", "head", "said"),
    [("9" * 64, _HEAD, "not the diff"), (_DIFF, "e" * 40, None)],
    ids=["wrong-digest", "moved-head"],
)
def test_a_deck_verdict_is_refused_the_same_way_a_comment_is(tmp_path, diff, head, said):
    from agentdeck.poller_engine import release

    port, store = _reviewable(tmp_path, diff=diff, head=head)
    deck_releases.write(
        store.paths.state, request_id=ACTION_ID, key="store#5",
        command=f"/reviewed pass {_HEAD} {_DIFF}", reviewer=True,
    )

    run_wake(store, port, now=_NOW, auto_release=True)

    rec = store.read()["issues"]["store#5"]
    assert release.review_pass(rec) is None
    assert rec.get("mode") != "merge"
    comments = [str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT]
    if said:
        assert any(said in body for body in comments)


def test_a_refused_verdict_sent_again_is_checked_again_not_dropped(tmp_path):
    """``agentdeck deck-request`` re-sends the same id; a refused copy did nothing (#2772)."""
    from agentdeck.poller_engine import release

    port, store = _reviewable(tmp_path, diff="9" * 64)
    command = f"/reviewed pass {_HEAD} {_DIFF}"
    deck_releases.write(
        store.paths.state, request_id=ACTION_ID, key="store#5", command=command,
        reviewer=True, now=_NOW,
    )
    run_wake(store, port, now=_NOW, auto_release=True)
    assert store.read()["controls"][CONTROL_ID]["status"] == "rejected"

    port._review_diffs = {"store#7": _DIFF}
    deck_releases.write(
        store.paths.state, request_id=ACTION_ID, key="store#5", command=command,
        reviewer=True, now=_NOW + timedelta(hours=3),
    )
    later = run_wake(store, port, now=_NOW + timedelta(hours=3), auto_release=True)

    assert later.report.commands_ingested == 1
    assert store.read()["controls"][CONTROL_ID]["status"] == "done"
    assert release.review_pass(store.read()["issues"]["store#5"])["diffs"] == {"store#7": _DIFF}


def test_a_refused_request_file_left_behind_is_not_a_second_attempt(tmp_path):
    """The same file read again — a wake that could not remove it — is not re-queued."""
    port, store = _reviewable(tmp_path, diff="9" * 64)
    state = store.read()
    deck_releases.write(
        store.paths.state, request_id=ACTION_ID, key="store#5",
        command=f"/reviewed pass {_HEAD} {_DIFF}", reviewer=True, now=_NOW,
    )
    folder = deck_releases.directory(store.paths.state)
    deck_releases.ingest(state, folder)
    state["controls"][CONTROL_ID]["status"] = "rejected"

    _done, log, queued = deck_releases.ingest(state, folder)

    assert queued == 0
    assert state["controls"][CONTROL_ID]["status"] == "rejected"
    assert log == [
        f"deck reviewed request {CONTROL_ID} for store#5 is already in the ledger "
        "(rejected); not queued again"
    ]


@pytest.mark.parametrize(
    ("kind", "command"),
    [("merge", "/merge"), ("claude", "/claude Rebase onto master.")],
)
def test_a_refused_command_other_than_a_verdict_keeps_its_one_id(tmp_path, kind, command):
    """Only ``/reviewed`` is replayed (#2772); a refused release or instruction stays refused."""
    store = store_for(tmp_path, _in_review())
    state = store.read()
    folder = deck_releases.directory(store.paths.state)
    deck_releases.write(
        store.paths.state, request_id=ACTION_ID, key="store#5", command=command, now=_NOW
    )
    deck_releases.ingest(state, folder)
    state["controls"][CONTROL_ID]["status"] = "rejected"
    deck_releases.write(
        store.paths.state, request_id=ACTION_ID, key="store#5", command=command,
        now=_NOW + timedelta(hours=1),
    )

    _done, log, queued = deck_releases.ingest(state, folder)

    assert queued == 0
    assert state["controls"][CONTROL_ID]["status"] == "rejected"
    assert "already in the ledger (rejected)" in log[0]


def test_the_same_id_on_a_different_verdict_is_not_a_re_send(tmp_path):
    store = store_for(tmp_path, _in_review())
    state = store.read()
    folder = deck_releases.directory(store.paths.state)
    for moment, head in ((_NOW, _HEAD), (_NOW + timedelta(hours=1), "b" * 40)):
        deck_releases.write(
            store.paths.state, request_id=ACTION_ID, key="store#5",
            command=f"/reviewed pass {head} {_DIFF}", reviewer=True, now=moment,
        )
        _done, log, queued = deck_releases.ingest(state, folder)
        state["controls"][CONTROL_ID]["status"] = "rejected"

    assert queued == 0
    assert _HEAD in state["controls"][CONTROL_ID]["body"]


def test_a_duplicate_of_a_taken_request_is_logged_not_dropped_silently(tmp_path):
    port = _ready_port()
    store = store_for(tmp_path, _in_review())
    _ask(store)
    run_wake(store, port, now=_NOW)
    _ask(store)

    later = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert later.report.commands_ingested == 0
    assert any(
        CONTROL_ID in line and "already in the ledger (processing)" in line for line in later.log
    )


def test_a_reviewer_request_reads_as_the_outside_reviewer_and_a_button_does_not(tmp_path):
    """Findings from the fallback reviewer are an app's handoff, as ChatGPT's comment is."""
    reviewer = deck_releases.write(
        tmp_path / "state.json", request_id="reviewer-0001", key="store#5",
        command="/claude Fix the findings below.", reviewer=True,
    )
    button = deck_releases.write(
        tmp_path / "state.json", request_id="button-00001", key="store#5",
        command="/claude Fix the findings below.",
    )

    assert deck_releases._control(json.loads(reviewer.read_text()))["app"] is True
    assert "app" not in deck_releases._control(json.loads(button.read_text()))


@pytest.mark.parametrize(
    "command",
    ["/claude Rebase: bring the base in.", f"/reviewed changes-needed {_HEAD}", "/merge"],
    ids=["claude", "verdict", "merge"],
)
def test_every_waiting_deck_request_wakes_the_live_reads_it_needs(tmp_path, command):
    folder = deck_releases.directory(tmp_path / "state.json")
    assert not deck_releases.request_waiting(folder)

    deck_releases.write(
        tmp_path / "state.json", request_id=ACTION_ID, key="store#5", command=command
    )
    assert deck_releases.request_waiting(folder)
