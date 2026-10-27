"""A live worker offers its head for review and verifies it while the review runs (#2350).

Before this, review and the final suite queued behind each other: the worker finished, the
outside reviewer read the head, and only a passed review asked for the suite (#2253). Now the
worker says ``ready-for-review`` and keeps its turn. These tests hold the three promises that
make the overlap safe:

* the offer is the worker's own, bound to the heads it pushed and to its generation, and a
  head the review rejected can never be offered again;
* a review that does not pass the offered head stops its verification — cancelled if it was
  running, refused if it is submitted later — and the worker is told so in its own chat, ahead
  of the findings;
* only the outside reviewer's verdicts reject. A person's ``/claude`` steers as it always did,
  and a verdict about older code leaves the offer standing.
"""

from __future__ import annotations

import importlib.util
import tomllib
from datetime import timedelta
from pathlib import Path

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, payloads, run_wake, store_for
from public_copy import shipped

from agentdeck.poller_adapter.runner import Workspace, compose_message
from agentdeck.poller_engine import authorization as auth
from agentdeck.poller_engine import config, ready_review
from agentdeck.poller_engine.__main__ import parallel_review
from agentdeck.poller_engine.cycle import _observe_context
from agentdeck.poller_engine.ports import LinkedPRs, PrState
from agentdeck.poller_engine.reconcile import CardSnapshot
from agentdeck.poller_engine.worker_control import WorkerControl

_KEY = "store#5"
_PR = "store#7"
_HEAD = "a" * 40
_FIX = "b" * 40
_GEN = "gen1"
_REVIEWER = "review-bot[bot]"
_POLICY = auth.parse_policy(
    {
        "work": {"allow": ["eero", _REVIEWER]},
        "release": {"allow": ["eero", _REVIEWER]},
        "bot": "claude-bot",
    }
)
_LATER = _NOW + timedelta(seconds=30)
_LATER_STILL = _NOW + timedelta(seconds=60)
_EXTENSIONS = Path(__file__).resolve().parent.parent / "poller" / "extensions"


# --- fixtures ---

def working(**extra) -> dict:
    """A card whose worker is still in its turn, carrying one instruction."""
    rec = {
        "status": "In progress",
        "generation": _GEN,
        "started": True,
        "processing": ["9"],
    }
    rec.update(extra)
    return rec


def offered(*, head: str = _HEAD, **extra) -> dict:
    rec = working(**extra)
    assert ready_review.offer(rec, heads={_PR: head}, generation=_GEN, now=_NOW) == "offered"
    return rec


def state_of(rec: dict) -> dict:
    return {
        "issues": {_KEY: rec},
        "controls": {
            "9": {
                "id": "9", "repo": "store", "number": 5, "kind": "claude",
                "payload": "do the work", "url": "https://example/9", "author": "eero",
                "status": "processing", "created_at": "t9",
            }
        },
    }


def job(job_id: str, *, state: str = "running", run_id: str = _GEN) -> dict:
    return {
        "id": job_id,
        "purpose": "verification",
        "run_id": run_id,
        "work_id": "github_issue:org/store#5",
        "state": state,
        "session_key": "claude_code:x",
    }


def port_for(*, head: str = _HEAD, comments=(), jobs=None, status="In progress") -> FakePort:
    return FakePort(
        comments=list(comments),
        snapshots={_KEY: CardSnapshot(status=status)},
        linked={_KEY: LinkedPRs(open={_PR: head})},
        pr_states={_PR: PrState(ref=_PR, head=head, merge_state="CLEAN")},
        review_diffs={_PR: "d" * 64},
        jobs=jobs,
    )


def findings(cid, *, head: str = _HEAD, author: str = _REVIEWER) -> dict:
    body = (
        f"/claude\nReview findings for PR #7 at {head}:\n\n"
        "1. `src/x.py:1` — the guard is inverted.\n\n<!-- chatgpt-review -->"
    )
    return comment(cid, body, author=author, user_type="Bot", association="NONE")


