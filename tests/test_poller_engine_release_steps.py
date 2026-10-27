"""Step pull requests: merge one part of a card and keep working on the rest (#2730).

A release normally ends its card: Done, a closing comment, the issue closed and the record
forgotten. A pull request whose body marks it a *step* is released exactly the same way, and
only the end differs — the issue stays open, the merged branch's worktrees go, and the card is
handed back to its worker with a turn queued for the next part. These tests hold that end, the
resumability of it, and the rule that a later release never mistakes an earlier step for its own.
"""

from __future__ import annotations

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, run_wake, store_for

from agentdeck.poller_engine import cycle, release
from agentdeck.poller_engine.ports import DeployState, EffectKind, LinkedPRs, PrState
from agentdeck.poller_engine.reconcile import CardSnapshot

_KEY = "store#5"
_PR = "store#7"
_NEXT = "store#9"
_WORKTREE = "/var/tmp/checkout/.worktrees/issue-5"


def released(extra=None):
    """A card whose release is watching its armed pull request merge."""
    rec = {
        "status": "In progress",
        "mode": "merge",
        "started": True,
        "started_at": _NOW.isoformat(),
        "generation": "gen1",
        "merge_control": "3",
        "worktrees": [_WORKTREE],
        "awaiting_merge": {"prs": [_PR], "premerged": {}, "since": _NOW.isoformat()},
        "release_step": [_PR],
    }
    rec.update(extra or {})
    return {
        "issues": {_KEY: rec},
        "controls": {
            "3": {
                "id": "3",
                "repo": "store",
                "number": 5,
                "kind": "merge",
                "payload": None,
                "status": "processing",
                "created_at": "t1",
            }
        },
    }


def port_for(*, linked=None, prs=None, status="In progress", **kwargs):
    return FakePort(
        snapshots={_KEY: CardSnapshot(status=status)},
        linked={_KEY: linked or LinkedPRs(merged=[_PR], step=[_PR])},
        pr_states=dict(prs or {_PR: PrState(ref=_PR, state="MERGED", merge_commit="sha1")}),
        **kwargs,
    )


def run(store, port, **kwargs):
    return run_wake(store, port, now=_NOW, **kwargs)


def rec_of(store, key=_KEY):
    return store.read()["issues"].get(key)


def comments_of(port) -> list[str]:
    return [str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT]


# --- the release set ---


def test_only_a_set_of_steps_is_a_step_release():
    rec: dict = {}
    release.note_release_set(rec, refs=[_PR], steps=[_PR])
    assert release.arm_done(rec, merged=[_PR])["step"] is True

    mixed: dict = {}
    release.note_release_set(mixed, refs=[_PR, _NEXT], steps=[_PR])
    assert release.arm_done(mixed, merged=[_PR, _NEXT])["step"] is False

    plain: dict = {}
    release.note_release_set(plain, refs=[_PR], steps=[])
    assert release.arm_done(plain, merged=[_PR])["step"] is False

    nothing: dict = {"release_step": [_PR]}
    release.note_release_set(nothing, refs=[], steps=[])
    assert "release_step" not in nothing


def test_the_assessment_records_whether_the_release_is_all_steps(tmp_path):
    ready = PrState(ref=_PR, state="OPEN", head="aaa", check_state="green", merge_state="CLEAN")
    state = released()
    rec = state["issues"][_KEY]
    for field in ("awaiting_merge", "release_step"):
        rec.pop(field)
    port = port_for(linked=LinkedPRs(open={_PR: "aaa"}, step=[_PR]), prs={_PR: ready})
    store = store_for(tmp_path, state)
    run(store, port)
    assert port.armed == [_PR]
    assert rec_of(store)["release_step"] == [_PR]


# --- the end of a step release ---


def test_a_merged_step_keeps_the_card_open_and_hands_it_back(tmp_path):
    port = port_for()
    store = store_for(tmp_path, released())
    outcome = run(store, port)

    kinds = port.kinds()
    assert EffectKind.CLOSE_ISSUE not in kinds
    assert "Done" not in [e.value for e in port.applied if e.kind is EffectKind.SET_STATUS]
    removed = [e for e in port.applied if e.kind is EffectKind.REMOVE_WORKTREES]
    assert removed and removed[0].value == [_WORKTREE]
    assert any(body.startswith("**Step merged.** Merged `store#7`") for body in comments_of(port))
    assert outcome.report.releases == 1

    rec = rec_of(store)
    assert rec is not None  # the card is not forgotten
    assert rec["steps_merged"] == [_PR]
    assert rec["branch"] == "agent/issue-5-step2"
    for gone in ("mode", "merged", "done_pending", "awaiting_merge", "release_step"):
        assert gone not in rec
    assert store.read()["controls"]["3"]["status"] == "done"

    # The worker's next turn starts in the same wake, on the new branch, told what merged.
    directive = next(d for d in outcome.directives if d["number"] == 5)
    assert directive["action"] == "followup"
    assert directive["branch"] == "agent/issue-5-step2"
    (message,) = directive["messages"]
    assert message["author"] == "engine:release-step"
    assert "`store#7` has merged" in message["payload"]
    assert rec["status"] == "In progress"


