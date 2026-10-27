"""Dependency handling: a card does not start while something it declares is open (#640).

Three things are held apart here, because they fail in different ways:

* **Reading a declaration** is pure text. A line that *opens* with the phrase and names a card
  is a declaration; prose that merely contains the words is not, and a card naming itself is
  dropped rather than deadlocked.
* **Deciding the hold** is the engine's. It applies at *start* only, it holds without consuming
  the label or the queued instructions, and it explains itself once per distinct hold.
* **Proving a blocker closed** is the port's, and anything it cannot prove closed still blocks.
  That direction is deliberate: waiting is recoverable, starting work somebody said was not
  ready is not.
"""

from __future__ import annotations

from datetime import timedelta

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, run_wake, store_for

from agentdeck.poller_engine import blockers
from agentdeck.poller_engine.ports import EffectKind
from agentdeck.poller_engine.reconcile import CardSnapshot

_KEY = "store#5"


def declared(body: str, *, repo: str = "store", number: int = 5) -> tuple[str, ...]:
    return blockers.declared(body, org="org", repo=repo, number=number)


def comments_of(port) -> list[str]:
    return [str(effect.value) for effect in port.applied if effect.kind is EffectKind.COMMENT]


def rec_of(store, key: str = _KEY) -> dict:
    return store.read()["issues"][key]


# --- reading a written declaration ---

def test_the_documented_line_names_its_blockers():
    assert declared("Blocked by: #12, #13") == ("store#12", "store#13")


def test_a_declaration_may_be_a_bullet_bold_or_another_repository():
    assert declared("- **Blocked by** other#4") == ("other#4",)
    assert declared("> Depends on org/store#9") == ("store#9",)
    assert declared("* Blocked-by: elsewhere/thing#3") == ("elsewhere/thing#3",)


def test_a_pasted_link_is_the_same_declaration_typed_out():
    body = "Blocked by https://github.com/org/store/issues/41"
    # Read once, as one blocker: the link's own number must not also count as a bare `#41`.
    assert declared(body) == ("store#41",)


def test_prose_that_merely_contains_the_words_declares_nothing():
    assert declared("This was blocked by a missing upload API, see #12 for context") == ()
    assert declared("Blocked by the collector rewrite") == ()


def test_a_card_that_names_itself_is_not_deadlocked():
    assert declared("Blocked by #5 and #6", number=5) == ("store#6",)


def test_only_the_declaring_line_is_read():
    body = "Add the /usage page.\n\nBlocked by: #620\n\nSee also #99 for the design."
    assert declared(body) == ("store#620",)


def test_an_empty_body_declares_nothing():
    assert declared("") == () and declared(None or "") == ()


def test_only_a_closed_or_merged_blocker_is_out_of_the_way():
    assert blockers.is_resolved("CLOSED") and blockers.is_resolved("merged")
    assert not blockers.is_resolved("OPEN")
    # Unknown is never proof, which is the whole reason an unreadable ref keeps blocking.
    assert not blockers.is_resolved(None) and not blockers.is_resolved("")


# --- the record's bookkeeping ---

def test_a_hold_applies_only_before_the_card_has_ever_started():
    rec = {"blocked_by": ["store#4"]}
    assert blockers.holding(rec) == ("store#4",)
    # Cold reloads, retries, follow-ups and releases all run on a started card: never stranded.
    assert blockers.holding({**rec, "started": True}) == ()


def test_clearing_the_hold_also_forgets_that_it_was_explained():
    rec: dict = {}
    blockers.record(rec, ["store#4"])
    blockers.mark_announced(rec, ("store#4",))
    assert blockers.announced(rec, ("store#4",))
    assert not blockers.announced(rec, ("store#4", "store#6"))  # a changed hold is said again

    blockers.record(rec, [])
    assert "blocked_by" not in rec and "blocked_notice" not in rec


# --- the wake ---

def test_a_labelled_card_with_an_open_blocker_is_not_started(tmp_path):
    initial = {"issues": {_KEY: {"status": "Todo", "manual_start": True}}}
    port = FakePort(snapshots={_KEY: CardSnapshot(blockers=("store#4",))})
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert any("blocked by store#4" in line for line in outcome.log)
    assert EffectKind.SET_STATUS not in port.kinds()  # not moved to In progress
    rec = rec_of(store)
    # Nothing is spent: the start and the label are still there for the wake that may use them.
    assert rec["manual_start"] is True and rec["blocked_by"] == ["store#4"]
    assert not rec.get("started")