def run(store, port, **kwargs):
    kwargs.setdefault("policy", _POLICY)
    kwargs.setdefault("auto_release", True)
    kwargs.setdefault("parallel_review", True)
    kwargs.setdefault("now", _NOW)
    return run_wake(store, port, **kwargs)


def rec_of(store) -> dict:
    return store.read()["issues"][_KEY]


def queued_texts(store) -> list[str]:
    rec = rec_of(store)
    controls = store.read().get("controls", {})
    return [str(controls[cid].get("payload")) for cid in rec.get("pending") or []]


def control_for(tmp_path, rec: dict | None = None, *, port=None, enabled=True, sleep=None):
    store = store_for(tmp_path, state_of(rec if rec is not None else working()))
    control = WorkerControl(
        store,
        port or port_for(),
        parallel_review=enabled,
        clock=lambda: _NOW,
        sleep=sleep or (lambda _seconds: None),
    )
    return store, control


# --- the offer ---

def test_the_offer_needs_an_outside_reviewer_and_the_poller_releasing():
    class Manifest:
        def __init__(self, capabilities, extensions):
            self.capabilities = capabilities
            self.extensions = extensions

    reviewer = {"chatgpt-nudge": {"review_program": ["agentdeck", "chat-review"]}}
    assert parallel_review(Manifest({"auto_release": True}, reviewer))
    assert not parallel_review(Manifest({"auto_release": False}, reviewer))
    assert not parallel_review(Manifest({"auto_release": True}, {"chatgpt-nudge": {}}))


@shipped("poller/extensions/chatgpt_nudge_core.py")
def test_the_outside_reviewer_is_read_from_the_overlay_where_hosts_name_it():
    """``review_program`` is a host fact, kept in the Overlay; the Manifest only names the
    extension. Reading the Manifest alone kept this off on the live instance (#2475)."""
    project = Path(__file__).resolve().parents[1] / "poller" / "manifests" / "agentdeck"
    report = config.Report()
    manifest = config.parse_manifest(
        tomllib.loads((project / "poller.toml").read_text()), report
    )
    overlay = config.parse_overlay(
        tomllib.loads((project / "overlay.example.toml").read_text()), report
    )

    assert not parallel_review(manifest), "the reviewer is not in the portable half"
    assert parallel_review(manifest, overlay)


def test_a_project_without_it_refuses_the_offer_and_keeps_the_old_order(tmp_path):
    store, control = control_for(tmp_path, enabled=False)
    result = control.ready_for_review("store", 5, _GEN)
    assert not result.ok
    assert result.reason.startswith("refused:")
    assert "finish in_review" in result.reason
    assert ready_review.current(rec_of(store)) is None


def test_the_offer_records_the_open_pull_request_heads_of_this_generation(tmp_path):
    store, control = control_for(tmp_path)
    result = control.ready_for_review("store", 5, _GEN)
    assert result.ok, result.reason
    assert "verification job" in result.reason
    ready = ready_review.current(rec_of(store), generation=_GEN)
    assert ready["heads"] == {_PR: _HEAD}
    assert not ready.get("rejected")


def test_offering_the_same_head_again_changes_nothing(tmp_path):
    store, control = control_for(tmp_path, offered())
    before = ready_review.current(rec_of(store))
    result = control.ready_for_review("store", 5, _GEN)
    assert result.ok
    assert result.reason.startswith("already offered")
    assert ready_review.current(rec_of(store)) == before


def test_an_asserted_head_no_pull_request_is_at_yet_is_retried_not_offered(tmp_path):
    """GitHub moves a pull request's head a little after the push lands (#2623).

    The command reads again twice before saying so, and never tells a worker to come back
    "in a minute": a worker cannot sleep, so it ended its turn with nothing offered (#2698).
    """
    port = port_for()
    reads = []
    original = port.linked_prs
    port.linked_prs = lambda repo, number: reads.append(number) or original(repo, number)
    slept = []
    store, control = control_for(tmp_path, port=port, sleep=slept.append)
    result = control.ready_for_review("store", 5, _GEN, head=_FIX[:12])
    assert not result.ok
    assert result.reason.startswith("unavailable:")
    assert "offer once more before you end your turn" in result.reason
    assert "in a minute" not in result.reason
    assert len(reads) == 3 and slept == [5.0, 5.0]
    assert ready_review.current(rec_of(store)) is None
    # Any abbreviation of the real head, in any case, is the same commit.
    assert control.ready_for_review("store", 5, _GEN, head=_HEAD[:9].upper()).ok


