"""The operator's category for a Work, and its pull requests' size (#2236).

Every case is a Work shape the front page can put in Needs you — built from the engine record's
own shape and read through ``poller_ops.read_board``, as the deck reads it — and the one
operator category and action list it gets, or no category at all. The #2232/#2234 live shapes
are among them: a release parked with its reason, a Needs More Info park standing in for a
review, a merged pull request on an issue still open, a Run past its restarts.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentdeck import poller_ops, work_actions
from agentdeck.poller_engine.revive import MAX_REVIVES
from agentdeck.poller_engine.state import EnginePaths, StateStore
from agentdeck.poller_engine.workspace import WorkspaceSnapshot
from agentdeck.poller_nudge import ExternalReviews
from agentdeck.run_trace import NEXT_HUMAN, RunTraceFacts, StoredOutcome, build_run_trace
from agentdeck.webhook_status_cache import WebhookStatusCacheService
from agentdeck.work import WorkRef
from agentdeck.work_attention import (
    CLOSED_UNMERGED,
    ISSUE_CLOSED,
    LABELS,
    MERGED_ISSUE_OPEN,
    NEEDS_ANSWER,
    NEEDS_RETRY,
    NEEDS_REVIEW,
    READY_FOR_MERGE,
    RELEASE_BLOCKED,
    WORK_ERROR,
    operator_attention,
)
from agentdeck.work_facts import (
    DiffStat,
    OutputFact,
    diffstat,
    new_source_size,
    source_size,
    target_branches,
    work_facts,
)
from agentdeck.work_overview import WorkAttention, build_work_card
from agentdeck.work_story import NEEDS_YOU, front_row, work_status
from agentdeck.work_workspace import RunVerification

ORG = "eerovil"
WORK = WorkRef(f"{ORG}/agentdeck", 5)
PR = f"https://github.com/{ORG}/agentdeck/pull/7"
HEAD = "abc1234def5678abc1234def5678abc1234def56"
AT = "2026-09-25T10:00:00+00:00"


def _card(tmp_path, record: dict) -> poller_ops.BoardCard:
    base = tmp_path / "instances" / "agentdeck"
    base.mkdir(parents=True, exist_ok=True)
    (base / "poller.toml").write_text(
        f'contract = 1\n\n[board]\norg = "{ORG}"\nproject_number = 1\n'
    )
    store = StateStore(EnginePaths.from_base(base / "state.json"))
    with store.transaction() as engine:
        engine["issues"] = {"agentdeck#5": dict(record)}
    poller_ops.forget_durable_reads()
    card = poller_ops.read_board(tmp_path).card(WORK.repository, WORK.number)
    assert card is not None
    return card


def _row(state: str = "open", *, head: str = HEAD, sized: bool = True, **extra) -> dict:
    row = {"state": state, "headRefOid": head, "baseRefName": "master", **extra}
    if state == "merged":
        row.update(state="closed", mergedAt=AT)
    if sized:
        # Only the two source files count towards the size a Work shows (#2331).
        row = {"additions": 94, "deletions": 9, "changedFiles": 4, "files": list(_FILES), **row}
    return row


_FILES = (
    {"path": "src/agentdeck/a.py", "additions": 30, "deletions": 3, "changeType": "MODIFIED"},
    {"path": "src/agentdeck/web/templates/b.html", "additions": 10, "deletions": 0,
     "changeType": "ADDED"},
    {"path": "tests/test_a.py", "additions": 50, "deletions": 5},
    {"path": ".claude/codebase/a.md", "additions": 4, "deletions": 1},
)


def _work(
    tmp_path, record: dict, *, outcome: str = "ended", rows=None, attention=(), reviewer=None,
    checks=None, verifications=(), verifies_itself=False, ci=None, releases_itself=False,
    review_only=False,
):
    card = _card(tmp_path, record)
    if review_only:
        # This repository has no tests: its review is its verification (#2729, #2832).
        card = replace(card, review_only_repos=(f"{ORG}/agentdeck",))
    if verifies_itself:
        card = replace(card, verifies_itself=True)
    if releases_itself:
        card = replace(card, releases_itself=True)
    if ci is not None:
        # This repository's CI is its verification (#2364); ``ci`` is the engine's readings.
        card = replace(card, ci_repos=(f"{ORG}/agentdeck",), ci_checks=tuple(ci))
    if reviewer is not None:
        card = replace(card, reviewer=reviewer)
    outcomes = (
        (StoredOutcome(card.generation, outcome, "", observed_at=time.time()),) if outcome else ()
    )
    trace = build_run_trace(RunTraceFacts(work=WORK, card=card, outcomes=outcomes))
    rows = {PR.lower(): _row()} if rows is None else rows
    return build_work_card(
        WORK,
        issue=None,
        title="Work",
        facts=work_facts(WORK, card=card, rows=rows, checks=checks),
        trace=trace,
        attention=attention,
        verifications=verifications,
    )


def _record(status: str = "In review", **extra) -> dict:
    return {
        "status": status,
        "engine_claim": True,
        "started": True,
        "started_at": AT,
        "generation": "gen-2",
        "comment_links": {"pull_request_url": PR},
        **extra,
    }


def _passed(head: str = HEAD) -> dict:
    return {"release_bits": {"bits": {"review-passed": True}, "heads": {"agentdeck": head}}}


def _ask(interaction: str = "", *, asks: bool = True) -> WorkAttention:
    return WorkAttention(
        "claude_code:main:worker", "Worker chat", "waiting", "/chats/w",
        asks=asks, question="Which branch?", interaction=interaction,
    )


REVIEW_ACTIONS = ("agent_review", "chatgpt_review", "review")

# The instance reviewer's state (#2254): ``seen`` says a review ran, ``clean`` that it closed
# clean about exactly that head.
KEY = f"{ORG}/agentdeck#7"
OLDER = "0" * 40
PR2 = f"https://github.com/{ORG}/other/pull/3"
KEY2 = f"{ORG}/other#3"
UP_TO_DATE = "reviewed, up to date with its base, and still not merged"
CONFLICT = (
    "13 commits behind its base, and GitHub would not merge the base in — most likely a "
    "conflict somebody has to resolve"
)


def _reviewer(clean=(), *, stalled=None, older=(), changes=None) -> ExternalReviews:
    keys = (KEY, KEY2)
    return ExternalReviews(
        None, {}, frozenset(),
        seen={key.casefold(): HEAD for key in keys},
        stalled={key.casefold(): (HEAD, why) for key, why in (stalled or {}).items()},
        clean={
            **{key.casefold(): HEAD for key in clean},
            **{key.casefold(): OLDER for key in older},
        },
        changes={key.casefold(): (HEAD, at) for key, at in (changes or {}).items()},
    )


# An hour ago: past the grace a sent-back head gets for its automatic fix round (#2332).
AN_HOUR_AGO = time.time() - 3600
# GitHub's merge answer for the head, as ``merge_checks.parse`` caches it (#2332).
DIRTY = {PR.lower(): {"head": HEAD, "mergeable": "CONFLICTING", "mergeState": "DIRTY"}}
PARKED_WHY = (
    "Auto-merge on `agentdeck#7` is no longer armed on GitHub and its checks are not failing, "
    "so nothing will merge it. `/merge` again to re-arm."
)


def _snapshot(repository: str, head: str = HEAD) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        root=f"/w/{repository.rsplit('/', 1)[-1]}",
        head=head,
        staged_sha256="0" * 64,
        tracked_diff_sha256="0" * 64,
        untracked_sha256="0" * 64,
        repository=repository,
    )


def _green(run_id: str = "an-earlier-run", head: str = HEAD) -> tuple[RunVerification, ...]:
    """A green verification of both test repositories at ``head`` (#2364). An earlier Run's by
    default: the engine credits a green run by the commit it tested, whoever submitted it."""
    tested = tuple(
        (name, _snapshot(name, head)) for name in (f"{ORG}/agentdeck", f"{ORG}/other")
    )
    return (RunVerification("job-green", run_id, "done", True, tested),)


GREEN = _green()


def _ci(state: str, head: str = HEAD) -> tuple:
    return ((f"{ORG}/agentdeck", 7, head, state),)


def _parked_release(status: str = "Ready to merge", **extra) -> dict:
    return _record(
        status,
        release_blocked={"why": PARKED_WHY, "status": status, "generation": "gen-2"},
        **extra,
    )


TWO = _record("In review", review_prs=["other#3"])
TWO_ROWS = {PR.lower(): _row(), PR2.lower(): _row()}

# (id, record, work kwargs, category, actions). ``hide`` ends every list and is added below.
CASES = [
    # Needs answer: a question in the chat's text, a form, an approval, a Needs More Info park
    # and a worker's own Blocked with nothing named to wait on.
    ("text-question", _record("In progress"), {"attention": (_ask(),)}, NEEDS_ANSWER, ("answer",)),
    ("question-form", _record("In progress"), {"attention": (_ask("mcp_form"),)},
     NEEDS_ANSWER, ("open",)),
    ("approval", _record("In progress"), {"attention": (_ask("permission", asks=False),)},
     NEEDS_ANSWER, ("open",)),
    ("browser-step", _record("In progress"), {"attention": (_ask("mcp_url", asks=False),)},
     NEEDS_ANSWER, ("open",)),
    ("needs-more-info", _record("Needs More Info"), {}, NEEDS_ANSWER, ("answer",)),
    # Blocked with nothing asked: what it said blocks it, and closing is the other way out.
    ("worker-blocked", _record("Blocked"), {}, NEEDS_ANSWER, ("close", "answer")),
    ("worker-blocked-with-a-question",
     _record("Blocked", park_note={"generation": "gen-2", "text": "Should it be limited?"}),
     {}, NEEDS_ANSWER, ("answer",)),
    # issues#393 (#2332): parked for an answer, but every pull request already merged.
    ("parked-but-merged", _record("Needs More Info"), {"rows": {PR.lower(): _row("merged")}},
     MERGED_ISSUE_OPEN, ("close", "answer")),
    # store#3368 (#2332): parked in Ready to merge with every head clean. The deck no longer
    # asks a Poller card for its merge (#3025), so it stays the release's blocker to look at.
    ("ready-release-parked-clean", _parked_release(),
     {"reviewer": _reviewer([KEY]), "verifications": GREEN},
     RELEASE_BLOCKED, ("inspect",)),
    ("ready-release-parked-clean-unverified", _parked_release(), {"reviewer": _reviewer([KEY])},
     RELEASE_BLOCKED, ("inspect",)),
    # issues#449 (#2332): parked after its pull request merged: the issue is left to close.
    ("ready-release-parked-merged", _parked_release(merged=["agentdeck#7"]), {"rows": {}},
     RELEASE_BLOCKED, ("close", "inspect")),
    ("ready-release-parked-unreviewed", _parked_release(), {}, RELEASE_BLOCKED,
     ("inspect",)),
    # abacus#1953 (#2332): clean at its head, but GitHub says it conflicts: no Merge.
    ("reviewer-clean-but-conflicting", _record("In review"),
     {"reviewer": _reviewer([KEY]), "checks": DIRTY}, NEEDS_RETRY,
     ("review", "inspect")),
    ("passed-but-conflicting", _record("Ready to merge", **_passed()), {"checks": DIRTY},
     NEEDS_RETRY, ("review", "inspect")),
    # bazaar#307 (#2332): the reviewer sent the head back and nothing picked it up.
    ("reviewer-sent-back", _record("In review"),
     {"reviewer": _reviewer(changes={KEY: AN_HOUR_AGO})}, NEEDS_RETRY,
     ("inspect", "review")),
    # Needs review: a person's review is what is left.
    ("human-review", _record("In review"), {}, NEEDS_REVIEW, REVIEW_ACTIONS),
    ("human-review-column", _record("Human review pending"), {}, NEEDS_REVIEW, REVIEW_ACTIONS),
    # A worker's human_review_pending finish, on a board without that column (#2235).
    ("human-review-fallback",
     _record("Needs More Info", parked_as="human_review_pending"), {},
     NEEDS_REVIEW, REVIEW_ACTIONS),
    ("passed-an-older-head", _record("Ready to merge", **_passed("0" * 40)), {},
     NEEDS_REVIEW, REVIEW_ACTIONS),
    # Ready for merge: every open pull request's head is the one the review passed.
    ("ready-to-merge", _record("Ready to merge", **_passed()), {"verifications": GREEN},
     READY_FOR_MERGE, ("merge",)),
    ("ready-to-merge-short-head", _record("Ready to merge", **_passed(HEAD[:7])),
     {"verifications": GREEN}, READY_FOR_MERGE, ("merge",)),
    ("ready-to-merge-unverified", _record("Ready to merge", **_passed()), {},
     NEEDS_RETRY, ("inspect",)),
    ("ready-to-merge-verified-older-head", _record("Ready to merge", **_passed()),
     {"verifications": _green(head=OLDER)}, NEEDS_RETRY, ("inspect",)),
    # Release blocked: only with the engine's own lasting or pending reason.
    ("release-parked",
     _record("Blocked", **_passed(),
             release_blocked={"why": "Auto-merge is disabled", "status": "Blocked",
                              "generation": "gen-2"}),
     {}, RELEASE_BLOCKED, ("inspect",)),
    ("release-blocking",
     _record("Ready to merge",
             blocked_pending={"why": "checks failed", "merged": [], "steps": {}}),
     {}, RELEASE_BLOCKED, ("inspect",)),
    # Work error: a release-like state nothing explains, or review with no pull request.
    ("release-stopped-without-reason", _record("Blocked", **_passed()), {},
     WORK_ERROR, ("inspect",)),
    ("in-review-without-output", _record("In review", comment_links={}), {"rows": {}},
     WORK_ERROR, ("inspect",)),
    ("ready-without-output", _record("Ready to merge", comment_links={}, **_passed()),
     {"rows": {}}, WORK_ERROR, ("inspect",)),
    # Merged, issue still open; and closed without merging.
    ("merged-issue-open", _record("In review"), {"rows": {PR.lower(): _row("merged")}},
     MERGED_ISSUE_OPEN, ("close",)),
    ("closed-unmerged", _record("In review"), {"rows": {PR.lower(): _row("closed")}},
     CLOSED_UNMERGED, ("close",)),
    # Needs retry: only what nothing retries by itself.
    # Ready to merge with nothing reviewed at its head is a review, never a guessed retry (#2332).
    ("ready-without-a-review", _record("Ready to merge"), {}, NEEDS_REVIEW, REVIEW_ACTIONS),
    ("run-past-its-restarts", _record("In progress", revives=MAX_REVIVES), {"outcome": "died"},
     NEEDS_RETRY, ("inspect",)),
    ("restarts-ran-out-into-needs-more-info",
     _record("Needs More Info", revives=MAX_REVIVES), {}, NEEDS_RETRY, ("inspect",)),
    ("dispatch-never-landed",
     _record("Blocked", dispatch_failed=True, dispatch_failed_at=AT,
             dispatch_failed_refusal="at_capacity"),
     {}, NEEDS_RETRY, ("inspect",)),
    ("chat-stopped", _record("In progress"),
     {"attention": (WorkAttention("claude_code:main:m", "Manual", "stalled", "/chats/m"),)},
     NEEDS_RETRY, ("inspect",)),
    # The instance reviewer (#2254): only a clean review of every open head is Ready for merge,
    # merged on GitHub (#3025).
    ("reviewer-clean-at-head", _record("In review"),
     {"reviewer": _reviewer([KEY]), "verifications": GREEN}, READY_FOR_MERGE, ("review",)),
    # issues#469 (#2364): clean at its head, but no verification ever ran.
    ("reviewer-clean-at-head-unverified", _record("In review"), {"reviewer": _reviewer([KEY])},
     NEEDS_RETRY, ("inspect",)),
    ("reviewer-clean-up-to-date-stall", _record("In review"),
     {"reviewer": _reviewer([KEY], stalled={KEY: UP_TO_DATE}), "verifications": GREEN},
     READY_FOR_MERGE, ("review",)),
    ("reviewer-reviewed-not-clean", _record("In review"), {"reviewer": _reviewer()},
     NEEDS_REVIEW, REVIEW_ACTIONS),
    ("reviewer-clean-older-head", _record("In review"), {"reviewer": _reviewer(older=[KEY])},
     NEEDS_REVIEW, REVIEW_ACTIONS),
    ("reviewer-clean-conflict-stalled", _record("In review"),
     {"reviewer": _reviewer([KEY], stalled={KEY: CONFLICT})}, NEEDS_RETRY,
     ("review", "inspect")),
    ("reviewer-clean-draft", _record("In review"),
     {"reviewer": _reviewer([KEY]), "rows": {PR.lower(): _row(isDraft=True)}},
     NEEDS_REVIEW, REVIEW_ACTIONS),
    ("reviewer-clean-unknown-head", _record("In review"),
     {"reviewer": _reviewer([KEY]), "rows": {PR.lower(): _row(head="")}},
     NEEDS_REVIEW, REVIEW_ACTIONS),
    ("two-prs-both-clean", TWO,
     {"reviewer": _reviewer([KEY, KEY2]), "rows": TWO_ROWS, "verifications": GREEN},
     READY_FOR_MERGE, ("review",)),
    ("two-prs-one-clean", TWO, {"reviewer": _reviewer([KEY]), "rows": TWO_ROWS},
     NEEDS_REVIEW, REVIEW_ACTIONS),
    ("two-prs-one-merged-one-clean", TWO,
     {"reviewer": _reviewer([KEY]), "rows": {PR.lower(): _row(), PR2.lower(): _row("merged")},
      "verifications": GREEN},
     READY_FOR_MERGE, ("review",)),
]


@pytest.mark.parametrize(
    ("record", "extra", "category", "actions"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_every_needs_you_shape_gets_exactly_one_operator_category(
    tmp_path, record, extra, category, actions
):
    work = _work(tmp_path, record, **extra)
    row = front_row(work, current=True)
    detail = operator_attention(work, work_status(work))

    assert row.section == NEEDS_YOU
    assert row.attention == detail  # the front page and the Work page read the same one
    assert (detail.category, detail.actions) == (category, (*_offered(actions), "hide"))
    assert detail.label == LABELS[category]


def _offered(actions: tuple[str, ...]) -> tuple[str, ...]:
    """The expected actions, less the two review buttons where no reviewer exists (#3623)."""
    from agentdeck import review_refs

    if review_refs.reviewer_available():
        return actions
    return tuple(action for action in actions if action not in ("agent_review", "chatgpt_review"))


# Shapes that need nobody: automatic progress, automatic recovery and named blockers.
QUIET = [
    ("running", _record("In progress"), {"outcome": "running"}),
    ("run-with-restarts-left", _record("In progress", revives=MAX_REVIVES - 1),
     {"outcome": "died"}),
    ("clean-dispatch-park-retries",
     _record("Blocked", dispatch_failed=True, dispatch_failed_clean=True, dispatch_failed_at=AT,
             dispatch_failed_refusal="at_capacity", dispatch_failed_refusal_final=True),
     {}),
    ("reviewer-running", _record("In progress", mode="merge-review"), {"outcome": "running"}),
    ("blocked-by-named-issues",
     _record("Blocked", blocked_until=[f"{ORG}/agentdeck#1"], **_passed()), {}),
    ("needs-more-info-waiting-on-issues",
     _record("Needs More Info", blocked_until=[f"{ORG}/agentdeck#1"]), {}),
    # Sent back a minute ago: the automatic fix round's to pick up first (#2332). A callable,
    # so "a minute ago" is read when the test runs, not at collection: a slow suite reaches
    # this case past the 15-minute grace (#2376).
    ("reviewer-just-sent-back", _record("In review"),
     {"reviewer": lambda: _reviewer(changes={KEY: time.time() - 60})}),
    # agentdeck#2327 (#2332): merged by `/merge` after its review escalated, deploy watched.
    ("deploying-after-an-escalated-review",
     _record("In review", merged=["agentdeck#7"], awaiting_deploy={"builds": {}, "since": AT}),
     {"rows": {PR.lower(): _row("merged")}}),
]


@pytest.mark.parametrize(
    ("record", "extra"), [case[1:] for case in QUIET], ids=[case[0] for case in QUIET]
)
def test_automatic_progress_recovery_and_named_blockers_get_no_category(tmp_path, record, extra):
    extra = {name: value() if callable(value) else value for name, value in extra.items()}
    work = _work(tmp_path, record, **extra)

    assert operator_attention(work, work_status(work)) is None
    assert front_row(work, current=True).section != NEEDS_YOU


# Which Needs retry shapes AgentDeck broke, by case id; every other one is the work's (#2888).
BUGS = {
    "run-past-its-restarts": "worker_died",
    "restarts-ran-out-into-needs-more-info": "worker_died",
}


@pytest.mark.parametrize(
    ("record", "extra", "bug"),
    [(case[1], case[2], BUGS.get(case[0], "")) for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_only_a_needs_retry_agentdeck_broke_names_a_bug(tmp_path, record, extra, bug):
    work = _work(tmp_path, record, **extra)

    assert operator_attention(work, work_status(work)).bug == bug


@pytest.mark.parametrize(
    ("record", "extra", "bug"),
    [
        # A start the host refused for something other than room, or with no refusal named.
        (_record("Blocked", dispatch_failed=True, dispatch_failed_at=AT,
                 dispatch_failed_refusal="codex_create_failed"), {},
         "start_refused:codex_create_failed"),
        (_record("Blocked", dispatch_failed=True, dispatch_failed_at=AT), {},
         "start_refused:unknown"),
        (_record("Blocked", dispatch_failed=True, dispatch_failed_at=AT,
                 dispatch_failed_refusal="over_budget"), {}, ""),
        # A chat whose turn died is a bug; one that only went quiet is not.
        (_record("In progress"),
         {"attention": (WorkAttention("claude_code:main:m", "Manual", "waiting", "/chats/m",
                                      cause="died_mid_turn"),)},
         "chat_stopped"),
        (_record("In progress"),
         {"attention": (WorkAttention("claude_code:main:m", "Manual", "waiting", "/chats/m",
                                      cause="reported_state"),)},
         ""),
        # #3354: an expired sign-in is the account's, not AgentDeck's.
        (_record("In progress"),
         {"attention": (WorkAttention("claude_code:alt:m", "Manual", "waiting", "/chats/m",
                                      cause="login_expired"),)},
         ""),
    ],
    ids=["refused-start", "unknown-start", "over-budget", "chat-died", "chat-quiet",
         "login-expired"],
)
def test_a_refused_start_or_a_dead_chat_is_a_bug_only_when_agentdeck_broke(
    tmp_path, record, extra, bug
):
    work = _work(tmp_path, record, **extra)
    found = operator_attention(work, work_status(work))

    assert (found.category, found.bug) == (NEEDS_RETRY, bug)


def test_a_review_that_stopped_and_will_not_retry_is_a_retry_with_review_actions(tmp_path):
    work = _work(
        tmp_path,
        _record("In progress", mode="merge-review"),
        outcome="died",
    )
    assert work.review is not None and work.review.state == "failed"
    stopped = replace(work, review=replace(work.review, next_expected=NEXT_HUMAN), stuck=False)

    found = operator_attention(stopped, work_status(stopped))

    assert found is not None and found.category == NEEDS_RETRY
    assert found.actions == _offered(
        ("inspect", "agent_review", "chatgpt_review", "review", "hide")
    )
    assert found.bug == "review_run_failed"


def test_with_no_reviewer_only_a_persons_review_is_offered(tmp_path, monkeypatch):
    """The public copy has no reviewer (#3623): its two review buttons would only refuse."""
    from agentdeck import chat_review, review_refs

    monkeypatch.setattr(chat_review, "PUBLIC_STUB", False, raising=False)  # the real module
    assert review_refs.reviewer_available()
    monkeypatch.setattr(chat_review, "PUBLIC_STUB", True, raising=False)  # the stand-in's mark
    assert not review_refs.reviewer_available()
    work = _work(tmp_path, _record("In progress", mode="merge-review"), outcome="died")
    stopped = replace(work, review=replace(work.review, next_expected=NEXT_HUMAN), stuck=False)

    found = operator_attention(stopped, work_status(stopped))

    assert found is not None and found.actions == ("inspect", "review", "hide")


def test_a_running_worker_is_never_offered_an_instruction(tmp_path):
    work = _work(tmp_path, _record("In review"), outcome="running")
    found = operator_attention(work, work_status(work))

    # A Run the trace calls running, with no chat of it thinking, is not mid-turn (#2590):
    # the review it waits on shows, but nothing is sent to a worker that is still running.
    assert found is not None
    assert not {"retry", "verify"} & set(found.actions)

    story = work_status(_mid_turn(work, SIDE_CHAT))
    assert (story.headline, story.detail.startswith("When it finishes: ")) == ("Working", True)
    assert operator_attention(_mid_turn(work, SIDE_CHAT), story) is None


def test_a_clean_work_is_never_offered_merge(tmp_path):
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]), verifications=GREEN)

    found = operator_attention(work, work_status(work))

    # Still ready for merge (#2327), but merged on GitHub: the deck cannot arm it.
    assert found is not None and found.category == READY_FOR_MERGE
    assert found.actions == ("review", "hide")
    assert found.reason == "Clean review at the current head; merge it on GitHub"


@pytest.mark.parametrize(
    ("reviewer", "reason"),
    [
        # issues#434: two outputs stalled on the same words read them once, naming both.
        (_reviewer(stalled={KEY: UP_TO_DATE, KEY2: UP_TO_DATE}),
         f"Not merged: {UP_TO_DATE} on {KEY} and {KEY2}"),
        (_reviewer([KEY2], stalled={KEY: UP_TO_DATE, KEY2: UP_TO_DATE}),
         f"Not merged: {UP_TO_DATE} on {KEY}; {KEY2} reviewed clean"),
        (_reviewer(stalled={KEY: UP_TO_DATE}),
         f"{KEY}: Not merged: {UP_TO_DATE} (+1 more)"),
        (_reviewer([KEY]), f"Reviewed at {HEAD[:7]}; ready for a person to decide on {KEY2}; "
                           f"{KEY} reviewed clean"),
    ],
    ids=["same-stalled-label", "one-clean", "different-labels", "one-clean-one-reviewed"],
)
def test_several_outputs_waiting_on_the_reviewer_read_as_one_line(tmp_path, reviewer, reason):
    work = _work(tmp_path, TWO, rows=TWO_ROWS, reviewer=reviewer)

    found = operator_attention(work, work_status(work))

    assert (found.category, found.reason) == (NEEDS_REVIEW, reason)
    # The review's own label, which the technical story reads, is untouched.
    assert work.review.label != reason


def test_a_work_with_every_output_clean_is_merged_on_github(tmp_path):
    work = _work(tmp_path, TWO, rows=TWO_ROWS, reviewer=_reviewer([KEY, KEY2]), verifications=GREEN)

    found = operator_attention(work, work_status(work))

    assert (found.category, found.actions) == (READY_FOR_MERGE, ("review", "hide"))
    assert work.review.label == "Every pull request is reviewed clean at its current head"


BROUGHT_FORWARD = "brought forward 3 times and still not merged — nothing here will move it again"


@pytest.mark.parametrize(
    ("stalled", "label"),
    [
        (None, f"Reviewed clean at {HEAD[:7]}"),
        ({KEY: UP_TO_DATE},
         f"Reviewed clean at {HEAD[:7]}; up to date with its base and still not merged"),
        ({KEY: BROUGHT_FORWARD},
         f"Reviewed clean at {HEAD[:7]}; not merged after 3 update rounds, and AgentDeck no "
         "longer updates the branch"),
    ],
    ids=["clean", "up-to-date-stall", "brought-forward-stall"],
)
def test_a_clean_review_at_the_head_reads_as_a_merge_everywhere(tmp_path, stalled, label):
    """abacus#1951 (#2327): the category said Ready for merge while the page asked for a
    review decision; every reading of one clean head now says the merge is what waits."""
    work = _work(
        tmp_path, _record("In review"), reviewer=_reviewer([KEY], stalled=stalled),
        verifications=GREEN,
    )
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert (work.review.state, work.review.decision, work.review.label) == (
        "passed", "merge", label
    )
    assert (story.headline, story.next, story.you) == (
        "Ready for you to merge", "your merge", "Merge the pull request"
    )
    assert story.summary == label + "."
    assert story.where_href == PR
    assert steps["review"] == ("done", "Passed")
    assert steps["merge"] == ("attention", "Needs you")
    found = operator_attention(work, story)
    assert (found.category, found.reason, found.actions) == (
        READY_FOR_MERGE, "Clean review at the current head; merge it on GitHub", ("review", "hide")
    )


@pytest.mark.parametrize("state", ["running", "queued"])
def test_a_clean_review_waits_on_its_running_verification_not_on_a_person(tmp_path, state):
    """#2352: while the final suite runs on the reviewed head the Work is verifying; it is
    neither Ready for merge nor in Needs you, and the Merge step waits rather than asks."""
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]))
    check = RunVerification("job-1", work.run_id, state, False, ())
    work = _work(
        tmp_path, _record("In review"), reviewer=_reviewer([KEY]), verifications=(check,)
    )
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert (work.review.state, work.review.needs_human, work.review.decision) == (
        "passed", False, ""
    )
    assert story.headline == "Verifying"
    assert steps["verify"] == ("active", state.capitalize())
    assert steps["merge"] == ("waiting", "Waiting")
    assert operator_attention(work, story) is None
    assert front_row(work, current=True).section != NEEDS_YOU


