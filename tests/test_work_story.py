"""The Work page tells the Work's story at the top and takes a message for it (#2218).

The status, the milestones and the message target are readings of facts the Work card already
has; nothing here is stored. Automated review and an automatic fix round are calm progress;
only a person's dependency is attention, and it comes with a direct way to answer.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from public_copy import private_feature

from agentdeck import chat_board as _chat_board
from agentdeck.models import Capability, IssueRef, Session, SessionStatus
from agentdeck.poller_ops import BoardCard, BoardDispatch, BoardScope, BoardView
from agentdeck.run_trace import NEEDS_ATTENTION, RUNNING, RunSession, RunView
from agentdeck.triage import CAUSE_PENDING_INTERACTION, AssistantInsight, AssistantView
from agentdeck.work import WorkRef
from agentdeck.work_facts import FOUND_BY_CARD, OutputFact, work_facts
from agentdeck.work_overview import (
    VerificationSummary,
    WorkAttention,
    WorkChat,
    WorkspaceLine,
    build_work_card,
    work_href,
)
from agentdeck.work_story import (
    ACTIVE,
    ATTENTION,
    CHAT_ACTION,
    CHAT_QUESTION,
    CHAT_STOPPED,
    CLARIFICATION,
    DONE,
    PROBLEM,
    RELEASE_DECISION,
    REVIEW_DECISION,
    message_target,
    work_status,
    workspace_rows,
)
from agentdeck.work_workspace import (
    CwdObservation,
    PathObservation,
    RepositoryRole,
    WorkRepositoryContext,
    WorkspaceEvidence,
    WorkspaceIssue,
    WorkspaceIssueKind,
    WorkspaceProvenance,
    WorkWorkspace,
)

WORK = WorkRef("eerovil/agentdeck", 2218)
PR = "https://github.com/eerovil/agentdeck/pull/2300"
NOW = datetime.now(UTC)


def _board_card(**fields) -> BoardCard:
    fields.setdefault("status", "In progress")
    fields.setdefault("ownership", True)
    fields.setdefault("pull_request_url", PR)
    return BoardCard("eerovil/agentdeck", WORK.number, **fields)


def _trace(run_id: str, state: str, next_expected: str = "", sessions=()):
    run = RunView(
        run_id=run_id,
        state=state,
        next_expected=next_expected,
        current=True,
        sessions=tuple(sessions),
    )
    return SimpleNamespace(current=run, preflight="", preflight_next="", unreadable=())


def _work(card=None, *, run: str = "", state: str = "", next_expected: str = "", **fields):
    trace = _trace(run or "gen-1", state, next_expected) if state else None
    return build_work_card(
        WORK, issue=None, title="Work story", facts=work_facts(WORK, card=card), trace=trace,
        **fields,
    )


def _verified(run_id: str = "gen-1") -> VerificationSummary:
    return VerificationSummary(
        "verified",
        "Verified agentdeck@abc1234",
        "job-1",
        run_id,
        (("eerovil/agentdeck", "abc1234"),),
    )


def test_a_pass_on_a_commit_the_pull_request_moved_past_names_that_commit():
    """#2327: still a pass, but on the commit it tested, not on what would merge."""
    from dataclasses import replace

    work = _work(_board_card(generation="gen-1"))
    moved = replace(_verified(), superseded=(("eerovil/agentdeck", "def5678"),))

    assert _steps(work_status(replace(work, verification=moved)))["verify"] == (
        "done", "Passed at abc1234"
    )
    assert _steps(work_status(replace(work, verification=_verified())))["verify"] == (
        "done", "Passed"
    )


def _steps(status) -> dict[str, tuple[str, str]]:
    return {step.key: (step.state, step.note) for step in status.milestones}


# --- the required states --------------------------------------------------------------------


def test_implementation_active_is_calm_progress():
    status = work_status(
        _work(_board_card(generation="gen-1"), state=RUNNING, next_expected="worker activity")
    )

    assert (status.tone, status.headline) == (ACTIVE, "Implementing")
    assert status.next == "worker activity" and status.you == "Nothing needed"
    assert _steps(status)["implement"] == ("active", "Working")
    assert _steps(status)["review"] == ("idle", "Not yet")


def test_automated_review_running_is_progress_not_attention():
    card = _board_card(review_mode="merge-review", generation="gen-review")
    work = _work(card, run="gen-review", state=RUNNING, next_expected="worker activity")
    status = work_status(work)

    assert (status.tone, status.headline) == (ACTIVE, "Reviewing")
    assert status.summary == "Automated review in progress."
    assert status.detail == "Implementation is done."
    assert status.next == "review result" and status.you == "Nothing needed"
    assert "decision" not in status.summary + status.detail + status.next
    assert _steps(status)["implement"] == ("done", "Done")
    assert _steps(status)["review"] == ("active", "Running")


def test_review_findings_the_worker_is_fixing_read_as_fixing_not_attention():
    card = _board_card(review_offer="rejected")
    status = work_status(_work(card))

    assert (status.tone, status.headline) == (ACTIVE, "Fixing review findings")
    assert "the worker is fixing them" in status.summary
    assert status.detail == "AgentDeck starts the fix Run itself."
    assert status.you == "Nothing needed"
    assert _steps(status) == {
        "implement": ("active", "Fixing"),
        "verify": ("waiting", "After review"),
        "review": ("waiting", "Changes asked"),
        "merge": ("waiting", "Waiting"),
    }


# --- the final suite runs after review (#2253) ------------------------------------------------


def _with(work, *, review_state: str = "", verification: str = ""):
    """The same Work card, with its review and its newest verification set by hand."""
    from dataclasses import replace

    from agentdeck.work_overview import ReviewSummary

    fields = {}
    if review_state:
        fields["review"] = ReviewSummary(review_state, f"Review {review_state}", "release")
    if verification:
        fields["verification"] = VerificationSummary(
            verification, f"Verification {verification}", "job-1", "gen-1",
            (("eerovil/agentdeck", "abc1234"),),
        )
    return replace(work, **fields)


def test_the_milestones_read_implement_review_verify_merge():
    status = work_status(_work(_board_card(generation="gen-1"), state=RUNNING))

    assert [step.key for step in status.milestones] == [
        "implement", "review", "verify", "merge",
    ]
    assert [step.label for step in status.milestones] == [
        "Implement", "Review", "Verify", "Merge",
    ]


@pytest.mark.parametrize("running", ["running", "queued"])
def test_the_final_run_after_a_passed_review_reads_verifying(running):
    work = _with(
        _work(_board_card(status="In review", in_review=True, status_role="in_review")),
        review_state="passed", verification=running,
    )
    status = work_status(work)

    assert (status.tone, status.headline) == (ACTIVE, "Verifying")
    assert status.summary == f"Verification {running}."
    assert _steps(status)["review"] == ("done", "Passed")
    assert _steps(status)["verify"] == ("active", running.capitalize())


@pytest.mark.parametrize("review_state", ["", "pending", "running", "changes"])
def test_a_stale_run_while_review_is_open_is_waiting_not_a_problem(review_state):
    """The suite is due on the head a review passes, so an older run is simply not due yet."""
    card = _board_card(status="In review", in_review=True, status_role="in_review")
    status = work_status(_with(_work(card), review_state=review_state, verification="stale"))

    assert _steps(status)["verify"] == ("waiting", "After review")


def test_a_stale_run_after_a_passed_review_is_still_a_problem():
    card = _board_card(status="In review", in_review=True, status_role="in_review")
    status = work_status(_with(_work(card), review_state="passed", verification="stale"))

    assert _steps(status)["verify"] == ("problem", "Stale")


def _ask(key: str = "claude_code:test:worker", title: str = "Worker chat", **fields):
    fields.setdefault("asks", True)
    return WorkAttention(key, title, "waiting", "/chats/w", **fields)


def _stopped(
    key: str = "claude_code:test:worker", title: str = "Worker chat", state: str = "waiting"
):
    """A chat that stopped without asking anything (stalled, died, reported waiting)."""
    return WorkAttention(key, title, state, "/chats/w")


def _approval(key: str = "c:a:approver", title: str = "Approver", kind: str = "permission"):
    return WorkAttention(key, title, "waiting", "/chats/a", question="Allow rm?", interaction=kind)


def _human_review() -> BoardCard:
    return _board_card(status="In review", in_review=True, status_role="human_review_pending")


def test_a_chats_question_is_the_one_strong_attention_state():
    ask = _ask(question="Should the   migration\nrun first?")
    status = work_status(_work(_board_card(generation="gen-1"), state=RUNNING, attention=(ask,)))

    assert (status.tone, status.headline) == (ATTENTION, "Needs your answer")
    assert status.summary == "Worker chat asked: Should the migration run first?"
    assert (status.next, status.you) == ("your answer", "Answer the question")
    assert (status.kind, status.where, status.where_href) == (
        CHAT_QUESTION,
        "Worker chat",
        "/chats/w",
    )


def test_a_long_question_is_shortened_and_a_textless_one_still_asks():
    status = work_status(_work(_board_card(), attention=(_ask(question="why " * 80),)))
    assert status.summary.endswith("…") and len(status.summary) < 200
    status = work_status(_work(_board_card(), attention=(_ask(),)))
    assert status.summary == "Worker chat is waiting for your answer."


def test_a_review_parked_for_a_person_is_a_review_decision_not_a_question():
    """#2207: In review, escalated to the operator, and no chat asked anything."""
    # The worker's turn ended in a handoff: that is no dependency of its own (#2219).
    status = work_status(_work(_human_review(), attention=(_stopped(state="finished"),)))

    assert (status.tone, status.headline) == (ATTENTION, "Review needs your decision")
    assert status.kind == REVIEW_DECISION
    assert (status.next, status.you) == ("review decision", "Decide review")
    assert (status.where, status.where_href) == ("PR eerovil/agentdeck#2300", PR)
    words = " ".join((status.headline, status.summary, status.detail, status.next, status.you))
    assert "answer" not in words.lower() and "question" not in words.lower()
    assert _steps(status)["review"] == ("attention", "Needs you")


def test_mixed_attention_keeps_every_kind_in_its_own_words():
    """A question, an approval, a stopped chat and a review at once (#2225 review)."""
    attention = (_stopped("c:a:other", "Other"), _approval(), _ask())
    status = work_status(_work(_human_review(), attention=attention))

    # The question leads, deterministically; the others are follow-ups, never "more questions".
    assert (status.headline, status.kind, status.where) == (
        "Needs your answer",
        CHAT_QUESTION,
        "Worker chat",
    )
    assert status.summary == "Worker chat is waiting for your answer."
    assert status.detail == (
        "Also waiting on its own page: Approver. Also stopped: Other. "
        "Review needs your decision too: Waiting for a human review."
    )
    assert status.you == "Answer the question, then decide review"


def test_only_other_questions_count_as_more_questions():
    two = (_ask(), _stopped("c:a:other", "Other"), _ask("c:a:second", "Second"))
    status = work_status(_work(_board_card(), attention=two))

    assert status.summary == "Worker chat (and 1 more question) is waiting for your answer."
    assert status.detail == "Also stopped: Other."


def test_an_approval_leads_when_nothing_asks_and_names_the_rest():
    attention = (
        _stopped("c:a:other", "Other"),
        _approval(),
        _approval("c:a:b", "Browser", "mcp_url"),
    )
    status = work_status(_work(_board_card(), attention=attention))

    assert (status.headline, status.kind) == ("Chat is waiting on you", CHAT_ACTION)
    assert status.summary == "Approver is waiting on: Allow rm?"
    assert status.detail == "Also waiting on its own page: Browser. Also stopped: Other."
    assert status.you == "Approve or decline on the chat's page"
    assert "answer" not in (status.headline + status.summary + status.you).lower()


def test_a_review_decision_still_names_a_stopped_chat():
    status = work_status(_work(_human_review(), attention=(_stopped("c:a:other", "Other"),)))

    assert status.kind == REVIEW_DECISION and status.detail == "Also stopped: Other."


def test_a_finished_handoff_alone_needs_nobody():
    status = work_status(_work(_board_card(), attention=(_stopped(state="finished"),)))
    assert status.tone != ATTENTION and status.kind == ""