def test_a_step_finish_that_died_half_way_resumes_without_repeating(tmp_path):
    intent = {
        "merged": [_PR],
        "deploy_links": [],
        "recovered": False,
        "step": True,
        "steps": {"comment": True, "worktrees": True, "status": False, "queued": False},
    }
    state = released({"done_pending": intent, "merged": [_PR], "branch": "agent/issue-5-step2"})
    state["issues"][_KEY].pop("awaiting_merge")
    port = port_for()
    store = store_for(tmp_path, state)
    outcome = run(store, port)
    assert not any("Step merged" in body for body in comments_of(port))
    assert EffectKind.REMOVE_WORKTREES not in port.kinds()
    assert rec_of(store)["steps_merged"] == [_PR]
    assert [d for d in outcome.directives if d["number"] == 5]


def test_a_step_with_a_deploy_continues_only_once_the_deploy_is_green(tmp_path):
    port = port_for()
    store = store_for(tmp_path, released())
    run(store, port, auto_deploy_repos={"store"})
    rec = rec_of(store)
    assert rec["awaiting_deploy"]["builds"] == {"store": {"sha": "sha1", "status": "pending"}}
    assert "steps_merged" not in rec

    port = port_for(deploys={"store": DeployState(status="ok", link="https://ci/1")})
    run(store, port, auto_deploy_repos={"store"})
    rec = rec_of(store)
    assert rec["steps_merged"] == [_PR]
    assert EffectKind.CLOSE_ISSUE not in port.kinds()
    assert any("Production deploy(s): [store](https://ci/1)" in b for b in comments_of(port))


def test_a_release_that_also_carries_a_completion_pr_finishes_the_card(tmp_path):
    port = port_for(
        linked=LinkedPRs(merged=[_PR, _NEXT], step=[_PR]),
        prs={
            _PR: PrState(ref=_PR, state="MERGED", merge_commit="sha1"),
            _NEXT: PrState(ref=_NEXT, state="MERGED", merge_commit="sha2"),
        },
    )
    state = released()
    rec = state["issues"][_KEY]
    rec.pop("release_step")
    rec["awaiting_merge"]["prs"] = [_PR, _NEXT]
    store = store_for(tmp_path, state)
    run(store, port)
    assert rec_of(store) is None
    assert EffectKind.CLOSE_ISSUE in port.kinds()


# --- the next release ---


def test_the_next_release_does_not_rewatch_an_earlier_steps_deploy(tmp_path):
    ready = PrState(ref=_NEXT, state="OPEN", head="bbb", check_state="green", merge_state="CLEAN")
    state = released({"steps_merged": [_PR]})
    rec = state["issues"][_KEY]
    for field in ("awaiting_merge", "release_step"):
        rec.pop(field)
    port = port_for(
        linked=LinkedPRs(open={_NEXT: "bbb"}, merged=[_PR], step=[_PR]),
        prs={_NEXT: ready, _PR: PrState(ref=_PR, state="MERGED", merge_commit="sha1")},
    )
    store = store_for(tmp_path, state)
    run(store, port, auto_deploy_repos={"store"})
    rec = rec_of(store)
    assert port.armed == [_NEXT]
    assert rec["awaiting_merge"]["premerged"] == {}
    assert "release_step" not in rec  # the completion pull request finishes the card


def test_an_earlier_step_alone_is_nothing_to_release(tmp_path):
    state = released({"steps_merged": [_PR]})
    rec = state["issues"][_KEY]
    for field in ("awaiting_merge", "release_step"):
        rec.pop(field)
    store = store_for(tmp_path, state)
    port = port_for()
    run(store, port)
    assert EffectKind.CLOSE_ISSUE not in port.kinds()
    assert rec_of(store)["status"] == "Blocked"


# --- a card closed and reopened (#2914) ---

_OLD = "store#3"


