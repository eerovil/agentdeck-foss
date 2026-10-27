"""Slice 10 of #176: the release progression — arm, observe, repair, deploy-gate, finish."""

from __future__ import annotations

from datetime import timedelta

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, payloads, run_wake, store_for

from agentdeck.poller_engine import release
from agentdeck.poller_engine.ports import (
    ArmOutcome,
    CommentBody,
    DeployState,
    EffectKind,
    LinkedPRs,
    PrState,
)
from agentdeck.poller_engine.reconcile import CardSnapshot

_KEY = "store#5"
_PR = "store#7"
_STALE = (_NOW - timedelta(hours=2)).isoformat()


def merge_state(extra=None, *, mode="merge"):
    rec = {
        "status": "In progress",
        "mode": mode,
        "started": True,
        "started_at": _NOW.isoformat(),
        "generation": "gen1",
        "merge_control": "3",
    }
    rec.update(extra or {})
    return {
        "issues": {_KEY: rec},
        "controls": {
            "3": {
                "id": "3", "repo": "store", "number": 5, "kind": "merge",
                "payload": None, "status": "processing", "created_at": "t1",
            }
        },
    }


def port_for(*, prs=None, open_heads=None, release_trees=None, merged=None, **kwargs):
    return FakePort(
        snapshots={_KEY: CardSnapshot(status="In progress")},
        linked={
            _KEY: LinkedPRs(
                open=dict(open_heads or {}),
                merged=list(merged or []),
            )
        },
        release_trees=dict(release_trees or {}),
        pr_states=dict(prs or {}),
        **kwargs,
    )


def ready_pr(ref=_PR, head="aaa"):
    return PrState(ref=ref, state="OPEN", head=head, check_state="green", merge_state="CLEAN")


def run(store, port, **kwargs):
    return run_wake(store, port, now=_NOW, **kwargs)


def rec_of(store, key=_KEY):
    return store.read()["issues"].get(key)


# --- arming ---

def test_a_ready_pr_is_armed_and_watched(tmp_path):
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: ready_pr()})
    store = store_for(tmp_path, merge_state())
    outcome = run(store, port)
    rec = rec_of(store)
    assert port.armed == [_PR]
    assert rec["awaiting_merge"]["prs"] == [_PR]
    assert EffectKind.COMMENT in port.kinds()
    assert "Auto-merge armed" in str(outcome.effects[-1].value)
    assert isinstance(outcome.effects[-1].value, CommentBody)
    assert outcome.effects[-1].value.pull_request_ref == _PR


def test_the_release_progresses_even_over_budget(tmp_path):
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: ready_pr()}, budget=False)
    store = store_for(tmp_path, merge_state())
    outcome = run(store, port)
    assert port.armed == [_PR]
    assert outcome.held is False


def test_auto_merge_disabled_blocks_the_release(tmp_path):
    port = port_for(
        open_heads={_PR: "aaa"}, prs={_PR: ready_pr()}, arm={_PR: ArmOutcome.UNAVAILABLE}
    )
    store = store_for(tmp_path, merge_state())
    run(store, port)
    rec = rec_of(store)
    assert rec["status"] == "Blocked"
    assert "mode" not in rec
    assert EffectKind.REOPEN_ISSUE in port.kinds()
    assert any("auto-merge is disabled" in str(e.value).lower() for e in port.applied)


def test_a_blocked_release_keeps_its_reason_with_the_park_it_made(tmp_path):
    """The intent is cleared once the park lands; its reason stays, tied to that park (#2235)."""
    port = port_for(
        open_heads={_PR: "aaa"}, prs={_PR: ready_pr()}, arm={_PR: ArmOutcome.UNAVAILABLE}
    )
    store = store_for(tmp_path, merge_state())
    run(store, port)
    rec = rec_of(store)
    assert "blocked_pending" not in rec
    kept = rec["release_blocked"]
    assert (kept["status"], kept["generation"]) == ("Blocked", "gen1")
    assert "auto-merge is disabled" in kept["why"].lower()


def test_a_partial_multi_pr_arm_is_cancelled_before_the_release_blocks(tmp_path):
    other = "store#8"
    port = port_for(
        open_heads={_PR: "aaa", other: "bbb"},
        prs={_PR: ready_pr(), other: ready_pr(ref=other, head="bbb")},
        arm={other: ArmOutcome.FAILED},
    )
    store = store_for(tmp_path, merge_state())

    run(store, port)

    assert port.armed == [_PR, other]
    assert port.cancelled_auto_merges == [_PR]
    assert rec_of(store)["status"] == "Blocked"


def test_no_linked_pr_blocks_the_release(tmp_path):
    port = port_for()
    store = store_for(tmp_path, merge_state())
    run(store, port)
    assert rec_of(store)["status"] == "Blocked"


# --- blockers before arming ---

def test_a_behind_branch_is_updated_before_its_ci_is_trusted(tmp_path):
    behind = PrState(ref=_PR, head="aaa", check_state="failing", merge_state="BEHIND")
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: behind})
    store = store_for(tmp_path, merge_state())
    run(store, port)
    assert port.updated_branches == [_PR]
    assert port.armed == []
    assert rec_of(store)["mode"] == "merge"


def test_a_red_pr_spawns_one_repair_worker_under_the_durable_delivery(tmp_path):
    red = PrState(ref=_PR, head="aaa", check_state="failing", merge_state="CLEAN",
                  failed_steps=["Tests"])
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: red})
    store = store_for(tmp_path, merge_state())
    outcome = run(store, port)
    rec = rec_of(store)
    assert rec["fix"]["keep"] is False
    assert rec["fix"]["heads"] == {_PR: "aaa"}
    directive = next(d for d in outcome.directives if d["action"] == "merge-fix")
    # #106 gap 3: the repair spawn carries a receipt like any other delivery.
    assert directive["dispatch_id"] and directive["generation"]
    assert rec["dispatch"]["sent_at"]
    announcement = next(
        effect.value
        for effect in port.applied
        if effect.kind is EffectKind.COMMENT and "needs fixing" in str(effect.value)
    )
    assert isinstance(announcement, CommentBody)
    assert announcement.pull_request_ref == _PR


def test_a_lint_only_failure_repairs_and_may_keep_merging(tmp_path):
    lint = PrState(ref=_PR, head="aaa", check_state="failing", merge_state="CLEAN",
                   failed_steps=["Lint_backend"])
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: lint})
    store = store_for(tmp_path, merge_state())
    run(store, port)
    assert rec_of(store)["fix"]["keep"] is True


def test_the_repair_spawn_is_not_budget_gated(tmp_path):
    red = PrState(ref=_PR, head="aaa", check_state="failing", merge_state="CLEAN",
                  failed_steps=["Tests"])
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: red}, budget=False)
    store = store_for(tmp_path, merge_state())
    outcome = run(store, port)
    assert "fix" in rec_of(store)
    assert len(outcome.directives) == 1


def test_pending_checks_wait_and_then_block_on_timeout(tmp_path):
    pending = PrState(ref=_PR, head="aaa", check_state="pending", merge_state="CLEAN")
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: pending})
    store = store_for(tmp_path, merge_state())
    outcome = run(store, port)
    assert rec_of(store)["mode"] == "merge"
    assert any("waiting" in line for line in outcome.log)

    store = store_for(tmp_path / "late", merge_state({"started_at": _STALE}))
    run(store, port_for(open_heads={_PR: "aaa"}, prs={_PR: pending}))
    assert rec_of(store)["status"] == "Blocked"