def test_a_push_github_shows_during_the_wait_is_offered(tmp_path):
    port = port_for()
    answers = [LinkedPRs(open={_PR: _HEAD}), LinkedPRs(open={_PR: _FIX})]
    port.linked_prs = lambda repo, number: answers.pop(0) if len(answers) > 1 else answers[0]
    slept = []
    store, control = control_for(tmp_path, port=port, sleep=slept.append)
    result = control.ready_for_review("store", 5, _GEN, head=_FIX[:12])
    assert result.ok, result.reason
    assert slept == [5.0]
    assert ready_review.current(rec_of(store), generation=_GEN)["heads"] == {_PR: _FIX}


def test_a_lease_that_ends_during_the_wait_offers_nothing(tmp_path):
    port = port_for()
    store, control = control_for(tmp_path, port=port)

    def superseded(_seconds):
        with store.transaction() as state:
            state["issues"][_KEY]["generation"] = "gen2"

    control._sleep = superseded
    result = control.ready_for_review("store", 5, _GEN, head=_FIX[:12])
    assert not result.ok
    assert not result.reason.startswith("unavailable:")
    assert ready_review.current(rec_of(store)) is None


def test_a_card_with_no_ready_pull_request_has_nothing_to_offer(tmp_path):
    port = port_for()
    port.linked[_KEY] = LinkedPRs(open={}, withheld=[_PR])
    _store, control = control_for(tmp_path, port=port)
    result = control.ready_for_review("store", 5, _GEN)
    assert not result.ok
    assert result.reason.startswith("refused:")
    assert "not as a draft" in result.reason


def test_unreadable_pull_requests_are_a_retry_not_a_refusal(tmp_path):
    port = port_for()
    port.linked[_KEY] = LinkedPRs(open={_PR: None})
    _store, control = control_for(tmp_path, port=port)
    result = control.ready_for_review("store", 5, _GEN)
    assert not result.ok
    assert result.reason.startswith("unavailable:")


def test_another_generation_cannot_offer(tmp_path):
    _store, control = control_for(tmp_path)
    result = control.ready_for_review("store", 5, "someone-else")
    assert not result.ok
    assert result.reason.startswith("stale:")


def test_a_rejected_head_cannot_be_offered_again_but_a_fix_can(tmp_path):
    rec = offered()
    ready_review.reject(rec, by="c1", now=_NOW)
    store, control = control_for(tmp_path, rec)
    refused = control.ready_for_review("store", 5, _GEN)
    assert not refused.ok
    assert "already rejected" in refused.reason
    assert "do not verify it" in refused.reason
    assert ready_review.rejected(rec_of(store))

    port = port_for(head=_FIX)
    control.port = port
    fixed = control.ready_for_review("store", 5, _GEN)
    assert fixed.ok, fixed.reason
    ready = ready_review.current(rec_of(store), generation=_GEN)
    assert ready["heads"] == {_PR: _FIX}
    assert not ready.get("rejected")
    # The rejected set is still remembered: a force-push back to it is refused too.
    control.port = port_for()
    assert not control.ready_for_review("store", 5, _GEN).ok


def test_a_fix_github_has_not_shown_yet_is_unavailable_not_already_rejected(tmp_path):
    """The pull request still shows the rejected head right after the fix is pushed (#2623)."""
    rec = offered()
    ready_review.reject(rec, by="c1", now=_NOW)
    store, control = control_for(tmp_path, rec)
    lagging = control.ready_for_review("store", 5, _GEN, head=_FIX)
    assert not lagging.ok
    assert lagging.reason.startswith("unavailable:"), lagging.reason
    assert ready_review.current(rec_of(store), generation=_GEN)["heads"] == {_PR: _HEAD}

    control.port = port_for(head=_FIX)
    assert control.ready_for_review("store", 5, _GEN, head=_FIX).ok