def test_a_clean_review_is_a_merge_again_once_verification_passed(tmp_path):
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]))
    work = _work(
        tmp_path, _record("In review"), reviewer=_reviewer([KEY]),
        verifications=_green(work.run_id),
    )

    assert work.verification.status == "verified"
    assert operator_attention(work, work_status(work)).category == READY_FOR_MERGE


SIDE_CHAT = ("Side chat", "/chats/side")


def _mid_turn(work, *chats, run_state: str = ""):
    """``work`` while the given chats are mid-turn, its Run in ``run_state`` (#2481)."""
    return replace(
        work,
        run_state=run_state or work.run_state,
        working_count=len(chats),
        working_chats=tuple(chats),
    )


@pytest.mark.parametrize(
    ("chats", "run_state", "summary"),
    [
        ((SIDE_CHAT,), "", "Side chat is working."),
        ((SIDE_CHAT, ("Worker", "/chats/w")), "", "Side chat and 1 more chat are working."),
        ((SIDE_CHAT,), "running", "Side chat is working."),
    ],
    ids=["chat", "two-chats", "chat-and-run"],
)
def test_a_merge_ready_work_is_working_while_a_turn_runs(tmp_path, chats, run_state, summary):
    """#2481: the screenshot's Work said Ready for merge while a chat on it was mid-turn. The
    turn may still push, so the Work is Working, offers no Merge, and refuses a press."""
    ready = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]), verifications=GREEN)
    ready_story = work_status(ready)
    assert operator_attention(ready, ready_story).category == READY_FOR_MERGE

    work = _mid_turn(ready, *chats, run_state=run_state)
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert (story.tone, story.headline, story.kind) == ("active", "Working", "")
    # The merge it waits for is said as any other reading is (#2590).
    assert story.summary == summary
    assert story.detail.startswith(f"When it finishes: {ready_story.headline}: ")
    assert "Merge waits" not in story.detail
    assert (story.where, story.where_href) == SIDE_CHAT
    assert steps["merge"] == ("waiting", "Waiting")
    assert steps["review"] == ("done", "Passed")
    assert operator_attention(work, story) is None
    assert front_row(work, current=True).section != NEEDS_YOU
    with pytest.raises(work_actions.Refused, match="not offered"):
        work_actions.plan(work_actions.MERGE, work, operator_attention(work, story))