# --- the single repair attempt ---

def _with_fix(keep=False, heads=None, at=None, diffs=None):
    extra = {"fix": {"at": at or _NOW.isoformat(), "keep": keep, "heads": heads or {_PR: "aaa"}}}
    if diffs:
        extra["fix"]["diffs"] = dict(diffs)
    return merge_state(extra)


def test_a_repair_that_pushed_and_is_still_broken_blocks(tmp_path):
    red = PrState(ref=_PR, head="zzz", check_state="failing", merge_state="CLEAN",
                  failed_steps=["Tests"])
    port = port_for(open_heads={_PR: "zzz"}, prs={_PR: red})
    store = store_for(tmp_path, _with_fix())
    run(store, port)
    rec = rec_of(store)
    assert rec["status"] == "Blocked"
    assert any("still unresolved" in str(e.value) for e in port.applied)


def test_a_repair_that_changed_code_and_went_green_is_handed_back(tmp_path):
    # A human's `/merge review` approved one exact diff, so a pushed fix still needs eyes
    # (#809 narrowed the hand-back to this case, it did not remove it).
    port = port_for(open_heads={_PR: "zzz"}, prs={_PR: ready_pr(head="zzz")})
    store = store_for(tmp_path, _with_fix(keep=False))
    run(store, port)
    rec = rec_of(store)
    assert rec["status"] == "In review"
    assert "mode" not in rec and "fix" not in rec
    assert port.armed == []


def test_a_repair_that_only_merged_the_base_and_went_green_keeps_merging(tmp_path):
    # #1436: the merge-fix worker pushed one `Merge branch 'master'` commit. The head moved but
    # the effective diff is byte-identical, so the release continues instead of asking the
    # approver to re-read a diff they already approved.
    port = port_for(
        open_heads={_PR: "zzz"},
        prs={_PR: ready_pr(head="zzz")},
        review_diffs={_PR: "diff-1"},
    )
    store = store_for(tmp_path, _with_fix(keep=False, diffs={_PR: "diff-1"}))
    run(store, port)
    rec = rec_of(store)
    assert port.armed == [_PR]
    assert "fix" not in rec
    assert rec["status"] != "In review"


def test_a_repair_that_changed_the_diff_is_still_handed_back(tmp_path):
    port = port_for(
        open_heads={_PR: "zzz"},
        prs={_PR: ready_pr(head="zzz")},
        review_diffs={_PR: "diff-2"},
    )
    store = store_for(tmp_path, _with_fix(keep=False, diffs={_PR: "diff-1"}))
    run(store, port)
    assert rec_of(store)["status"] == "In review"
    assert port.armed == []


def test_the_repair_dispatch_snapshots_the_diffs_it_will_compare(tmp_path):
    red = PrState(ref=_PR, head="aaa", check_state="failing", merge_state="CLEAN",
                  failed_steps=["Tests"])
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: red}, review_diffs={_PR: "diff-1"})
    store = store_for(tmp_path, merge_state())
    run(store, port)
    assert rec_of(store)["fix"]["diffs"] == {_PR: "diff-1"}
    comments = [str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT]
    assert any("If the repair changes code" in body for body in comments)


def test_a_repair_that_cannot_hand_back_reads_no_diffs(tmp_path):
    # A lint-only repair keeps merging whatever was pushed, so the comparison — and the
    # GitHub request that feeds it — would never be consulted.
    red = PrState(ref=_PR, head="aaa", check_state="failing", merge_state="CLEAN",
                  failed_steps=["Lint"])
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: red}, review_diffs={_PR: "diff-1"})
    store = store_for(tmp_path, merge_state())
    run(store, port)
    assert "diffs" not in rec_of(store)["fix"]


def test_a_lint_repair_that_went_green_keeps_merging(tmp_path):
    port = port_for(open_heads={_PR: "zzz"}, prs={_PR: ready_pr(head="zzz")})
    store = store_for(tmp_path, _with_fix(keep=True))
    run(store, port)
    assert port.armed == [_PR]
    assert "fix" not in rec_of(store)


def test_a_repair_still_running_waits_until_its_own_timeout(tmp_path):
    red = PrState(ref=_PR, head="aaa", check_state="failing", merge_state="CLEAN",
                  failed_steps=["Tests"])
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: red})
    store = store_for(tmp_path, _with_fix(at=_STALE))
    run(store, port)
    assert rec_of(store)["status"] == "Blocked"


def _ended_repair(heads=None):
    state = _with_fix(heads=heads)
    state["issues"][_KEY]["fix"]["generation"] = "gen1"
    return state


def _red(head="aaa", step="Vercel"):
    return PrState(ref=_PR, head=head, check_state="failing", merge_state="CLEAN",
                   failed_steps=[step])


def test_the_repair_names_the_failing_check_and_records_its_own_lease(tmp_path):
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: _red()})
    store = store_for(tmp_path, merge_state())
    run(store, port)
    rec = rec_of(store)
    assert rec["fix"]["generation"] == rec["generation"] != "gen1"
    comments = [str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT]
    assert any(f"`Vercel` on `{_PR}` is failing" in body for body in comments)


def test_a_repair_worker_that_ended_without_pushing_blocks_at_once(tmp_path):
    """#2771: ended, nothing pushed, still red — finished, not "did not finish" an hour later."""
    port = port_for(
        open_heads={_PR: "aaa"}, prs={_PR: _red()}, turn_ends={_KEY: {"observed": "ended"}}
    )
    store = store_for(tmp_path, _ended_repair())
    run(store, port)
    rec = rec_of(store)
    assert rec["status"] == "Blocked"
    why = rec["release_blocked"]["why"]
    assert "finished without pushing" in why and f"`Vercel` on `{_PR}`" in why
    assert "did not finish" not in why
    assert rec["release_blocked"]["heads"] == {_PR: "aaa"}


def test_a_repair_turn_the_runtime_has_not_seen_end_keeps_waiting(tmp_path):
    for name, extra in (
        ("running", {}),
        ("gone", {"turn_ends": {_KEY: {"observed": "gone"}}}),
    ):
        port = port_for(open_heads={_PR: "aaa"}, prs={_PR: _red()}, **extra)
        store = store_for(tmp_path / name, _ended_repair())
        run(store, port)
        assert rec_of(store)["mode"] == "merge", name


def test_an_older_repair_with_no_lease_still_waits_for_its_time_limit(tmp_path):
    port = port_for(
        open_heads={_PR: "aaa"}, prs={_PR: _red()}, turn_ends={_KEY: {"observed": "ended"}}
    )
    store = store_for(tmp_path, _with_fix())
    run(store, port)
    assert rec_of(store)["mode"] == "merge"


def _parked_on_heads(heads, column="Blocked"):
    return {
        "issues": {
            _KEY: {
                "status": column,
                "started": True,
                "engine_claim": True,
                "generation": "gen1",
                "release_blocked": {
                    "why": "the repair worker did not finish within the time limit",
                    "status": column,
                    "generation": "gen1",
                    **({"heads": heads} if heads is not None else {}),
                },
            }
        }
    }


def _parked_port(head, column="Blocked"):
    return FakePort(
        snapshots={_KEY: CardSnapshot(status=column, owned=True)},
        linked={_KEY: LinkedPRs(open={_PR: head})},
    )