# --- a review that does not pass ---

def test_findings_on_the_offered_head_cancel_its_running_verification(tmp_path):
    port = port_for(comments=[findings(10)], jobs={"job-1": job("job-1")})
    store = store_for(tmp_path, state_of(offered()))
    outcome = run(store, port)

    assert port.cancelled_jobs == ["job-1"]
    assert ready_review.rejected(rec_of(store))
    # One steer into the live turn: the note first, then the findings themselves.
    steers = [d for d in outcome.directives if d.get("steer")]
    assert len(steers) == 1
    texts = payloads(steers[0])
    assert "did not pass" in texts[0]
    assert "`job-1` was cancelled" in texts[0]
    assert "Do not verify this head" in texts[0]
    assert "Review findings for PR #7" in texts[1]


def test_findings_before_any_verification_reject_the_head_with_nothing_to_cancel(tmp_path):
    port = port_for(comments=[findings(10)])
    store = store_for(tmp_path, state_of(offered()))
    outcome = run(store, port)

    assert port.cancelled_jobs == []
    assert ready_review.rejected(rec_of(store))
    texts = payloads([d for d in outcome.directives if d.get("steer")][0])
    assert "No verification job had started" in texts[0]
    assert "only then start the verification job" in texts[0]


def test_a_verification_submitted_after_the_rejection_is_cancelled_and_explained_once(
    tmp_path,
):
    port = port_for(comments=[findings(10)])
    store = store_for(tmp_path, state_of(offered()))
    run(store, port)
    assert port.cancelled_jobs == []

    # The worker had not read the note yet and submitted its suite anyway.
    port.comments = []
    port.jobs["job-2"] = job("job-2", state="queued")
    run(store, port, now=_LATER)
    assert port.cancelled_jobs == ["job-2"]
    assert any(
        "`job-2` was cancelled" in str(control.get("payload"))
        for control in store.read()["controls"].values()
    )

    notes = [
        cid for cid in store.read()["controls"]
        if cid.startswith("engine:ready-review:cancelled:")
    ]
    run(store, port, now=_LATER_STILL)
    assert port.cancelled_jobs == ["job-2"]
    assert [
        cid for cid in store.read()["controls"]
        if cid.startswith("engine:ready-review:cancelled:")
    ] == notes


def test_a_cancel_that_did_not_land_is_tried_again(tmp_path):
    port = port_for(comments=[findings(10)], jobs={"job-1": job("job-1")})
    port.cancel_fails = True
    store = store_for(tmp_path, state_of(offered()))
    outcome = run(store, port)
    texts = payloads([d for d in outcome.directives if d.get("steer")][0])
    assert "could not be cancelled yet" in texts[0]

    port.comments = []
    port.cancel_fails = False
    run(store, port, now=_LATER)
    assert port.cancelled_jobs == ["job-1", "job-1"]
    assert port.jobs["job-1"]["state"] == "cancelled"


def test_an_unreadable_queue_still_tells_the_worker(tmp_path):
    port = port_for(comments=[findings(10)])
    port.verification_jobs = lambda: None
    store = store_for(tmp_path, state_of(offered()))
    outcome = run(store, port)
    assert ready_review.rejected(rec_of(store))
    texts = payloads([d for d in outcome.directives if d.get("steer")][0])
    assert "could not be cancelled yet" in texts[0]


def test_another_runs_job_is_never_cancelled(tmp_path):
    port = port_for(comments=[findings(10)], jobs={"other": job("other", run_id="gen0")})
    store = store_for(tmp_path, state_of(offered()))
    run(store, port)
    assert port.cancelled_jobs == []


def test_a_finished_job_is_left_alone(tmp_path):
    port = port_for(comments=[findings(10)], jobs={"done": job("done", state="done")})
    store = store_for(tmp_path, state_of(offered()))
    run(store, port)
    assert port.cancelled_jobs == []