def test_the_hold_is_explained_once_however_many_wakes_it_lasts(tmp_path):
    initial = {"issues": {_KEY: {"status": "Todo", "manual_start": True}}}
    port = FakePort(snapshots={_KEY: CardSnapshot(blockers=("store#4",))})
    store = store_for(tmp_path, initial)

    run_wake(store, port, now=_NOW)
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    run_wake(store, port, now=_NOW + timedelta(seconds=60))

    said = comments_of(port)
    assert len(said) == 1
    assert "store#4" in said[0] and "Waiting on a dependency" in said[0]


def test_a_hold_nobody_can_name_is_explained_in_words_rather_than_as_a_ref(tmp_path):
    """A card whose dependency list the port could not read in full still waits (#684)."""
    initial = {"issues": {_KEY: {"status": "Todo", "manual_start": True}}}
    port = FakePort(snapshots={_KEY: CardSnapshot(blockers=(blockers.UNREAD_DEPENDENCIES,))})
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    said = comments_of(port)[0]
    assert "more dependencies here than could be read" in said
    # Never printed as a card reference: there is no `unread dependencies` issue to go and read.
    assert f"`{blockers.UNREAD_DEPENDENCIES}`" not in said


def test_an_unnameable_hold_beside_a_named_one_says_both(tmp_path):
    said = blockers.notice(("store#4", blockers.UNREAD_DEPENDENCIES))

    assert "`store#4`" in said and "also lists more dependencies" in said
    assert f"`{blockers.UNREAD_DEPENDENCIES}`" not in said


def test_a_dependency_that_changes_is_explained_again(tmp_path):
    initial = {"issues": {_KEY: {"status": "Todo", "manual_start": True}}}
    port = FakePort(snapshots={_KEY: CardSnapshot(blockers=("store#4",))})
    store = store_for(tmp_path, initial)

    run_wake(store, port, now=_NOW)
    port.snapshots[_KEY] = CardSnapshot(blockers=("store#4", "store#6"))
    run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert len(comments_of(port)) == 2


def test_a_refused_explanation_is_retried_and_never_starts_the_work(tmp_path):
    initial = {"issues": {_KEY: {"status": "Todo", "manual_start": True}}}
    port = FakePort(
        snapshots={_KEY: CardSnapshot(blockers=("store#4",))},
        refuse=(EffectKind.COMMENT,),
    )
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert outcome.report.errors  # visible, and said again next wake
    assert "blocked_notice" not in rec_of(store)


def test_the_card_starts_by_itself_on_the_wake_after_the_blocker_closes(tmp_path):
    initial = {"issues": {_KEY: {"status": "Todo", "manual_start": True}}}
    port = FakePort(snapshots={_KEY: CardSnapshot(blockers=("store#4",))})
    store = store_for(tmp_path, initial)

    assert run_wake(store, port, now=_NOW).directives == []

    port.snapshots[_KEY] = CardSnapshot()
    outcome = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert len(outcome.directives) == 1
    assert outcome.directives[0]["action"] == "new"
    rec = rec_of(store)
    assert rec["status"] == "In progress"
    assert "blocked_by" not in rec and "blocked_notice" not in rec


def test_a_command_introducing_a_blocked_card_is_held_on_its_first_wake(tmp_path):
    # The card does not exist until the command creates it, which is *after* reconciliation —
    # so its dependencies are read on the way into dispatch rather than not at all.
    port = FakePort(
        comments=[comment("1", "/claude fix the thing")],
        snapshots={_KEY: CardSnapshot(blockers=("store#4",))},
    )
    store = store_for(tmp_path)

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    # Held, not dropped: the command stays queued for the wake that actually starts the work.
    assert rec_of(store)["pending"] == ["1"]
    assert store.read()["controls"]["1"]["status"] == "queued"