def test_a_release_park_ends_when_its_pull_request_gets_a_new_head(tmp_path):
    """#2771: a push from the chat is newer than the park's reason."""
    unmoved = store_for(tmp_path / "unmoved", _parked_on_heads({_PR: "aaa"}))
    run(unmoved, _parked_port("aaa"))
    assert rec_of(unmoved)["release_blocked"]["heads"] == {_PR: "aaa"}

    store = store_for(tmp_path, _parked_on_heads({_PR: "aaa"}))
    outcome = run(store, _parked_port("bbb"))
    rec = rec_of(store)
    assert "release_blocked" not in rec
    assert rec["status"] == "Blocked"
    assert any("park ended" in line for line in outcome.log)


def test_a_park_in_ready_to_merge_ends_on_a_new_head_with_or_without_the_bits(tmp_path):
    for name, kwargs in (
        ("plain", {}),
        ("bits", {"auto_release": True}),
    ):
        store = store_for(tmp_path / name, _parked_on_heads({_PR: "aaa"}, "Ready to merge"))
        port = _parked_port("bbb", "Ready to merge")
        run(store, port, ready_to_merge="Ready to merge", **kwargs)
        assert "release_blocked" not in rec_of(store), name


def test_a_park_with_no_recorded_heads_is_left_alone(tmp_path):
    store = store_for(tmp_path, _parked_on_heads(None))
    run(store, _parked_port("bbb"))
    assert "release_blocked" in rec_of(store)


# --- watching the armed merges ---

def _awaiting(prs=(_PR,), premerged=None, since=None):
    return merge_state(
        {
            "awaiting_merge": {
                "prs": list(prs),
                "premerged": dict(premerged or {}),
                "since": since or _NOW.isoformat(),
            }
        }
    )


def test_all_armed_prs_merged_with_nothing_to_deploy_finishes_done(tmp_path):
    merged = PrState(ref=_PR, state="MERGED", merge_commit="sha1")
    port = port_for(prs={_PR: merged})
    store = store_for(tmp_path, _awaiting())
    outcome = run(store, port)
    assert rec_of(store) is None
    kinds = port.kinds()
    for expected in (EffectKind.SET_STATUS, EffectKind.COMMENT, EffectKind.CLOSE_ISSUE):
        assert expected in kinds
    assert port.ownership_edits() == []  # the label stays on (#2497)
    assert outcome.report.releases == 1


def test_a_merged_auto_deploy_repo_moves_to_the_deploy_gate(tmp_path):
    merged = PrState(ref=_PR, state="MERGED", merge_commit="sha1")
    port = port_for(prs={_PR: merged})
    store = store_for(tmp_path, _awaiting())
    run(store, port, auto_deploy_repos={"store"})
    rec = rec_of(store)
    assert rec["awaiting_deploy"]["builds"] == {"store": {"sha": "sha1", "status": "pending"}}
    assert rec["merged"] == [_PR]
    assert EffectKind.CLOSE_ISSUE not in port.kinds()


def test_the_deploy_gate_starts_watching_from_the_merge_the_port_reported(tmp_path):
    # #815 end to end: the merge time GitHub reports reaches the gate's `since`, so a deploy
    # that ran in the gap between the merge and this wake still counts as observed.
    merged_at = (_NOW - timedelta(minutes=2)).isoformat()
    merged = PrState(ref=_PR, state="MERGED", merge_commit="sha1", merged_at=merged_at)
    port = port_for(prs={_PR: merged})
    store = store_for(tmp_path, _awaiting())
    run(store, port, auto_deploy_repos={"store"})
    assert rec_of(store)["awaiting_deploy"]["since"] == merged_at


def test_an_armed_pr_that_fell_behind_is_updated_and_the_clock_resets(tmp_path):
    behind = PrState(ref=_PR, state="OPEN", head="aaa", merge_state="BEHIND")
    port = port_for(prs={_PR: behind})
    since = (_NOW - timedelta(minutes=30)).isoformat()
    store = store_for(tmp_path, _awaiting(since=since))
    run(store, port)
    rec = rec_of(store)
    assert port.updated_branches == [_PR]
    assert rec["awaiting_merge"]["since"] == _NOW.isoformat()
    assert rec["status"] == "In progress"


def test_an_armed_pr_closed_without_merging_blocks(tmp_path):
    closed = PrState(ref=_PR, state="CLOSED")
    port = port_for(prs={_PR: closed})
    store = store_for(tmp_path, _awaiting())
    run(store, port)
    assert rec_of(store)["status"] == "Blocked"
    assert any("closed without merging" in str(e.value) for e in port.applied)


def test_auto_merge_that_never_fires_blocks_on_timeout(tmp_path):
    stuck = PrState(ref=_PR, state="OPEN", head="aaa", merge_state="BLOCKED")
    port = port_for(prs={_PR: stuck})
    store = store_for(tmp_path, _awaiting(since=_STALE))
    run(store, port)
    assert rec_of(store)["status"] == "Blocked"
    assert port.cancelled_auto_merges == [_PR]


# --- an armed PR that broke after arming ---

def _red_after_arming(**overrides):
    fields = {
        "ref": _PR, "state": "OPEN", "head": "aaa", "check_state": "failing",
        "merge_state": "CLEAN", "failed_steps": ["Tests"],
    }
    return PrState(**{**fields, **overrides})


def test_ci_that_goes_red_after_arming_is_repaired_instead_of_waiting_out_the_timeout(tmp_path):
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: _red_after_arming()})
    store = store_for(tmp_path, _awaiting())
    run(store, port)
    rec = rec_of(store)
    # The watch is over and the request GitHub was holding is gone, so nothing can merge
    # behind the repair worker that is about to push.
    assert "awaiting_merge" not in rec
    assert port.cancelled_auto_merges == [_PR]
    assert rec["status"] == "In progress"
    announcement = next(
        str(effect.value) for effect in port.applied
        if effect.kind is EffectKind.COMMENT and "went red after auto-merge" in str(effect.value)
    )
    assert "`Tests`" in announcement  # the reason names the check, not "check GitHub"


def test_the_repair_a_post_arm_failure_earns_is_the_ordinary_one(tmp_path):
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: _red_after_arming()})
    store = store_for(tmp_path, _awaiting())
    run(store, port)
    outcome = run(store, port)  # the next wake re-assesses from the live world
    rec = rec_of(store)
    assert rec["fix"]["heads"] == {_PR: "aaa"}
    assert any(d["action"] == "merge-fix" for d in outcome.directives)


def test_a_conflict_that_appears_after_arming_is_repaired_too(tmp_path):
    dirty = PrState(ref=_PR, state="OPEN", head="aaa", merge_state="DIRTY")
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: dirty})
    store = store_for(tmp_path, _awaiting())
    run(store, port)
    assert "awaiting_merge" not in rec_of(store)
    assert any("now conflicts with its base" in str(e.value) for e in port.applied)


def test_the_post_arm_repair_is_spent_once_so_a_flapping_check_cannot_loop(tmp_path):
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: _red_after_arming()})
    spent = _awaiting()
    spent["issues"][_KEY]["post_arm_fix"] = True
    store = store_for(tmp_path, spent)
    run(store, port)
    assert rec_of(store)["status"] == "Blocked"
    assert any("already spent its repair attempt" in str(e.value) for e in port.applied)