def test_changes_needed_on_the_offered_head_rejects_it_too(tmp_path):
    verdict = comment(10, f"/reviewed changes-needed {_HEAD}", author="eero")
    port = port_for(comments=[verdict], jobs={"job-1": job("job-1")})
    store = store_for(tmp_path, state_of(offered()))
    run(store, port)
    assert port.cancelled_jobs == ["job-1"]
    assert ready_review.rejected(rec_of(store))
    assert any("verdict and its reasons" in text for text in queued_texts(store))


def test_a_persons_claude_comment_rejects_nothing(tmp_path):
    port = port_for(comments=[comment(10, "/claude also rename the flag")],
                    jobs={"job-1": job("job-1")})
    store = store_for(tmp_path, state_of(offered()))
    outcome = run(store, port)
    assert port.cancelled_jobs == []
    assert not ready_review.rejected(rec_of(store))
    assert payloads([d for d in outcome.directives if d.get("steer")][0]) == [
        "also rename the flag"
    ]


def test_a_person_quoting_the_findings_heading_is_still_a_person(tmp_path):
    body = f"/claude Review findings for PR #7 at {_HEAD}: please also check this"
    port = port_for(comments=[comment(10, body)], jobs={"job-1": job("job-1")})
    store = store_for(tmp_path, state_of(offered()))
    run(store, port)
    assert port.cancelled_jobs == []
    assert not ready_review.rejected(rec_of(store))


def test_findings_about_an_older_head_leave_the_offer_standing(tmp_path):
    port = port_for(head=_FIX, comments=[findings(10, head=_HEAD)],
                    jobs={"job-1": job("job-1")})
    store = store_for(tmp_path, state_of(offered(head=_FIX)))
    run(store, port)
    assert port.cancelled_jobs == []
    assert not ready_review.rejected(rec_of(store))


def test_a_resting_card_is_reviewed_the_ordinary_way(tmp_path):
    rec = offered()
    rec["status"] = "In review"
    rec["processing"] = []
    port = port_for(comments=[findings(10)], jobs={"job-1": job("job-1")}, status="In review")
    store = store_for(tmp_path, state_of(rec))
    run(store, port)
    assert port.cancelled_jobs == []
    assert not ready_review.rejected(rec_of(store))


def test_without_the_capability_findings_cancel_nothing(tmp_path):
    port = port_for(comments=[findings(10)], jobs={"job-1": job("job-1")})
    store = store_for(tmp_path, state_of(offered()))
    run(store, port, parallel_review=False)
    assert port.cancelled_jobs == []
    assert not ready_review.rejected(rec_of(store))


# --- the offer's lifetime ---

def test_a_new_dispatch_starts_with_nothing_offered(tmp_path):
    rec = offered()
    ready_review.reject(rec, by="c1", now=_NOW)
    rec.update(status="In review", processing=[])
    port = port_for(comments=[comment(10, "/claude one more thing")], status="In review")
    store = store_for(tmp_path, state_of(rec))
    run(store, port)
    after = rec_of(store)
    assert after["status"] == "In progress"
    assert ready_review.current(after) is None


def test_a_work_dispatch_is_told_to_offer_its_head(tmp_path):
    port = FakePort(comments=[comment(1, "/claude fix the header")])
    outcome = run(store_for(tmp_path), port, policy=_POLICY)
    assert outcome.directives[0]["ready_for_review"] is True
    outcome = run(store_for(tmp_path / "off"), FakePort(
        comments=[comment(1, "/claude fix the header")]
    ), parallel_review=False)
    assert "ready_for_review" not in outcome.directives[0]