def test_a_held_card_the_engine_has_not_labelled_yet_survives_the_next_wake(tmp_path):
    """#1663: the ownership label goes on at *spawn*, so a held card has never worn one.

    Reading that absence as a hand-back released the card, cancelled the queued instruction
    and forgot what was blocking it — and the durable command id then refused to queue the
    same instruction again, so the promise in the notice above could never come true. It is
    what stranded two tickets whose blocker had closed 70 minutes earlier.
    """
    port = FakePort(
        comments=[comment("1", "/claude fix the thing")],
        snapshots={_KEY: CardSnapshot(owned=False, blockers=("store#4",))},
    )
    store = store_for(tmp_path)

    assert run_wake(store, port, now=_NOW).directives == []
    outcome = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert outcome.directives == []
    rec = rec_of(store)
    assert rec["pending"] == ["1"]
    assert rec["blocked_by"] == ["store#4"]
    assert store.read()["controls"]["1"]["status"] == "queued"


def test_an_unlabelled_held_card_starts_on_the_wake_after_its_blocker_closes(tmp_path):
    port = FakePort(
        comments=[comment("1", "/claude fix the thing")],
        snapshots={_KEY: CardSnapshot(owned=False, blockers=("store#4",))},
    )
    store = store_for(tmp_path)
    run_wake(store, port, now=_NOW)
    run_wake(store, port, now=_NOW + timedelta(seconds=30))

    port.snapshots[_KEY] = CardSnapshot(owned=False)
    outcome = run_wake(store, port, now=_NOW + timedelta(seconds=60))

    assert len(outcome.directives) == 1
    assert outcome.directives[0]["action"] == "new"
    rec = rec_of(store)
    assert rec["status"] == "In progress"
    assert "blocked_by" not in rec


def test_a_command_on_a_card_nobody_can_read_still_runs_and_says_so(tmp_path):
    port = FakePort(comments=[comment("1", "/claude fix the thing")])
    store = store_for(tmp_path)

    outcome = run_wake(store, port, now=_NOW)

    assert len(outcome.directives) == 1
    assert any("dependencies unread: store#5" in line for line in outcome.log)


def test_work_already_under_way_is_never_held_by_a_dependency(tmp_path):
    initial = {
        "issues": {
            _KEY: {"status": "In review", "started": True, "generation": "gen1",
                   "blocked_by": ["store#4"]}
        }
    }
    port = FakePort(
        comments=[comment("1", "/claude one more thing")],
        snapshots={_KEY: CardSnapshot(status="In review", blockers=("store#4",))},
    )
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert len(outcome.directives) == 1
    assert outcome.directives[0]["action"] == "followup"


def test_an_unreadable_card_keeps_the_hold_the_last_readable_wake_proved(tmp_path):
    initial = {"issues": {_KEY: {"status": "Todo", "manual_start": True}}}
    port = FakePort(snapshots={_KEY: CardSnapshot(blockers=("store#4",))})
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)

    port.snapshots.clear()  # the card could not be read this wake
    outcome = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    # An unreadable snapshot is not proof that a blocker closed.
    assert outcome.directives == []
    assert rec_of(store)["blocked_by"] == ["store#4"]


# --- the other half: a card that stopped because something had to land first (#1230) ---
#
# #640 above is a *start* gate. These are cards that started, ran, and reported `blocked`
# naming what they were waiting for. Nothing watched that reference before, so the card sat in
# Blocked with the ownership label on it until a person typed `/claude the blocker merged`.

def _blocked(**extra) -> dict:
    """A card resting in Blocked exactly as `finish blocked --blocked-by` leaves it."""
    return {
        "issues": {
            _KEY: {
                "status": "Blocked",
                "started": True,
                "generation": "gen1",
                "blocked_until": ["store#122"],
                **extra,
            }
        }
    }


def _resting(**extra) -> FakePort:
    return FakePort(snapshots={_KEY: CardSnapshot(status="Blocked")}, **extra)


def test_a_merged_prerequisite_resumes_the_card_with_no_human_comment(tmp_path):
    port = _resting(ref_states={"store#122": "MERGED"})
    store = store_for(tmp_path, _blocked())

    outcome = run_wake(store, port, now=_NOW)

    assert len(outcome.directives) == 1
    assert outcome.directives[0]["action"] == "resume"
    said = comments_of(port)
    assert len(said) == 1
    assert "store#122" in said[0] and "The blocker has landed" in said[0]
    rec = rec_of(store)
    assert rec["status"] == "In progress"
    assert "blocked_until" not in rec


def test_a_closed_blocking_issue_counts_as_landed_too(tmp_path):
    """A blocker does not have to be a pull request, and abandoning one clears it as well."""
    port = _resting(ref_states={"store#122": "CLOSED"})
    store = store_for(tmp_path, _blocked())

    assert len(run_wake(store, port, now=_NOW).directives) == 1