def test_a_turn_holds_back_what_a_person_must_fix_until_it_ends(tmp_path):
    """#2572 reverses #2481's exception: a red suite waits on screen while a chat on the Work
    is mid-turn, named in the detail, and asks for a person again once the turn ends."""
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]))
    check = RunVerification("job-1", work.run_id, "failed", False, ())
    work = _work(
        tmp_path, _record("In review"), reviewer=_reviewer([KEY]), verifications=(check,)
    )
    idle = work_status(work)
    busy = _mid_turn(work, SIDE_CHAT)
    story = work_status(busy)

    assert (idle.headline, front_row(work, current=True).section) == (
        "Verification failed", NEEDS_YOU
    )
    assert (story.tone, story.headline) == ("active", "Working")
    assert story.detail.startswith(f"When it finishes: Verification failed: {idle.summary}")
    assert operator_attention(busy, story) is None
    assert front_row(busy, current=True).section != NEEDS_YOU


STOPPED_CHAT = WorkAttention("claude_code:main:m", "Manual", "stalled", "/chats/m")


@pytest.mark.parametrize(
    "record, extra",
    [
        (_record("Blocked", dispatch_failed=True, dispatch_failed_at=AT), {}),
        (_record("Blocked", dispatch_failed=True, dispatch_failed_at=AT,
                 dispatch_failed_clean=True), {}),
        (_record("In progress"), {"attention": (STOPPED_CHAT,)}),
        (_record("In progress", revives=MAX_REVIVES), {"outcome": "died"}),
    ],
    ids=["never-started", "retrying-the-start", "chat-stopped", "stuck-run"],
)
def test_a_stopped_work_is_working_while_a_turn_on_it_runs(tmp_path, record, extra):
    """#2487: the Work said Worker never started while the card's earlier worker chat, which
    the operator had carried on in, was mid-turn. A turn on the Work proves it has not
    stopped, so it reads Working, says what it will say when the turn ends (#2590), and
    offers no Retry until then."""
    work = _work(tmp_path, record, rows={}, **extra)
    idle = work_status(work)
    assert idle.headline in {
        "Worker never started", "Retrying the start", "Chat needs a look", "Run needs a look"
    }

    busy = _mid_turn(work, SIDE_CHAT)
    story = work_status(busy)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert (story.tone, story.headline, story.kind) == ("active", "Working", "")
    assert (story.summary, story.where, story.where_href) == (
        "Side chat is working.", *SIDE_CHAT
    )
    assert story.detail.startswith(f"When it finishes: {idle.headline}: ")
    assert "Shown again" not in story.detail
    assert steps["implement"] == ("active", "Working")
    assert "attention" not in {step.state for step in story.milestones}
    assert operator_attention(busy, story) is None
    assert front_row(busy, current=True).section != NEEDS_YOU