def test_a_dispatch_is_told_whether_the_engine_asks_for_the_final_suite(tmp_path):
    """#2360: the brief says who starts the final suite, as the Work page's Verify step does."""

    def directive(path, **kwargs):
        port = FakePort(comments=[comment(1, "/claude fix the header")])
        return run(store_for(path), port, parallel_review=False, **kwargs).directives[0]

    assert directive(tmp_path / "auto")["verify_turn"] is True
    assert directive(tmp_path / "turn", auto_release=False, verify_turn=True)["verify_turn"]
    assert "verify_turn" not in directive(tmp_path / "off", auto_release=False)

    ordinary = " ".join(compose_message(
        {**_DIRECTIVE, "verify_turn": True},
        Workspace(path=Path("/w/issue-5"), branch="b"),
        card_control="/ctl",
    ).split())
    assert "Do not submit the final suite before a review passes" in ordinary
    assert "AgentDeck sends this chat one more turn" in ordinary
    asks_nobody = " ".join(compose_message(
        _DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"), card_control="/ctl"
    ).split())
    assert "Nothing in this project starts the final suite for you" in asks_nobody
    assert "one more turn" not in asks_nobody


def test_an_offer_no_full_wake_has_shown_forces_one(tmp_path):
    rec = offered()
    state = state_of(rec)
    state["last_live_wake"] = _NOW.isoformat()
    store = store_for(tmp_path, state)
    port = port_for()
    outcome = run(store, port, webhook_quiet=True, webhook_skip_s=3600)
    assert not outcome.quiet
    assert ready_review.current(rec_of(store))["offered"] is True
    # Shown once, the next quiet wake may skip again.
    outcome = run(store, port, webhook_quiet=True, webhook_skip_s=3600)
    assert outcome.quiet


def test_the_reviewer_extension_sees_only_the_offered_heads_of_a_held_card():
    rec = offered(worker_rounds=2)
    state = state_of(rec)
    ready_review.set_enabled(state, True)
    row = _observe_context(state)["cards"][0]
    # The round this worker's report will bring, so the review asked for now answers it.
    assert row["review_ready"] == {"heads": [_HEAD], "reports": 3}

    ready_review.reject(rec, by="c1", now=_NOW)
    assert _observe_context(state)["cards"][0]["review_ready"]["heads"] == []

    rec["status"] = "In review"
    assert "review_ready" not in _observe_context(state)["cards"][0]
    ready_review.set_enabled(state, False)
    rec["status"] = "In progress"
    assert "review_ready" not in _observe_context(state)["cards"][0]


# --- the reviewer's extension ---

def _nudge_core():
    spec = importlib.util.spec_from_file_location(
        "chatgpt_nudge_core_ready_review", _EXTENSIONS / "chatgpt_nudge_core.py"
    )
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    return core


@shipped("poller/extensions/chatgpt_nudge_core.py")
def test_a_held_cards_unoffered_push_is_not_reviewed():
    core = _nudge_core()
    context = {"cards": [{"ref": _KEY, "status": "In progress", "reports": 2,
                          "review_ready": {"heads": [], "reports": 3}}]}
    rounds = core._card_rounds(context)
    answers = core._answers({"org/store#7": _HEAD}, {"org/store#7": _KEY}, rounds, {})
    # Never answered and not offered: nothing to ask about.
    assert answers["org/store#7"] is None


@shipped("poller/extensions/chatgpt_nudge_core.py")
def test_an_offered_head_is_reviewed_once_across_the_workers_finish():
    core = _nudge_core()
    held = {"cards": [{"ref": _KEY, "status": "In progress", "reports": 2,
                       "review_ready": {"heads": [_HEAD], "reports": 3}}]}
    token = core._answers(
        {"org/store#7": _HEAD}, {"org/store#7": _KEY}, core._card_rounds(held), {}
    )["org/store#7"]
    assert token == f"{_HEAD}@3"

    # The worker finishes In review on the same head: its round is the one already answered.
    rested = {"cards": [{"ref": _KEY, "status": "In review", "reports": 3}]}
    again = core._answers(
        {"org/store#7": _HEAD}, {"org/store#7": _KEY}, core._card_rounds(rested),
        {"org/store#7": token},
    )["org/store#7"]
    assert again == token