def test_a_needs_you_wake_on_tells_the_question_and_knows_the_work_s_own_chats():
    """#3446: one waiting question leaves the row's words empty; the wake still quotes it,
    and every leg of the Work's chats maps to the durable chat a registration is keyed by."""
    from types import SimpleNamespace

    from agentdeck.web.work_view import needs_you_stops
    from agentdeck.work_story import FrontSections, front_row

    ask = _ask("claude_code:alt:leg2", question="Which colour?")
    row = front_row(_work(_board_card(), attention=(ask,)), current=True)
    assert row.reason == "" and row.attention.label == "Needs answer"
    overview = SimpleNamespace(front=FrontSections(needs_you=(row,)))
    [stop] = needs_you_stops(overview, identity=lambda key: f"chat-of:{key.rsplit(':', 1)[-1]}")
    assert stop.reason == "Which colour?" and stop.issue == row.card.source_label
    assert stop.chats == ("chat-of:leg2",) and stop.stopped_at == 0.0


def test_the_front_page_answers_only_a_real_question():
    from agentdeck.work_story import NEEDS_YOU, front_row

    question = front_row(_work(_board_card(), attention=(_ask(),)), current=True)
    assert (question.section, question.action) == (NEEDS_YOU, "Answer")
    for attention, reason in (
        ((_approval(),), "Waiting on a chat's own page"),
        ((_stopped(),), "A chat stopped"),
    ):
        row = front_row(_work(_board_card(), attention=attention), current=True)
        assert (row.section, row.action, row.reason) == (NEEDS_YOU, "Open", reason)
        assert "Worker chat" not in row.reason and "Approver" not in row.reason


def test_a_blocked_release_is_a_release_decision():
    status = work_status(_work(_board_card(release_blocked="checks failed")))

    assert (status.headline, status.kind) == ("Release needs your decision", RELEASE_DECISION)
    assert (status.next, status.you) == ("release decision", "Decide the release")
    assert status.summary == "Release blocked: checks failed."


def test_a_card_parked_in_needs_more_info_asks_for_clarification_on_its_issue():
    card = _board_card(status="Needs More Info", status_role="needs_more_info")
    work = _work(card)
    status = work_status(work)

    assert (status.headline, status.kind) == ("Work needs clarification", CLARIFICATION)
    assert (status.where, status.where_href) == (work.source_label, work.source_url)
    assert "answer" not in (status.headline + status.you).lower()


def test_a_plain_needs_more_info_card_needs_clarification_without_any_review():
    """A worker that finished straight into Needs More Info: no review (#2225)."""
    card = _board_card(status="Needs More Info", status_role="needs_more_info")
    work = _work(card)
    assert work.review is None and work.needs_attention
    status = work_status(work)

    assert (status.tone, status.headline, status.kind) == (
        ATTENTION,
        "Work needs clarification",
        CLARIFICATION,
    )
    assert status.summary == (
        "The card is in Needs More Info: the worker needs more information to go on."
    )
    assert (status.next, status.you) == ("your clarification", "Clarify the Work")
    assert (status.where, status.where_href) == (work.source_label, work.source_url)
    assert "answer" not in (status.headline + status.summary + status.you).lower()


def test_a_needs_more_info_card_with_a_question_keeps_both_in_view():
    card = _board_card(status="Needs More Info", status_role="needs_more_info")
    status = work_status(_work(card, attention=(_ask(question="Which branch?"),)))

    assert (status.headline, status.kind) == ("Needs your answer", CHAT_QUESTION)
    assert status.detail == "Work needs clarification too: the card is in Needs More Info."
    assert status.you == "Answer the question, then clarify the Work"


def test_a_plain_blocked_or_done_card_is_not_a_clarification():
    blocked = work_status(_work(_board_card(status="Blocked", status_role="blocked")))
    assert blocked.kind != CLARIFICATION
    done = _work(_board_card(status="Done", status_role="needs_more_info", done=True))
    assert done.parked == "" and work_status(done).kind != CLARIFICATION


def test_a_needs_more_info_left_from_the_spec_does_not_hold_a_verifying_work():
    """#2777, from eerovil/score-plugins#376: the board still said Needs More Info
    from the spec while the build Run's own suite was queued, and the Work sat in Needs you."""
    from agentdeck.work_story import NEEDS_YOU, front_row
    from agentdeck.work_workspace import RunVerification

    def work(state: str, *, run_id: str = "gen-1", **fields):
        check = RunVerification("job-1", run_id, state, False, ())
        card = _board_card(status="Needs More Info", status_role="needs_more_info", **fields)
        return _work(card, state="waiting", verifications=(check,))

    for state in ("queued", "running"):
        for fields in ({}, {"auto_release": True}):
            verifying = work(state, **fields)
            assert verifying.parked == "" and verifying.review is None, (state, fields)
            status = work_status(verifying)
            assert (status.tone, status.headline) == (ACTIVE, "Verifying"), (state, fields)
            assert front_row(verifying, current=True).section != NEEDS_YOU
    # A suite that finished, or one an earlier Run queued, leaves the question standing.
    for held in (work("failed"), work("cancelled"), work("queued", run_id="gen-0")):
        assert work_status(held).kind == CLARIFICATION
        assert front_row(held, current=True).section == NEEDS_YOU
    # Only Needs More Info: a card a worker blocked still waits on a person.
    check = RunVerification("job-1", "gen-1", "running", False, ())
    blocked = _work(
        _board_card(status="Blocked", status_role="blocked"),
        state="waiting",
        verifications=(check,),
    )
    assert blocked.parked == "blocked"
    assert front_row(blocked, current=True).section == NEEDS_YOU


def test_a_chat_that_only_stopped_is_a_look_not_a_question():
    status = work_status(_work(_board_card(generation="gen-1"), attention=(_stopped(),)))

    assert (status.tone, status.headline, status.kind) == (
        ATTENTION,
        "Chat needs a look",
        CHAT_STOPPED,
    )
    assert status.summary == "Worker chat stopped; it has not asked you anything."
    assert status.detail == "Deckhand says: waiting."
    assert status.you == "Look at the chat" and status.where_href == "/chats/w"


def test_a_done_work_is_settled_and_names_what_merged():
    card = _board_card(status="Done", done=True)
    work = _work(card)
    merged = OutputFact(PR, "eerovil/agentdeck", 2300, FOUND_BY_CARD, "merged")
    work = work.__class__(**{**work.__dict__, "outputs": (merged,)})
    status = work_status(work)

    assert (status.tone, status.headline, status.next) == (DONE, "Done", "")
    assert status.detail == "Merged eerovil/agentdeck#2300."
    assert _steps(status)["merge"] == ("done", "Merged")


def test_a_passed_review_reads_as_merging_until_its_pull_request_merges():
    card = _board_card(review_mode="merge", auto_release=True)
    status = work_status(_work(card))

    assert (status.tone, status.headline, status.next) == (ACTIVE, "Merging", "merge")
    assert _steps(status)["merge"] == ("active", "Merging")

    # Once merged, the card is still open only for the deploy (#2316).
    merged = OutputFact(PR, "eerovil/agentdeck", 2300, FOUND_BY_CARD, "merged")
    work = _work(card)
    status = work_status(work.__class__(**{**work.__dict__, "outputs": (merged,)}))

    assert (status.tone, status.headline, status.next) == (ACTIVE, "Deploying", "deploy")
    assert status.summary == "Merged eerovil/agentdeck#2300."
    assert _steps(status)["merge"] == ("done", "Merged")

    # A second pull request still open means the merge step has not finished.
    other = OutputFact(PR.replace("2300", "2301"), "eerovil/agentdeck", 2301, FOUND_BY_CARD, "open")
    status = work_status(work.__class__(**{**work.__dict__, "outputs": (merged, other)}))

    assert status.headline == "Merging"
    assert _steps(status)["merge"] == ("active", "Merging")


def test_a_stuck_run_is_a_problem_not_a_question():
    status = work_status(_work(_board_card(generation="gen-1"), state=NEEDS_ATTENTION))

    assert status.tone == PROBLEM and status.headline == "Run needs a look"
    assert _steps(status)["implement"][0] == "problem"


def test_every_state_carries_a_text_mark_not_just_a_colour():
    status = work_status(_work(_board_card(generation="gen-1"), state=RUNNING))
    assert status.mark and all(step.mark for step in status.milestones)


# --- workspace in a line per repository -----------------------------------------------------


def _cwd(dirty: bool | None) -> CwdObservation:
    return CwdObservation(
        session_key="claude_code:test:w",
        chat_id="claude_code:test:w",
        cwd="/w",
        observation=PathObservation("/w", exists=True),
        provenance=WorkspaceProvenance(WorkspaceEvidence.CHAT_SESSION),
        dirty=dirty,
    )


def test_a_multi_repository_workspace_is_one_compact_row_each():
    workspace = WorkWorkspace(
        work_id=WORK.work_id,
        repositories=(
            WorkRepositoryContext(
                "eerovil/agentdeck",
                (RepositoryRole.SOURCE,),
                branches=("agent/issue-2218",),
                cwds=(_cwd(False),),
            ),
            WorkRepositoryContext(
                "eerovil/trailhead",
                (RepositoryRole.SOURCE,),
                branches=("feature/foo",),
                cwds=(_cwd(True),),
            ),
            WorkRepositoryContext(
                "acmecorp/trailhead",
                (RepositoryRole.BORROWED,),
                issues=(WorkspaceIssue(WorkspaceIssueKind.REMOTE_MISMATCH, "gone"),),
            ),
        ),
    )
    rows = [(row.name, row.branch, row.state, row.problem) for row in workspace_rows(workspace)]

    assert rows == [
        ("agentdeck", "agent/issue-2218", "clean", False),
        # Two repositories named "trailhead" keep their owners, so neither is taken for the other.
        ("eerovil/trailhead", "feature/foo", "dirty", False),
        ("acmecorp/trailhead", "", "1 problem", True),
    ]


def test_repository_names_are_compared_case_folded_and_once_each():
    rows = workspace_rows(
        None,
        (
            WorkspaceLine("Eerovil/Trailhead", "a", "claimed"),
            WorkspaceLine("acmecorp/trailhead", "b", "claimed"),
            WorkspaceLine("eerovil/agentdeck", "c", "claimed"),
            WorkspaceLine("eerovil/agentdeck", "d", "released"),
        ),
    )
    assert [row.name for row in rows] == [
        "Eerovil/Trailhead",
        "acmecorp/trailhead",
        "agentdeck",
        "agentdeck",
    ]


def test_without_an_inspected_workspace_the_rows_are_the_claims():
    rows = workspace_rows(None, (WorkspaceLine("eerovil/agentdeck", "agent/x", "claimed"),))
    assert [(row.name, row.branch, row.state) for row in rows] == [
        ("agentdeck", "agent/x", "claimed")
    ]


# --- where a message goes -------------------------------------------------------------------


def _chat(key: str, title: str, *, working=False, minutes=5) -> WorkChat:
    return WorkChat(
        key,
        key,
        title,
        f"/chats/{title}",
        working,
        NOW - timedelta(minutes=minutes),
        (),
        legs=(key,),
    )


def test_a_question_owns_the_message_and_asks_for_an_answer():
    asker, runner = _chat("c:a:asker", "Asker"), _chat("c:a:runner", "Runner", working=True)
    ask = WorkAttention(asker.session_key, "Asker", "waiting", asker.href, asks=True)
    card = _work(_board_card(), attention=(ask,))
    trace = _trace(
        "gen-1",
        RUNNING,
        sessions=(RunSession("c:a:runner", "c:a:runner", "claude_code", "a", None),),
    )

    target = message_target(card, trace, (asker, runner), lambda key: True)

    assert target is not None and (target.session_key, target.answer) == ("c:a:asker", True)