def test_a_turn_on_the_work_outweighs_a_park_the_worker_made(tmp_path):
    """#2572 (reversing the #2487 review rule): the Work said Blocked and Unblock the card
    while its worker chat, carried on after the park, was mid-turn. What runs outweighs the
    column; the reading is in the detail and asks for a person again when the turn ends. A
    stopped chat beside it is the reading now, and the park its own (#2590)."""
    work = _work(tmp_path, _record("Blocked"), rows={})
    assert front_row(work, current=True).section == NEEDS_YOU

    busy = _mid_turn(work, ("Worker chat", "/chats/w"))
    story = work_status(busy)

    assert (story.tone, story.headline, story.you) == ("active", "Working", "Nothing needed")
    assert story.summary == "Worker chat is working."
    assert story.detail == "When it finishes: Blocked: The card waits in Blocked."
    assert operator_attention(busy, story) is None
    assert front_row(busy, current=True).section != NEEDS_YOU


@pytest.mark.parametrize("status", ["Blocked", "Needs More Info"])
def test_a_run_with_no_chat_mid_turn_does_not_hide_a_park(tmp_path, status):
    """#2590: mid-turn means a chat on the Work is thinking. A Run the trace calls running
    whose chat is not is no turn, so the park still asks for a person."""
    work = _work(tmp_path, _record(status), rows={})
    running = _mid_turn(work, run_state="running")

    assert work_status(running).headline == work_status(work).headline != "Working"
    assert front_row(running, current=True).section == NEEDS_YOU


def test_a_run_whose_own_chat_stalled_still_needs_a_look(tmp_path):
    """#2235/#2572: a Run still going with its worker chat stalled is the turn itself stuck."""
    work = _work(tmp_path, _record("In progress"), rows={}, attention=(STOPPED_CHAT,))
    story = work_status(_mid_turn(work, run_state="running"))

    assert story.headline == "Chat needs a look"
    assert front_row(_mid_turn(work, run_state="running"), current=True).section == NEEDS_YOU


@pytest.mark.parametrize("interaction", ["", "permission"])
def test_a_chat_waiting_on_a_person_waits_while_another_runs(tmp_path, interaction):
    """#2590 (reversing #2572's exception): a question or an approval in one chat while
    another chat on the Work is mid-turn is Working too, with the question in the detail.
    It asks for a person again when the turn ends."""
    work = _work(
        tmp_path, _record("Blocked"), rows={}, attention=(_ask(interaction, asks=not interaction),)
    )
    busy = _mid_turn(work, SIDE_CHAT)
    story, idle = work_status(busy), work_status(work)

    assert idle.kind in {"chat_question", "chat_action"}
    assert front_row(work, current=True).section == NEEDS_YOU
    assert (story.tone, story.headline, story.kind) == ("active", "Working", "")
    assert story.detail.startswith(f"When it finishes: {idle.headline}: ")
    assert operator_attention(busy, story) is None
    assert front_row(busy, current=True).section != NEEDS_YOU


def test_a_failed_review_run_is_waiting_while_a_turn_runs(tmp_path):
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]))
    failed = replace(work, review=replace(work.review, state="failed", run_id="gen-1"))
    assert work_status(failed).headline == "Review stopped"

    story = work_status(_mid_turn(failed, SIDE_CHAT))
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert story.headline == "Working"
    assert story.detail.startswith("When it finishes: Review stopped: ")
    assert steps["review"] == ("waiting", "Waiting")


@pytest.mark.parametrize("state", ["running", "queued"])
def test_a_calm_reading_is_working_too_while_a_chat_runs(tmp_path, state):
    """#2590: no exceptions: a verification in progress is said in the detail as well."""
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]))
    check = RunVerification("job-1", work.run_id, state, False, ())
    work = _work(
        tmp_path, _record("In review"), reviewer=_reviewer([KEY]), verifications=(check,)
    )
    assert work_status(work).headline == "Verifying"

    story = work_status(_mid_turn(work, SIDE_CHAT))
    assert (story.headline, story.next) == ("Working", "")
    assert story.detail.startswith("When it finishes: Verifying: ")


def test_a_chat_mid_turn_on_a_work_with_no_run_is_working_not_not_started(tmp_path):
    work = _work(tmp_path, _record("In progress"), rows={})
    idle = replace(work, run_state="preflight")
    assert work_status(idle).headline == "Not started"

    story = work_status(_mid_turn(idle, SIDE_CHAT))
    assert (story.headline, story.summary) == ("Working", "Side chat is working.")


def test_a_failed_verification_of_a_clean_review_is_never_ready_for_merge(tmp_path):
    """#2352 review: a red suite is the Verify step's to report, not a merge to ask for."""
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]))
    check = RunVerification("job-1", work.run_id, "failed", False, ())
    work = _work(
        tmp_path, _record("In review"), reviewer=_reviewer([KEY]), verifications=(check,)
    )
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert (work.review.needs_human, work.review.decision) == (False, "")
    assert story.headline == "Verification failed"
    assert steps["verify"] == ("problem", "Failed")
    assert steps["merge"] == ("waiting", "Waiting")
    found = operator_attention(work, story)
    assert found.category == NEEDS_RETRY
    assert "merge" not in found.actions


def _red(run_id: str, head: str = HEAD) -> tuple[RunVerification, ...]:
    """The current Run's own failed suite, on ``head`` of this repository (#2460)."""
    tested = ((f"{ORG}/agentdeck", _snapshot(f"{ORG}/agentdeck", head)),)
    return (RunVerification("job-red", run_id, "failed", False, tested),)


def _red_work(tmp_path, head: str = HEAD, **kwargs):
    run_id = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY])).run_id
    return _work(
        tmp_path, _record("In review"), reviewer=_reviewer([KEY]),
        verifications=_red(run_id, head), **kwargs,
    )


def test_a_red_run_on_a_head_the_pull_request_moved_past_leaves_green_ci_a_merge(tmp_path):
    """issues#485 (#2460): a suite failed on the old head, the worker pushed the fix and Drone
    passed the new head. The card said Needs retry, Verification failed and "Look at the
    verification" beside "CI passed", and never offered the merge the engine waited on."""
    work = _red_work(tmp_path, OLDER, ci=_ci("green"))
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert work.verification.label == (
        f"Verification failed on {ORG}/agentdeck@{OLDER[:7]}; "
        f"the pull request's head is now {ORG}/agentdeck@{HEAD[:7]}"
    )
    assert (work.review.needs_human, work.review.decision) == (True, "merge")
    assert story.headline == "Ready for you to merge"
    assert steps["verify"] == ("done", "CI passed")
    assert steps["merge"] == ("attention", "Needs you")
    found = operator_attention(work, story)
    # Merged on GitHub: the deck no longer asks a Poller card for the merge (#3025).
    assert (found.category, found.actions) == (READY_FOR_MERGE, ("review", "hide"))


def test_a_red_run_on_an_old_head_is_not_a_failure_of_the_new_one(tmp_path):
    """#2460: with no CI, the new head simply has no green run yet: ask for one, not a retry."""
    work = _red_work(tmp_path, OLDER)
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert story.headline == "Verification not run"
    assert steps["verify"] == ("attention", "Not run; ask for it")
    found = operator_attention(work, story)
    assert (found.category, found.reason, found.actions) == (
        NEEDS_RETRY, f"Verification not run on {ORG}/agentdeck#7@{HEAD[:7]}",
        ("inspect", "hide"),
    )


def test_a_red_run_on_the_current_head_still_decides_a_job_verified_work(tmp_path):
    """#2352 stands: a red suite on the head that would merge is the Verify step's problem."""
    work = _red_work(tmp_path)
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert work.verification.superseded == ()
    assert work.verification.label == f"Verification failed on {ORG}/agentdeck@{HEAD[:7]}"
    assert (story.headline, story.you) == ("Verification failed", "Look at the verification")
    assert steps["verify"] == ("problem", "Failed")
    found = operator_attention(work, story)
    assert (found.category, found.actions) == (NEEDS_RETRY, ("inspect", "hide"))


@pytest.mark.parametrize(
    ("ci", "headline", "category", "reason"),
    [
        (_ci("green"), "Ready for you to merge", READY_FOR_MERGE,
         "Clean review at the current head; merge it on GitHub"),
        (_ci("failing"), "Verification failed", NEEDS_RETRY,
         f"CI failed on {ORG}/agentdeck#7@{HEAD[:7]}"),
        # CI has not answered for this head: the worker's red suite is all there is to go on.
        (_ci("none"), "Verification failed", NEEDS_RETRY,
         f"Verification failed on {ORG}/agentdeck@{HEAD[:7]}"),
    ],
    ids=["ci-green", "ci-failing", "ci-silent"],
)
def test_ci_that_read_the_head_outranks_a_workers_red_job(
    tmp_path, ci, headline, category, reason
):
    """#2460: in a ``github_checks`` repository the engine credits only CI (#2364), so a job a
    worker ran itself does not decide the card once CI has read the head."""
    work = _red_work(tmp_path, ci=ci)
    story = work_status(work)

    assert story.headline == headline
    found = operator_attention(work, story)
    assert (found.category, found.reason) == (category, reason)