@shipped("poller/extensions/chatgpt_nudge_core.py")
def test_a_project_without_the_offer_reviews_every_new_head_as_before():
    core = _nudge_core()
    context = {"cards": [{"ref": _KEY, "status": "In progress", "reports": 2}]}
    answers = core._answers(
        {"org/store#7": _HEAD}, {"org/store#7": _KEY}, core._card_rounds(context), {}
    )
    assert answers["org/store#7"] == f"{_HEAD}@2"


# --- recognising the reviewer ---

def test_the_findings_heading_names_the_head_it_read():
    text = f"Review findings for PR #2204 at {_HEAD}:\n\n1. x"
    assert ready_review.is_findings(text)
    assert ready_review.findings_head(text) == _HEAD
    assert ready_review.is_findings("anything\n\n<!-- chatgpt-review -->")
    assert ready_review.findings_head("anything\n\n<!-- chatgpt-review -->") == ""
    assert not ready_review.is_findings("please look at the findings")


def test_an_offered_head_matches_only_a_real_abbreviation():
    rec = offered()
    ready = ready_review.current(rec)
    assert ready_review.names_head(ready, _HEAD[:7])
    assert ready_review.names_head(ready, _HEAD.upper())
    assert not ready_review.names_head(ready, _HEAD[:6])
    assert not ready_review.names_head(ready, "")
    assert not ready_review.names_head(ready, _FIX)


# --- the worker's brief ---

_DIRECTIVE = {
    "action": "work",
    "repo": "store",
    "number": 5,
    "ref": "store#5",
    "generation": "g",
    "dispatch_id": "d",
    "run_id": "g",
    "messages": [{"payload": "fix it"}],
}


def test_the_brief_asks_for_the_offer_then_the_verification():
    message = compose_message(
        {**_DIRECTIVE, "ready_for_review": True},
        Workspace(path=Path("/w/issue-5"), branch="b"),
        card_control="/ctl",
    )
    flat = " ".join(message.split())
    assert "/ctl ready-for-review --head $(git rev-parse HEAD)" in message
    assert flat.index("ready-for-review") < flat.index("job submit --verification")
    assert "Do not verify a rejected head" in flat
    assert "in a minute" not in flat
    assert "run the offer once more, before your turn ends" in flat
    # The Work's resolution is asked for with the verification job here too (#2355).
    assert "work-resolution" in flat
    assert "last gate before release" not in flat

    ordinary = " ".join(compose_message(
        _DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"), card_control="/ctl"
    ).split())
    assert "ready-for-review" not in ordinary
    assert "last gate before release" in ordinary


# --- the Work card's hint ---

def test_the_board_reads_the_offer_only_from_the_live_worker():
    from agentdeck.poller_ops import _card_review_offer

    rec = offered()
    assert _card_review_offer(rec) == "reviewing"
    ready_review.reject(rec, by="c1", now=_NOW)
    assert _card_review_offer(rec) == "rejected"
    assert _card_review_offer({**rec, "generation": "later"}) == ""
    assert _card_review_offer({**rec, "status": "In review"}) == ""
    assert _card_review_offer({"status": "In progress", "ready_for_review": "junk"}) == ""


def test_a_work_says_its_review_runs_beside_the_workers_verification():
    from agentdeck.poller_ops import BoardCard
    from agentdeck.work_overview import REVIEW_CHANGES, REVIEW_RUNNING, summarize_review

    card = BoardCard(
        "eerovil/agentdeck", 2350, status="In progress", ownership=True, review_offer="reviewing"
    )
    review = summarize_review(card)
    assert (review.state, review.label) == (REVIEW_RUNNING, "Reviewing while the worker verifies")
    assert not review.needs_human

    card = BoardCard(
        "eerovil/agentdeck", 2350, status="In progress", ownership=True, review_offer="rejected"
    )
    review = summarize_review(card)
    assert review.state == REVIEW_CHANGES
    assert "the worker is fixing them" in review.label
    assert not review.needs_human

    # No offer: an ordinary working card has no review line, as before.
    assert summarize_review(
        BoardCard("eerovil/agentdeck", 2350, status="In progress", ownership=True)
    ) is None