def test_a_closing_release_remembers_what_it_merged(tmp_path):
    port = port_for(
        linked=LinkedPRs(merged=[_PR, _NEXT], step=[_PR]),
        prs={
            _PR: PrState(ref=_PR, state="MERGED", merge_commit="sha1"),
            _NEXT: PrState(ref=_NEXT, state="MERGED", merge_commit="sha2"),
        },
    )
    state = released({"steps_merged": [_OLD]})
    rec = state["issues"][_KEY]
    rec.pop("release_step")
    rec["awaiting_merge"]["prs"] = [_PR, _NEXT]
    store = store_for(tmp_path, state)
    run(store, port)
    assert rec_of(store) is None
    assert store.read()["released_prs"] == {_KEY: [_OLD, _PR, _NEXT]}


def _reopened(linked_merged):
    """A reopened card's fresh record, its step pull request open and green."""
    state = released()
    rec = state["issues"][_KEY]
    for field in ("awaiting_merge", "release_step"):
        rec.pop(field)
    ready = PrState(ref=_NEXT, state="OPEN", head="bbb", check_state="green", merge_state="CLEAN")
    port = port_for(
        linked=LinkedPRs(open={_NEXT: "bbb"}, merged=linked_merged, step=[_NEXT]),
        prs={_NEXT: ready, **{
            ref: PrState(ref=ref, state="MERGED", merge_commit="sha0") for ref in linked_merged
        }},
    )
    return state, port


def test_a_reopened_cards_old_unmarked_merge_does_not_make_its_step_final(tmp_path):
    state, port = _reopened([_OLD])
    state["released_prs"] = {_KEY: [_OLD]}
    store = store_for(tmp_path, state)
    run(store, port, auto_deploy_repos={"store"})
    rec = rec_of(store)
    assert port.armed == [_NEXT]
    assert rec["release_step"] == [_NEXT]
    assert rec["awaiting_merge"]["premerged"] == {}  # the old merge's deploy is not re-watched
    assert rec["steps_merged"] == [_OLD]
    assert "released_prs" not in store.read()


def test_without_the_memory_the_old_merge_still_reads_as_this_releases(tmp_path):
    """The failure #2914 reported, kept so the test above is known to exercise the fix."""
    state, port = _reopened([_OLD])
    store = store_for(tmp_path, state)
    run(store, port, auto_deploy_repos={"store"})
    assert "release_step" not in rec_of(store)


def test_a_card_handed_back_keeps_its_steps_for_when_it_returns():
    state = {"issues": {_KEY: {"status": "In review", "steps_merged": [_PR]}}}
    cycle._forget(state, _KEY)
    assert state == {"issues": {}, "released_prs": {_KEY: [_PR]}}
    cycle._restore_released(state)  # no record yet: nothing to hand back
    assert state["released_prs"] == {_KEY: [_PR]}
    state["issues"][_KEY] = {"status": "Todo", "steps_merged": [_OLD]}
    cycle._restore_released(state)
    assert state == {"issues": {_KEY: {"status": "Todo", "steps_merged": [_OLD, _PR]}}}


def test_forgetting_a_card_with_nothing_merged_leaves_no_trace():
    state = {"issues": {_KEY: {"status": "Todo"}}}
    cycle._forget(state, _KEY)
    assert state == {"issues": {}}


def test_the_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(cycle, "_RELEASED_CAP", 2)
    state: dict = {"issues": {}}
    for number in (1, 2, 3):
        state["issues"][f"store#{number}"] = {"steps_merged": [f"store#{number + 10}"]}
        cycle._forget(state, f"store#{number}")
    assert list(state["released_prs"]) == ["store#2", "store#3"]


# --- a step merged by hand ---


def _resting_after_a_hand_merge():
    return {
        "issues": {
            _KEY: {
                "status": "In review",
                "started": True,
                "generation": "gen1",
                "worktrees": [_WORKTREE],
            }
        },
        "controls": {},
    }


def test_a_step_a_person_merged_by_hand_enters_the_guarded_release(tmp_path):
    store = store_for(tmp_path, _resting_after_a_hand_merge())
    outcome = run(store, port_for(status="In review"), auto_release=True)
    rec = rec_of(store)
    assert rec["mode"] == "merge"
    assert "steps_merged" not in rec  # not continued in the wake that noticed it
    assert not [d for d in outcome.directives if d["number"] == 5]

    port = port_for(status="In progress")
    outcome = run(store, port, auto_release=True)
    rec = rec_of(store)
    assert rec["steps_merged"] == [_PR]
    assert EffectKind.CLOSE_ISSUE not in port.kinds()
    assert [d for d in outcome.directives if d["number"] == 5]