def test_a_review_decision_never_turns_a_message_into_an_answer():
    stopped, asker = _chat("c:a:stopped", "Stopped"), _chat("c:a:asker", "Asker", minutes=30)
    card = _work(
        _human_review(),
        attention=(WorkAttention("c:a:stopped", "Stopped", "stalled", stopped.href),),
    )

    target = message_target(card, None, (stopped, asker), lambda key: True)
    assert target is not None and (target.session_key, target.answer) == ("c:a:stopped", False)
    assert target.why == "it stopped and is waiting for a look"

    # With a real question as well, the asking chat takes it, as an answer.
    both = _work(
        _human_review(),
        attention=(
            WorkAttention("c:a:stopped", "Stopped", "stalled", stopped.href),
            WorkAttention("c:a:asker", "Asker", "waiting", asker.href, asks=True),
        ),
    )
    target = message_target(both, None, (stopped, asker), lambda key: True)
    assert target is not None and (target.session_key, target.answer) == ("c:a:asker", True)


@pytest.mark.parametrize("state", ["finished", "merged", "closed"])
def test_a_chat_that_handed_off_is_not_said_to_have_stopped(state):
    """abacus#1951 (#2327): a finished turn is a handoff, as the status already reads it."""
    chat = _chat("c:a:done", "Done")
    card = _work(
        _human_review(), attention=(WorkAttention("c:a:done", "Done", state, chat.href),)
    )

    target = message_target(card, None, (chat,), lambda key: True)

    assert target is not None and target.session_key == "c:a:done"
    assert target.why == "it is this Work's latest chat"


def test_a_chat_the_poller_restarts_is_not_said_to_have_stopped():
    chat = _chat("c:a:worker", "Worker")
    item = WorkAttention("c:a:worker", "Worker", "stalled", chat.href, recovering=True)

    target = message_target(_work(_human_review(), attention=(item,)), None, (chat,),
                            lambda key: True)

    assert target is not None and target.why == "it is this Work's latest chat"


def test_otherwise_the_current_runs_chat_takes_it_not_the_newest():
    old_run, newer = _chat("c:a:run", "Run chat", minutes=30), _chat("c:a:new", "Newer", minutes=1)
    trace = _trace(
        "gen-1", RUNNING, sessions=(RunSession("c:a:run", "c:a:run", "claude_code", "a", None),)
    )

    target = message_target(_work(_board_card()), trace, (old_run, newer), lambda key: True)

    assert target is not None and target.session_key == "c:a:run" and not target.answer


def test_a_chat_that_cannot_take_a_message_is_skipped_and_none_left_is_none():
    first, second = _chat("c:a:one", "One", minutes=1), _chat("c:a:two", "Two", minutes=9)
    card = _work(_board_card())

    target = message_target(card, None, (first, second), lambda key: key == "c:a:two")
    assert target is not None and target.session_key == "c:a:two"
    assert message_target(card, None, (first, second), lambda key: False) is None


# --- the page and its message route ---------------------------------------------------------


def _app(tmp_path, card: BoardCard | None = None, *, injectable=True):
    from test_web import _app_with_state

    app = _app_with_state(tmp_path)
    board = BoardView(
        cards={card.identity: card} if card else {},
        scopes=(BoardScope("agentdeck", org="eerovil", cards=(card,) if card else ()),),
    )
    app.state.issues.read_board = lambda: board
    app.state.config.inject.enabled = True
    state = app.state.app_state
    state.update_session(
        Session(
            key="claude_code:test:worker",
            account_key="claude_code:test",
            session_id="worker",
            status=SessionStatus.IDLE,
            last_activity=NOW,
            title="Worker chat",
            capabilities=frozenset({Capability.INJECT}) if injectable else frozenset(),
        )
    )
    state.set_issues("claude_code:test:worker", (IssueRef(WORK.repository, WORK.number),))
    return app


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_the_page_offers_a_work_level_message_box_near_the_status(tmp_path):
    html = _client(_app(tmp_path)).get(work_href(WORK.work_id)).text

    main = html.split("<main", 1)[1]
    assert (
        main.index('id="work-status"')
        < main.index('id="work-message"')
        < main.index('id="work-lifecycle"')
    )
    assert 'placeholder="Message this Work…"' in main
    assert 'data-wd-message-target="claude_code:test:worker"' in main
    assert ">Send</button>" in main and "data-wd-answer" not in main
    form = re.search(r'<form[^>]*id="work-message"[^>]*>', main).group(0)
    assert f'hx-post="{work_href(WORK.work_id)}/message"' in form


def _insight(**fields) -> AssistantView:
    fields.setdefault("kind", "waiting")
    return AssistantView(
        state="ready",
        insights=(
            AssistantInsight(session_key="claude_code:test:worker", headline="stopped", **fields),
        ),
    )


def test_a_question_turns_the_box_into_answer_question(tmp_path):
    app = _app(tmp_path)
    app.state.app_state.sessions["claude_code:test:worker"].question = "Ship it now?"
    app.state.assistant.config.enabled = True
    app.state.assistant.view = AssistantView(
        state="ready",
        insights=(
            AssistantInsight(
                session_key="claude_code:test:worker", kind="needs_input", headline="asks"
            ),
        ),
    )

    main = _client(app).get(work_href(WORK.work_id)).text.split("<main", 1)[1]

    assert 'data-wd-tone="attention"' in main
    assert 'data-wd-attention-kind="chat_question"' in main
    assert "Worker chat asked: Ship it now?" in main
    assert ">Answer question</button>" in main and 'placeholder="Answer the question…"' in main


def test_a_review_escalation_page_sends_a_message_and_says_where_it_goes(tmp_path):
    """The #2207 page: In review, escalated to the operator, the worker chat finished."""
    app = _app(tmp_path, _human_review())
    app.state.assistant.config.enabled = True
    app.state.assistant.view = _insight(kind="finished")

    main = _client(app).get(work_href(WORK.work_id)).text.split("<main", 1)[1]

    assert 'data-wd-attention-kind="review_decision"' in main
    assert "Review needs your decision" in main and "Decide review" in main
    assert f'<a href="{PR}" data-wd-where target="_blank" rel="noopener">PR ' in main
    assert "Needs your answer" not in main and "Answer" not in main
    assert ">Send</button>" in main and "data-wd-answer" not in main
    assert 'placeholder="Message this Work…"' in main
    assert 'data-wd-message-target="claude_code:test:worker"' in main


def test_only_a_real_question_makes_a_card_ask(tmp_path):
    from agentdeck.questions import OpenQuestion

    app = _app(tmp_path)
    app.state.assistant.config.enabled = True
    client = _client(app)
    url = work_href(WORK.work_id)

    # A waiting card with nothing asked is not a question, and neither is a blank one.
    app.state.assistant.view = _insight()
    assert 'data-wd-attention-kind="chat_stopped"' in client.get(url).text
    app.state.app_state.sessions["claude_code:test:worker"].question = "  \n "
    assert 'data-wd-attention-kind="chat_stopped"' in client.get(url).text
    app.state.app_state.sessions["claude_code:test:worker"].question = None

    # A card raised by an interaction that is no longer pending asks nothing now.
    app.state.assistant.view = _insight(cause=CAUSE_PENDING_INTERACTION)
    assert 'data-wd-attention-kind="chat_stopped"' in client.get(url).text

    # An open question counts until it is answered.
    app.state.assistant.view = _insight()
    asked = OpenQuestion("claude_code:test:worker", "Which branch?", NOW, "trailing", 3, 3)
    app.state.app_state.open_questions["claude_code:test:worker"] = asked
    assert "Worker chat asked: Which branch?" in client.get(url).text
    app.state.app_state.open_questions["claude_code:test:worker"] = OpenQuestion(
        "claude_code:test:worker", "Which branch?", NOW, "trailing", 3, 4, answered_seq=4
    )
    assert 'data-wd-attention-kind="chat_stopped"' in client.get(url).text


def test_an_answered_question_stops_asking_before_the_chat_replies(tmp_path):
    """The reply is recorded at once, while ``Session.question`` keeps the text (#2224 review)."""
    from agentdeck.models import TranscriptEvent

    app = _app(tmp_path)
    state = app.state.app_state
    session = state.sessions["claude_code:test:worker"]
    session.last_role, session.last_text, session.last_event_seq = "agent", "Which branch?", 1
    state.reconcile_open_question(session, (TranscriptEvent(1, "assistant", "Which branch?"),))
    app.state.assistant.config.enabled = True
    app.state.assistant.view = _insight()
    client = _client(app)
    url = work_href(WORK.work_id)

    asking = client.get(url).text
    assert "Worker chat asked: Which branch?" in asking and ">Answer question</button>" in asking

    # The operator answers; the agent has not written anything since.
    session.last_role, session.last_text, session.last_event_seq = "user", "main", 2
    state.reconcile_open_question(session, (TranscriptEvent(2, "user", "main"),))
    assert session.question == "Which branch?"  # the interval the review found
    assert state.open_question_for(session.key).answered_seq == 2

    answered = client.get(url).text
    assert 'data-wd-attention-kind="chat_stopped"' in answered
    assert "Needs your answer" not in answered and "data-wd-answer" not in answered
    assert ">Send</button>" in answered


def _interaction(monkeypatch, kind: str):
    from agentdeck.models import InteractionQuestion, PendingInteraction
    from agentdeck.providers import PROVIDERS

    questions = ()
    if kind in {"question", "mcp_form"}:
        questions = (InteractionQuestion("q1", "Branch", "Which branch?"),)
    pending = PendingInteraction("i-1", kind, "t", None, "Allow command?", questions=questions)
    monkeypatch.setattr(
        PROVIDERS["claude_code"],
        "pending_interaction",
        lambda account, session: pending if session.key == "claude_code:test:worker" else None,
    )


def _status_and_box(app) -> tuple[str, str]:
    main = _client(app).get(work_href(WORK.work_id)).text.split("<main", 1)[1]
    status = main.split('id="work-status"', 1)[1].split("</section>", 1)[0]
    box = main.split('id="work-sec-message"', 1)[1].split("</div>", 1)[0]
    return status, box


@pytest.mark.parametrize("kind", ["question", "mcp_form"])
def test_a_question_form_asks_but_is_answered_on_the_chats_page(tmp_path, monkeypatch, kind):
    _interaction(monkeypatch, kind)
    app = _app(tmp_path)
    app.state.assistant.config.enabled = True
    app.state.assistant.view = _insight(cause=CAUSE_PENDING_INTERACTION)

    status, box = _status_and_box(app)

    assert 'data-wd-attention-kind="chat_question"' in status
    assert "Needs your answer" in status and "Worker chat asked: Which branch?" in status
    assert "Answer the question on the chat&#39;s page" in status
    # A message cannot answer an interaction form, so the box is not an answer box.
    assert ">Send</button>" in box and "data-wd-answer" not in box
    assert "a message does not settle that" in box


@pytest.mark.parametrize(
    ("kind", "act"),
    [
        ("permission", "Approve or decline on the chat&#39;s page"),
        ("command_approval", "Approve or decline on the chat&#39;s page"),
        ("mcp_url", "Continue in the browser from the chat&#39;s page"),
    ],
)
def test_an_approval_or_browser_step_is_not_a_question(tmp_path, monkeypatch, kind, act):
    _interaction(monkeypatch, kind)
    app = _app(tmp_path, _human_review())
    app.state.assistant.config.enabled = True
    app.state.assistant.view = _insight(cause=CAUSE_PENDING_INTERACTION)

    status, box = _status_and_box(app)

    assert 'data-wd-attention-kind="chat_action"' in status
    assert "Chat is waiting on you" in status
    assert "Worker chat is waiting on: Allow command?" in status
    assert f"{act}, then decide review" in status
    assert "Review needs your decision too" in status
    # The operator's category files it under Needs answer (#2236); the story itself never
    # calls an approval a question.
    op, _, story = status.partition("</div>")
    assert 'data-op-category="needs_answer"' in op and 'data-op-actions="open hide"' in op
    assert "answer" not in story.lower()
    assert ">Send</button>" in box and "data-wd-answer" not in box