def test_a_blocker_still_open_keeps_the_card_blocked(tmp_path):
    port = _resting(ref_states={"store#122": "OPEN"})
    store = store_for(tmp_path, _blocked())

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    # store#122 is not a card the poller holds, so the one comment is the note that nothing
    # is working on it (#2382) -- never a resume.
    [note] = comments_of(port)
    assert note.startswith("**Nothing is working on `store#122`.**")
    assert any("still waiting on store#122" in line for line in outcome.log)
    assert rec_of(store)["blocked_until"] == ["store#122"]


def test_a_blocker_nobody_could_read_holds_the_card_rather_than_resuming_it(tmp_path):
    """Fails closed, like the start gate: waiting a wake is cheap, resuming onto a
    prerequisite that never landed costs a whole worker turn against the wrong base."""
    port = _resting()  # no scripted state for store#122 at all
    store = store_for(tmp_path, _blocked())

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert rec_of(store)["blocked_until"] == ["store#122"]


def test_the_card_waits_for_every_reference_it_named(tmp_path):
    port = _resting(ref_states={"store#122": "MERGED", "other#7": "OPEN"})
    store = store_for(tmp_path, _blocked(blocked_until=["store#122", "other#7"]))

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    # The hold narrows to what is actually left, so the eventual notice names only that.
    assert rec_of(store)["blocked_until"] == ["other#7"]

    port._ref_states["other#7"] = "MERGED"
    # The very next wake: a card waiting on a blocker is not cooled down like a parked one.
    resumed = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert len(resumed.directives) == 1
    said = comments_of(port)[-1]
    assert "other#7" in said and "store#122" not in said


def test_resuming_happens_once_however_many_wakes_follow(tmp_path):
    port = _resting(ref_states={"store#122": "MERGED"})
    store = store_for(tmp_path, _blocked())

    outcomes = [
        run_wake(store, port, now=_NOW + timedelta(seconds=seconds))
        for seconds in (0, 1000, 2000)
    ]

    assert len(outcomes[0].directives) == 1
    # The hold is spent in the same transaction that arms the resume, so however the card's
    # own delivery then goes, nothing here arms a second one or explains itself twice.
    armed = [
        line for outcome in outcomes for line in outcome.log if "landed -> resuming" in line
    ]
    assert len(armed) == 1
    assert len(comments_of(port)) == 1


# --- the wake after the blocker closes, not the next recheck window (#2241) ---
#
# Blocked is a terminal column, and a terminal card is re-read only every `terminal_recheck_s`
# (fifteen minutes here). A card waiting on a blocker sat there for up to that long after the
# blocker merged, although its own notice promises the wake after — long enough that a person
# watching the board typed `/claude` to move it.

_RECHECK_S = 900


def test_a_worker_reported_hold_resumes_on_the_wake_after_its_blocker_merges(tmp_path):
    port = _resting(ref_states={"store#122": "OPEN"})
    store = store_for(tmp_path, _blocked())
    assert run_wake(store, port, now=_NOW, terminal_recheck_s=_RECHECK_S).directives == []

    port._ref_states["store#122"] = "MERGED"
    outcome = run_wake(
        store, port, now=_NOW + timedelta(seconds=30), terminal_recheck_s=_RECHECK_S
    )

    assert [d["action"] for d in outcome.directives] == ["resume"]
    assert "blocked_until" not in rec_of(store)


def test_a_native_dependency_hold_resumes_on_the_wake_after_it_closes(tmp_path):
    port = _declaring("store#122", holding=("store#122",))
    store = store_for(tmp_path, _hold_less())
    assert run_wake(store, port, now=_NOW, terminal_recheck_s=_RECHECK_S).directives == []
    assert rec_of(store)["blocked_by"] == ["store#122"]

    port.snapshots[_KEY] = CardSnapshot(status="Blocked", dependencies=("store#122",))
    outcome = run_wake(
        store, port, now=_NOW + timedelta(seconds=30), terminal_recheck_s=_RECHECK_S
    )

    assert [d["action"] for d in outcome.directives] == ["resume"]