def test_an_earlier_runs_failed_verification_with_no_green_run_is_not_a_merge(tmp_path):
    """#2364: nothing green covers the head, whichever Run's failure is on record."""
    check = RunVerification("job-1", "an-earlier-run", "failed", False, ())
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]), verifications=(check,))

    found = operator_attention(work, work_status(work))
    assert (found.category, found.actions) == (NEEDS_RETRY, ("inspect", "hide"))


def test_an_earlier_runs_green_run_of_the_same_head_leaves_the_merge_standing(tmp_path):
    """The engine credits a green run by the commit it tested, whoever submitted it."""
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]), verifications=GREEN)

    assert operator_attention(work, work_status(work)).category == READY_FOR_MERGE


@pytest.mark.parametrize(
    ("verifies_itself", "headline", "next_up", "attention", "verify"),
    [(True, "Verification due", "verification result", None,
      ("waiting", "Next, by AgentDeck")),
     (False, "Verification not run", "", NEEDS_RETRY, ("attention", "Not run; ask for it"))],
    ids=["engine-asks", "nobody-asks"],
)
def test_a_clean_review_no_green_run_covers_is_never_ready_for_merge(
    tmp_path, verifies_itself, headline, next_up, attention, verify
):
    """issues#469 (#2364): Review passed, Verify not run, and the card said Ready for merge.
    Where the engine asks for the run itself the Work waits on the deck; elsewhere it asks the
    person for a verification, never for a merge. The Verify step agrees with the headline
    and says who starts the run (#2360)."""
    work = _work(
        tmp_path, _record("In review"), reviewer=_reviewer([KEY]), verifies_itself=verifies_itself
    )
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert (work.review.next_expected, work.review.needs_human) == (next_up, False)
    assert story.headline == headline
    assert steps["verify"] == verify
    assert steps["merge"] == ("waiting", "Waiting")
    found = operator_attention(work, story)
    assert (found.category if found else None) == attention
    if found is not None:
        assert found.reason == f"Verification not run on {ORG}/agentdeck#7@{HEAD[:7]}"
        assert "merge" not in found.actions


@pytest.mark.parametrize(
    ("outcome", "headline", "verify"),
    [("ended", "Verification not run", ("attention", "Not run; ask for it")),
     ("running", "Verification due", ("waiting", "Next, by AgentDeck"))],
    ids=["turn-ended", "turn-running"],
)
def test_a_verify_turn_that_ended_with_no_green_run_is_a_persons_again(
    tmp_path, outcome, headline, verify
):
    """#2364: the engine asks once per head. Once it asked for this head and that turn is
    over with nothing green, the deck is no longer on it."""
    record = _record("In review", verify_asked={"agentdeck#7": HEAD})
    work = _work(
        tmp_path, record, reviewer=_reviewer([KEY]), verifies_itself=True, outcome=outcome
    )

    story = work_status(work)
    assert story.headline == headline
    assert {step.key: (step.state, step.note) for step in story.milestones}["verify"] == verify


def _bits(review: bool | None, **heads: str) -> dict:
    """The engine's published release-bits reading (#2121), keyed ``repo#n`` as it writes it."""
    bits = {"rebase-current": True, "tests-pass": False}
    if review is not None:
        bits["review-passed"] = review
    return {"release_bits": {"bits": bits, "heads": heads, "at": AT}}


@pytest.mark.parametrize(
    ("bits", "outcome", "headline", "attention"),
    [
        # issues#468: the pass for the shared pull request was posted on another card.
        (_bits(False, **{"agentdeck#7": HEAD}), "ended", "Verification not run", NEEDS_RETRY),
        # A reading taken before a push says nothing about the head now.
        (_bits(False, **{"agentdeck#7": OLDER}), "ended", "Verification due", None),
        (_bits(None, **{"agentdeck#7": HEAD}), "ended", "Verification due", None),
        ({}, "ended", "Verification due", None),
        # A worker still running may yet change what the engine reads.
        (_bits(False, **{"agentdeck#7": HEAD}), "running", "Verification due", None),
    ],
    ids=["red-at-head", "red-at-older-head", "bit-unknown", "no-reading", "worker-running"],
)
def test_a_red_review_bit_means_the_engine_will_not_verify(
    tmp_path, bits, outcome, headline, attention
):
    """#2405: the reviewer's clean verdict on a pull request is not the card's review bit.
    The engine arms a verify turn only on its own green bit, so a card whose bit reads red at
    the current heads is not waiting on the deck and must not sit in Working now."""
    work = _work(
        tmp_path, _record("In review", **bits), reviewer=_reviewer([KEY]),
        verifies_itself=True, outcome=outcome,
    )
    story = work_status(work)

    assert story.headline == headline
    found = operator_attention(work, story)
    assert (found.category if found else None) == attention
    assert front_row(work, current=True).section == (NEEDS_YOU if found else "active")


def test_ci_the_engine_never_reads_is_not_waited_on(tmp_path):
    """#2405, issues#472: the chat registered a pull request the engine does not link to the
    card, so the engine never reads its CI and "Waiting for CI" would wait forever."""
    record = _record("In review", **_bits(True, **{"other#9": HEAD}))
    work = _work(tmp_path, record, reviewer=_reviewer([KEY]), ci=())
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert [check.state for check in work.ci] == ["untracked"]
    assert steps["verify"] != ("waiting", "Waiting for CI")
    assert story.headline == "Verification not run"
    assert operator_attention(work, story).category == NEEDS_RETRY


def test_a_red_reading_about_other_pull_requests_says_nothing_about_this_one(tmp_path):
    """#2407 review: a red reading that names none of the open pull requests may be about one
    since merged, taken before the engine read the one open now; the engine may still verify."""
    record = _record("In review", **_bits(False, **{"other#9": HEAD}))
    work = _work(tmp_path, record, reviewer=_reviewer([KEY]), verifies_itself=True)

    assert work_status(work).headline == "Verification due"
    assert operator_attention(work, work_status(work)) is None


def test_ci_the_engine_links_but_has_not_read_is_still_waited_on(tmp_path):
    record = _record("In review", **_bits(True, **{"agentdeck#7": OLDER}))
    work = _work(tmp_path, record, reviewer=_reviewer([KEY]), ci=())

    assert [check.state for check in work.ci] == ["unread"]
    assert work_status(work).headline == "Verification due"


def test_the_engines_release_reading_reaches_the_card(tmp_path):
    card = _card(tmp_path, _record("In review", **_bits(False, **{"agentdeck#7": HEAD})))
    assert card.release_reading == ((PR, HEAD),)
    assert card.review_bit_red

    assert poller_ops._card_release_reading({}, ORG) == ((), False)
    assert poller_ops._card_release_reading(_bits(None, **{"agentdeck#7": HEAD}), ORG) == (
        ((PR, HEAD),), False,
    )
    # A reading with nothing it could name is no reading, whatever its bits say.
    assert poller_ops._card_release_reading(_bits(False, agentdeck=HEAD), ORG) == ((), False)
    assert poller_ops._card_release_reading({"release_bits": {"bits": [], "heads": {}}}, ORG) == (
        (), False,
    )


def _board_card(tmp_path, manifest_tail: str, record: dict) -> poller_ops.BoardCard:
    base = tmp_path / "instances" / "agentdeck"
    base.mkdir(parents=True)
    (base / "poller.toml").write_text(
        f'contract = 1\n\n[board]\norg = "{ORG}"\nproject_number = 1\n\n' + manifest_tail
    )
    store = StateStore(EnginePaths.from_base(base / "state.json"))
    with store.transaction() as engine:
        engine["issues"] = {"agentdeck#5": record}
    poller_ops.forget_durable_reads()
    card = poller_ops.read_board(tmp_path).card(WORK.repository, WORK.number)
    assert card is not None
    return card


def test_a_verdict_on_its_way_to_the_poller_is_nobodys_decision(tmp_path):
    """#2760: the stand-in reviewer posted "Release verdict sent to the poller" and closed its
    chat; until the poller's next wake recorded the pass, the Work asked for a person's review
    (37 of 45 short Needs review visits)."""
    from agentdeck.poller_engine import deck_releases
    from agentdeck.work_overview import REVIEW_PENDING

    base = tmp_path / "instances" / "agentdeck"
    base.mkdir(parents=True)
    sent = deck_releases.write(
        base / "state.json",
        request_id="verdict-0001",
        key="agentdeck#5",
        command=f"/reviewed pass {HEAD} {'a' * 64}",
        by="claude-reviewer",
        reviewer=True,
    )
    on_its_way = _work(tmp_path, _record("In review"), reviewer=_reviewer())

    assert on_its_way.review is not None and on_its_way.review.state == REVIEW_PENDING
    assert front_row(on_its_way, current=True).section != NEEDS_YOU

    sent.unlink()  # the wake took it: what the reviewer recorded is what counts now
    recorded = _work(tmp_path, _record("In review"), reviewer=_reviewer())
    assert front_row(recorded, current=True).section == NEEDS_YOU


def test_the_repositories_that_deploy_on_merge_reach_the_work_card(tmp_path):
    """#2760: what the engine watches a deploy of after it merges."""
    card = _board_card(
        tmp_path,
        '[[repos]]\nid = "agentdeck"\nholds_code = true\nauto_deploys = true\n\n'
        '[[repos]]\nid = "homr"\nholds_code = true\n',
        _record("In review"),
    )
    assert card.deploy_repos == (f"{ORG}/agentdeck",)


def test_the_engines_ci_reading_reaches_the_work_card(tmp_path):
    """#2364: the manifest names the CI repository, the engine's ``ci_checks`` the verdict."""
    card = _board_card(
        tmp_path,
        '[[repos]]\nid = "agentdeck"\nholds_code = true\nverification = "github_checks"\n',
        _record("In review", ci_checks={"agentdeck#7": {"head": HEAD, "state": "green"}}),
    )
    assert card.ci_repos == (f"{ORG}/agentdeck",)
    assert card.ci_checks == ((f"{ORG}/agentdeck", 7, HEAD, "green"),)
    assert not card.verifies_itself  # CI is read, but nothing asks a worker or merges

    card = replace(card, reviewer=_reviewer([KEY]))
    work = build_work_card(
        WORK, issue=None, title="Work", facts=work_facts(WORK, card=card, rows={PR.lower(): _row()})
    )
    assert operator_attention(work, work_status(work)).category == READY_FOR_MERGE


def test_the_review_only_repositories_reach_the_work_card(tmp_path):
    """#2832: the manifest names the repositories whose review is their verification."""
    card = _board_card(
        tmp_path,
        '[[repos]]\nid = "agentdeck"\nholds_code = true\nverification = "review_only"\n\n'
        '[[repos]]\nid = "homr"\nholds_code = true\nverification = "agentdeck_job"\n',
        _record("In review"),
    )
    assert card.review_only_repos == (f"{ORG}/agentdeck",)
    assert card.ci_repos == ()