def test_a_hand_merged_step_waits_for_its_production_deploy(tmp_path):
    store = store_for(tmp_path, _resting_after_a_hand_merge())
    run(store, port_for(status="In review"), auto_release=True, auto_deploy_repos={"store"})
    port = port_for(status="In progress")
    outcome = run(store, port, auto_release=True, auto_deploy_repos={"store"})
    rec = rec_of(store)
    assert rec["awaiting_deploy"]["builds"] == {"store": {"sha": "sha1", "status": "pending"}}
    assert "steps_merged" not in rec
    assert not [d for d in outcome.directives if d["number"] == 5]

    port = port_for(status="In progress", deploys={"store": DeployState(status="ok")})
    outcome = run(store, port, auto_release=True, auto_deploy_repos={"store"})
    assert rec_of(store)["steps_merged"] == [_PR]
    assert EffectKind.CLOSE_ISSUE not in port.kinds()
    assert [d for d in outcome.directives if d["number"] == 5]


def test_a_non_step_merged_by_hand_is_left_alone(tmp_path):
    state = {
        "issues": {_KEY: {"status": "In review", "started": True, "generation": "gen1"}},
        "controls": {},
    }
    port = port_for(status="In review", linked=LinkedPRs(merged=[_PR]))
    store = store_for(tmp_path, state)
    outcome = run(store, port, auto_release=True)
    assert "steps_merged" not in rec_of(store)
    assert outcome.directives == []


def test_a_step_merged_before_the_release_armed_is_recorded_with_it(tmp_path):
    """Both steps of one release are continued past, so neither can continue the card again."""
    early = "store#6"
    port = port_for(
        linked=LinkedPRs(merged=[early, _PR], step=[early, _PR]),
        prs={
            _PR: PrState(ref=_PR, state="MERGED", merge_commit="sha1"),
            early: PrState(ref=early, state="MERGED", merge_commit="sha0"),
        },
    )
    store = store_for(tmp_path, released({"release_step": [early, _PR]}))
    run(store, port)
    assert rec_of(store)["steps_merged"] == [_PR, early]

    # Resting again with nothing open: neither step can continue the card a second time.
    state = {
        "issues": {
            _KEY: {
                "status": "In review",
                "started": True,
                "generation": "gen1",
                "steps_merged": [_PR, early],
            }
        },
        "controls": {},
    }
    store = store_for(tmp_path / "again", state)
    port = port_for(
        status="In review",
        linked=LinkedPRs(merged=[early, _PR], step=[early, _PR]),
        prs={
            _PR: PrState(ref=_PR, state="MERGED", merge_commit="sha1"),
            early: PrState(ref=early, state="MERGED", merge_commit="sha0"),
        },
    )
    outcome = run(store, port, auto_release=True)
    assert not any("Step merged" in body for body in comments_of(port))
    assert outcome.directives == []


def test_a_step_announcement_that_did_not_post_is_retried_before_anything_else(tmp_path):
    """A refused comment stays owed: no later step runs until the next wake posts it."""
    store = store_for(tmp_path, released())
    port = port_for(refuse=(EffectKind.COMMENT,))
    outcome = run(store, port)
    rec = rec_of(store)
    assert rec["done_pending"]["steps"]["comment"] is False
    assert EffectKind.REMOVE_WORKTREES not in port.kinds()
    assert "steps_merged" not in rec
    assert not [d for d in outcome.directives if d["number"] == 5]

    port = port_for()
    outcome = run(store, port)
    assert any("Step merged" in body for body in comments_of(port))
    assert rec_of(store)["steps_merged"] == [_PR]
    assert [d for d in outcome.directives if d["number"] == 5]


def test_a_hand_merged_step_continues_without_release_bits(tmp_path):
    """Neither auto_release nor verify_turn: the step is still noticed and released."""
    store = store_for(tmp_path, _resting_after_a_hand_merge())
    run(store, port_for(status="In review"))
    assert rec_of(store)["mode"] == "merge"

    port = port_for(status="In progress")
    outcome = run(store, port)
    assert rec_of(store)["steps_merged"] == [_PR]
    assert EffectKind.CLOSE_ISSUE not in port.kinds()
    assert [d for d in outcome.directives if d["number"] == 5]


def test_without_release_bits_a_plain_hand_merge_is_left_alone(tmp_path):
    store = store_for(tmp_path, _resting_after_a_hand_merge())
    outcome = run(store, port_for(status="In review", linked=LinkedPRs(merged=[_PR])))
    assert "mode" not in rec_of(store)
    assert outcome.directives == []