def test_a_pre_arm_repair_does_not_use_up_the_post_arm_one(tmp_path):
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: _red_after_arming()})
    # A release that already repaired once before arming still gets rescued after arming:
    # arming is what earns the second attempt, and the spent record goes with the watch.
    already = _awaiting()
    already["issues"][_KEY]["fix"] = {"at": _NOW.isoformat(), "keep": False, "heads": {_PR: "zzz"}}
    store = store_for(tmp_path, already)
    run(store, port)
    rec = rec_of(store)
    assert rec["status"] == "In progress"
    assert "fix" not in rec


def test_an_armed_pr_whose_auto_merge_vanished_rests_instead_of_waiting(tmp_path):
    gone = PrState(ref=_PR, state="OPEN", head="aaa", check_state="green", auto_merge=False)
    port = port_for(prs={_PR: gone})
    store = store_for(tmp_path, _awaiting())
    run(store, port)
    assert rec_of(store)["status"] == "Blocked"
    assert any("no longer armed on GitHub" in str(e.value) for e in port.applied)


def _then_pushed(tmp_path, store, head="bbb"):
    """A later wake over the parked card, with its pull request on a new head.

    The parked record is carried into a fresh store without its live-read stamp, as a wake
    past the recheck throttle would see it.
    """
    rec = dict(rec_of(store))
    rec.pop("last_live_check", None)
    store = store_for(tmp_path, {"issues": {_KEY: rec}})
    run(
        store,
        FakePort(
            snapshots={_KEY: CardSnapshot(status=rec["status"], owned=True)},
            linked={_KEY: LinkedPRs(open={_PR: head})},
        ),
    )
    return store


def test_merge_watch_parks_record_their_heads_and_a_push_ends_them(tmp_path):
    """#2771 review: every park with an open pull request records the head it parked on."""
    spent = _awaiting()
    spent["issues"][_KEY]["post_arm_fix"] = True
    cases = {
        "repair spent": (spent, _red_after_arming()),
        "auto-merge gone": (
            _awaiting(),
            PrState(ref=_PR, state="OPEN", head="aaa", check_state="green", auto_merge=False),
        ),
        "timed out": (
            _awaiting(since=_STALE),
            PrState(ref=_PR, state="OPEN", head="aaa", check_state="green", auto_merge=True),
        ),
    }
    for name, (state, pr) in cases.items():
        store = store_for(tmp_path / name.replace(" ", "-"), state)
        run(store, port_for(open_heads={_PR: "aaa"}, prs={_PR: pr}))
        rec = rec_of(store)
        assert rec["status"] == "Blocked", name
        assert rec["release_blocked"]["heads"] == {_PR: "aaa"}, name
        later = _then_pushed(tmp_path / f"{name.replace(' ', '-')}-later", store)
        assert "release_blocked" not in rec_of(later), name


def test_a_park_finished_on_a_later_wake_still_records_its_heads(tmp_path):
    """The heads ride the block intent, so a park completed by recovery keeps them."""
    state = merge_state(
        {
            "blocked_pending": {
                "why": "Auto-merge did not fire",
                "merged": [],
                "steps": dict.fromkeys(release.BLOCKED_STEPS, False),
                "heads": {_PR: "aaa"},
            }
        }
    )
    store = store_for(tmp_path, state)
    run(store, port_for(open_heads={_PR: "aaa"}))
    assert rec_of(store)["release_blocked"]["heads"] == {_PR: "aaa"}


def test_a_behind_branch_still_waits_rather_than_being_judged_on_stale_ci(tmp_path):
    # Red CI on a behind branch ran against stale code, so it must not spend the attempt.
    behind = _red_after_arming(merge_state="BEHIND")
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: behind})
    store = store_for(tmp_path, _awaiting())
    run(store, port)
    rec = rec_of(store)
    assert rec["awaiting_merge"]["prs"] == [_PR]
    assert port.updated_branches == [_PR]
    assert port.cancelled_auto_merges == []


def test_stale_review_cannot_hand_back_an_active_merge_watch(tmp_path):
    state = _awaiting()
    rec = state["issues"][_KEY]
    rec.update(mode="merge-review", review_heads={_PR: "aaa"})
    state["controls"]["3"]["kind"] = "merge-review"
    port = port_for(
        prs={_PR: PrState(ref=_PR, state="OPEN", head="aaa", merge_state="CLEAN")},
        review_verdicts={_KEY: "changes-needed"},
    )
    store = store_for(tmp_path, state)

    run(store, port)

    rec = rec_of(store)
    assert rec["status"] == "In progress"
    assert rec["mode"] == "merge"
    assert rec["awaiting_merge"]["since"] == _NOW.isoformat()
    assert port.cancelled_auto_merges == []


def test_review_handback_cancels_remote_request_and_fresh_merge_gets_a_new_clock(tmp_path):
    state = merge_state({"review_heads": {_PR: "aaa"}}, mode="merge-review")
    state["controls"]["3"]["kind"] = "merge-review"
    first_port = port_for(
        open_heads={_PR: "aaa"}, review_verdicts={_KEY: "changes-needed"}
    )
    store = store_for(tmp_path, state)

    run(store, first_port)

    rec = rec_of(store)
    assert rec["status"] == "In review"
    assert first_port.cancelled_auto_merges == [_PR]

    fresh_now = _NOW + timedelta(seconds=30)
    second_port = FakePort(
        comments=[comment(4, "/merge", created=fresh_now.isoformat())],
        snapshots={_KEY: CardSnapshot(status="In review")},
        linked={_KEY: LinkedPRs(open={_PR: "aaa"})},
        pr_states={_PR: ready_pr()},
    )
    run_wake(store, second_port, now=fresh_now)

    rec = rec_of(store)
    assert rec["mode"] == "merge"
    assert rec["awaiting_merge"]["since"] == fresh_now.isoformat()
    assert second_port.armed == [_PR]


def test_handback_retries_until_the_native_merge_request_is_cancelled(tmp_path):
    state = merge_state({"review_heads": {_PR: "aaa"}}, mode="merge-review")
    state["controls"]["3"]["kind"] = "merge-review"
    port = port_for(
        open_heads={_PR: "aaa"},
        review_verdicts={_KEY: "changes-needed"},
        cancel={_PR: False},
    )
    store = store_for(tmp_path, state)

    run(store, port)
    rec = rec_of(store)
    assert rec["mode"] == "merge-review"
    assert rec["status"] == "In progress"

    port.cancel[_PR] = True
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    rec = rec_of(store)
    assert rec["status"] == "In review"


def test_an_unreadable_pr_defers_the_watch(tmp_path):
    port = port_for()  # no pr_states -> discovery incomplete
    store = store_for(tmp_path, _awaiting())
    run(store, port)
    assert rec_of(store)["awaiting_merge"]["prs"] == [_PR]


# --- the deploy gate ---

def _deploying(status="pending", since=None, link=None):
    build = {"sha": "sha1", "status": status}
    if link:
        build["link"] = link
    return merge_state(
        {
            "merged": [_PR],
            "awaiting_deploy": {"builds": {"store": build}, "since": since or _NOW.isoformat()},
        }
    )


def test_a_successful_deploy_finishes_the_release_with_its_link(tmp_path):
    port = port_for(deploys={"store": DeployState(status="ok", link="https://ci/42")})
    store = store_for(tmp_path, _deploying())
    outcome = run(store, port)
    assert rec_of(store) is None
    body = next(e.value for e in port.applied if e.kind is EffectKind.COMMENT)
    assert "https://ci/42" in str(body)
    assert outcome.report.releases == 1