def test_a_message_to_the_work_is_sent_through_the_chosen_chats_own_send(tmp_path, monkeypatch):
    from agentdeck.web import routes_actions

    sent: list[tuple[str, str]] = []

    async def fake_inject(request, session_key):
        form = await request.form()
        sent.append((session_key, form["message"]))
        return HTMLResponse(
            "chat status", status_code=202, headers={"X-AgentDeck-Action-State": "accepted"}
        )

    monkeypatch.setattr(routes_actions, "inject_message", fake_inject)
    response = _client(_app(tmp_path)).post(
        work_href(WORK.work_id) + "/message", data={"message": "please also update the docs"}
    )

    assert response.status_code == 202
    assert sent == [("claude_code:test:worker", "please also update the docs")]
    assert 'data-wd-receipt="accepted"' in response.text and "Worker chat" in response.text


def test_a_refused_send_answers_in_words(tmp_path, monkeypatch):
    from agentdeck.web import routes_actions

    async def refuse(request, session_key):
        raise HTTPException(status_code=422, detail="that word cannot be delivered")

    monkeypatch.setattr(routes_actions, "inject_message", refuse)
    response = _client(_app(tmp_path)).post(
        work_href(WORK.work_id) + "/message", data={"message": "Stuck?"}
    )

    assert response.status_code == 422
    assert "Not sent: that word cannot be delivered" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_with_no_chat_able_to_take_it_nothing_is_sent(tmp_path, monkeypatch):
    from agentdeck.web import routes_actions

    async def forbidden(request, session_key):
        raise AssertionError("sent to a chat that cannot take a message")

    monkeypatch.setattr(routes_actions, "inject_message", forbidden)
    client = _client(_app(tmp_path, injectable=False))
    response = client.post(work_href(WORK.work_id) + "/message", data={"message": "hi"})
    assert response.status_code == 409 and 'data-wd-receipt="refused"' in response.text
    assert "data-wd-no-target" in client.get(work_href(WORK.work_id)).text


def test_a_work_send_is_a_direct_send_action():
    from starlette.requests import Request

    from agentdeck.web.action_timing import identify_action

    def request(method: str, path: str) -> Request:
        return Request(
            {"type": "http", "method": method, "path": path, "headers": [], "query_string": b""}
        )

    path = "/works/github_issue:eerovil/agentdeck#2218/message"
    assert identify_action(request("POST", path)) == ("send", None)
    assert identify_action(request("GET", path)) == (None, None)
    assert identify_action(request("POST", "/works/github_issue:x/y#1")) == (None, None)


@pytest.mark.asyncio
async def test_a_retried_work_send_reaches_the_chat_once(tmp_path, monkeypatch):
    """An uncertain Work send retried under its action ID is one message, not two."""
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from agentdeck.models import InjectResult
    from agentdeck.providers import PROVIDERS

    app = _app(tmp_path)
    app.state.app_state.sessions["claude_code:test:worker"].cwd = tmp_path
    delivered: list[str] = []
    release = asyncio.Event()

    async def fake_inject(account, session, message, *, timeout_s, allow_over_budget=False):
        delivered.append(message)
        await release.wait()
        return InjectResult(True)

    monkeypatch.setattr(PROVIDERS["claude_code"], "inject", fake_inject)
    url = work_href(WORK.work_id) + "/message"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first, again = [
            await client.post(
                url,
                data={"message": "ship it"},
                headers={"X-AgentDeck-Action-ID": "work-send-0001"},
            )
            for _ in range(2)
        ]
        other = await client.post(
            url,
            data={"message": "and the docs"},
            headers={"X-AgentDeck-Action-ID": "work-send-0002"},
        )
        for _ in range(20):
            await asyncio.sleep(0)

    assert (first.status_code, again.status_code, other.status_code) == (202, 202, 202)
    # The action ID came from the client and went back out unchanged.
    assert first.headers["X-AgentDeck-Action-ID"] == "work-send-0001"
    items = app.state.injector.status("claude_code:test:worker").items
    assert [item.client_action_id for item in items] == ["work-send-0001", "work-send-0002"]
    assert delivered == ["ship it"]
    release.set()
    await app.state.injector.stop()


@pytest.mark.asyncio
async def test_a_ticked_work_send_posts_to_the_works_own_issue(tmp_path, monkeypatch):
    """Also post to issue (#2416) from the Work page goes to the Work's issue, once."""
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from agentdeck import gh_cli, issue_post
    from agentdeck.models import InjectResult
    from agentdeck.providers import PROVIDERS
    from agentdeck.web import routes_actions

    app = _app(tmp_path)
    app.state.app_state.sessions["claude_code:test:worker"].cwd = tmp_path

    async def fake_inject(account, session, message, *, timeout_s, allow_over_budget=False):
        return InjectResult(True)

    posted = []

    async def run_once(argv, *, timeout, stdin=None, repo=None, owner=None):
        posted.append((argv[3], repo, stdin.decode()))
        return 0, b"https://github.com/eerovil/agentdeck/issues/2218#issuecomment-1\n", b""

    monkeypatch.setattr(PROVIDERS["claude_code"], "inject", fake_inject)
    monkeypatch.setattr(routes_actions, "leave_answer", lambda **kwargs: None)
    monkeypatch.setattr(issue_post.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(gh_cli, "run_once", run_once)
    url = work_href(WORK.work_id) + "/message"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get(work_href(WORK.work_id))
        sent = await client.post(
            url,
            data={"message": "keep the old API", "post_to_issue": "eerovil/agentdeck#2218"},
            headers={"X-AgentDeck-Action-ID": "work-post-0001", "origin": "http://test"},
        )
        wrong = await client.post(
            url,
            data={"message": "elsewhere", "post_to_issue": "eerovil/agentdeck#1"},
            headers={"X-AgentDeck-Action-ID": "work-post-0002", "origin": "http://test"},
        )
        for _ in range(20):
            await asyncio.sleep(0)

    assert 'name="post_to_issue" value="eerovil/agentdeck#2218"' in page.text
    assert sent.status_code == 202
    assert 'data-wd-issue-post="done"' in sent.text and "Posted to #2218" in sent.text
    assert posted == [("2218", "eerovil/agentdeck", posted[0][2])]
    assert "> keep the old API" in posted[0][2]
    assert wrong.status_code == 409 and "issue changed" in wrong.text
    await app.state.injector.stop()


# --- in the browser -------------------------------------------------------------------------


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_live_attention_change_keeps_what_is_being_typed(tmp_path):
    from conftest import async_playwright
    from test_web import _live_server

    card = _board_card(
        generation="gen-1", dispatch=BoardDispatch(id="d-1", run_id="gen-1", work_id=WORK.work_id)
    )
    app = _app(tmp_path, card)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.add_init_script(
            "document.addEventListener('htmx:sseOpen', () => { window.__sseOpen = true; }, true)"
        )
        await page.goto(base + work_href(WORK.work_id))
        box = page.locator('#work-message textarea[name="message"]')
        await box.fill("half-typed instruction")
        await box.focus()

        app.state.app_state.sessions["claude_code:test:worker"].question = "Ship it now?"
        app.state.assistant.config.enabled = True
        app.state.assistant.view = AssistantView(
            state="ready",
            insights=(
                AssistantInsight(
                    session_key="claude_code:test:worker", kind="needs_input", headline="asks"
                ),
            ),
        )
        app.state.app_state.bus.publish("assistant")

        # The Work page has no stream since #2864: what is typed is never touched, and the
        # question shows, with the box turned into an answer box, on the next load.
        assert await box.input_value() == "half-typed instruction"
        assert await page.locator("#work-message-result").inner_text() == ""
        await page.reload()
        await page.locator('#work-status[data-wd-tone="attention"]').wait_for(timeout=15000)
        await page.locator("#work-sec-message button.is-answer").wait_for(timeout=15000)
        box = page.locator('#work-message textarea[name="message"]')
        assert await box.get_attribute("placeholder") == "Answer the question…"
        # The story, the milestones and the answer box are all on the first phone screen.
        for selector in ("#work-status", "#work-message", "#work-lifecycle"):
            box_of = page.locator(selector)
            bottom = await box_of.evaluate("el => el.getBoundingClientRect().bottom")
            assert bottom <= 844, selector
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_live_review_escalation_stays_a_review_decision_on_a_phone(tmp_path):
    """#2207: the card is escalated while the page is open; no chat asked anything. Since #2864
    the page shows it on the next load."""
    from conftest import async_playwright
    from test_web import _live_server

    card = _board_card(
        generation="gen-1", dispatch=BoardDispatch(id="d-1", run_id="gen-1", work_id=WORK.work_id)
    )
    app = _app(tmp_path, card)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.add_init_script(
            "document.addEventListener('htmx:sseOpen', () => { window.__sseOpen = true; }, true)"
        )
        await page.goto(base + work_href(WORK.work_id))
        target = page.locator("#work-sec-message [data-wd-message-target]")
        before = await target.get_attribute("data-wd-message-target")
        box = page.locator('#work-message textarea[name="message"]')
        await box.fill("half-typed note")

        escalated = _human_review()
        board = BoardView(
            cards={escalated.identity: escalated},
            scopes=(BoardScope("agentdeck", org="eerovil", cards=(escalated,)),),
        )
        app.state.issues.read_board = lambda: board
        app.state.assistant.config.enabled = True
        app.state.assistant.view = _insight(kind="finished")
        app.state.app_state.bus.publish("assistant")
        # No live update on a Work page since #2864: what is typed stays, and a reload shows
        # the escalation.
        assert await box.input_value() == "half-typed note"
        await page.reload()

        status = page.locator('#work-status[data-wd-attention-kind="review_decision"]')
        await status.wait_for(timeout=15000)
        # The headline is shown in capitals, so its text is compared case-folded.
        assert "review needs your decision" in (await status.inner_text()).casefold()
        assert "answer" not in (await page.locator("main.work-main").inner_text()).casefold()
        assert await page.locator("#work-sec-message button[type=submit]").inner_text() == "Send"
        assert await page.locator("#work-sec-message [data-wd-answer]").count() == 0
        assert await target.get_attribute("data-wd-message-target") == before
        assert await box.get_attribute("placeholder") == "Message this Work…"
        # Why the Work waits, and what to do about it, are on the first phone screen.
        for selector in ("#work-status .wd-status-head", "#work-status [data-wd-you]"):
            bottom = await page.locator(selector).evaluate(
                "el => el.getBoundingClientRect().bottom"
            )
            assert bottom <= 844, selector
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_an_unanswered_work_send_retries_under_the_same_action_id(tmp_path, monkeypatch):
    from conftest import async_playwright
    from test_web import _live_server

    from agentdeck.web import routes_actions

    seen: list[str | None] = []

    async def fake_inject(request, session_key):
        return HTMLResponse("ok", status_code=202, headers={"X-AgentDeck-Action-State": "accepted"})

    monkeypatch.setattr(routes_actions, "inject_message", fake_inject)
    app = _app(tmp_path)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )

        async def once_lost(route):
            seen.append(route.request.headers.get("x-agentdeck-action-id"))
            if len(seen) == 1:
                await route.abort()  # the answer never arrives: an uncertain write
            else:
                await route.continue_()

        await page.route("**/message", once_lost)
        await page.goto(base + work_href(WORK.work_id))
        box = page.locator('#work-message textarea[name="message"]')
        await box.fill("run the migration")
        await page.locator("#work-sec-message button[type=submit]").click()
        retry = page.locator("#work-message-result .wd-message-retry")
        await retry.wait_for(timeout=10000)
        # Nothing was replayed on its own, and the text is still there.
        assert len(seen) == 1 and await box.input_value() == "run the migration"
        await retry.click()
        await page.locator('#work-message-result [data-wd-receipt="accepted"]').wait_for(
            timeout=10000
        )
        assert len(seen) == 2 and seen[0] and seen[0] == seen[1]
        assert await box.input_value() == ""
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_an_unanswered_work_secret_is_never_kept_for_a_retry(tmp_path):
    """A temporary secret is typed again, never replayed from memory — the composer's rule."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _app(tmp_path)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.route("**/message", lambda route: route.abort())
        await page.goto(base + work_href(WORK.work_id))
        box = page.locator('#work-message textarea[name="message"]')
        send = page.locator("#work-sec-message button[type=submit]")
        result = page.locator("#work-message-result")

        # An ordinary message that got no answer is kept, and offered for Retry...
        await box.fill("run the migration")
        await send.click()
        await result.locator(".wd-message-retry").wait_for(timeout=10000)
        assert await page.evaluate("() => Boolean(document.forms['work-message']._workSent)")

        # ...but a secret is not, and neither is the earlier message's copy.
        await box.fill("/secret DEPLOY_TOKEN hunter2-value")
        await send.click()
        await result.locator("text=Enter it again to resend.").wait_for(timeout=10000)
        assert await result.locator(".wd-message-retry").count() == 0
        kept = await page.evaluate(
            """() => {
                const form = document.forms['work-message'];
                const copies = [form._workSent, form._agentdeckSendRetryPayload];
                return copies.filter(Boolean).map(data => [...data.values()].join(' '));
            }"""
        )
        assert kept == []
        await browser.close()


def test_a_card_waiting_on_a_blocker_keeps_its_review_pass_on_the_page():
    """#2367: a Blocked card waiting on another issue hides the review summary, but the pass
    it kept for the current head is still a pass, not "Not yet"."""
    head = "5be0c7f4d207fc616c052a3100f78e33a9a68acf"
    card = _board_card(
        status="Blocked",
        status_role="blocked",
        blocked_by=("eerovil/agentdeck#2365",),
        auto_release=True,
        review_pass_heads=(head,),
    )
    work = _work(card)
    assert work.review is None

    def at(sha: str):
        output = OutputFact(PR, "eerovil/agentdeck", 2300, FOUND_BY_CARD, "open", sha)
        return work_status(work.__class__(**{**work.__dict__, "outputs": (output,)}))

    # The kept pass names its commit: a moved base could have changed what would merge.
    assert _steps(at(head.upper()))["review"] == ("done", "Passed at 5BE0C7F")
    # A push since the pass, or no known head, is not covered by it.
    assert _steps(at("def5678"))["review"] == ("idle", "Not yet")
    assert _steps(at(""))["review"] == ("idle", "Not yet")
    # The pass does not turn into a merge on its own.
    assert _steps(at(head))["merge"] == ("waiting", "Waiting")


def test_the_kept_review_pass_is_read_from_the_engine_record():
    """#2367: a Blocked card's pass lives in ``release_review_pass``, not a bits reading."""
    from agentdeck.poller_ops import _card_review_pass_heads

    head = "5be0c7f4d207fc616c052a3100f78e33a9a68acf"
    record = {"release_review_pass": {"heads": {"agentdeck#2333": head}}}
    assert _card_review_pass_heads(record) == (head,)
    assert _card_review_pass_heads({}) == ()
    assert _card_review_pass_heads({"release_review_pass": {"heads": ["x"]}}) == ()
    assert _card_review_pass_heads({"release_review_pass": {"heads": {"a": ""}}}) == ()