def test_a_hand_merged_step_release_never_arms_a_pull_request_opened_after_it(tmp_path):
    """Nothing approved this release, so it covers only what was seen merged."""
    store = store_for(tmp_path, _resting_after_a_hand_merge())
    run(store, port_for(status="In review"))
    assert rec_of(store)["release_only"] == [_PR]

    ready = PrState(ref=_NEXT, state="OPEN", head="bbb", check_state="green", merge_state="CLEAN")
    port = port_for(
        status="In progress",
        linked=LinkedPRs(open={_NEXT: "bbb"}, merged=[_PR], step=[_PR]),
        prs={_NEXT: ready, _PR: PrState(ref=_PR, state="MERGED", merge_commit="sha1")},
    )
    run(store, port)
    rec = rec_of(store)
    assert port.armed == []
    assert rec["steps_merged"] == [_PR]
    assert "release_only" not in rec


def test_a_deploy_blocked_hand_merged_step_is_not_released_again_every_wake(tmp_path):
    """Parked in Ready to merge on a red deploy: the recheck owns it, not a new release."""
    kwargs = {"auto_deploy_repos": {"store"}, "ready_to_merge": "Ready to merge"}
    store = store_for(tmp_path, _resting_after_a_hand_merge())
    run(store, port_for(status="In review"), **kwargs)
    run(store, port_for(status="In progress"), **kwargs)
    failed = {"store": DeployState(status="bad")}
    run(store, port_for(status="In progress", deploys=failed), **kwargs)
    rec = rec_of(store)
    assert rec["status"] == "Ready to merge"
    assert "mode" not in rec and rec["release_blocked"]

    # The next live read sees the same thing it saw before the release: nothing open, every
    # merge a step. It must leave the park alone.
    outcome = cycle.CycleOutcome(report=cycle.CycleReport())
    linked = LinkedPRs(merged=[_PR], step=[_PR])
    assert not cycle._release_hand_merged_step(outcome, key=_KEY, rec=rec, linked=linked, now=_NOW)
    assert "mode" not in rec


def _parked_before_its_merge():
    """The card's own step release parked in Blocked on a red check, nothing merged yet."""
    return {
        "issues": {
            _KEY: {
                "status": "Blocked",
                "started": True,
                "generation": "gen1",
                "worktrees": [_WORKTREE],
                "release_blocked": {"why": "CI red", "status": "Blocked", "generation": "gen1"},
            }
        },
        "controls": {},
    }


def test_a_step_merged_by_hand_after_its_release_parked_in_blocked_continues(tmp_path):
    for bits in (False, True):
        store = store_for(tmp_path / str(bits), _parked_before_its_merge())
        run(store, port_for(status="Blocked"), auto_release=bits)
        rec = rec_of(store)
        assert rec["mode"] == "merge", bits
        assert rec["step_release_started"] == [_PR]
        assert "release_blocked" not in rec

        port = port_for(status="In progress")
        outcome = run(store, port, auto_release=bits)
        rec = rec_of(store)
        assert rec["steps_merged"] == [_PR], bits
        assert "step_release_started" not in rec
        assert [d for d in outcome.directives if d["number"] == 5]


def test_the_same_merged_steps_are_never_released_twice():
    rec = {"step_release_started": [_PR]}
    outcome = cycle.CycleOutcome(report=cycle.CycleReport())
    linked = LinkedPRs(merged=[_PR], step=[_PR])
    assert not cycle._release_hand_merged_step(outcome, key=_KEY, rec=rec, linked=linked, now=_NOW)
    early = "store#6"
    wider = LinkedPRs(merged=[early, _PR], step=[early, _PR])
    assert cycle._release_hand_merged_step(outcome, key=_KEY, rec=rec, linked=wider, now=_NOW)
    assert rec["step_release_started"] == [early, _PR]


def test_a_poller_released_step_parked_on_a_red_deploy_is_not_released_again(tmp_path):
    """No Ready-to-merge column: the park is Blocked, which the hand-merge check now reads."""
    store = store_for(tmp_path, released())
    run(store, port_for(), auto_deploy_repos={"store"})
    port = port_for(deploys={"store": DeployState(status="bad")})
    run(store, port, auto_deploy_repos={"store"})
    rec = rec_of(store)
    assert rec["status"] == "Blocked"
    assert rec["step_release_started"] == [_PR]

    outcome = cycle.CycleOutcome(report=cycle.CycleReport())
    linked = LinkedPRs(merged=[_PR], step=[_PR])
    assert not cycle._release_hand_merged_step(outcome, key=_KEY, rec=rec, linked=linked, now=_NOW)