@pytest.mark.parametrize(
    ("review_only", "headline", "verify", "category"),
    [
        (True, "Ready for you to merge", ("done", "Review is the check"), READY_FOR_MERGE),
        # The same card in a repository with tests still needs its run.
        (False, "Verification not run", ("attention", "Not run; ask for it"), NEEDS_RETRY),
    ],
    ids=["review-only", "agentdeck-job"],
)
def test_a_clean_review_is_the_whole_check_in_a_repository_with_no_tests(
    tmp_path, review_only, headline, verify, category
):
    """#2832: a notes repository's reviewed pull request is a merge, never a run to ask for."""
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]), review_only=review_only)
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert story.headline == headline
    assert steps["verify"] == verify
    assert operator_attention(work, story).category == category


def test_a_review_only_head_the_review_has_not_passed_is_not_covered(tmp_path):
    """The review covers only the head it read: an unreviewed head is not a merge yet."""
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer(), review_only=True)
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert steps["verify"] == ("waiting", "Review is the check")
    assert story.headline != "Verification not run"
    found = operator_attention(work, story)
    assert found is None or found.category != READY_FOR_MERGE


def test_auto_release_is_what_makes_the_engine_ask_for_verification(tmp_path):
    card = _board_card(tmp_path, "[capabilities]\nauto_release = true\n", _record("In review"))

    assert card.verifies_itself



def test_auto_release_is_what_makes_the_engine_merge_a_pass(tmp_path):
    assert not _card(tmp_path / "a", _record("In review")).releases_itself
    card = _board_card(tmp_path / "b", "[capabilities]\nauto_release = true\n", _record())
    assert card.releases_itself


def test_a_pass_on_a_project_that_merges_only_by_hand_asks_the_person_to_merge(tmp_path):
    """#2399: Trailhead's review passed and CI went green, and the Work said "waiting for
    release" and "Nothing needed" — but nothing there merges a pass except a person."""
    work = _work(tmp_path, _record("In review", **_passed()), verifications=GREEN)
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert story.headline == "Ready for you to merge"
    assert story.you == "Merge the pull request"
    assert story.summary == f"Review passed at {HEAD[:7]}; this project merges only by hand."
    assert steps["merge"] == ("attention", "Needs you")
    found = operator_attention(work, story)
    assert found.category == READY_FOR_MERGE
    assert found.actions == ("review", "hide")  # merged on GitHub (#3025)
    assert front_row(work, current=True).section == NEEDS_YOU


def test_a_pass_the_project_merges_itself_still_needs_nobody(tmp_path):
    work = _work(
        tmp_path, _record("In review", **_passed()), verifications=GREEN, releases_itself=True
    )
    story = work_status(work)

    assert (story.headline, story.next, story.you) == ("Review passed", "release", "Nothing needed")


@pytest.mark.parametrize(
    ("ci", "headline"),
    [("pending", "Verifying"), ("failing", "Verification failed")],
)
def test_a_hand_merged_pass_is_not_a_merge_until_its_ci_is_green(tmp_path, ci, headline):
    record = _record("In review", **_passed())
    work = _work(tmp_path, record, ci=[(f"{ORG}/agentdeck", 7, HEAD, ci)])
    story = work_status(work)

    assert story.headline == headline
    assert not work.review.needs_human
    assert story.kind != "merge_decision"


def test_a_hand_merged_pass_of_an_older_head_is_not_a_merge(tmp_path):
    work = _work(tmp_path, _record("In review", **_passed(OLDER)), verifications=GREEN)

    assert not work.review.needs_human
    assert work_status(work).headline != "Ready for you to merge"

def test_one_output_keeps_the_reviewers_own_label(tmp_path):
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer(stalled={KEY: UP_TO_DATE}))

    found = operator_attention(work, work_status(work))

    assert found.reason == work.review.label == f"Not merged: {UP_TO_DATE}"


def test_needs_you_is_grouped_by_category_in_the_front_pages_order(tmp_path):
    from agentdeck.work_story import front_sections

    review = _work(tmp_path / "a", _record("In review"))
    ready = _work(tmp_path / "b", _record("Ready to merge", **_passed()), verifications=GREEN)
    answer = _work(tmp_path / "c", _record("Needs More Info"))
    retry = _work(tmp_path / "d", _record("Needs More Info", revives=MAX_REVIVES))

    works = (review, answer, retry, ready)
    front = front_sections([front_row(work, current=True) for work in works])

    # In the order a Work moves through them (#3578), each at its place in the page's flow.
    assert [group.category for group in front.groups] == [
        NEEDS_ANSWER, NEEDS_RETRY, NEEDS_REVIEW, READY_FOR_MERGE
    ]
    assert [group.glyph for group in front.groups] == ["?", "↻", "◐", "↑"]
    assert [
        [group.category for group in front.groups_in(place)]
        for place in ("start", "stuck", "release", "cleanup")
    ] == [[NEEDS_ANSWER], [NEEDS_RETRY], [NEEDS_REVIEW, READY_FOR_MERGE], []]
    assert dict((key, count) for key, count, _ in front.summary) == {
        "merge": 1, "stuck": 1, "answer": 1, "review": 1
    }


def test_a_closed_issue_is_not_listed_as_recently_stopped(tmp_path):
    """Issue closed only waits for Finish: Needs you lists it, Recently stopped does not (#3388)."""
    from datetime import UTC, datetime

    from agentdeck.work_story import front_sections

    at = datetime(2026, 10, 22, 12, tzinfo=UTC)
    rows = [
        replace(front_row(_work(tmp_path / name, _record("In review")), current=True),
                stopped_at=at)
        for name in ("a", "b")
    ]
    closed = replace(rows[1], card=replace(rows[1].card, work_id="github_issue:owner/repo#2"),
                     attention=replace(rows[1].attention, category=ISSUE_CLOSED))

    front = front_sections([rows[0], closed])

    assert closed in front.needs_you and front.stopped == (rows[0],)


def test_each_needs_you_category_lists_its_most_recent_work_first(tmp_path):
    """Recency wins inside a category, over which part of the Work waits (#2267)."""
    from datetime import UTC, datetime, timedelta

    from agentdeck.work_story import front_sections

    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    review = _work(tmp_path / "a", _record("In review"))
    answer = _work(tmp_path / "b", _record("Needs More Info"))

    def at(work, number, hours):
        when = None if hours is None else now - timedelta(hours=hours)
        return replace(work, work_id=f"github_issue:owner/repo#{number}", last_activity=when)

    works = (
        at(review, 1, 30), at(review, 2, None), at(review, 3, 1), at(review, 4, 5),
        at(answer, 5, 48), at(answer, 6, 2),
    )
    front = front_sections([front_row(work, current=True) for work in works])

    assert [
        (group.category, [row.card.work_id.rpartition("#")[2] for row in group.rows])
        for group in front.groups
    ] == [(NEEDS_ANSWER, ["6", "5"]), (NEEDS_REVIEW, ["3", "4", "1", "2"])]


def test_a_parked_work_asks_its_workers_last_question(tmp_path):
    work = replace(_work(tmp_path, _record("Needs More Info")),
                   question="Should ZX2026 inherit the ZX2025 size groups?")

    found = operator_attention(work, work_status(work))

    assert (found.category, found.reason) == (
        NEEDS_ANSWER, "Should ZX2026 inherit the ZX2025 size groups?"
    )
    plain = operator_attention(_work(tmp_path / "x", _record("Needs More Info")), work_status(work))
    assert plain.reason == "The card is in Needs More Info"


def test_the_reason_is_the_engines_own_words_never_a_label(tmp_path):
    parked = _record(
        "Blocked", **_passed(),
        release_blocked={"why": "Auto-merge is disabled", "status": "Blocked",
                         "generation": "gen-2"},
    )
    work = _work(tmp_path, parked)

    found = operator_attention(work, work_status(work))

    assert found.reason == "Auto-merge is disabled"
    assert work.review.cause == "release_parked" and work.review.blocker == found.reason


# --- the pull requests' size -----------------------------------------------------------------


def _output(
    n: int, state: str = "open", *, base: str = "master", size=(10, 2, 1), new=(0, 0)
) -> OutputFact:
    additions, deletions, files = size
    return OutputFact(
        f"https://github.com/{ORG}/r{n}/pull/{n}", f"{ORG}/r{n}", n, "poller_card", state,
        base=base, additions=additions, deletions=deletions, files=files,
        new_lines=new[0], new_files=new[1],
    )


def test_the_size_sums_every_counted_output_across_repositories():
    outputs = (
        _output(1),
        _output(2, "merged", size=(5, 5, 3), new=(6, 2)),
        _output(3, "closed", size=(99, 99, 9), new=(None, None)),
    )

    # A pull request closed without merging is not part of what the Work changes.
    assert diffstat(outputs) == DiffStat(15, 7, 4, 6, 2)
    # One output's split unknown keeps the size but drops the split, never half of it (#3617).
    unsplit = diffstat((outputs[0], _output(4, new=(None, None))))
    assert unsplit == DiffStat(20, 4, 2) and not unsplit.split


@pytest.mark.parametrize(
    ("stat", "grade", "mostly_new"),
    [
        (DiffStat(150, 50, 2, 0, 0), "good", False),
        (DiffStat(151, 50, 2, 0, 0), "ok", False),
        (DiffStat(450, 150, 3, 0, 0), "ok", False),
        (DiffStat(451, 150, 3, 0, 0), "bad", False),
        # A large diff of new files stays green: only edited lines grade it.
        (DiffStat(3000, 50, 20, 3000, 18), "good", True),
        # Three quarters of the lines in new files is GitHub's [+]; just under is [•].
        (DiffStat(300, 100, 4, 300, 2), "good", True),
        (DiffStat(299, 101, 4, 299, 2), "good", False),
        # A rename alone changes no line: nothing new, so [•].
        (DiffStat(0, 0, 1, 0, 0), "good", False),
    ],
)
def test_the_diff_icon_is_graded_by_edited_lines_only(stat, grade, mostly_new):
    assert (stat.grade, stat.mostly_new) == (grade, mostly_new)


@pytest.mark.parametrize(
    "outputs",
    [
        (),
        (_output(1), _output(2, size=(None, None, None))),  # one row predates the fields
        (_output(1, "closed"),),
    ],
    ids=["no-output", "one-unknown", "only-closed"],
)
def test_an_incomplete_size_is_omitted_rather_than_shown_wrong(outputs):
    assert diffstat(outputs) is None


def test_the_target_is_named_only_when_it_is_not_master():
    assert target_branches((_output(1), _output(2, base=""))) == ()
    assert target_branches((_output(1), _output(2, base="release"))) == ("release",)
    assert target_branches((_output(1, "closed", base="old"),)) == ()


def test_the_size_comes_from_the_cached_row_and_is_on_the_card(tmp_path):
    rows = {PR.lower(): _row(baseRefName="release")}
    work = _work(tmp_path, _record("In review"), rows=rows)

    assert work.diffstat == DiffStat(40, 3, 2, 10, 1) and work.targets == ("release",)
    assert operator_attention(work, work_status(work)).target == "release"
    unsized = _work(tmp_path, _record("In review"), rows={PR.lower(): _row(sized=False)})
    assert unsized.diffstat is None