def test_a_failed_deploy_blocks_and_keeps_the_merges_visible(tmp_path):
    port = port_for(deploys={"store": DeployState(status="bad", link="https://ci/43")})
    store = store_for(tmp_path, _deploying())
    run(store, port)
    rec = rec_of(store)
    assert rec["status"] == "Blocked"
    assert rec["merged"] == [_PR]
    assert any("deploy failed" in str(e.value).lower() for e in port.applied)


def test_a_deploy_that_never_terminates_blocks_on_timeout(tmp_path):
    port = port_for(deploys={"store": DeployState(status="pending")})
    store = store_for(tmp_path, _deploying(since=_STALE))
    run(store, port)
    assert rec_of(store)["status"] == "Blocked"


def test_a_deploy_still_running_just_waits(tmp_path):
    port = port_for(deploys={"store": DeployState(status="pending")})
    store = store_for(tmp_path, _deploying())
    run(store, port)
    assert rec_of(store)["awaiting_deploy"]["builds"]["store"]["status"] == "pending"


# --- re-reading a park the deploy itself has since undone (#2069) ---

def _parked(extra=None):
    """A card exactly as ``_block_release`` leaves it after a red deploy: no mode, no watch."""
    rec = {
        "status": "Blocked",
        "started": True,
        "started_at": _NOW.isoformat(),
        "generation": "gen1",
        "merged": [_PR],
    }
    rec.update(extra or {})
    return {"issues": {_KEY: rec}, "controls": {}}


def _later(**kwargs):
    return _NOW + timedelta(**kwargs)


def parked_port(**kwargs):
    """A port whose board agrees the card is parked, which is where these wakes start."""
    port = port_for(**kwargs)
    port.snapshots[_KEY] = CardSnapshot(status="Blocked")
    return port


def merged_pr(ref=_PR, commit=None):
    """A merged pull request, carrying the commit the seed rebuilds its deploy watch from."""
    return PrState(
        ref=ref, state="MERGED", head="aaa",
        merge_commit=commit or ("merge1" if ref == _PR else f"merge-{ref.replace(chr(35), '-')}"),
    )


def seed_wake(store, port, *, refs=(_PR,), repos=("store",), hours=3, **kwargs):
    """One wake that can actually seed: the repositories deploy and the merges are readable."""
    for ref in refs:
        port._pr_states.setdefault(ref, merged_pr(ref))
    return run_wake(
        store, port, now=_later(hours=hours),
        auto_deploy_repos=frozenset(repos), **kwargs,
    )


def test_a_failed_deploy_leaves_a_recheck_behind(tmp_path):
    port = port_for(deploys={"store": DeployState(status="bad")})
    store = store_for(tmp_path, _deploying())
    run(store, port)
    rec = rec_of(store)
    assert rec["status"] == "Blocked"
    watch = rec["deploy_recheck"]
    assert watch["builds"]["store"]["sha"] == "sha1"  # the exact watch, commit identity and all


def test_a_recovered_deploy_finishes_the_parked_release_and_says_so(tmp_path):
    port = parked_port(deploys={"store": DeployState(status="ok", link="https://ci/44")})
    store = store_for(
        tmp_path,
        _parked(
            {
                "deploy_recheck": {
                    "builds": {"store": {"sha": "sha1", "status": "bad"}},
                    "from": _NOW.isoformat(),
                },
                "deploy_recheck_seeded": True,
            }
        ),
    )
    outcome = run_wake(store, port, now=_later(hours=3))
    assert rec_of(store) is None  # released and forgotten, like any finished release
    assert EffectKind.CLOSE_ISSUE in port.kinds()
    body = str(next(e.value for e in port.applied if e.kind is EffectKind.COMMENT))
    assert "has since gone green" in body
    assert "no new merge was made" in body
    assert "https://ci/44" in body
    assert outcome.report.releases == 1


def test_a_deploy_still_red_stays_parked_and_silent(tmp_path):
    port = parked_port(deploys={"store": DeployState(status="bad")})
    store = store_for(
        tmp_path,
        _parked(
            {
                "deploy_recheck": {
                    "builds": {"store": {"sha": "sha1", "status": "bad"}},
                    "from": _NOW.isoformat(),
                },
                "deploy_recheck_seeded": True,
            }
        ),
    )
    run_wake(store, port, now=_later(hours=3))
    rec = rec_of(store)
    assert rec["status"] == "Blocked"
    assert rec["deploy_recheck"]  # still watching
    assert EffectKind.CLOSE_ISSUE not in port.kinds()
    assert EffectKind.COMMENT not in port.kinds()  # the park was already explained once


def test_a_recheck_gives_up_once_the_deploy_is_plainly_not_coming_back(tmp_path):
    port = parked_port(deploys={"store": DeployState(status="bad")})
    store = store_for(
        tmp_path,
        _parked(
            {
                "deploy_recheck": {
                    "builds": {"store": {"sha": "sha1", "status": "bad"}},
                    "from": _NOW.isoformat(),
                },
                "deploy_recheck_seeded": True,
            }
        ),
    )
    run_wake(store, port, now=_later(days=8))
    rec = rec_of(store)
    assert rec["status"] == "Blocked"
    assert "deploy_recheck" not in rec
    assert EffectKind.CLOSE_ISSUE not in port.kinds()


def test_a_park_made_before_the_recheck_existed_is_seeded_once(tmp_path):
    """The six cards this shipped for had already dropped their watch (#2069).

    The commit is not gone with it: it is on the merged pull request, which is where the
    forward path reads it from too. So the seed rebuilds the *same* watch — repository to
    merge commit — and there is only ever one deploy question, asked one way.
    """
    port = parked_port(merged=[_PR], deploys={"store": DeployState(status="pending")})
    store = store_for(tmp_path, _parked())
    seed_wake(store, port)
    rec = rec_of(store)
    assert rec["deploy_recheck"]["builds"] == {"store": {"sha": "merge1", "status": "pending"}}
    assert "since" not in rec["deploy_recheck"]  # no second, time-based way to ask
    assert rec["deploy_recheck_seeded"] is True
    assert rec["status"] == "Blocked"


def test_a_seeded_recheck_releases_on_the_next_healthy_deploy(tmp_path):
    port = parked_port(merged=[_PR], deploys={"store": DeployState(status="ok", link="https://ci/45")})
    store = store_for(tmp_path, _parked())
    seed_wake(store, port)
    assert rec_of(store) is None
    assert EffectKind.CLOSE_ISSUE in port.kinds()


def test_a_park_whose_merge_commit_cannot_be_read_is_not_seeded_yet(tmp_path):
    """Unreadable is not a verdict, so it must not spend the card's one re-read either.

    Without a commit there is no honest deploy question to ask, and inventing a weaker one —
    "did anything deploy after now" — is what six review rounds kept rediscovering as wrong.
    """
    port = parked_port(merged=[_PR], deploys={"store": DeployState(status="ok")})
    store = store_for(tmp_path, _parked())
    run_wake(store, port, now=_later(hours=3), auto_deploy_repos=frozenset({"store"}))
    rec = rec_of(store)
    assert "deploy_recheck" not in rec
    assert "deploy_recheck_seeded" not in rec
    assert EffectKind.CLOSE_ISSUE not in port.kinds()