def test_a_waiting_card_says_passed_only_on_the_engines_fresh_reading():
    """#2367 review: only the review bit's reading judged the content, so only it says
    "Passed"; a kept pass at the same head names the commit instead."""
    head = "5be0c7f4d207fc616c052a3100f78e33a9a68acf"

    def review(**heads):
        card = _board_card(
            status="Blocked", status_role="blocked",
            blocked_by=("eerovil/agentdeck#2365",), auto_release=True, **heads,
        )
        work = _work(card)
        output = OutputFact(PR, "eerovil/agentdeck", 2300, FOUND_BY_CARD, "open", head)
        status = work_status(work.__class__(**{**work.__dict__, "outputs": (output,)}))
        return _steps(status)["review"]

    assert review(review_passed_heads=(head,)) == ("done", "Passed")
    assert review(review_passed_heads=(head,), review_pass_heads=(head,)) == ("done", "Passed")
    assert review(review_pass_heads=(head,)) == ("done", "Passed at 5be0c7f")
    assert review() == ("idle", "Not yet")


# --- a ChatGPT review opened by hand (#2411) -------------------------------------------------

HEAD = "537208c4d207fc616c052a3100f78e33a9a68acf"


def _finished_on_a_stale_card(chat_reviews=None):
    """bazaar#327: the issue closed, the merged pull request is the result, and the engine's
    card still rests In review, so its review summary says a person closes the issue."""
    card = _board_card(status="In review", status_role="in_review")
    merged = OutputFact(PR, "eerovil/agentdeck", 2300, FOUND_BY_CARD, "merged", HEAD)
    facts = work_facts(WORK, card=card)
    facts = facts.__class__(**{**facts.__dict__, "outputs": (merged,), "finished_at": NOW})
    return build_work_card(
        WORK, issue=None, title="Work story", issue_state="closed", facts=facts,
        chat_reviews=chat_reviews,
    )


def test_a_finished_work_never_asks_for_a_review_from_a_stale_card():
    work = _finished_on_a_stale_card()
    assert work.work_state == "done" and work.review is not None and work.review.needs_human

    status = work_status(work)
    assert status.headline == "Done"
    assert _steps(status)["review"] == ("idle", "None")


def test_a_finished_work_says_chatgpt_reviewed_what_merged():
    reviewed = {("eerovil/agentdeck", 2300): ("0000000", HEAD[:7].upper())}
    assert _steps(work_status(_finished_on_a_stale_card(reviewed)))["review"] == (
        "done", "ChatGPT reviewed"
    )
    # A review of a commit the pull request moved past, or of another pull request, is not it.
    for other in (
        {("eerovil/agentdeck", 2300): ("def5678",)},
        {("eerovil/agentdeck", 2301): (HEAD,)},
        {("other/agentdeck", 2300): (HEAD,)},
    ):
        assert _steps(work_status(_finished_on_a_stale_card(other)))["review"] == (
            "idle", "None"
        )


def test_an_open_work_with_no_review_in_play_says_chatgpt_reviewed():
    card = _board_card(status="In progress")
    output = OutputFact(PR, "eerovil/agentdeck", 2300, FOUND_BY_CARD, "open", HEAD)
    facts = work_facts(WORK, card=card)
    facts = facts.__class__(**{**facts.__dict__, "outputs": (output,)})

    def review(reviews):
        work = build_work_card(
            WORK, issue=None, title="Work story", facts=facts, chat_reviews=reviews
        )
        assert work.review is None
        return _steps(work_status(work))["review"]

    assert review({("eerovil/agentdeck", 2300): (HEAD,)}) == ("done", "ChatGPT reviewed")
    assert review({}) == ("idle", "Not yet")
    # A head the deck never learned is not the reviewed one.
    blank = OutputFact(PR, "eerovil/agentdeck", 2300, FOUND_BY_CARD, "open", "")
    facts = facts.__class__(**{**facts.__dict__, "outputs": (blank,)})
    assert review({("eerovil/agentdeck", 2300): ("",)}) == ("idle", "Not yet")


@private_feature(_chat_board)
def test_the_work_reads_finished_review_chats_off_the_chat_board(tmp_path):
    from agentdeck.chat_board import ChatBoardService, ChatRow
    from agentdeck.web.work_view import _chat_reviews, _review_heads

    service = ChatBoardService(
        tmp_path / "streams", repository="eerovil/review-chats", store_path=tmp_path / "b.json"
    )
    for number, title, state in (
        (1, "Review Acmecorp/Bazaar#328", "closed"),
        (2, "Review acmecorp/bazaar#328", "closed"),
        (3, "Review acmecorp/bazaar#329", "open"),  # still running: not a finished review
        (4, "Review acmecorp/bazaar#330", "closed"),  # no recorded commit
        (5, "Fix acmecorp/bazaar#328 something", "closed"),  # a fix chat is not a review
        (6, "Review acmecorp/bazaar#300", "closed"),  # a Work's chat, titled by its issue
    ):
        service.board.rows[number] = ChatRow(number, title, state)
    pr = ("acmecorp/bazaar", 328)
    heads = {
        1: {pr: "aaaaaaa"},
        2: {pr: "bbbbbbb"},
        3: {("acmecorp/bazaar", 329): "ccccccc"},
        5: {pr: "ddddddd"},
        # #2637: credited to each pull request it covered, never to the issue in its title.
        6: {pr: "eeeeeee", ("acmecorp/lib", 4): "fffffff"},
    }
    state = SimpleNamespace(chat_review_heads=lambda repository: heads)
    app = SimpleNamespace(state=SimpleNamespace(chat_board=service))

    assert _chat_reviews(app, _review_heads(app, state)) == {
        pr: ("aaaaaaa", "bbbbbbb", "eeeeeee"),
        ("acmecorp/lib", 4): ("fffffff",),
    }

    service._enabled = False
    assert _chat_reviews(app, _review_heads(app, state)) == {}


def test_a_blocked_review_chat_asks_a_person_instead_of_reviewing():
    """#2442: the reviewer awaits its chat, and the chat board says the chat is blocked."""
    from agentdeck.poller_nudge import ExternalReviews

    reviews = ExternalReviews(
        None, {"eerovil/agentdeck#2300": "eerovil/review-chats#2653"}, frozenset()
    )
    card = _board_card(
        status="In review", in_review=True, status_role="in_review", reviewer=reviews
    )

    def status(holds):
        facts = work_facts(WORK, card=card, chat_holds=holds)
        return work_status(build_work_card(WORK, issue=None, title="Work story", facts=facts))

    running = status({})
    assert running.headline == "Reviewing" and _steps(running)["review"] == ("active", "Running")

    held = status({"eerovil/review-chats#2653": ("blocked",)})
    assert held.headline != "Reviewing" and held.kind == REVIEW_DECISION
    assert _steps(held)["review"] == ("attention", "Needs you")
    assert "External review is blocked in eerovil/review-chats#2653" in held.summary


@private_feature(_chat_board)
def test_the_work_reads_held_chats_off_the_chat_board(tmp_path):
    from agentdeck.chat_board import ChatBoardService, ChatRow
    from agentdeck.web.work_view import _chat_holds

    service = ChatBoardService(
        tmp_path / "streams", repository="eerovil/Review-Chats", store_path=tmp_path / "b.json"
    )
    for number, state, labels in (
        (1, "open", ("blocked",)),
        (2, "open", ("in-progress",)),  # a run is working on it: still a running review
        (3, "closed", ("blocked",)),  # a closed chat is the reviewer's to settle
        (4, "open", ("blocked", "in-progress", "needs-more-info")),
        (5, "open", ()),
    ):
        service.board.rows[number] = ChatRow(number, "Review x/y#1", state, labels)
    app = SimpleNamespace(state=SimpleNamespace(chat_board=service))

    assert _chat_holds(app) == {
        "eerovil/review-chats#1": ("blocked",),
        "eerovil/review-chats#4": ("needs-more-info", "blocked"),
    }

    service._enabled = False
    assert _chat_holds(app) == {}


# --- what an automatic wait reads as, and where it is listed (#2573) ------------------------


def _queue_row(key: str, state: str = "queued", **fields) -> dict:
    # A job line measures against the real clock, so stamp against it too: module-level
    # NOW is taken at import, and a long suite reaches these tests 10+ minutes later (#2694).
    now = datetime.now(UTC).timestamp()
    row = {
        "id": f"job-{key}",
        "state": state,
        "session_key": key,
        "command": ["uv", "run", "pytest", "-q"],
        "submitted_at": now - 600,
        "started_at": now - 60 if state == "running" else None,
    }
    return {**row, **fields}


def _sections(*works):
    from agentdeck.work_story import front_row, front_sections

    return front_sections([front_row(work, current=True) for work in works])


def test_a_card_blocked_by_open_issues_waits_on_other_work():
    from agentdeck.work_story import IDLE, IN_PROGRESS, WAITING_BLOCKERS, front_row

    card = _board_card(blocked_by=("Eerovil/AgentDeck#2141", "eerovil/store#2142"))
    work = _work(card)
    status = work_status(work)

    assert (status.tone, status.headline, status.kind) == (
        IDLE, "Waiting on other work", WAITING_BLOCKERS
    )
    # The Work's own repository, in any case, is said as #n; another keeps its name.
    assert status.summary == "Blocked by #2141, eerovil/store#2142."
    assert status.next == "blockers to close"
    row = front_row(work, current=True)
    assert row.section == IN_PROGRESS and row.waits
    assert row.line == "Blocked by #2141, eerovil/store#2142"
    sections = _sections(work)
    assert [item.card.work_id for item in sections.waiting] == [work.work_id]
    assert sections.active == ()