def test_tests_and_docs_are_left_out_of_the_size():
    assert source_size(_row()) == (40, 3, 2)
    # The template is added, so 10 of the 43 source lines are in a new file (#3617).
    assert new_source_size(_row()) == (10, 1)
    untyped = {"changedFiles": 1, "files": [{"path": "a.py", "additions": 1, "deletions": 0}]}
    assert source_size(untyped) == (1, 0, 1) and new_source_size(untyped) is None
    only_tests = {"changedFiles": 2, "files": list(_FILES[2:])}
    assert source_size(only_tests) == (0, 0, 0)
    # gh stops the list at 100: a sum over part of it would be wrong, so there is no size.
    long = {"changedFiles": 101, "files": [dict(_FILES[0])] * 100}
    assert source_size(long) is None
    assert source_size({"changedFiles": 1, "files": [{"path": "a.py", "additions": -1}]}) is None
    assert source_size({"additions": 40, "deletions": 3, "changedFiles": 2}) is None


def test_a_work_with_only_tests_and_docs_says_so(tmp_path):
    from agentdeck.web.work_view import WorkOverview, render_work_front
    from agentdeck.work_story import front_sections

    rows = {PR.lower(): _row(changedFiles=2, files=list(_FILES[2:]))}
    work = _work(tmp_path, _record("In review"), rows=rows)
    overview = WorkOverview(front=front_sections([front_row(work, current=True)]))

    assert work.diffstat == DiffStat(0, 0, 0, 0, 0)
    html = render_work_front(_templates(tmp_path), overview, "release")
    assert '<span class="fr-size" data-work-diffstat>no source change</span>' in html


def test_the_webhook_write_through_keeps_the_size_gh_pr_view_stores():
    from agentdeck.git_context import _PR_FIELDS

    assert {"additions", "deletions", "changedFiles", "files"} <= set(_PR_FIELDS.split(","))
    pull = {
        "number": 7, "title": "t", "html_url": PR, "state": "open", "updated_at": AT,
        "head": {"ref": "b", "sha": HEAD}, "base": {"ref": "master"},
        "additions": 40, "deletions": 3, "changed_files": 2,
    }
    value = WebhookStatusCacheService._pull_value(pull)
    assert (value["additions"], value["deletions"], value["changedFiles"]) == (40, 3, 2)
    # A payload without them leaves the row without, so the size reads as unknown.
    bare = WebhookStatusCacheService._pull_value({**pull, "additions": None, "changed_files": True})
    assert "additions" not in bare and "changedFiles" not in bare and bare["deletions"] == 3


def test_a_delivery_keeps_the_file_list_only_for_the_same_head_and_base():
    from agentdeck.webhook_status_cache import _keep_file_list

    held = _row()
    size = {"additions": 94, "deletions": 9, "changedFiles": 4}
    same = {"headRefOid": HEAD, "baseRefName": "master", **size}
    _keep_file_list(held, same)
    assert same["files"] == held["files"]
    for moved in ({"headRefOid": "b" * 40, "baseRefName": "master", **size},
                  {"headRefOid": HEAD, "baseRefName": "release", **size},
                  {"headRefOid": None, "baseRefName": "master", **size},
                  # The base branch moved under the same name and head: the totals say so.
                  {"headRefOid": HEAD, "baseRefName": "master", **size, "changedFiles": 5},
                  {"headRefOid": HEAD, "baseRefName": "master", **size, "additions": 90},
                  {"headRefOid": HEAD, "baseRefName": "master"}):
        _keep_file_list(held, moved)
        assert "files" not in moved
    fresh = {"headRefOid": HEAD, "baseRefName": "master"}
    _keep_file_list(None, fresh)
    _keep_file_list(_row(sized=False), fresh)
    assert "files" not in fresh


@pytest.mark.asyncio
async def test_the_backfill_rereads_unsized_rows_inside_the_same_budget():
    from agentdeck.web.work_view import WorkOverview, WorkOverviewService

    calls: list[tuple[str, list]] = []

    async def issues(refs, *, limit):
        calls.append(("issue", list(refs)))
        return len(refs)

    async def pulls(refs, *, limit):
        calls.append(("pull", list(refs)))
        return len(refs)

    # An open pull request's row no webhook rewrote within the hour is read again (#3217):
    # only #20's has aged, and #12 is both unsized and open, so it is read once.
    def stale(refs, older_than_s):
        assert older_than_s == WorkOverviewService.OPEN_ROW_STALE_S
        return [ref for ref in refs if ref[1] in (12, 20)]

    app = SimpleNamespace(state=SimpleNamespace(issues=SimpleNamespace(
        refresh_issue_records=issues, refresh_pull_rows=pulls, stale_pull_rows=stale)))
    service = WorkOverviewService(app)
    service.overview = WorkOverview(
        unresolved=((f"{ORG}/agentdeck", 1), (f"{ORG}/agentdeck", 2)),
        unsized=tuple((f"{ORG}/agentdeck", n, HEAD) for n in (10, 11, 12)),
        open_rows=tuple((f"{ORG}/agentdeck", n, HEAD) for n in (12, 20, 21)),
    )

    assert await service.backfill(0.0) == 4
    assert await service.backfill(30.0) == 0  # inside the minute's budget
    assert await service.backfill(61.0) == 2
    assert await service.backfill(122.0) == 0  # every one tried: nothing until the hour is up
    # A push is a new head, whose row lost its file list: that one is read again (#2331).
    service.overview = WorkOverview(unsized=((f"{ORG}/agentdeck", 10, "b" * 40),))
    assert await service.backfill(183.0) == 1

    assert calls == [
        ("issue", [(f"{ORG}/agentdeck", 1), (f"{ORG}/agentdeck", 2)]),
        ("pull", [(f"{ORG}/agentdeck", 10), (f"{ORG}/agentdeck", 11)]),
        ("pull", [(f"{ORG}/agentdeck", 12), (f"{ORG}/agentdeck", 20)]),
        ("pull", [(f"{ORG}/agentdeck", 10)]),
    ]
    assert WorkOverview(unsized=((ORG, 1),)) == WorkOverview()


def test_only_a_held_row_without_the_size_is_backfilled(tmp_path):
    from agentdeck.web.work_view import unsized_pulls

    held = _work(tmp_path, _record("In review"), rows={PR.lower(): _row(sized=False)})
    sized = _work(tmp_path, _record("In review"))
    never = _work(tmp_path, _record("In review"), rows={})
    # A list that disagrees with the file count is stale, and is read again; one gh cut at its
    # limit has no size either, but reading it again would only cut it short again.
    stale = _work(tmp_path, _record("In review"), rows={PR.lower(): _row(changedFiles=5)})
    cut = {PR.lower(): _row(changedFiles=101, files=[dict(_FILES[0])] * 100)}
    long = _work(tmp_path, _record("In review"), rows=cut)

    assert long.diffstat is None and stale.diffstat is None
    assert unsized_pulls([held, sized, never, held, long]) == ((f"{ORG}/agentdeck", 7, HEAD),)
    assert unsized_pulls([stale]) == ((f"{ORG}/agentdeck", 7, HEAD),)


def test_an_open_pull_request_row_ages_out_into_the_backfill(tmp_path):
    """A merge no webhook delivered left a finished Work's pull request open for good (#3217)."""
    from agentdeck.github_cache import GitHubMetadata, GitHubMetadataCache
    from agentdeck.issue_context import IssueResolver
    from agentdeck.web.work_view import open_pull_rows

    held = _work(tmp_path, _record("In review"))
    merged = _work(tmp_path, _record("In review"), rows={PR.lower(): _row(state="merged")})
    never = _work(tmp_path, _record("In review"), rows={})
    assert open_pull_rows([held, merged, never, held]) == ((f"{ORG}/agentdeck", 7, HEAD),)

    cache = GitHubMetadataCache(tmp_path / "cache.sqlite3")
    cache.offer(f"pr-ref:v1:{ORG}/agentdeck:7".lower(), GitHubMetadata(_row()))
    resolver = IssueResolver(poller_root=tmp_path, metadata_cache=cache)
    refs = [(f"{ORG}/AgentDeck", 7, HEAD), (f"{ORG}/agentdeck", 8, HEAD)]
    assert resolver.stale_pull_rows(refs, 3600.0) == []  # just written; #8 has no row
    assert resolver.stale_pull_rows(refs, 0.0) == [refs[0]]


# --- where it shows ----------------------------------------------------------------------------


def _templates(tmp_path):
    from test_web import _app_with_state

    return _app_with_state(tmp_path).state.templates


def test_the_needs_you_row_names_the_category_its_reason_and_the_size(tmp_path):
    from agentdeck.web.work_view import WorkOverview, render_work_front
    from agentdeck.work_story import front_sections

    parked = _record(
        "Blocked", **_passed(),
        release_blocked={"why": "Auto-merge is disabled", "status": "Blocked",
                         "generation": "gen-2"},
    )
    rows = {PR.lower(): _row(baseRefName="release")}
    work = _work(tmp_path, parked, rows=rows)
    overview = WorkOverview(front=front_sections([front_row(work, current=True)]))

    html = render_work_front(_templates(tmp_path), overview, "release")

    assert 'data-op-category="release_blocked"' in html
    assert 'data-op-actions="inspect hide"' in html
    # The category heads its group; the row gives the reason (#2254).
    assert 'data-front-group="release_blocked"' in html and ">Release blocked<" in html
    assert '<span class="fr-reason">Auto-merge is disabled</span>' in html
    # The technical headline stays on the Work page, not on the row.
    assert "Release needs your decision" not in html
    assert "+40/&minus;3 · 2 files (1 new)" in html and "into release" in html
    # GitHub's diff square leads the title: 33 lines in an edited file is a green [•] (#3617).
    assert re.search(
        r'<span class="fr-title"><span class="fr-diff fr-diff-good" role="img" '
        r'title="Diff: 10 lines in 1 new file, 33 lines in 1 edited file" '
        r'aria-label="Diff: 10 lines in 1 new file, 33 lines in 1 edited file"[^>]*'
        r'data-work-diff="good edited"><svg[^>]*>.*?<circle',
        html,
    )
    # Every action is on the Work page; the row only opens it.
    assert "data-front-action" not in html


def test_a_needs_review_row_opens_the_work_and_master_is_not_named(tmp_path):
    from agentdeck.web.work_view import WorkOverview, render_work_front
    from agentdeck.work_story import front_sections

    work = _work(tmp_path, _record("In review"))
    overview = WorkOverview(front=front_sections([front_row(work, current=True)]))

    html = render_work_front(_templates(tmp_path), overview, "release")

    assert 'data-front-group="needs_review"' in html and ">Needs review<" in html
    assert 'data-front-action' not in html and f'href="{PR}"' not in html
    assert "data-work-diffstat" in html and "data-work-target" not in html
    assert "data-work-staging" not in html

    # Staging already runs its head (#3180): the row says so; the links are on the page.
    from agentdeck.staging_deploys import StagingCopy

    staged = replace(work, staging=StagingCopy("s1", (("Store", "https://s1.example/"),)))
    overview = WorkOverview(front=front_sections([front_row(staged, current=True)]))
    html = render_work_front(_templates(tmp_path), overview, "release")
    assert (
        "<span class=\"fr-staging\" data-work-staging>ready to test</span>"
        " · <span class=\"fr-reason"
    ) in html
    assert "https://s1.example/" not in html