def test_a_park_in_a_project_with_no_deploying_repository_is_refused(tmp_path):
    """Nothing deploys, so nothing can show the park's reason has gone away."""
    port = parked_port(merged=[_PR], prs={_PR: merged_pr()})
    store = store_for(tmp_path, _parked())
    run_wake(store, port, now=_later(hours=3), auto_deploy_repos=frozenset())
    rec = rec_of(store)
    assert "deploy_recheck" not in rec
    assert rec["deploy_recheck_seeded"] is True  # a decisive answer spends the one re-read
    assert EffectKind.CLOSE_ISSUE not in port.kinds()


def test_a_park_with_a_pull_request_still_open_is_not_seeded_yet(tmp_path):
    """Half a release is not a finished release waiting on a deploy — but it is not a "no"."""
    port = parked_port(
        open_heads={"store#9": "bbb"}, merged=[_PR],
        deploys={"store": DeployState(status="ok")},
    )
    store = store_for(tmp_path, _parked())
    seed_wake(store, port)
    rec = rec_of(store)
    assert "deploy_recheck" not in rec
    assert rec["status"] == "Blocked"
    assert EffectKind.CLOSE_ISSUE not in port.kinds()
    # The one re-read is NOT spent: "still open" is a pending answer, not a refusal.
    assert "deploy_recheck_seeded" not in rec


def test_a_pull_request_open_on_one_wake_and_merged_by_the_next_still_seeds(tmp_path):
    """The one-shot marker must not be spent by a wake that answered too early (#2069 review).

    Consuming it before eligibility was known suppressed the card forever: a release whose
    last pull request merged an hour after the park had already used up its only re-read.
    """
    other = "store#9"
    port = parked_port(open_heads={other: "bbb"}, merged=[_PR],
                       deploys={"store": DeployState(status="ok")})
    store = store_for(tmp_path, _parked())

    seed_wake(store, port)
    assert "deploy_recheck" not in rec_of(store)

    # The pull request merges, and the next wake finds a complete release set.
    port.linked[_KEY] = LinkedPRs(open={}, merged=[_PR, other])
    seed_wake(store, port, refs=(_PR, other), hours=4)

    assert rec_of(store) is None, "wake 2 must seed and recheck, not stay suppressed"
    assert EffectKind.CLOSE_ISSUE in port.kinds()
    body = str(next(e.value for e in port.applied if e.kind is EffectKind.COMMENT))
    assert _PR in body and other in body


def test_an_abandoned_pull_request_is_decided_and_spends_the_one_recheck(tmp_path):
    """"Closed unmerged" is final, unlike "still open": that set can never be complete."""
    port = parked_port(merged=[_PR], deploys={"store": DeployState(status="ok")})
    port.linked[_KEY] = LinkedPRs(open={}, merged=[_PR], closed_unmerged=["store#9"])
    store = store_for(tmp_path, _parked())
    seed_wake(store, port)
    rec = rec_of(store)
    assert "deploy_recheck" not in rec
    assert rec["deploy_recheck_seeded"] is True
    assert EffectKind.CLOSE_ISSUE not in port.kinds()


def test_a_park_whose_other_pull_request_was_closed_unmerged_is_never_seeded(tmp_path):
    """"Nothing is open" is not "everything shipped" (#2069 review).

    A pull request closed without merging leaves nothing open, so a seeding rule that reads
    an absence as proof would close a card half of whose release set never landed.
    """
    port = parked_port(merged=[_PR], deploys={"store": DeployState(status="ok")})
    port.linked[_KEY] = LinkedPRs(open={}, merged=[_PR], closed_unmerged=["store#9"])
    store = store_for(tmp_path, _parked())
    seed_wake(store, port)
    rec = rec_of(store)
    assert "deploy_recheck" not in rec
    assert rec["status"] == "Blocked"
    assert EffectKind.CLOSE_ISSUE not in port.kinds()


def test_a_park_with_nothing_proven_merged_is_never_seeded(tmp_path):
    """The merge has to be positively readable now, not just remembered on the record."""
    port = parked_port(deploys={"store": DeployState(status="ok")})
    port.linked[_KEY] = LinkedPRs(open={}, merged=[])
    store = store_for(tmp_path, _parked())
    seed_wake(store, port)
    assert "deploy_recheck" not in rec_of(store)
    assert EffectKind.CLOSE_ISSUE not in port.kinds()


def test_a_pull_request_merged_after_the_park_must_deploy_too(tmp_path):
    """The seed follows the current proven merge set, not the one the card parked with.

    A linked pull request can merge after the park and before the first re-read, in another
    code repository. Seeding from the stored snapshot would pass every current-link guard and
    still ask for deploy evidence only about `store` — closing the card with no evidence at
    all for `shop` (#2069 review).
    """
    later = "shop#2"
    port = parked_port(deploys={"store": DeployState(status="ok")})
    port.linked[_KEY] = LinkedPRs(open={}, merged=[_PR, later])
    store = store_for(tmp_path, _parked())  # historical rec["merged"] is [_PR] alone

    seed_wake(store, port, refs=(_PR, later), repos=("store", "shop"))

    rec = rec_of(store)
    assert rec is not None, "shop has not deployed, so the card must not close"
    builds = rec["deploy_recheck"]["builds"]
    assert sorted(builds) == ["shop", "store"]
    # Each repository is asked about its own merge commit, exactly as the forward watch would.
    assert builds["store"]["sha"] == "merge1"
    assert builds["shop"]["sha"] == merged_pr(later).merge_commit
    assert rec["merged"] == [_PR, later]  # refreshed to what actually shipped
    assert EffectKind.CLOSE_ISSUE not in port.kinds()

    # Once the second repository is healthy too, the release finishes and names both.
    port.deploys["shop"] = DeployState(status="ok")
    seed_wake(store, port, refs=(_PR, later), repos=("store", "shop"), hours=4)
    assert rec_of(store) is None
    assert EffectKind.CLOSE_ISSUE in port.kinds()
    body = str(next(e.value for e in port.applied if e.kind is EffectKind.COMMENT))
    assert _PR in body and later in body


def test_a_card_is_only_ever_offered_one_recheck(tmp_path):
    """A re-read that ran its course must not look like a card that never had one."""
    port = parked_port(merged=[_PR], deploys={"store": DeployState(status="ok")})
    store = store_for(tmp_path, _parked({"deploy_recheck_seeded": True}))
    seed_wake(store, port)
    rec = rec_of(store)
    assert "deploy_recheck" not in rec
    assert rec["status"] == "Blocked"
    assert EffectKind.CLOSE_ISSUE not in port.kinds()


def test_an_active_release_is_never_touched_by_the_recheck(tmp_path):
    """``mode`` means a release is already being driven; two drivers would race."""
    port = port_for(deploys={"store": DeployState(status="pending")})
    store = store_for(tmp_path, _deploying())
    run(store, port)
    assert "deploy_recheck" not in rec_of(store)


# --- review verdicts ---

def _reviewing(heads=None, extra=None):
    base = {"review_heads": dict(heads or {_PR: "aaa"}), "status": "In progress"}
    base.update(extra or {})
    return merge_state(base, mode="merge-review")