def test_a_starred_work_leads_its_section():
    """#3116: a starred Work comes first in Active (and Needs you), ahead of newer ones."""
    from dataclasses import replace

    base = _work(_board_card(), chats=(_chat("c:a:worker", "Worker", working=True),))
    now = datetime.now(UTC)
    newer = replace(base, work_id="github_issue:o/r#1", last_activity=now)
    older = replace(base, work_id="github_issue:o/r#2", last_activity=now - timedelta(hours=1))
    assert [row.card.work_id for row in _sections(newer, older).active] == [
        newer.work_id, older.work_id
    ]
    starred = replace(older, starred=True)
    assert [row.card.work_id for row in _sections(newer, starred).active] == [
        starred.work_id, newer.work_id
    ]


def test_a_blocked_card_with_a_turn_running_is_still_working():
    work = _work(
        _board_card(blocked_by=("eerovil/agentdeck#2141",)),
        chats=(_chat("c:a:worker", "Worker", working=True),),
    )
    assert work_status(work).headline == "Working"


def _woken(key: str, title: str) -> WorkChat:
    """A chat working on a turn only its own background notification opened (#2760)."""
    from dataclasses import replace

    return replace(_chat(key, title, working=True), background=True)


def test_a_chat_its_own_notification_woke_keeps_the_work_in_needs_you():
    """#2760: a render watcher woke the chat; nobody answered, so the Work still waits."""
    from agentdeck.work_story import NEEDS_YOU, front_row

    work = _work(
        _board_card(status="Blocked", status_role="blocked"),
        chats=(_woken("c:a:worker", "Worker"),),
    )
    row = front_row(work, current=True)

    assert work.woke_itself
    assert row.section == NEEDS_YOU and row.attention is not None
    assert row.status.headline == "Working"  # the headline rule of #2590 is untouched


def test_a_woken_works_own_page_offers_and_plans_what_its_row_does():
    """#2760 review: the Work page judged attention on the Working story, so every action the
    held row offered was refused as "not offered for this Work now"."""
    from agentdeck import work_actions
    from agentdeck.web.work_view import WorkDetail
    from agentdeck.work_story import front_row

    work = _work(
        _board_card(status="Blocked", status_role="blocked"),
        chats=(_woken("c:a:worker", "Worker"),),
    )
    row = front_row(work, current=True)
    detail = WorkDetail(work, None, None, (), ())

    assert detail.attention == row.attention
    writes = [action for action in row.attention.actions if action in work_actions.WRITES]
    assert writes
    for action in writes:
        assert work_actions.plan(action, detail.card, detail.attention) is not None


def test_a_merge_waiting_for_its_deploy_reads_as_deploying_not_finished_or_needs_you():
    """#2760 review: inside the deploy grace the Work sat under Finished, then moved to Needs you
    once the grace ran out; it should wait as Deploying."""
    from dataclasses import replace

    from agentdeck.work_story import FINISHED, NEEDS_YOU, front_row

    closed = replace(_work(_board_card()), work_state="done", awaiting_finish=True)
    due = replace(closed, deploy_due=True)
    row = front_row(due, current=True)

    assert (row.status.headline, row.status.kind) == ("Deploying", "deploying")
    assert row.section not in {FINISHED, NEEDS_YOU} and row.waits
    assert front_row(closed, current=True).section == NEEDS_YOU  # past the grace: Finish it


def test_a_closed_issue_with_an_open_pull_request_follows_the_pull_request():
    """#3187: a Poller card done with its issue closed by another chat, its pull request open."""
    from dataclasses import replace

    from agentdeck.work_facts import OutputFact
    from agentdeck.work_overview import ReviewSummary
    from agentdeck.work_story import NEEDS_YOU, front_row

    opened = OutputFact("https://github.com/o/r/pull/9", "o/r", 9, "registered", state="open")
    closed = replace(_work(_board_card()), work_state="done", awaiting_finish=True)
    ready = ReviewSummary(
        "passed", "Review passed", "merge", needs_human=True, decision="merge"
    )
    reviewing = ReviewSummary("running", "Review running", "release")
    for review, headline in ((reviewing, "Reviewing"), (ready, "Ready for you to merge")):
        work = replace(closed, outputs=(opened,), review=review)
        status = work_status(work)
        assert status.headline == headline and status.summary.endswith(" · issue closed.")
        attention = front_row(work, current=True).attention
        assert attention is None or attention.category != "issue_closed"
    # No pull request open: unchanged.
    assert work_status(closed).headline == "Issue closed"
    assert front_row(closed, current=True).section == NEEDS_YOU


def test_a_turn_someone_started_still_takes_the_work_out_of_needs_you():
    from agentdeck.work_story import IN_PROGRESS, front_row

    parked = _board_card(status="Blocked", status_role="blocked")
    answered = _work(parked, chats=(_chat("c:a:worker", "Worker", working=True),))
    mixed = _work(
        parked,
        chats=(_woken("c:a:worker", "Worker"), _chat("c:a:other", "Other", working=True)),
    )

    assert not answered.woke_itself and not mixed.woke_itself
    assert front_row(answered, current=True).section == IN_PROGRESS
    assert front_row(mixed, current=True).section == IN_PROGRESS


def test_a_woken_chat_on_a_work_nobody_waits_on_stays_active():
    from agentdeck.work_story import IN_PROGRESS, front_row

    work = _work(_board_card(), chats=(_woken("c:a:worker", "Worker"),))

    assert work.woke_itself
    assert front_row(work, current=True).section == IN_PROGRESS


def test_a_card_a_person_must_unblock_still_needs_them():
    from agentdeck.work_story import NEEDS_YOU, front_row

    work = _work(_board_card(status="Blocked", status_role="blocked"))
    status = work_status(work)

    assert (status.tone, status.headline) == (ATTENTION, "Blocked")
    assert front_row(work, current=True).section == NEEDS_YOU


def test_a_job_a_chat_queued_is_what_the_work_waits_for():
    from agentdeck.work_overview import work_jobs
    from agentdeck.work_story import WAITING_JOB

    chat = _chat("c:a:worker", "Worker")
    jobs = work_jobs([_queue_row(chat.session_key)], (chat,))
    work = _work(_board_card(), chats=(chat,), jobs=jobs)
    status = work_status(work)

    assert (status.tone, status.headline, status.kind) == (ACTIVE, "Job queued", WAITING_JOB)
    assert re.fullmatch(r"uv run pytest -q · queued 1\d min\.", status.summary)
    waiting = _sections(work).waiting
    assert [item.card.work_id for item in waiting] == [work.work_id]
    assert waiting[0].line == status.summary.rstrip(".")


def test_a_running_job_comes_first_and_the_rest_are_counted():
    from agentdeck.work_overview import work_jobs

    chat = _chat("c:a:worker", "Worker")
    queue = [
        _queue_row(chat.session_key, label="focused tests"),
        _queue_row(chat.session_key, "running", label="x" * 60),
    ]
    jobs = work_jobs(queue, (chat,))
    status = work_status(_work(_board_card(), chats=(chat,), jobs=jobs))

    assert [job.state for job in jobs] == ["running", "queued"]
    assert status.headline == "Job running"
    assert re.fullmatch("x" * 39 + r"… · running \d+ min \(\+1 more\)\.", status.summary)


def test_a_chat_mid_turn_reads_working_whatever_job_it_queued():
    from agentdeck.work_overview import work_jobs

    chat = _chat("c:a:worker", "Worker", working=True)
    jobs = work_jobs([_queue_row(chat.session_key)], (chat,))
    work = _work(_board_card(), chats=(chat,), jobs=jobs)

    assert work_status(work).headline == "Working"
    assert _sections(work).waiting == ()


def test_only_the_works_own_unfinished_ordinary_jobs_count():
    from agentdeck.work_overview import work_jobs

    chat = WorkChat(
        "chat-1", "c:a:new-leg", "Worker", "/chats/w", False, NOW, (),
        legs=("c:a:old-leg", "c:a:new-leg"),
    )
    queue = [
        _queue_row("c:a:old-leg", label="an earlier leg's job"),
        _queue_row("c:a:someone-else"),
        _queue_row("c:a:new-leg", "done"),
        _queue_row("c:a:new-leg", "running", purpose="verification", work_id=WORK.work_id),
        _queue_row("c:a:new-leg", "running", started_at="not a time"),
    ]
    jobs = work_jobs(queue, (chat,))

    assert [(job.label, job.state) for job in jobs] == [
        ("uv run pytest -q", "running"), ("an earlier leg's job", "queued")
    ]
    assert jobs[0].since is None


def test_both_deploying_readings_are_listed_under_live_checking():
    """A Poller card's Deploying is the step after its merge (#3578): listed with Live checking."""
    from agentdeck.work_story import DEPLOYING

    watched = _work(_board_card(awaiting_deploy=True))
    card = _board_card(review_mode="merge", auto_release=True)
    merged = OutputFact(PR, "eerovil/agentdeck", 2300, FOUND_BY_CARD, "merged")
    landed = _work(card)
    landed = landed.__class__(**{**landed.__dict__, "outputs": (merged,)})

    for work in (watched, landed):
        status = work_status(work)
        assert (status.headline, status.kind) == ("Deploying", DEPLOYING)
        front = _sections(work)
        assert [item.card.work_id for item in front.live] == [work.work_id]
        assert not front.waiting


def test_a_job_line_says_how_long_it_has_queued_or_run():
    from agentdeck.work_overview import WorkJob
    from agentdeck.work_story import _job_line

    at = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    queued = WorkJob("queued", "pytest -q", at)
    running = WorkJob("running", "pytest -q", at)

    assert _job_line(queued, at + timedelta(seconds=59)) == "pytest -q · queued under a minute"
    assert _job_line(running, at + timedelta(minutes=6, seconds=30)) == "pytest -q · running 6 min"
    assert _job_line(running, at + timedelta(minutes=125)) == "pytest -q · running 2 h 5 min"
    assert _job_line(queued, at + timedelta(hours=2)) == "pytest -q · queued 2 h"
    # A clock behind the job's own stamp is not a negative wait; an unknown start says none.
    assert _job_line(queued, at - timedelta(minutes=3)) == "pytest -q · queued under a minute"
    assert _job_line(WorkJob("running", "pytest -q", None), at) == "pytest -q"


def test_a_paused_or_finished_work_is_never_working():
    """#2590: only a pause a person made and a finished Work outweigh a chat mid-turn."""
    from dataclasses import replace

    chats = (_chat("c:a:worker", "Worker", working=True),)
    work = _work(_board_card(), chats=chats)
    assert work_status(work).headline == "Working"

    assert work_status(replace(work, paused=True, label_paused=True)).headline == "Paused"
    assert work_status(replace(work, work_state="done")).headline == "Done"


def test_a_turn_only_reading_adds_nothing_to_working():
    """#2590: Implementing, Starting, Not started and Idle say only what the turn already does."""
    work = _work(_board_card(), chats=(_chat("c:a:worker", "Worker", working=True),))
    status = work_status(work)

    assert (status.headline, status.summary) == ("Working", "Worker is working.")
    assert not status.detail.startswith("When it finishes")


# --- Works as a tree of parent and sub-issues (#3622) ------------------------------------------


def _tree_rows():
    """A parent Work with three sub-issues' Works: one asks a question, one works, one is quiet."""
    from dataclasses import replace

    from agentdeck.work_story import front_row

    def card(work, number, **fields):
        return replace(
            work,
            work_id=f"github_issue:eerovil/agentdeck#{number}",
            href=f"/works/github_issue:eerovil%2Fagentdeck%23{number}",
            source_label=f"eerovil/agentdeck#{number}",
            title=f"Issue {number}",
            **fields,
        )

    now = datetime.now(UTC)
    quiet = _work(_board_card(status="Backlog", pull_request_url=""))
    working = _work(_board_card(), chats=(_chat("c:a:worker", "Worker", working=True),))
    asking = _work(_board_card(), attention=(_ask(),))
    parent = card(quiet, 10, last_activity=now - timedelta(days=3))
    rows = {
        10: front_row(parent, current=False),
        11: front_row(card(asking, 11, last_activity=now - timedelta(hours=2)), current=True),
        12: front_row(card(working, 12, last_activity=now), current=True),
        13: front_row(card(quiet, 13, last_activity=now - timedelta(days=2)), current=False),
    }
    parents = {f"github_issue:eerovil/agentdeck#{n}": "github_issue:eerovil/agentdeck#10"
               for n in (11, 12, 13)}
    return rows, parents


