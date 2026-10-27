"""A reviewed, green pull request that falls behind is merged, not looped (#2449).

#2421 spent two hours after it was reviewed and green without one line of it changing. Six
times ``master`` moved, a worker rebased, and the rebased head lost its review: the rebase
round's own suite finished after the worker's turn ended, so the head it tested was never bound
as the engine's own move. A verify turn was then asked for a suite that was already running, and
the green run waited up to fifteen minutes for the card's idle re-read — long enough for the
next merge to leave it behind again.

These tests hold the three fixes: an ended rebase round's suite still binds its head, no verify
turn is queued while that head's suite runs, and a new green run gets the card read at once.
The #2122 guarantee is unchanged throughout: only the exact head a rebase round's own suite
tested may reuse the review.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, run_wake, store_for

from agentdeck.poller_engine import release, verification
from agentdeck.poller_engine.ports import EffectKind, LinkedPRs, PrState
from agentdeck.poller_engine.reconcile import CardSnapshot

_KEY = "store#5"
_PR = "store#7"
_WORK = "github_issue:org/store#5"
_EMPTY = hashlib.sha256(b"").hexdigest()


def _tested(head: str) -> dict:
    return {
        "root": "/w",
        "head": head,
        "staged_sha256": _EMPTY,
        "tracked_diff_sha256": _EMPTY,
        "untracked_sha256": _EMPTY,
        "untracked_count": 0,
    }


def _job(*, head: str = "h1", run_id: str = "gen-rebase", state: str = "done", **extra) -> dict:
    job = {
        "id": f"job-{run_id}",
        "purpose": "verification",
        "run_id": run_id,
        "work_id": _WORK,
        "state": state,
        "result_class": "success" if state == "done" else "",
    }
    if state != "queued":
        job["tested_workspace"] = _tested(head)
        job["tested_workspaces"] = {"unresolved:/w": _tested(head)}
    if state == "done":
        job["verified_workspace"] = _tested(head)
        job["verified_workspaces"] = {"unresolved:/w": _tested(head)}
    job.update(extra)
    return job


def _rec(
    *, reviewed_head: str = "h0", kept_run: str | None = "gen-rebase", green=(), due=False
) -> dict:
    """In review after a rebase round: passed at the old head, the round kept under its Run."""
    rec = {
        "status": "In review",
        "started": True,
        "generation": "gen-verify",
        # Read a moment ago, so the idle re-check alone would not read it again this wake.
        "last_live_check": _NOW.isoformat(),
    }
    release.record_review_pass(
        rec, heads={_PR: reviewed_head}, diffs={_PR: "diff-1"}, trees=None, now=_NOW
    )
    for sha in green:
        release.record_tests_pass(rec, head=sha, now=_NOW)
    if due:
        rec.pop("last_live_check")   # its idle re-check has come round
    if kept_run:
        rec["rebase_round_runs"] = [{"refs": [_PR], "run_id": kept_run}]
    return rec


def _due() -> dict:
    """Reviewed and current at h1 with no green run, and its idle re-check due."""
    return _rec(reviewed_head="h1", kept_run=None, due=True)


def _port(*, head: str = "h1", jobs=()) -> FakePort:
    return FakePort(
        snapshots={_KEY: CardSnapshot(status="In review")},
        linked={_KEY: LinkedPRs(open={_PR: head})},
        pr_states={_PR: PrState(ref=_PR, head=head, merge_state="CLEAN")},
        review_diffs={_PR: "diff-1"},
        jobs={job["id"]: job for job in jobs},
    )


def _run(store, port, **kwargs):
    kwargs.setdefault("now", _NOW)
    return run_wake(store, port, auto_release=True, **kwargs)


def _card(store) -> dict:
    return store.read()["issues"][_KEY]


def _released(port) -> bool:
    return any(
        effect.kind is EffectKind.COMMENT and "**Releasing.**" in str(effect.value)
        for effect in port.applied
    )


def _verify_turns(store) -> list[str]:
    from agentdeck.poller_engine.cycle import _VERIFY_CID_PREFIX

    return [cid for cid in store.read().get("controls", {}) if cid.startswith(_VERIFY_CID_PREFIX)]


# --- an ended rebase round's suite still binds its head ---


def test_the_2421_shape_merges_in_the_wake_its_rebase_suite_lands(tmp_path):
    """Rebase round ended, verify round since dispatched, then the rebase suite goes green."""
    store = store_for(tmp_path, {"issues": {_KEY: _rec()}})
    port = _port(jobs=[_job()])

    outcome = _run(store, port)

    assert any("tests green on h1" in line for line in outcome.log)
    assert _released(port)
    rec = _card(store)
    assert "rebase_round_runs" not in rec
    assert "release_recheck" not in rec


def test_a_push_on_top_of_the_rebased_head_still_loses_the_review(tmp_path):
    """The suite tested h1; the pull request now sits at h2 with the same digest (#2122)."""
    store = store_for(tmp_path, {"issues": {_KEY: _rec(green=("h2",))}})
    port = _port(head="h2", jobs=[_job(head="h1")])

    _run(store, port)

    assert not _released(port)
    bits = release.recorded_bits(_card(store))["bits"]
    assert bits[release.ReleaseBit.REVIEW_PASSED.value] is False


def test_a_job_of_another_run_binds_nothing(tmp_path):
    store = store_for(tmp_path, {"issues": {_KEY: _rec(kept_run="gen-rebase")}})
    port = _port(jobs=[_job(run_id="gen-someone-else")])

    _run(store, port)

    assert not _released(port)
    assert release.rebase_round_runs(_card(store)) == [{"refs": [_PR], "run_id": "gen-rebase"}]


def test_a_verify_round_cannot_overwrite_the_kept_rebase_round():
    from agentdeck.poller_engine.cycle import _round_id

    rec: dict = {"generation": "gen-rebase"}
    release.arm_rebase_round(rec, [_PR], round_id=_round_id(["rebase"]))
    release.keep_rebase_round_for_run(rec, run_id="gen-rebase")
    release.retire_pending_rebase_round(rec)
    # The verify round arms the same pending field; the kept entry is elsewhere.
    release.arm_rebase_round(rec, [_PR], round_id=_round_id(["verify"]))
    release.arm_verify_round(rec, round_id=_round_id(["verify"]))
    release.keep_rebase_round_for_run(rec, run_id="gen-verify")   # a verify round: not kept
    assert release.rebase_round_runs(rec) == [{"refs": [_PR], "run_id": "gen-rebase"}]
    assert release.take_rebase_round_for_run(rec, run_id="gen-verify") == {}
    assert release.take_rebase_round_for_run(rec, run_id="gen-rebase") == {_PR: ""}
    assert release.take_rebase_round_for_run(rec, run_id="gen-rebase") == {}


def test_kept_rounds_are_bounded_and_retired_by_fresh_work():
    rec: dict = {}
    for n in range(8):
        rec["rebase_round_pending"] = {"refs": [_PR], "round": f"r{n}"}
        release.keep_rebase_round_for_run(rec, run_id=f"gen{n}")
    assert [entry["run_id"] for entry in release.rebase_round_runs(rec)] == [
        "gen3", "gen4", "gen5", "gen6", "gen7",
    ]
    release.retire_rebase_round_runs(rec)
    assert release.rebase_round_runs(rec) == []



# --- the rebase round's lineage outlives its marker (#2453) ---


def test_a_bound_rebase_round_remembers_which_head_it_came_from(tmp_path):
    rec = _rec()
    rec["rebase_round_runs"] = [{"refs": [_PR], "run_id": "gen-rebase", "from": {_PR: "h0"}}]
    store = store_for(tmp_path, {"issues": {_KEY: rec}})

    _run(store, _port(jobs=[_job()]))

    edges = release.rebase_lineage(_card(store))
    assert [(e["ref"], e["from"], e["to"]) for e in edges] == [(_PR, "h0", "h1")]


def test_a_round_without_a_recorded_source_writes_no_lineage(tmp_path):
    """A round armed before this existed binds its head exactly as before, and no lineage."""
    store = store_for(tmp_path, {"issues": {_KEY: _rec()}})

    _run(store, _port(jobs=[_job()]))

    assert release.rebase_lineage(_card(store)) == []


def test_only_a_pure_rebase_round_records_where_it_started():
    from agentdeck.poller_engine.cycle import (
        _REBASE_CID_PREFIX,
        _VERIFY_CID_PREFIX,
        _rebase_round_sources,
    )

    rebase = f"{_REBASE_CID_PREFIX}{_KEY}:{_PR}@ABC,store#8@?"
    verify = f"{_VERIFY_CID_PREFIX}{_KEY}:{_PR}@def"
    assert _rebase_round_sources([rebase]) == {_PR: "abc"}
    # A verify round may push a code fix, which must never read as a rebase's output.
    assert _rebase_round_sources([rebase, verify]) == {}
    assert _rebase_round_sources([rebase, "c1"]) == {}
    assert _rebase_round_sources([]) == {}


def test_the_source_travels_with_the_round_to_its_run():
    from agentdeck.poller_engine.cycle import _round_id

    rec: dict = {}
    release.arm_rebase_round(
        rec, [_PR], round_id=_round_id(["r"]), sources={_PR: "H0", "store#8": "x"}
    )
    assert rec["rebase_round_pending"]["from"] == {_PR: "h0"}
    release.keep_rebase_round_for_run(rec, run_id="gen-rebase")
    assert release.take_rebase_round_for_run(rec, run_id="gen-rebase") == {_PR: "h0"}



def _late_verdict_setup(tmp_path, *, runs, jobs=()):
    """Reviewed at A, rebased by the engine to B, and a verdict on A arriving now (#2453)."""
    from agentdeck.poller_engine.cycle import _REBASE_CID_PREFIX

    old, new = "a" * 40, "b" * 40
    rec = {"status": "In review", "started": True, "generation": "gen-verify"}
    if runs:
        rec["rebase_round_runs"] = [
            {"refs": [_PR], "run_id": "gen-rebase", "from": {_PR: old}}
        ]
    cid = f"{_REBASE_CID_PREFIX}{_KEY}:{_PR}@{old}"
    control = {
        "id": cid, "repo": "store", "number": 5, "kind": "claude", "payload": "rebase",
        "body": "", "url": "", "author": "engine:release-bits",
        "created_at": _NOW.isoformat(), "status": "done",
    }
    store = store_for(tmp_path, {"issues": {_KEY: rec}, "controls": {cid: control}})
    digest = "d" * 64
    port = _port(head=new, jobs=jobs)
    port._review_diffs = {_PR: digest}
    port.comments.append(comment("v1", f"/reviewed pass {old} {digest} org/{_PR}"))
    return store, port, old, new


def test_a_verdict_before_the_rebase_suite_binds_waits_at_the_reviewed_head(tmp_path):
    """The rebase moved A to B but its suite has not landed: keep the pass at A, carry later."""
    store, port, old, new = _late_verdict_setup(tmp_path, runs=True)

    _run(store, port)

    rec = _card(store)
    assert release.review_pass(rec)["heads"] == {_PR: old}
    assert not _released(port)
    assert release.rebase_round(rec) == {}          # B has no provenance yet
    assert not any(e.kind is EffectKind.COMMENT and "names nothing" in str(e.value)
                   for e in port.applied)

    # The rebase round's own suite lands on B: now the carry-over moves the pass there.
    port.jobs["job-gen-rebase"] = _job(head=new)
    _run(store, port, now=_NOW + timedelta(minutes=1))

    rec = _card(store)
    assert release.review_pass(rec)["heads"] == {_PR: new}
    assert _released(port)


def test_a_verdict_with_no_rebase_round_waiting_and_no_lineage_is_refused(tmp_path):
    store, port, _old, _new = _late_verdict_setup(tmp_path, runs=False)

    _run(store, port)

    assert release.review_pass(_card(store)) is None
    # Refused quietly: the head moved and nothing proves the engine moved it.
    assert store.read()["controls"]["v1"]["rejection"] == "reviewed-head-moved"
    assert not any("names nothing" in str(e.value) for e in port.applied)


def test_a_rebase_round_reports_where_it_started_while_it_waits():
    rec: dict = {
        "rebase_round_pending": {"refs": [_PR], "round": "r", "from": {_PR: "AA"}},
        "rebase_round_runs": [{"refs": [_PR], "run_id": "g", "from": {_PR: "bb"}}],
    }
    assert release.rebase_round_sources(rec, _PR) == {"aa", "bb"}
    assert release.rebase_round_sources(rec, "store#8") == set()
    assert release.rebase_round_sources({}, _PR) == set()


# --- no verify turn while that head's suite runs ---


def test_no_verify_turn_while_the_rebase_suite_is_still_running(tmp_path):
    store = store_for(tmp_path, {"issues": {_KEY: _due()}})
    port = _port(jobs=[_job(state="running")])

    outcome = _run(store, port)

    assert _verify_turns(store) == []
    assert any("suite already running (job job-gen-rebase)" in line for line in outcome.log)


def test_a_queued_suite_that_has_not_read_its_tree_yet_is_waited_for(tmp_path):
    store = store_for(tmp_path, {"issues": {_KEY: _due()}})
    _run(store, _port(jobs=[_job(state="queued")]))
    assert _verify_turns(store) == []


def test_a_suite_running_on_another_head_does_not_hold_the_verify_turn(tmp_path):
    store = store_for(tmp_path, {"issues": {_KEY: _due()}})
    _run(store, _port(jobs=[_job(head="h0", state="running")]))
    assert len(_verify_turns(store)) == 1


def _two_prs(*jobs) -> tuple[dict, FakePort]:
    """A card with two open pull requests, both reviewed and current, neither tested."""
    other = "store#8"
    rec = _due()
    release.record_review_pass(
        rec, heads={_PR: "h1", other: "h2"}, diffs={_PR: "diff-1", other: "diff-2"},
        trees=None, now=_NOW,
    )
    port = FakePort(
        snapshots={_KEY: CardSnapshot(status="In review")},
        linked={_KEY: LinkedPRs(open={_PR: "h1", other: "h2"})},
        pr_states={
            _PR: PrState(ref=_PR, head="h1", merge_state="CLEAN"),
            other: PrState(ref=other, head="h2", merge_state="CLEAN"),
        },
        review_diffs={_PR: "diff-1", other: "diff-2"},
        jobs={job["id"]: job for job in jobs},
    )
    return rec, port


def test_one_running_suite_does_not_hold_back_another_pull_requests_verify_turn(tmp_path):
    """Only h1 has a suite running; h2 has none, so the verify turn is still asked for."""
    rec, port = _two_prs(_job(head="h1", state="running"))
    store = store_for(tmp_path, {"issues": {_KEY: rec}})
    _run(store, port)
    assert len(_verify_turns(store)) == 1


def test_suites_running_on_every_head_hold_the_verify_turn(tmp_path):
    rec, port = _two_prs(
        _job(head="h1", state="running"),
        _job(head="h2", state="running", run_id="gen-other"),
    )
    store = store_for(tmp_path, {"issues": {_KEY: rec}})
    outcome = _run(store, port)
    assert _verify_turns(store) == []
    assert any("suite already running" in line for line in outcome.log)


def test_a_red_suite_gets_the_verify_turn_on_the_next_reading(tmp_path):
    store = store_for(tmp_path, {"issues": {_KEY: _due()}})
    _run(store, _port(jobs=[_job(state="running")]))
    assert _verify_turns(store) == []

    failed = _job(state="done", result_class="failure")
    _run(store, _port(jobs=[failed]), now=_NOW + timedelta(hours=1))
    assert len(_verify_turns(store)) == 1


def test_unfinished_for_heads_is_work_wide_and_case_folded():
    running = _job(state="running", run_id="gen-any")
    assert verification.unfinished_for_heads([running], work_id=_WORK.upper(), heads={"h1"})
    assert not verification.unfinished_for_heads([running], work_id="", heads={"h1"})
    other = _job(state="running", work_id="github_issue:org/store#6")
    assert not verification.unfinished_for_heads([other], work_id=_WORK, heads={"h1"})
    not_verification = _job(state="running", purpose="")
    assert not verification.unfinished_for_heads([not_verification], work_id=_WORK, heads={"h1"})
    assert not verification.unfinished_for_heads(None, work_id=_WORK, heads={"h1"})


# --- a new green run gets the card read at once ---


def test_a_green_credit_is_read_in_the_same_wake_not_after_the_idle_recheck(tmp_path):
    """No rebase involved: reviewed at h1, the verify suite lands green, the card releases."""
    store = store_for(tmp_path, {"issues": {_KEY: _rec(reviewed_head="h1", kept_run=None)}})
    port = _port(jobs=[_job(run_id="gen-verify")])

    _run(store, port)

    assert _released(port)


def test_without_a_new_green_run_the_idle_recheck_still_holds(tmp_path):
    store = store_for(tmp_path, {"issues": {_KEY: _rec(reviewed_head="h1", kept_run=None)}})
    port = _port()
    _run(store, port)
    assert port.snapshot_batches == [] or all(
        _KEY not in [f"{q.repo}#{q.number}" for q in batch] for batch in port.snapshot_batches
    )


def test_the_wanted_read_survives_a_deferred_reading_and_clears_after_a_real_one(tmp_path):
    rec = _rec(reviewed_head="h1", kept_run=None)
    release.want_release_recheck(rec)
    store = store_for(tmp_path, {"issues": {_KEY: rec}})

    deferred = _port()
    deferred.linked_prs = lambda repo, number: None   # discovery incomplete: deferred
    _run(store, deferred)
    assert release.release_recheck_wanted(_card(store))

    _run(store, _port(jobs=[_job(state="running")]))
    assert not release.release_recheck_wanted(_card(store))


def test_a_wanted_read_turns_a_quiet_wake_into_a_full_one(tmp_path):
    rec = _rec(reviewed_head="h1", kept_run=None)
    release.want_release_recheck(rec)
    state = {
        "issues": {_KEY: rec},
        "last_live_wake": (_NOW - timedelta(seconds=30)).isoformat(),
    }
    store = store_for(tmp_path, state)

    outcome = _run(store, _port(jobs=[_job(run_id="gen-verify")]), webhook_quiet=True)

    assert outcome.quiet != "webhook-quiet"


# --- a run that tested nothing earns one more ask (#2475) ---


def _cut_off(exit_code: int = 69, finished_at: float = 2.0) -> dict:
    """The verify turn's suite, finished at h1 without a test result: the pod connection dropped."""
    return _job(
        run_id="gen-verify", state="failed", exit_code=exit_code, result_class="failure",
        finished_at=finished_at,
    )


def _turn_ended(store) -> None:
    """The verify turn's worker reported and the card rests in review again, re-check due."""
    with store.transaction() as state:
        state["issues"][_KEY] = _due()
        for control in state.get("controls", {}).values():
            control["status"] = "done"


def test_a_head_whose_run_tested_nothing_is_asked_once_more_and_only_once(tmp_path):
    """#2472 waited thirteen minutes for a person after its suite's copy-in dropped."""
    store = store_for(tmp_path, {"issues": {_KEY: _due()}})
    _run(store, _port())
    [first] = _verify_turns(store)

    _turn_ended(store)
    later = _NOW + timedelta(hours=1)
    outcome = _run(store, _port(jobs=[_cut_off()]), now=later)
    assert sorted(_verify_turns(store)) == sorted([first, f"{first}:again"])
    assert any("tested nothing -> verify turn queued again" in line for line in outcome.log)

    _turn_ended(store)
    _run(store, _port(jobs=[_cut_off()]), now=later + timedelta(hours=1))
    assert len(_verify_turns(store)) == 2


def test_a_head_first_asked_after_a_cut_off_run_still_gets_only_two_asks(tmp_path):
    """The worker's own suite was cut off twice before any verify turn: the first ask is the
    plain one, and a real failure after it buys nothing more."""
    store = store_for(tmp_path, {"issues": {_KEY: _due()}})
    _run(store, _port(jobs=[_cut_off()]))
    [first] = _verify_turns(store)
    assert not first.endswith(":again")

    _turn_ended(store)
    ran = _job(run_id="gen-verify-2", state="failed", exit_code=1, finished_at=9.0)
    _run(store, _port(jobs=[_cut_off(), ran]), now=_NOW + timedelta(hours=1))
    assert _verify_turns(store) == [first]


def test_a_run_that_tested_and_failed_is_never_asked_again(tmp_path):
    store = store_for(tmp_path, {"issues": {_KEY: _due()}})
    _run(store, _port())
    _turn_ended(store)
    _run(store, _port(jobs=[_cut_off(exit_code=1)]), now=_NOW + timedelta(hours=1))
    assert len(_verify_turns(store)) == 1


def test_only_the_latest_run_on_the_head_decides():
    """An older cut-off run does not count once a later one on the same head really ran."""
    ran = _job(run_id="gen-later", state="failed", exit_code=1, finished_at=5.0)
    jobs = [_cut_off(finished_at=2.0), ran]
    assert verification.ended_untested(jobs, work_id=_WORK, heads={"h1"}) == ""
    assert verification.ended_untested(jobs[:1], work_id=_WORK, heads={"h1"}) == "job-gen-verify"
    # Another head's cut-off run says nothing about this one, and nor does another Work's.
    assert verification.ended_untested(jobs[:1], work_id=_WORK, heads={"h2"}) == ""
    other_work = "github_issue:org/store#6"
    assert verification.ended_untested(jobs[:1], work_id=other_work, heads={"h1"}) == ""
    # The Work id is compared case-folded, as everywhere else it is read.
    upper = _WORK.upper()
    assert verification.ended_untested(jobs[:1], work_id=upper, heads={"h1"}) == "job-gen-verify"
    assert verification.ended_untested(None, work_id=_WORK, heads={"h1"}) == ""


def test_each_head_is_judged_by_its_own_latest_run():
    """Two pull requests: one's later real run does not hide the other's cut-off one."""
    cut = _cut_off(finished_at=2.0)
    other = _job(head="h2", run_id="gen-h2", state="failed", exit_code=1, finished_at=5.0)
    assert verification.ended_untested([cut, other], work_id=_WORK, heads={"h1", "h2"}) == (
        "job-gen-verify"
    )
    assert verification.ended_untested([cut, other], work_id=_WORK, heads={"h2"}) == ""