def test_the_work_page_shows_the_same_category_above_the_technical_story(tmp_path):
    from agentdeck.web.work_view import WorkDetail, render_work_sections

    work = _work(tmp_path, _record("Ready to merge", **_passed()), verifications=GREEN)
    detail = WorkDetail(card=work, trace=None, workspace=None, chats=(), verifications=())

    sections = dict(render_work_sections(_templates(tmp_path), detail))

    status = sections["status"]
    assert 'data-op-category="ready_for_merge"' in status
    assert 'data-op-actions="merge hide"' in status
    assert status.index("Ready for merge") < status.index("wd-status-head")
    # The technical story is still there, word for word.
    assert detail.status.headline in status
    assert "+40/−3 · 2 files" in sections["result"]
    assert "data-wd-staging" not in sections["result"]

    from agentdeck.staging_deploys import StagingCopy

    links = (("Store", "https://s1.example/"), ("Admin", "https://admin-s1.example/"))
    staged = replace(work, staging=StagingCopy("s1", links))
    detail = WorkDetail(card=staged, trace=None, workspace=None, chats=(), verifications=())
    result = dict(render_work_sections(_templates(tmp_path), detail))["result"]
    assert 'data-wd-staging="s1">Ready to test on staging: <a href="https://s1.example/"' in result
    assert '>Store</a> · <a href="https://admin-s1.example/"' in result

    # A summary written before the head's commit says so on its one dated line (#3199).
    from datetime import UTC, datetime, timedelta

    from agentdeck.work_resolution import WorkResolution

    pushed = datetime(2026, 9, 26, 13, 2, tzinfo=UTC)
    outputs = tuple(replace(output, head_at=pushed) for output in work.outputs)
    for written, behind in ((pushed - timedelta(hours=8), True), (pushed, False)):
        resolution = WorkResolution(WORK.work_id, "p", "s", "r", "c", updated_at=written)
        detail = WorkDetail(card=replace(work, outputs=outputs), trace=None, workspace=None,
                            chats=(), verifications=(), resolution=resolution)
        html = dict(render_work_sections(_templates(tmp_path), detail))["resolution"]
        assert ("written before the latest commit (<time" in html) is behind
        assert html.count("<p class=\"rt-note wd-resolution-when\">") == 1


def test_facts_mark_follows_the_facts_and_nothing_else(tmp_path):
    """#2332: a stop is dated by whether this moved, so a rename or a clock must not move it."""
    from datetime import UTC, datetime

    from agentdeck.work_stops import facts_mark

    work = _work(tmp_path, _record("In review"), reviewer=_reviewer())
    clean = _work(tmp_path / "b", _record("In review"), reviewer=_reviewer([KEY]))
    later = datetime(2026, 9, 28, tzinfo=UTC)

    assert facts_mark(work) == facts_mark(replace(work, last_activity=later, title="Renamed"))
    assert facts_mark(work) != facts_mark(clean)


# --- what each #2332 shape says, not only which category it lands in -------------------------


def test_a_merged_card_the_engine_is_deploying_says_deploying_whatever_the_review_said(tmp_path):
    """agentdeck#2327: merged by `/merge` after its review escalated, and it read Idle."""
    work = _work(
        tmp_path,
        _record("In review", merged=["agentdeck#7"], awaiting_deploy={"builds": {}, "since": AT}),
        rows={PR.lower(): _row("merged")},
    )
    story = work_status(work)
    steps = {step.key: step.note for step in story.milestones}

    assert (story.headline, story.summary, story.next) == (
        "Deploying", f"Merged {ORG}/agentdeck#7.", "deploy"
    )
    assert steps["merge"] == "Merged"
    assert work.last_activity is not None and work.last_activity.isoformat() == AT


def test_a_blocked_card_says_what_its_worker_said_blocks_it(tmp_path):
    note = {"generation": "gen-2", "text": "Blocked on external DNS; the fix is elsewhere."}
    work = _work(tmp_path, _record("Blocked", park_note=note))

    found = operator_attention(work, work_status(work))

    assert found.reason == "Blocked: external DNS"
    assert work_status(work).summary == "Blocked: external DNS."


def test_a_note_from_an_earlier_lease_is_not_this_cards(tmp_path):
    note = {"generation": "gen-1", "text": "Should it be limited to new orders?"}
    work = _work(tmp_path, _record("Needs More Info", park_note=note))

    assert work.question == ""
    assert operator_attention(work, work_status(work)).reason == "The card is in Needs More Info"


def test_a_parked_question_comes_from_the_workers_report(tmp_path):
    note = {
        "generation": "gen-2",
        "text": "Found two paths.\n\nShould it be limited to new orders?",
    }
    work = _work(tmp_path, _record("Needs More Info", park_note=note))

    assert operator_attention(work, work_status(work)).reason == (
        "Should it be limited to new orders?"
    )


def test_a_parked_card_whose_work_merged_says_so_and_keeps_its_question(tmp_path):
    note = {"generation": "gen-2", "text": "Narrow the branch, or rewrite it?"}
    work = _work(tmp_path, _record("Needs More Info", park_note=note),
                 rows={PR.lower(): _row("merged")})

    found = operator_attention(work, work_status(work))

    assert found.reason == f"{KEY}; asked: Narrow the branch, or rewrite it?"
    assert work.review.label == f"Merged {KEY}; the issue is still open"


def test_a_conflicting_head_names_the_pull_request_and_its_base(tmp_path):
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]), checks=DIRTY)
    story = work_status(work)

    assert operator_attention(work, story).reason == f"{KEY} conflicts with master"
    assert work.review.label == f"Reviewed clean at {HEAD[:7]}, but it conflicts with master"
    assert (story.headline, story.you) == ("Conflicts with its base", "Ask for a rebase")


def test_a_conflict_on_an_older_head_is_not_this_heads(tmp_path):
    older = {PR.lower(): {"head": OLDER, "mergeable": "CONFLICTING", "mergeState": "DIRTY"}}
    work = _work(
        tmp_path, _record("In review"), reviewer=_reviewer([KEY]), checks=older,
        verifications=GREEN,
    )

    assert operator_attention(work, work_status(work)).category == READY_FOR_MERGE


def test_a_head_sent_back_waits_for_its_fix_round_then_asks_for_one(tmp_path):
    fresh = _work(tmp_path / "a", _record("In review"),
                  reviewer=_reviewer(changes={KEY: time.time() - 60}))
    stale = _work(tmp_path / "b", _record("In review"),
                  reviewer=_reviewer(changes={KEY: AN_HOUR_AGO}))

    assert (fresh.review.state, fresh.review.label) == (
        "changes", f"Reviewer asked for changes at {HEAD[:7]}"
    )
    assert work_status(fresh).headline == "Fixing review findings"
    found = operator_attention(stale, work_status(stale))
    assert found.reason == f"Reviewer asked for changes at {HEAD[:7]}; no fix round started"
    assert work_status(stale).headline == "Review findings need a fix"


def test_a_release_parked_after_a_failed_deploy_says_it_is_being_rechecked(tmp_path):
    why = (
        "**Production deploy failed.** The merge is on the base branch but the deploy did not "
        "go green; a broken production deploy needs a human."
    )
    record = _record(
        "Ready to merge",
        merged=["agentdeck#7"],
        release_blocked={"why": why, "status": "Ready to merge", "generation": "gen-2"},
        deploy_recheck={"builds": {}, "from": AT},
    )
    work = _work(tmp_path, record, rows={})

    found = operator_attention(work, work_status(work))

    assert (found.category, found.actions) == (RELEASE_BLOCKED, ("close", "inspect", "hide"))
    assert found.reason == f"{why}; AgentDeck is re-checking the deploy"


# --- CI as the verification (#2364) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("ci", "headline", "verify", "category"),
    [
        (_ci("pending"), "Verifying", ("active", "CI running"), None),
        (_ci("green"), "Ready for you to merge", ("done", "CI passed"), READY_FOR_MERGE),
        (_ci("failing"), "Verification failed", ("problem", "CI failed"), NEEDS_RETRY),
        # No check reported is never green.
        (_ci("none"), "Verification due", ("waiting", "Waiting for CI"), None),
        # Green on the head before a push says nothing about the head now.
        (_ci("green", OLDER), "Verification due", ("waiting", "Waiting for CI"), None),
        # Nothing read yet for this head.
        ((), "Verification due", ("waiting", "Waiting for CI"), None),
    ],
    ids=["pending", "green", "failing", "no-checks", "head-moved", "unread"],
)
def test_ci_on_the_exact_head_is_what_verifies_a_ci_repository(
    tmp_path, ci, headline, verify, category
):
    """Trailhead: a clean review is Ready for merge only once Drone is green on that head."""
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]), ci=ci)
    story = work_status(work)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert story.headline == headline
    assert steps["verify"] == verify
    found = operator_attention(work, story)
    assert (found.category if found else None) == category
    if category != READY_FOR_MERGE:
        assert steps["merge"] == ("waiting", "Waiting")
        assert front_row(work, current=True).section == (NEEDS_YOU if found else "active")


def test_red_ci_is_the_workers_repair(tmp_path):
    work = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY]), ci=_ci("failing"))

    found = operator_attention(work, work_status(work))
    assert found.reason == f"CI failed on {ORG}/agentdeck#7@{HEAD[:7]}"
    assert found.actions == ("inspect", "hide")


def test_a_green_job_does_not_stand_in_for_ci(tmp_path):
    """A repository whose CI is its verification is not verified by a worker's job."""
    work = _work(
        tmp_path, _record("In review"), reviewer=_reviewer([KEY]), ci=_ci("none"),
        verifications=GREEN,
    )

    assert operator_attention(work, work_status(work)) is None
    assert work_status(work).headline == "Verification due"


def test_a_card_carrying_on_after_a_merged_step_is_not_asked_to_close(tmp_path):
    """#2730: the engine already queued the worker's next turn; nothing waits on a person."""
    record = _record("In review", steps_merged=["agentdeck#7"], pending=["engine:release-step"])
    work = _work(tmp_path, record, rows={PR.lower(): _row("merged")})

    assert work.review.label == "Step merged; the worker carries on with the next part"
    assert work.review.cause != "merged_open"
    assert operator_attention(work, work_status(work)) is None


def test_a_merged_completion_beside_a_step_still_asks_to_close(tmp_path):
    record = _record("In review", steps_merged=["other#3"])
    work = _work(tmp_path, record, rows={PR.lower(): _row("merged")})

    assert work.review.label == f"Merged {KEY}, {KEY2}; the issue is still open"
    assert work.review.cause == "merged_open"