def test_a_card_parked_in_blocked_by_a_person_still_cools_down(tmp_path):
    """Only a card waiting on something is read every wake; a deliberate park costs nothing."""
    port = _declaring()
    store = store_for(tmp_path, _hold_less())
    run_wake(store, port, now=_NOW, terminal_recheck_s=_RECHECK_S)
    port.snapshot_batches.clear()

    run_wake(store, port, now=_NOW + timedelta(seconds=30), terminal_recheck_s=_RECHECK_S)

    assert all(_KEY not in batch for batch in port.snapshot_batches)


def test_a_card_whose_one_resume_is_spent_cools_down_again(tmp_path):
    """Dependencies that stay closed hold nothing, so re-reading this card would buy nothing."""
    port = _declaring("store#122")
    store = store_for(
        tmp_path, _hold_less(blocked_resumed=blockers.fingerprint(("store#122",)))
    )
    run_wake(store, port, now=_NOW, terminal_recheck_s=_RECHECK_S)
    port.snapshot_batches.clear()

    run_wake(store, port, now=_NOW + timedelta(seconds=30), terminal_recheck_s=_RECHECK_S)

    assert all(_KEY not in batch for batch in port.snapshot_batches)


def test_a_hold_read_every_wake_still_resumes_exactly_once(tmp_path):
    port = _resting(ref_states={"store#122": "MERGED"})
    store = store_for(tmp_path, _blocked())

    outcomes = [
        run_wake(
            store, port, now=_NOW + timedelta(seconds=seconds), terminal_recheck_s=_RECHECK_S
        )
        for seconds in (0, 30, 60, 90)
    ]

    assert sum(len(outcome.directives) for outcome in outcomes) == 1
    assert len(comments_of(port)) == 1


def test_a_card_a_person_already_resumed_is_not_given_a_second_session(tmp_path):
    """The manual `/claude ...` path stays supported, and must not race this one."""
    port = FakePort(
        snapshots={_KEY: CardSnapshot(status="In progress")},
        ref_states={"store#122": "MERGED"},
    )
    store = store_for(tmp_path, _blocked(status="In progress", processing=["1"]))

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert comments_of(port) == []


def test_an_explanation_that_did_not_land_arms_nothing_and_is_retried(tmp_path):
    """A card must never come back to life with no trace in its own thread of why."""
    port = _resting(ref_states={"store#122": "MERGED"}, refuse={EffectKind.COMMENT})
    store = store_for(tmp_path, _blocked())

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert rec_of(store)["blocked_until"] == ["store#122"]

    port.refuse.clear()
    assert len(run_wake(store, port, now=_NOW + timedelta(seconds=1000)).directives) == 1


def test_a_card_whose_label_was_removed_is_left_alone(tmp_path):
    """Removing the ownership label is the safe stop; a landed blocker cannot undo it."""
    port = FakePort(
        snapshots={_KEY: CardSnapshot(status="Blocked", owned=False)},
        ref_states={"store#122": "MERGED"},
    )
    store = store_for(tmp_path, _blocked())

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert comments_of(port) == []


def test_starting_work_forgets_whatever_the_card_was_waiting_on(tmp_path):
    """Otherwise a later blocker closing would arm a resume on top of finished work."""
    port = FakePort(
        comments=[comment("1", "/claude carry on")],
        snapshots={_KEY: CardSnapshot(status="Blocked")},
        ref_states={"store#122": "OPEN"},
    )
    store = store_for(tmp_path, _blocked())

    outcome = run_wake(store, port, now=_NOW)

    assert len(outcome.directives) == 1
    assert "blocked_until" not in rec_of(store)


# --- a card that reached Blocked without recording a hold at all (#1588) ---
#
# Only `finish blocked --blocked-by` writes the hold above. A card held from the start by a
# GitHub native dependency — the mechanism the docs tell every flow to use — records nothing,
# so it had nothing to clear and was never re-examined. It fell back on the card's live
# dependencies: the whole declared list, and the part of it still holding the card.

def _hold_less(**extra) -> dict:
    """A card resting in Blocked with no block-related field of any kind on its record."""
    return {
        "issues": {
            _KEY: {"status": "Blocked", "started": True, "generation": "gen1", **extra}
        }
    }


def _declaring(*dependencies: str, holding: tuple[str, ...] = (), **extra) -> FakePort:
    return FakePort(
        snapshots={
            _KEY: CardSnapshot(
                status="Blocked", dependencies=dependencies, blockers=holding
            )
        },
        **extra,
    )