def test_a_parent_row_folds_its_children_and_leads_with_the_one_that_needs_you():
    from agentdeck.work_story import NEEDS_YOU, front_sections
    from agentdeck.work_tree import fold

    rows, parents = _tree_rows()
    parent_id = rows[10].card.work_id
    folded = fold(list(rows.values()), parents=parents,
                  counts={parent_id: (1, 4)})

    [row] = folded
    assert row.card.work_id == parent_id and row.card.title == "Issue 10"
    # In the asking child's category, its question named by number, one tap from it.
    assert row.section == NEEDS_YOU and row.attention.category == rows[11].attention.category
    assert row.lead.card.work_id == rows[11].card.work_id and row.actor is rows[11].card
    assert row.line.startswith("#11: ")
    assert row.progress == "1/4 done · 1 waits for you · 1 working"
    # The newest child's activity orders it; no child is a row of its own anywhere.
    assert row.card.last_activity == rows[12].card.last_activity
    sections = front_sections(folded)
    listed = [item.card.work_id for item in (*sections.needs_you, *sections.active,
                                             *sections.waiting, *sections.finished)]
    assert listed == [parent_id]
    # What tells a person per Work still names the child, under its own id.
    assert [item.card.work_id for item in sections.needs_you_works] == [rows[11].card.work_id]


def test_a_parent_row_with_working_children_is_active_and_redraws_when_one_moves():
    from dataclasses import replace

    from agentdeck.work_story import IN_PROGRESS
    from agentdeck.work_tree import fold

    rows, parents = _tree_rows()
    del rows[11]
    [row] = fold(list(rows.values()), parents=parents)
    assert row.section == IN_PROGRESS and row.lead.card.work_id == rows[12].card.work_id
    assert row.progress == "1 working"

    moved = {**rows, 13: replace(rows[13], card=replace(rows[13].card, title="Renamed"))}
    [again] = fold(list(moved.values()), parents=parents)
    assert again.revision != row.revision


def test_a_tree_folds_to_its_root_survives_a_cycle_and_drops_a_hidden_child():
    from agentdeck.work_story import front_sections
    from agentdeck.work_tree import ancestors, fold

    rows, parents = _tree_rows()
    ids = {n: rows[n].card.work_id for n in rows}
    # 13 under 12 under 10: two levels fold into the root.
    deep = {**parents, ids[13]: ids[12]}
    [row] = fold(list(rows.values()), parents=deep)
    assert {child.card.work_id for child in row.children} == {ids[11], ids[12], ids[13]}
    # A cycle neither loops nor loses a row.
    looped = {ids[10]: ids[11], ids[11]: ids[10]}
    assert ancestors(ids[10], looped) == (ids[11],)
    assert len(fold([rows[10], rows[11]], parents=looped)) == 1
    # A hidden child is left out of its parent's row and its count, and stays its own row
    # for the Hidden list to bring back.
    shown, alone = fold(list(rows.values()), parents=parents, hidden={ids[11]})
    assert ids[11] not in {child.card.work_id for child in shown.children}
    assert "waits for you" not in shown.progress and alone is rows[11]
    assert [row.card.work_id for row in front_sections([shown, alone], hidden={ids[11]}).hidden
            ] == [ids[11]]
    # An archived finished parent stays hidden when a child only starts working; only a
    # question brings it back (second escalated review).
    from dataclasses import replace

    from agentdeck.work_story import FINISHED

    archived = replace(rows[10], section=FINISHED, card=replace(
        rows[10].card, finished_at=datetime.now(UTC) - timedelta(hours=1)))
    [kept] = fold([archived, rows[12]], parents=parents, hidden={ids[10]})
    assert kept.section == FINISHED and kept.lead is None
    placed = front_sections([kept], now=datetime.now(UTC), hidden={ids[10]})
    assert placed.hidden == (kept,) and placed.active == () and placed.finished == ()
    # A hidden middle parent keeps what hangs under it: 13 under hidden 11 under 10.
    middle = {**parents, ids[13]: ids[11]}
    top, put = fold(list(rows.values()), parents=middle, hidden={ids[11]})
    assert [child.card.work_id for child in top.children] == [ids[12]]
    assert put.card.work_id == ids[11] and [c.card.work_id for c in put.children] == [ids[13]]


def test_a_parent_no_agent_works_on_is_not_needs_you_by_itself():
    """D3: its sub-issues are the work; a parent listed only for them never asks either."""
    from dataclasses import replace

    from agentdeck.work_story import NEEDS_YOU, QUIET, front_row
    from agentdeck.work_tree import fold

    rows, parents = _tree_rows()
    unstarted = front_row(replace(rows[10].card, start_label="agent"), current=True)
    assert unstarted.section == NEEDS_YOU
    calm = {10: unstarted, 13: rows[13]}
    [row] = fold(list(calm.values()), parents=parents)
    assert row.section == QUIET and row.own.section == QUIET
    # A parent with sub-issues but no child's Work listed still says how far they are, and
    # redraws when GitHub's count moves (review of 43c24b2).
    [alone] = fold([rows[10]], parents=parents, counts={rows[10].card.work_id: (1, 3)})
    [moved] = fold([rows[10]], parents=parents, counts={rows[10].card.work_id: (2, 3)})
    assert alone.progress == "1/3 done" and moved.progress == "2/3 done"
    assert alone.revision != moved.revision != rows[10].revision
    assert fold([rows[10]], parents=parents) == [rows[10]]
    # A parent listed only for its children, with no child left to show, is not listed.
    assert fold([rows[10]], parents=parents, tree_only={rows[10].card.work_id}) == []
    # Without its child it is an ordinary unstarted Work again.
    assert fold([unstarted], parents=parents)[0].section == NEEDS_YOU
    # With open sub-issues none of which is listed, GitHub's count still makes it a parent
    # (review of 309ef80); once every one is done it is an ordinary unstarted Work again.
    parent_id = unstarted.card.work_id
    [counted] = fold([unstarted], parents=parents, counts={parent_id: (1, 3)})
    assert counted.section == QUIET and counted.progress == "1/3 done"
    [done] = fold([unstarted], parents=parents, counts={parent_id: (3, 3)})
    assert done.section == NEEDS_YOU and done.progress == "3/3 done"
    # One rule whether or not a child is listed (owner's choice on the fourth escalation):
    # all closed on GitHub with a finished child still listed is not quiet either.
    from agentdeck.work_story import FINISHED

    finished = replace(rows[13], section=FINISHED, card=replace(
        rows[13].card, finished_at=datetime.now(UTC)))
    [closed] = fold([unstarted, finished], parents=parents, counts={parent_id: (3, 3)})
    assert closed.own.section == NEEDS_YOU and closed.section == NEEDS_YOU
    [still] = fold([unstarted, finished], parents=parents, counts={parent_id: (2, 3)})
    assert still.own.section == QUIET
    # With no count known, the listed children decide.
    assert fold([unstarted, finished], parents=parents)[0].own.section == NEEDS_YOU
    assert fold([unstarted, rows[13]], parents=parents)[0].own.section == QUIET


def test_a_child_that_newly_needs_you_ends_its_parents_hide():
    """Owner's choice on #3622: a question is never buried under a hidden parent."""
    from dataclasses import replace

    from agentdeck.work_tree import hides_ended

    rows, parents = _tree_rows()
    parent_id = rows[10].card.work_id
    hidden_at = datetime.now(UTC) - timedelta(hours=1)
    later = replace(rows[11], stopped_at=hidden_at + timedelta(minutes=5))
    earlier = replace(rows[11], stopped_at=hidden_at - timedelta(minutes=5))
    assert hides_ended([rows[10], later], parents=parents,
                       hidden_at={parent_id: hidden_at}) == {parent_id}
    assert hides_ended([rows[10], earlier], parents=parents,
                       hidden_at={parent_id: hidden_at}) == frozenset()
    # Under a hidden grandparent too, and under a hidden middle parent.
    middle = rows[13].card.work_id
    deep = {**parents, later.card.work_id: middle}
    assert hides_ended([rows[10], rows[13], later], parents=deep,
                       hidden_at={middle: hidden_at}) == {middle}
    assert hides_ended([rows[10], rows[13], later], parents=deep,
                       hidden_at={parent_id: hidden_at}) == {parent_id}


def test_the_parent_row_and_the_work_page_draw_the_tree():
    """#3622: the row's progress line opens the child that asks; the Work page lists the
    sub-issues with their state and says what the issue is part of."""
    from dataclasses import replace

    from fastapi.templating import Jinja2Templates

    from agentdeck import app as app_mod
    from agentdeck import work_tree
    from agentdeck.web import render as render_mod
    from agentdeck.web.work_view import (
        WorkDetail,
        WorkOverview,
        render_work_front,
        render_work_sections,
    )
    from agentdeck.work_story import front_sections

    templates = Jinja2Templates(directory=str(app_mod._TEMPLATES_DIR))
    render_mod.register_filters(templates)
    rows, parents = _tree_rows()
    parent_id = rows[10].card.work_id
    folded = work_tree.fold(list(rows.values()), parents=parents, counts={parent_id: (1, 4)})
    overview = WorkOverview(front=front_sections(folded))

    html = render_work_front(templates, overview, "needs")
    assert f'data-work-id="{parent_id}"' in html and "Issue 10" in html
    assert f'data-work-tree="{rows[11].card.work_id}"' in html
    assert f'href="{rows[11].card.href}"' in html and "1/4 done · 1 waits for you" in html
    assert f'data-work-id="{rows[11].card.work_id}"' not in html  # never a row of its own
    # A phone opens the parent's Work page, never its chat (D8, review of 65786a2).
    with_chat = replace(folded[0], card=replace(folded[0].card, chat_href="/chats/x"))
    tapped = render_work_front(
        templates, WorkOverview(front=front_sections([with_chat])), "needs"
    )
    assert "data-chat-href" not in tapped

    home = WorkRef("eerovil/agentdeck", 10)
    tree = work_tree.WorkTree(
        parent=work_tree.tree_line(
            WorkRef("eerovil/agentdeck", 9), home, title="The map", state="open", row=None,
            href="/works/github_issue:eerovil%2Fagentdeck%239",
        ),
        children=(
            work_tree.tree_line(WorkRef("eerovil/agentdeck", 11), home, title="Asks",
                                state="open", row=rows[11], href=rows[11].card.href),
            work_tree.tree_line(WorkRef("eerovil/store", 5), home, title="Elsewhere",
                                state="closed", row=None, href="/works/x"),
        ),
        counts=(1, 2),
    )
    detail = WorkDetail(card=rows[10].card, trace=None, workspace=None, chats=(),
                        verifications=(), tree=tree)
    section = dict(render_work_sections(templates, detail))["tree"]
    assert "Part of" in section and "#9 The map" in section
    assert "Sub-issues" in section and "1/2 done" in section
    assert 'data-work-tree-child="github_issue:eerovil/agentdeck#11"' in section
    assert "Needs you · " in section and "store#5" in section and "Closed" in section
    # The waiting child's own control, acting on its own Work (D9).
    button = rows[11].buttons[0]
    assert f'data-front-action="{button.action}"' in section
    assert (button.href or rows[11].card.href) in section
    # A Work in no tree draws nothing there.
    plain = WorkDetail(card=rows[10].card, trace=None, workspace=None, chats=(),
                       verifications=())
    assert "data-work-tree" not in dict(render_work_sections(templates, plain))["tree"]