def test_a_pass_on_a_current_diff_releases(tmp_path):
    port = port_for(
        open_heads={_PR: "aaa"},
        prs={_PR: ready_pr()},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(tmp_path, _reviewing())
    run(store, port)
    rec = rec_of(store)
    assert rec["mode"] == "merge"
    # The approval is still durable: it is spent only when auto-merge is armed.
    assert rec["review_heads"] == {_PR: "aaa"}


def test_a_pass_on_a_changed_head_hands_back_without_merging(tmp_path):
    port = port_for(
        open_heads={_PR: "zzz"},
        prs={_PR: ready_pr(head="zzz")},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(tmp_path, _reviewing())
    run(store, port)
    rec = rec_of(store)
    assert rec["status"] == "In review"
    assert "mode" not in rec and "review_heads" not in rec
    assert port.armed == []


def test_a_pass_after_two_base_syncs_keeps_merging_when_the_pr_diff_is_identical(tmp_path):
    port = port_for(
        open_heads={_PR: "third-head"},
        review_diffs={_PR: "diff-1"},
        prs={_PR: ready_pr(head="third-head")},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(
        tmp_path,
        _reviewing(extra={
            "review_diffs": {_PR: "diff-1"},
            # The engine's own base syncs, each naming the head it produced — which is how
            # #1436 is kept now that a head is not its own provenance (#2122).
            "rebase_round": {_PR: "third-head"},
        }),
    )
    run(store, port)
    assert rec_of(store)["mode"] == "merge"


def test_a_pass_does_not_survive_a_push_the_engine_did_not_make(tmp_path):
    """The same unchanged digest on a head nobody recorded producing hands back (#2122)."""
    port = port_for(
        open_heads={_PR: "pushed-head"},
        review_diffs={_PR: "diff-1"},
        prs={_PR: ready_pr(head="pushed-head")},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(
        tmp_path,
        _reviewing(extra={"review_diffs": {_PR: "diff-1"}}),
    )
    run(store, port)
    assert rec_of(store).get("mode") != "merge"


def test_a_base_change_that_alters_the_pr_diff_hands_back_before_merging(tmp_path):
    port = port_for(
        open_heads={_PR: "aaa"},
        review_diffs={_PR: "diff-2"},
        prs={_PR: ready_pr()},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(
        tmp_path,
        _reviewing(extra={"review_diffs": {_PR: "diff-1"}}),
    )
    run(store, port)
    assert rec_of(store)["status"] == "In review"
    assert port.armed == []


def test_a_stale_pass_does_not_become_a_standing_review_pass(tmp_path):
    """A verdict the freshness check rejected must not be re-bound to the live diff (#2101).

    The release bits keep a clean verdict as a standing fact, and `handback` deliberately
    preserves it. So writing that snapshot for every raw `pass` would launder an unreviewed
    push into a reviewed one: the reviewer passed the old diff, somebody pushed a new one, and
    the snapshot would name the *new* diff as reviewed. Once that head also had a green suite,
    the poller would release code no reviewer ever saw.
    """
    port = port_for(
        open_heads={_PR: "aaa"},
        review_diffs={_PR: "diff-2"},
        prs={_PR: ready_pr()},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(
        tmp_path,
        _reviewing(extra={"review_diffs": {_PR: "diff-1"}}),
    )
    run(store, port, auto_release=True)
    rec = rec_of(store)
    assert rec["status"] == "In review"
    assert release.review_pass(rec) is None


def test_a_current_pass_does_become_a_standing_review_pass(tmp_path):
    """The other half: a verdict that *was* current is exactly what the bit is for."""
    port = port_for(
        open_heads={_PR: "aaa"},
        review_diffs={_PR: "diff-1"},
        prs={_PR: ready_pr()},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(tmp_path, _reviewing(extra={"review_diffs": {_PR: "diff-1"}}))
    run(store, port, auto_release=True)
    assert (release.review_pass(rec_of(store)) or {}).get("heads") == {_PR: "aaa"}


def test_a_project_without_the_capability_persists_no_release_bits(tmp_path):
    """The capability's contract is that an opted-out project neither reads nor acts on the
    bits. Writing durable state onto its cards anyway would make that sentence false (#2101)."""
    port = port_for(
        open_heads={_PR: "aaa"},
        review_diffs={_PR: "diff-1"},
        prs={_PR: ready_pr()},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(tmp_path, _reviewing(extra={"review_diffs": {_PR: "diff-1"}}))
    run(store, port)
    assert release.review_pass(rec_of(store)) is None


def test_the_standing_pass_records_the_diff_freshness_validated_not_a_later_one(tmp_path):
    """No second live read between checking the content and keeping it (#2101).

    Reading the diffs again after freshness had passed looked harmless — the check had just
    proved the content current. But the check validated one instant and the second read a
    later one, so a push in between paired the validated heads with a newer digest, and a
    later wake's diff-based freshness would accept content this review never covered.

    This port changes its answer after the first `review_diffs` call, which is exactly that
    push. The recorded snapshot must carry the digest freshness compared against, never the
    one that landed afterwards.
    """
    class ShiftingDiffPort(FakePort):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.diff_reads = 0

        def review_diffs(self, refs):
            self.diff_reads += 1
            digest = "diff-1" if self.diff_reads == 1 else "diff-pushed-since"
            return {ref: digest for ref in refs}

    port = ShiftingDiffPort(
        snapshots={_KEY: CardSnapshot(status="In progress")},
        linked={_KEY: LinkedPRs(open={_PR: "aaa"})},
        pr_states={_PR: ready_pr()},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(tmp_path, _reviewing(extra={"review_diffs": {_PR: "diff-1"}}))
    run(store, port, auto_release=True)
    recorded = release.review_pass(rec_of(store)) or {}
    assert recorded.get("heads") == {_PR: "aaa"}
    assert (recorded.get("diffs") or {}).get(_PR) == "diff-1"
    # The freshness check is the only thing allowed to read the diff on this path.
    assert port.diff_reads == 1


def test_a_pass_after_a_base_only_branch_update_releases(tmp_path):
    port = port_for(
        open_heads={_PR: "zzz"},
        release_trees={_PR: "tree-1"},
        prs={_PR: ready_pr(head="zzz")},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(
        tmp_path,
        _reviewing(extra={"review_trees": {_PR: "tree-1"}}),
    )
    run(store, port)
    assert rec_of(store)["mode"] == "merge"


def test_a_pass_after_a_content_changing_update_hands_back(tmp_path):
    port = port_for(
        open_heads={_PR: "zzz"},
        release_trees={_PR: "tree-2"},
        prs={_PR: ready_pr(head="zzz")},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(
        tmp_path,
        _reviewing(extra={"review_trees": {_PR: "tree-1"}}),
    )
    run(store, port)
    assert rec_of(store)["status"] == "In review"
    assert port.armed == []


def test_changes_needed_hands_back(tmp_path):
    port = port_for(
        open_heads={_PR: "aaa"},
        prs={_PR: ready_pr()},
        review_verdicts={_KEY: "changes-needed"},
    )
    store = store_for(tmp_path, _reviewing())
    run(store, port)
    state = store.read()
    assert state["issues"][_KEY]["status"] == "In review"
    assert state["controls"]["3"]["status"] == "rejected"

    # The command ledger must not recover the completed review on the next wake. That
    # stale recovery used to overwrite a later plain /merge and send the card back here.
    next_port = FakePort(
        comments=[comment(4, "/merge")],
        snapshots={_KEY: CardSnapshot(status="In review")},
        linked={_KEY: LinkedPRs(open={_PR: "aaa"})},
        pr_states={_PR: ready_pr()},
    )
    run(store, next_port)
    state = store.read()
    rec = rec_of(store)
    assert rec["mode"] == "merge"
    assert "recovery_action" not in rec
    assert state["controls"]["4"]["status"] == "processing"
    assert next_port.armed == [_PR]


def test_a_queued_instruction_supersedes_a_pass(tmp_path):
    state = _reviewing(extra={"pending": ["9"]})
    state["controls"]["9"] = {
        "id": "9", "repo": "store", "number": 5, "kind": "claude",
        "payload": "one more thing", "status": "queued", "created_at": "t2",
    }
    port = port_for(
        open_heads={_PR: "aaa"},
        prs={_PR: ready_pr()},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(tmp_path, state)
    outcome = run(store, port)
    rec = rec_of(store)
    # Nothing was released, the approval is spent, and the newer instruction runs instead.
    assert port.armed == []
    assert "mode" not in rec and "review_heads" not in rec
    assert [payloads(d) for d in outcome.directives] == [["one more thing"]]


def test_no_verdict_yet_leaves_the_review_alone(tmp_path):
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: ready_pr()})
    store = store_for(tmp_path, _reviewing())
    run(store, port)
    assert rec_of(store)["mode"] == "merge-review"


def test_an_approval_that_cannot_be_verified_defers(tmp_path):
    class Blind(FakePort):
        def linked_prs(self, repo, number):
            return None

    port = Blind(
        snapshots={_KEY: CardSnapshot(status="In progress")},
        review_verdicts={_KEY: "pass"},
    )
    store = store_for(tmp_path, _reviewing())
    outcome = run(store, port)
    rec = rec_of(store)
    assert rec["mode"] == "merge-review"
    assert rec["review_heads"] == {_PR: "aaa"}
    assert any("could not be verified" in line for line in outcome.log)


def test_the_approval_is_spent_only_once_auto_merge_is_armed(tmp_path):
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: ready_pr()})
    store = store_for(tmp_path, merge_state({"review_heads": {_PR: "aaa"}}))
    run(store, port)
    rec = rec_of(store)
    assert port.armed == [_PR]
    assert "review_heads" not in rec


def test_a_release_whose_approved_diff_moved_is_handed_back_before_arming(tmp_path):
    port = port_for(open_heads={_PR: "zzz"}, prs={_PR: ready_pr(head="zzz")})
    store = store_for(tmp_path, merge_state({"review_heads": {_PR: "aaa"}}))
    run(store, port)
    assert rec_of(store)["status"] == "In review"
    assert port.armed == []


# --- resumable terminal transitions ---

def test_a_refused_terminal_step_resumes_without_repeating_what_landed(tmp_path):
    merged = PrState(ref=_PR, state="MERGED", merge_commit="sha1")
    port = port_for(prs={_PR: merged}, refuse={EffectKind.CLOSE_ISSUE})
    store = store_for(tmp_path, _awaiting())
    run(store, port)
    rec = rec_of(store)
    assert rec is not None
    steps = rec["done_pending"]["steps"]
    assert steps["status"] is True and steps["comment"] is True
    assert steps["closed"] is False
    first_comments = [e for e in port.applied if e.kind is EffectKind.COMMENT]

    port.refuse.clear()
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert rec_of(store) is None
    # The Done comment was posted exactly once across both wakes.
    assert len([e for e in port.applied if e.kind is EffectKind.COMMENT]) == len(first_comments)


def test_a_refused_worktree_removal_keeps_the_finished_card_for_cleanup_retry(tmp_path):
    merged = PrState(ref=_PR, state="MERGED", merge_commit="sha1")
    port = port_for(prs={_PR: merged}, refuse={EffectKind.REMOVE_WORKTREES})
    watching = _awaiting()
    watching["issues"][_KEY]["worktrees"] = ["/w/issue-5"]
    store = store_for(tmp_path, watching)
    run(store, port)
    rec = rec_of(store)
    assert rec["forget_pending"] is True
    assert rec["worktrees"] == ["/w/issue-5"]


def test_pending_finalization_is_resumed_before_any_new_assessment(tmp_path):
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: ready_pr()})
    store = store_for(
        tmp_path,
        merge_state(
            {
                "merged": [_PR],
                "done_pending": {
                    "merged": [_PR],
                    "deploy_links": [],
                    "steps": dict.fromkeys(release.DONE_STEPS, False),
                },
            }
        ),
    )
    run(store, port)
    assert rec_of(store) is None
    assert port.armed == []  # no fresh assessment happened


def test_a_finish_saved_owing_the_old_ownership_step_still_finishes(tmp_path):
    """A `/merge` finish armed before #2497 lists an `ownership` step. Nothing performs it any
    more, so the saved intent finishes without it and the label stays on."""
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: ready_pr()})
    store = store_for(
        tmp_path,
        merge_state(
            {
                "merged": [_PR],
                "done_pending": {
                    "merged": [_PR],
                    "deploy_links": [],
                    "steps": {
                        "status": True, "comment": True, "closed": True, "ownership": False,
                    },
                },
            }
        ),
    )
    run(store, port)
    assert rec_of(store) is None
    assert port.ownership_edits() == []


# --- withheld pull requests and named release targets (#1445) ---

_FOLLOWUP = "store#9"


def test_a_card_whose_only_linked_pr_is_withheld_says_so(tmp_path):
    """"No linked PR found" sends a reader hunting for one that is sitting right there."""
    port = port_for(prs={})
    port.linked[_KEY] = LinkedPRs(withheld=[_FOLLOWUP])
    store = store_for(tmp_path, merge_state())
    run(store, port)
    comments = [str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT]
    assert any(_FOLLOWUP in body and "draft" in body for body in comments)
    assert not any("No linked PR found" in body for body in comments)


def test_a_withheld_pr_is_never_armed(tmp_path):
    """The 211 shape: one ready PR and one draft follow-up mentioning the same card."""
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: ready_pr()})
    port.linked[_KEY] = LinkedPRs(open={_PR: "aaa"}, withheld=[_FOLLOWUP])
    store = store_for(tmp_path, merge_state())
    run(store, port)
    assert port.armed == [_PR]


def test_a_named_target_replaces_discovery_for_the_whole_release(tmp_path):
    """A pin must survive the wake that set it, or every later step re-picks the wrong PR."""
    port = port_for(
        open_heads={_PR: "aaa", _FOLLOWUP: "bbb"},
        prs={_PR: ready_pr(), _FOLLOWUP: ready_pr(_FOLLOWUP, "bbb")},
    )
    store = store_for(tmp_path, merge_state({"release_only": [_PR]}))
    run(store, port)
    assert port.armed == [_PR]


def test_a_named_target_reaches_a_pr_discovery_never_found(tmp_path):
    """Filtering the discovered set would be no hatch at all for a PR it missed."""
    port = port_for(prs={_FOLLOWUP: ready_pr(_FOLLOWUP, "bbb")})
    port.resolvable[_FOLLOWUP] = "bbb"
    store = store_for(tmp_path, merge_state({"release_only": [_FOLLOWUP]}))
    run(store, port)
    assert port.armed == [_FOLLOWUP]


def test_an_unreadable_resolve_defers_rather_than_releasing_nothing(tmp_path):
    port = port_for(open_heads={_PR: "aaa"}, prs={_PR: ready_pr()})
    port.resolve_unreadable = True
    store = store_for(tmp_path, merge_state({"release_only": [_PR]}))
    run(store, port)
    assert not port.armed
    assert rec_of(store)["mode"] == "merge"