def test_the_whole_declared_list_stands_in_for_a_hold_once_nothing_holds():
    assert blockers.cleared_dependencies(("store#12", "store#13"), ()) == (
        "store#12", "store#13",
    )


def test_a_card_declaring_no_dependency_offers_nothing_to_clear():
    """Blocked with no dependency is somebody's deliberate parking, not a stuck card."""
    assert blockers.cleared_dependencies((), ()) == ()


def test_one_unproven_dependency_holds_the_whole_card():
    assert blockers.cleared_dependencies(("store#12", "store#13"), ("store#13",)) == ()


def test_a_native_dependency_that_closed_resumes_a_card_that_recorded_no_hold(tmp_path):
    """The clean reproduction: created with a native `blocked_by`, blocker now closed."""
    port = _declaring("store#122")
    store = store_for(tmp_path, _hold_less())

    outcome = run_wake(store, port, now=_NOW)

    assert len(outcome.directives) == 1
    assert outcome.directives[0]["action"] == "resume"
    said = comments_of(port)
    assert len(said) == 1
    assert "store#122" in said[0] and "The blocker has landed" in said[0]
    assert rec_of(store)["status"] == "In progress"


def test_a_card_parked_in_blocked_with_no_dependency_is_left_where_it_is(tmp_path):
    port = _declaring()
    store = store_for(tmp_path, _hold_less())

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert comments_of(port) == []
    assert rec_of(store)["status"] == "Blocked"


def test_a_dependency_still_open_keeps_the_hold_less_card_where_it_is(tmp_path):
    port = _declaring("store#122", holding=("store#122",))
    store = store_for(tmp_path, _hold_less())

    assert run_wake(store, port, now=_NOW).directives == []


def test_a_dependency_nobody_could_read_holds_the_hold_less_card_too(tmp_path):
    """Fails closed exactly as the start gate does: an unreadable page is still a hold."""
    port = _declaring(
        "store#122",
        blockers.UNREAD_DEPENDENCIES,
        holding=(blockers.UNREAD_DEPENDENCIES,),
    )
    store = store_for(tmp_path, _hold_less())

    assert run_wake(store, port, now=_NOW).directives == []


def test_one_set_of_cleared_dependencies_resumes_a_card_only_once(tmp_path):
    """Without the fence, a card whose next worker reports Blocked for a reason it cannot
    name would be resumed on every wake forever: the dependencies stay closed."""
    port = _declaring("store#122")
    store = store_for(
        tmp_path, _hold_less(blocked_resumed=blockers.fingerprint(("store#122",)))
    )

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert comments_of(port) == []


def test_a_card_that_never_ran_is_started_rather_than_resumed(tmp_path):
    """A card held from creation has no branch or worktree to resume into, and its notice
    must not claim it stopped."""
    port = _declaring("store#122")
    store = store_for(tmp_path, _hold_less(started=False))

    outcome = run_wake(store, port, now=_NOW)

    assert len(outcome.directives) == 1
    assert outcome.directives[0]["action"] == "new"
    assert "Work is starting on it now" in comments_of(port)[0]


def test_a_worker_reported_hold_still_outranks_the_declared_list(tmp_path):
    """The refs a worker named are re-read from GitHub; the fallback never short-circuits it."""
    port = FakePort(
        snapshots={
            _KEY: CardSnapshot(status="Blocked", dependencies=("store#122",), blockers=())
        },
        ref_states={"store#122": "OPEN"},
    )
    store = store_for(tmp_path, _blocked())

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert rec_of(store)["blocked_until"] == ["store#122"]


def parsed(*values: str) -> tuple[str, ...]:
    return blockers.parse_refs(values, org="org", repo="store", number=5)


def test_a_worker_reference_needs_no_declaration_line():
    assert parsed("#122") == ("store#122",)
    assert parsed("other#7", "elsewhere/thing#3") == ("other#7", "elsewhere/thing#3")


def test_a_worker_may_paste_the_pull_request_url():
    assert parsed("https://github.com/org/store/pull/122") == ("store#122",)


def test_a_reference_naming_no_card_parses_to_nothing():
    assert parsed("the upload API") == ()


def test_a_card_naming_itself_is_dropped_here_too():
    assert parsed("#5", "#6") == ("store#6",)