@pytest.mark.asyncio
async def test_a_maps_work_page_draws_its_tickets_from_cached_reads():
    """The tree section draws a map's sub-issues as boxes that open their Works, with arrows
    from each ticket's cached "blocked by" read; a ticket's own record outranks the list's
    copy. A read that failed is said, and a parent that is not a map keeps its list."""
    from types import SimpleNamespace

    from fastapi.templating import Jinja2Templates

    from agentdeck import app as app_mod
    from agentdeck import wayfinder_map as wm
    from agentdeck import work_tree
    from agentdeck.web import render as render_mod
    from agentdeck.web import work_view
    from agentdeck.web.work_view import WorkDetail, render_work_sections

    blocked = {11: [], 12: [{"repository": "eerovil/agentdeck", "number": 11}], 13: None}
    reads = []

    async def blocked_by(repository, number):
        reads.append(number)
        return blocked[number]

    async def issue_body(repository, number):
        return "## Destination\n\nShip it.\n\n## Not yet specified\n\n- Restores\n"

    held = {("eerovil/agentdeck", 12): {"state": "closed", "title": "Own title",
                                        "labels": ["wayfinder:task"]}}
    issues = SimpleNamespace(blocked_by=blocked_by, issue_body=issue_body,
                             cached_issues=lambda refs: held)
    app = SimpleNamespace(state=SimpleNamespace(issues=issues))
    rows, _parents = _tree_rows()
    items = [{"repository": "eerovil/agentdeck", "number": n, "title": f"Issue {n}",
              "state": "open", "labels": ["wayfinder:research"]} for n in (11, 12, 13)]
    ref = WorkRef("eerovil/agentdeck", 10)
    front = {rows[11].card.work_id: rows[11]}

    plain = await work_view._wayfinder_diagram(app, ref, {"labels": ["agent"]}, items, front)
    assert plain is None and reads == []
    diagram = await work_view._wayfinder_diagram(
        app, ref, {"labels": ["wayfinder:map"]}, items, front
    )
    assert sorted(reads) == [11, 12, 13] and diagram.partial
    box = {item.key.rsplit("#", 1)[-1]: item for item in diagram.boxes}
    assert box["11"].state == wm.NEEDS_YOU and box["12"].state == wm.DONE
    assert box["12"].head == "#12 · task" and box["12"].lines == ("Own title",)
    assert box["13"].state == wm.WAITING and len(diagram.arrows) == 1
    # #13 has no record of its own on the deck: its type comes from the sub-issue list.
    assert box["13"].head == "#13 · research"

    templates = Jinja2Templates(directory=str(app_mod._TEMPLATES_DIR))
    render_mod.register_filters(templates)
    tree = work_tree.WorkTree(counts=(1, 3), diagram=diagram)
    detail = WorkDetail(card=rows[10].card, trace=None, workspace=None, chats=(),
                        verifications=(), tree=tree)
    section = dict(render_work_sections(templates, detail))["tree"]
    assert "Map" in section and "1/3 done" in section and "data-work-tree-child" not in section
    assert 'href="/works/github_issue:eerovil%2Fagentdeck%2312"' in section
    assert section.count("data-wm-box") == 5 and "wm-box wm-fog" in section
    assert "data-wayfinder-map-partial" in section and "Destination" in section


@pytest.mark.asyncio
async def test_a_maps_pages_and_pushes_point_at_its_waiting_questions():
    """#3696: a map's row takes the map issue's title, leads with the question by its header,
    counts questions, work and blocked tickets, and opens the question's chat at its form; its
    page lists what waits and its boxes say what they ask; a ticket's push names the map and
    the question; a ticket's chat page names its map and the next question."""
    from dataclasses import replace
    from types import SimpleNamespace

    from fastapi.templating import Jinja2Templates

    from agentdeck import app as app_mod
    from agentdeck import work_tree
    from agentdeck.web import render as render_mod
    from agentdeck.web import work_view
    from agentdeck.web.work_view import WorkDetail, WorkOverview, render_work_front
    from agentdeck.work_story import front_row, front_sections

    rows, parents = _tree_rows()
    asks = WorkAttention("c:a:t11", "Ticket chat", "waiting", "/chats/t11", asks=True,
                         question="Which lists does the deck keep?", interaction="question",
                         header="Lists")
    rows[10] = front_row(replace(rows[10].card, labels=("agent", "Wayfinder:Map"),
                                 issue_title="Make wayfinder great"), current=False)
    rows[11] = front_row(replace(rows[11].card, attention=(asks,), issue_title="Decide lists",
                                 title="Chat named it"), current=True)
    map_id, ticket = rows[10].card.work_id, rows[11].card.work_id
    # Five tickets, one done; #11-#13 listed, so the fifth (filed with no agent) is blocked.
    [row] = work_tree.fold(list(rows.values()), parents=parents, counts={map_id: (1, 5)})
    assert row.map.title == "Make wayfinder great" and row.map.tickets
    assert row.progress == "1/5 done · 1 asks you · 1 working · 1 blocked"
    [asked] = row.map.questions
    assert asked.line == "Lists: Which lists does the deck keep?"
    assert asked.href == "/chats/t11#pending-interaction" and asked.title == "Decide lists"
    # A ticket the owner hid keeps its own row for the Hidden list: listed, so not blocked.
    put = work_tree.fold(list(rows.values()), parents=parents, counts={map_id: (1, 5)},
                         hidden={ticket})
    shown = next(item for item in put if item.card.work_id == map_id)
    assert shown.progress == "1/5 done · 1 working · 1 blocked"

    templates = Jinja2Templates(directory=str(app_mod._TEMPLATES_DIR))
    render_mod.register_filters(templates)
    overview = WorkOverview(front=front_sections([row]))
    html = render_work_front(templates, overview, "needs")
    assert "Make wayfinder great" in html and "Issue 10" not in html
    assert "Lists: Which lists does the deck keep?" in html
    assert 'class="fr-tree" href="/chats/t11#pending-interaction"' in html
    # A parent that is not a map keeps its words.
    plain = work_tree.fold(
        [replace(rows[10], card=replace(rows[10].card, labels=())), *list(rows.values())[1:]],
        parents=parents, counts={map_id: (1, 5)},
    )[0]
    assert plain.map is None and plain.progress == "1/5 done · 1 waits for you · 1 working"

    # The ticket's push names the map and the question, and opens its form.
    stopped = replace(overview, front=replace(overview.front, needs_you=(
        replace(row, children=tuple(
            replace(child, unseen=True) for child in row.children
        )),
    )))
    sends, _sent = work_view.needs_you_pushes(stopped, set())
    assert [(push.title, push.body, push.url) for push in sends] == [
        ("Make wayfinder great asks", asked.line, asked.href)
    ]

    # The chat page: its map, and where Submit leads on to.
    app = SimpleNamespace(state=SimpleNamespace(work_overview=SimpleNamespace(
        overview=overview)))
    strip = work_view.chat_map_strip(app, ticket)
    assert strip.map.short == "1/5 done · 1 asks you" and strip.next is None
    other = work_view.chat_map_strip(app, rows[12].card.work_id)
    assert other.next == asked
    assert work_view.chat_map_strip(app, map_id) is None  # the map's own chat
    assert work_view.chat_map_strip(app, "github_issue:eerovil/agentdeck#99") is None

    # The map page: what waits, and a box that says what it asks and opens the form.
    async def blocked_by(repository, number):
        return []

    async def issue_body(repository, number):
        return "## Destination\n\nShip it.\n"

    issues = SimpleNamespace(blocked_by=blocked_by, issue_body=issue_body,
                             cached_issues=lambda refs: {})
    page_app = SimpleNamespace(state=SimpleNamespace(issues=issues, work_overview=None))
    items = [{"repository": "eerovil/agentdeck", "number": n, "title": f"Issue {n}",
              "state": "open", "labels": ["wayfinder:research"]} for n in (11, 12)]
    diagram = await work_view._wayfinder_diagram(
        page_app, WorkRef("eerovil/agentdeck", 10), {"labels": ["wayfinder:map"]}, items,
        {ticket: rows[11]},
    )
    box = {item.key.rsplit("#", 1)[-1]: item for item in diagram.boxes}
    assert " ".join(box["11"].lines) == "Asks: Lists · Issue 11"
    assert box["11"].href == asked.href and box["12"].href.startswith("/works/")
    assert [item.work_id for item in diagram.questions] == [ticket]
    tree = work_tree.WorkTree(counts=(1, 5), diagram=diagram)
    detail = WorkDetail(card=rows[10].card, trace=None, workspace=None, chats=(),
                        verifications=(), tree=tree)
    section = dict(work_view.render_work_sections(templates, detail))["tree"]
    assert "Waiting for you" in section and f'data-wm-question="{ticket}"' in section
    assert 'href="/chats/t11#pending-interaction"' in section


def test_a_parent_row_stands_where_its_most_urgent_member_would():
    """#3622 review: D5's order holds after Needs you too. A working child lifts a parent
    that only waits, and a parent with only a finished child is Recently finished, dated by
    when that child finished."""
    from dataclasses import replace

    from agentdeck.work_story import FINISHED, IN_PROGRESS, QUIET, front_sections
    from agentdeck.work_tree import fold

    rows, parents = _tree_rows()
    waiting = replace(rows[10], section=IN_PROGRESS, status=replace(
        rows[10].status, kind="waiting_blockers"))
    assert waiting.waits
    [row] = fold([waiting, rows[12]], parents=parents)
    assert row.lead.card.work_id == rows[12].card.work_id
    assert front_sections([row]).active == (row,)

    done_at = datetime.now(UTC) - timedelta(hours=1)
    finished = replace(rows[13], section=FINISHED,
                       card=replace(rows[13].card, finished_at=done_at))
    assert rows[10].section == QUIET
    [row] = fold([rows[10], finished], parents=parents,
                 tree_only={rows[10].card.work_id})
    assert row.section == FINISHED and row.card.finished_at == done_at
    assert front_sections([row], now=datetime.now(UTC)).finished == (row,)


@pytest.mark.asyncio
async def test_parent_issue_records_are_read_when_missing_or_stale():
    """#3622 review: a parent first named by a child has no record until something reads it,
    and its counts move only when it is read again. The front page asks off its pass; a
    parent's page asks before deciding it is a Work."""
    from types import SimpleNamespace

    from agentdeck.web import work_view
    from agentdeck.work_facts import WorkFacts

    asked, published = [], []

    async def refresh(refs, limit=None):
        asked.append(list(refs))
        return 1

    app = SimpleNamespace(state=SimpleNamespace(
        issues=SimpleNamespace(refresh_stale_issue_records=refresh, refresh_tree_records=refresh),
        app_state=SimpleNamespace(bus=SimpleNamespace(publish=published.append)),
    ))
    work_view._refresh_tree_records(app, {"github_issue:eerovil/agentdeck#10"})
    work_view._refresh_tree_records(app, {"github_issue:eerovil/agentdeck#11"})  # one at a time
    await app.state.tree_refresh
    assert asked == [[("eerovil/agentdeck", 10)]]
    assert published == [work_view.WORK_INPUTS_TOPIC]

    # Re-read too: a listed parent none of whose children is listed, whose count a child's
    # close moves (third escalated review); not a Work in no tree.
    ids = {n: f"github_issue:eerovil/agentdeck#{n}" for n in (10, 11, 20)}
    shown = [SimpleNamespace(ref=WorkRef("eerovil/agentdeck", n)) for n in (10, 20)]
    facts = {
        ids[10]: WorkFacts(record={"subIssues": {"total": 3, "completed": 1}}),
        ids[20]: WorkFacts(record={"title": "Alone"}),
    }
    assert work_view._tree_record_ids(shown, {}, facts) == {ids[10]}
    assert work_view._tree_record_ids(shown, {ids[20]: ids[11]}, facts) == {ids[10], ids[11]}

    ref = WorkRef("eerovil/agentdeck", 10)
    assert await work_view._reread_tree_issue(app, ref, WorkFacts(ref=ref))
    parentless = WorkFacts(ref=ref, record={"title": "Plain", "state": "open"})
    assert not await work_view._reread_tree_issue(app, ref, parentless)
    assert asked[-1] == [("eerovil/agentdeck", 10)] and len(asked) == 2
