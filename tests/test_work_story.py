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
from agentdeck.poller_ops import BoardScope, BoardView
from agentdeck.triage import CAUSE_PENDING_INTERACTION, AssistantInsight, AssistantView
from agentdeck.work import WorkRef
from agentdeck.work_facts import OutputFact, WorkFacts
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
    DONE,
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


def _work(*, pull: bool = False, **fields):
    """A Work card; ``pull`` gives it its one pull request (``PR``), state unknown."""
    if pull:
        fields.setdefault(
            "facts",
            WorkFacts(
                WORK, outputs=(OutputFact(PR, "eerovil/agentdeck", 2300, "chat_registration"),)
            ),
        )
    return build_work_card(WORK, issue=None, title="Work story", **fields)


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

    work = _work(pull=True)
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


def _with(work, *, verification: str = ""):
    """The same Work card, with its newest verification set by hand."""
    from dataclasses import replace

    fields = {}
    if verification:
        fields["verification"] = VerificationSummary(
            verification, f"Verification {verification}", "job-1", "gen-1",
            (("eerovil/agentdeck", "abc1234"),),
        )
    return replace(work, **fields)


def test_the_milestones_read_implement_review_verify_merge():
    status = work_status(_work(pull=True))

    assert [step.key for step in status.milestones] == [
        "implement", "review", "verify", "merge",
    ]
    assert [step.label for step in status.milestones] == [
        "Implement", "Review", "Verify", "Merge",
    ]
    assert (status.headline, status.summary) == ("Idle", "Nothing is running.")
    assert _steps(status)["implement"] == ("idle", "Not started")
    assert _steps(status)["review"] == ("idle", "Not yet")


@pytest.mark.parametrize("running", ["running", "queued"])
def test_a_running_verification_reads_verifying(running):
    status = work_status(_with(_work(pull=True), verification=running))

    assert (status.tone, status.headline) == (ACTIVE, "Verifying")
    assert status.summary == f"Verification {running}."
    assert _steps(status)["verify"] == ("active", running.capitalize())


def test_a_stale_run_is_waiting_until_the_work_is_done():
    """The suite is due on the head a review passes, so an older run is simply not due yet."""
    from dataclasses import replace

    work = _with(_work(pull=True), verification="stale")
    assert _steps(work_status(work))["verify"] == ("waiting", "After review")
    done = work_status(replace(work, work_state="done"))
    assert _steps(done)["verify"] == ("problem", "Stale")


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


def test_a_chats_question_is_the_one_strong_attention_state():
    ask = _ask(question="Should the   migration\nrun first?")
    status = work_status(_work(pull=True, attention=(ask,)))

    assert (status.tone, status.headline) == (ATTENTION, "Needs your answer")
    assert status.summary == "Worker chat asked: Should the migration run first?"
    assert (status.next, status.you) == ("your answer", "Answer the question")
    assert (status.kind, status.where, status.where_href) == (
        CHAT_QUESTION,
        "Worker chat",
        "/chats/w",
    )


def test_a_long_question_is_shortened_and_a_textless_one_still_asks():
    status = work_status(_work(pull=True, attention=(_ask(question="why " * 80),)))
    assert status.summary.endswith("…") and len(status.summary) < 200
    status = work_status(_work(pull=True, attention=(_ask(),)))
    assert status.summary == "Worker chat is waiting for your answer."


def test_mixed_attention_keeps_every_kind_in_its_own_words():
    """A question, an approval and a stopped chat at once (#2225 review)."""
    attention = (_stopped("c:a:other", "Other"), _approval(), _ask())
    status = work_status(_work(pull=True, attention=attention))

    # The question leads, deterministically; the others are follow-ups, never "more questions".
    assert (status.headline, status.kind, status.where) == (
        "Needs your answer",
        CHAT_QUESTION,
        "Worker chat",
    )
    assert status.summary == "Worker chat is waiting for your answer."
    assert status.detail == "Also waiting on its own page: Approver. Also stopped: Other."
    assert status.you == "Answer the question"


def test_only_other_questions_count_as_more_questions():
    two = (_ask(), _stopped("c:a:other", "Other"), _ask("c:a:second", "Second"))
    status = work_status(_work(pull=True, attention=two))

    assert status.summary == "Worker chat (and 1 more question) is waiting for your answer."
    assert status.detail == "Also stopped: Other."


def test_an_approval_leads_when_nothing_asks_and_names_the_rest():
    attention = (
        _stopped("c:a:other", "Other"),
        _approval(),
        _approval("c:a:b", "Browser", "mcp_url"),
    )
    status = work_status(_work(pull=True, attention=attention))

    assert (status.headline, status.kind) == ("Chat is waiting on you", CHAT_ACTION)
    assert status.summary == "Approver is waiting on: Allow rm?"
    assert status.detail == "Also waiting on its own page: Browser. Also stopped: Other."
    assert status.you == "Approve or decline on the chat's page"
    assert "answer" not in (status.headline + status.summary + status.you).lower()


def test_a_finished_handoff_alone_needs_nobody():
    status = work_status(_work(pull=True, attention=(_stopped(state="finished"),)))
    assert status.tone != ATTENTION and status.kind == ""


def test_a_needs_you_wake_on_tells_the_question_and_knows_the_work_s_own_chats():
    """#3446: one waiting question leaves the row's words empty; the wake still quotes it,
    and every leg of the Work's chats maps to the durable chat a registration is keyed by."""
    from types import SimpleNamespace

    from agentdeck.web.work_view import needs_you_stops
    from agentdeck.work_story import FrontSections, front_row

    ask = _ask("claude_code:alt:leg2", question="Which colour?")
    row = front_row(_work(pull=True, attention=(ask,)), current=True)
    assert row.reason == "" and row.attention.label == "Needs answer"
    overview = SimpleNamespace(front=FrontSections(needs_you=(row,)))
    [stop] = needs_you_stops(overview, identity=lambda key: f"chat-of:{key.rsplit(':', 1)[-1]}")
    assert stop.reason == "Which colour?" and stop.issue == row.card.source_label
    assert stop.chats == ("chat-of:leg2",) and stop.stopped_at == 0.0


def test_the_front_page_answers_only_a_real_question():
    from agentdeck.work_story import NEEDS_YOU, front_row

    question = front_row(_work(pull=True, attention=(_ask(),)), current=True)
    assert (question.section, question.action) == (NEEDS_YOU, "Answer")
    for attention, reason in (
        ((_approval(),), "Waiting on a chat's own page"),
        ((_stopped(),), "A chat stopped"),
    ):
        row = front_row(_work(pull=True, attention=attention), current=True)
        assert (row.section, row.action, row.reason) == (NEEDS_YOU, "Open", reason)
        assert "Worker chat" not in row.reason and "Approver" not in row.reason


def test_a_chat_that_only_stopped_is_a_look_not_a_question():
    status = work_status(_work(pull=True, attention=(_stopped(),)))

    assert (status.tone, status.headline, status.kind) == (
        ATTENTION,
        "Chat needs a look",
        CHAT_STOPPED,
    )
    assert status.summary == "Worker chat stopped; it has not asked you anything."
    assert status.detail == "Deckhand says: waiting."
    assert status.you == "Look at the chat" and status.where_href == "/chats/w"


def test_a_done_work_is_settled_and_names_what_merged():
    from dataclasses import replace

    merged = OutputFact(PR, "eerovil/agentdeck", 2300, "chat_registration", "merged")
    work = replace(_work(pull=True), work_state="done", outputs=(merged,))
    status = work_status(work)

    assert (status.tone, status.headline, status.next) == (DONE, "Done", "")
    assert status.detail == "Merged eerovil/agentdeck#2300."
    assert _steps(status)["merge"] == ("done", "Merged")


def test_every_state_carries_a_text_mark_not_just_a_colour():
    status = work_status(_work(pull=True))
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
    card = _work(pull=True, attention=(ask,))

    target = message_target(card, (asker, runner), lambda key: True)

    assert target is not None and (target.session_key, target.answer) == ("c:a:asker", True)


def test_a_stopped_chat_takes_the_message_unless_another_asks():
    stopped, asker = _chat("c:a:stopped", "Stopped"), _chat("c:a:asker", "Asker", minutes=30)
    card = _work(
        pull=True,
        attention=(WorkAttention("c:a:stopped", "Stopped", "stalled", stopped.href),),
    )

    target = message_target(card, (stopped, asker), lambda key: True)
    assert target is not None and (target.session_key, target.answer) == ("c:a:stopped", False)
    assert target.why == "it stopped and is waiting for a look"

    # With a real question as well, the asking chat takes it, as an answer.
    both = _work(
        pull=True,
        attention=(
            WorkAttention("c:a:stopped", "Stopped", "stalled", stopped.href),
            WorkAttention("c:a:asker", "Asker", "waiting", asker.href, asks=True),
        ),
    )
    target = message_target(both, (stopped, asker), lambda key: True)
    assert target is not None and (target.session_key, target.answer) == ("c:a:asker", True)


@pytest.mark.parametrize("state", ["finished", "merged", "closed"])
def test_a_chat_that_handed_off_is_not_said_to_have_stopped(state):
    """abacus#1951 (#2327): a finished turn is a handoff, as the status already reads it."""
    chat = _chat("c:a:done", "Done")
    card = _work(
        pull=True, attention=(WorkAttention("c:a:done", "Done", state, chat.href),)
    )

    target = message_target(card, (chat,), lambda key: True)

    assert target is not None and target.session_key == "c:a:done"
    assert target.why == "it is this Work's latest chat"


def test_a_chat_that_restarts_by_itself_is_not_said_to_have_stopped():
    chat = _chat("c:a:worker", "Worker")
    item = WorkAttention("c:a:worker", "Worker", "stalled", chat.href, recovering=True)

    target = message_target(_work(pull=True, attention=(item,)), (chat,), lambda key: True)

    assert target is not None and target.why == "it is this Work's latest chat"


def test_otherwise_a_working_chat_takes_it_then_the_newest():
    old, newer = _chat("c:a:old", "Old", minutes=30), _chat("c:a:new", "Newer", minutes=1)
    working = _chat("c:a:busy", "Busy", working=True, minutes=60)

    target = message_target(_work(pull=True), (old, newer), lambda key: True)
    assert target is not None and target.session_key == "c:a:new" and not target.answer
    target = message_target(_work(pull=True), (old, newer, working), lambda key: True)
    assert target is not None and target.session_key == "c:a:busy"


def test_a_chat_that_cannot_take_a_message_is_skipped_and_none_left_is_none():
    first, second = _chat("c:a:one", "One", minutes=1), _chat("c:a:two", "Two", minutes=9)
    card = _work(pull=True)

    target = message_target(card, (first, second), lambda key: key == "c:a:two")
    assert target is not None and target.session_key == "c:a:two"
    assert message_target(card, (first, second), lambda key: False) is None


# --- the page and its message route ---------------------------------------------------------


def _app(tmp_path, *, injectable=True):
    from test_web import _app_with_state

    app = _app_with_state(tmp_path)
    board = BoardView(scopes=(BoardScope("agentdeck", org="eerovil"),))
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
    app = _app(tmp_path)
    app.state.assistant.config.enabled = True
    app.state.assistant.view = _insight(cause=CAUSE_PENDING_INTERACTION)

    status, box = _status_and_box(app)

    assert 'data-wd-attention-kind="chat_action"' in status
    assert "Chat is waiting on you" in status
    assert "Worker chat is waiting on: Allow command?" in status
    assert act in status
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

    app = _app(tmp_path)
    app.state.app_state.sessions["claude_code:test:worker"].cwd = tmp_path

    async def fake_inject(account, session, message, *, timeout_s, allow_over_budget=False):
        return InjectResult(True)

    posted = []

    async def run_once(argv, *, timeout, stdin=None, repo=None, owner=None):
        posted.append((argv[3], repo, stdin.decode()))
        return 0, b"https://github.com/eerovil/agentdeck/issues/2218#issuecomment-1\n", b""

    monkeypatch.setattr(PROVIDERS["claude_code"], "inject", fake_inject)
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

    app = _app(tmp_path)
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


# --- a ChatGPT review opened by hand (#2411) -------------------------------------------------

HEAD = "537208c4d207fc616c052a3100f78e33a9a68acf"


def _finished(chat_reviews=None):
    """bazaar#327: the issue closed, and the merged pull request is the result."""
    merged = OutputFact(PR, "eerovil/agentdeck", 2300, "chat_registration", "merged", HEAD)
    facts = WorkFacts(WORK, outputs=(merged,), finished_at=NOW)
    return build_work_card(
        WORK, issue=None, title="Work story", issue_state="closed", facts=facts,
        chat_reviews=chat_reviews,
    )


def test_a_finished_work_asks_for_no_review():
    status = work_status(_finished())
    assert status.headline == "Done"
    assert _steps(status)["review"] == ("idle", "None")


def test_a_finished_work_says_chatgpt_reviewed_what_merged():
    reviewed = {("eerovil/agentdeck", 2300): ("0000000", HEAD[:7].upper())}
    assert _steps(work_status(_finished(reviewed)))["review"] == ("done", "ChatGPT reviewed")
    # A review of a commit the pull request moved past, or of another pull request, is not it.
    for other in (
        {("eerovil/agentdeck", 2300): ("def5678",)},
        {("eerovil/agentdeck", 2301): (HEAD,)},
        {("other/agentdeck", 2300): (HEAD,)},
    ):
        assert _steps(work_status(_finished(other)))["review"] == ("idle", "None")


def test_an_open_work_with_no_review_in_play_says_chatgpt_reviewed():
    output = OutputFact(PR, "eerovil/agentdeck", 2300, "chat_registration", "open", HEAD)
    facts = WorkFacts(WORK, outputs=(output,))

    def review(reviews):
        work = build_work_card(
            WORK, issue=None, title="Work story", facts=facts, chat_reviews=reviews
        )
        return _steps(work_status(work))["review"]

    assert review({("eerovil/agentdeck", 2300): (HEAD,)}) == ("done", "ChatGPT reviewed")
    assert review({}) == ("idle", "Not yet")
    # A head the deck never learned is not the reviewed one.
    blank = OutputFact(PR, "eerovil/agentdeck", 2300, "chat_registration", "open", "")
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


def test_a_starred_work_leads_its_section():
    """#3116: a starred Work comes first in Active (and Needs you), ahead of newer ones."""
    from dataclasses import replace

    base = _work(pull=True, chats=(_chat("c:a:worker", "Worker", working=True),))
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


def _woken(key: str, title: str) -> WorkChat:
    """A chat working on a turn only its own background notification opened (#2760)."""
    from dataclasses import replace

    return replace(_chat(key, title, working=True), background=True)


def test_a_chat_its_own_notification_woke_keeps_the_work_in_needs_you():
    """#2760: a render watcher woke the chat; nobody answered, so the Work still waits."""
    from agentdeck.work_story import NEEDS_YOU, front_row

    work = _work(
        pull=True, attention=(_stopped(),), chats=(_woken("c:a:worker", "Worker"),)
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
        pull=True, attention=(_stopped(),), chats=(_woken("c:a:worker", "Worker"),)
    )
    row = front_row(work, current=True)
    detail = WorkDetail(work, None, (), ())

    assert detail.attention == row.attention and detail.attention is not None
    assert detail.buttons
    writes = [action for action in row.attention.actions if action in work_actions.WRITES]
    for action in writes:
        assert work_actions.plan(action, detail.card, detail.attention) is not None


def test_a_closed_issue_with_an_open_pull_request_follows_the_pull_request():
    """#3187: an issue another chat closed while its pull request is still open."""
    from dataclasses import replace

    from agentdeck.work_story import NEEDS_YOU, front_row

    opened = OutputFact("https://github.com/o/r/pull/9", "o/r", 9, "registered", state="open")
    closed = replace(_work(pull=True), work_state="done", awaiting_finish=True)
    for attention, headline in (((), "Idle"), ((_ask(),), "Needs your answer")):
        work = replace(closed, outputs=(opened,), attention=attention)
        status = work_status(work)
        assert status.headline == headline and status.summary.endswith(" · issue closed.")
        row = front_row(work, current=True).attention
        assert row is None or row.category != "issue_closed"
    # No pull request open: unchanged.
    assert work_status(closed).headline == "Issue closed"
    assert front_row(closed, current=True).section == NEEDS_YOU


def test_a_turn_someone_started_still_takes_the_work_out_of_needs_you():
    from agentdeck.work_story import IN_PROGRESS, front_row

    stopped = (_stopped(),)
    answered = _work(
        pull=True, attention=stopped, chats=(_chat("c:a:worker", "Worker", working=True),)
    )
    mixed = _work(
        pull=True,
        attention=stopped,
        chats=(_woken("c:a:worker", "Worker"), _chat("c:a:other", "Other", working=True)),
    )

    assert not answered.woke_itself and not mixed.woke_itself
    assert front_row(answered, current=True).section == IN_PROGRESS
    assert front_row(mixed, current=True).section == IN_PROGRESS


def test_a_woken_chat_on_a_work_nobody_waits_on_stays_active():
    from agentdeck.work_story import IN_PROGRESS, front_row

    work = _work(pull=True, chats=(_woken("c:a:worker", "Worker"),))

    assert work.woke_itself
    assert front_row(work, current=True).section == IN_PROGRESS


def test_a_job_a_chat_queued_is_what_the_work_waits_for():
    from agentdeck.work_overview import work_jobs
    from agentdeck.work_story import WAITING_JOB

    chat = _chat("c:a:worker", "Worker")
    jobs = work_jobs([_queue_row(chat.session_key)], (chat,))
    work = _work(pull=True, chats=(chat,), jobs=jobs)
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
    status = work_status(_work(pull=True, chats=(chat,), jobs=jobs))

    assert [job.state for job in jobs] == ["running", "queued"]
    assert status.headline == "Job running"
    assert re.fullmatch("x" * 39 + r"… · running \d+ min \(\+1 more\)\.", status.summary)


def test_a_chat_mid_turn_reads_working_whatever_job_it_queued():
    from agentdeck.work_overview import work_jobs

    chat = _chat("c:a:worker", "Worker", working=True)
    jobs = work_jobs([_queue_row(chat.session_key)], (chat,))
    work = _work(pull=True, chats=(chat,), jobs=jobs)

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
    work = _work(pull=True, chats=chats)
    assert work_status(work).headline == "Working"

    assert work_status(replace(work, paused=True, label_paused=True)).headline == "Paused"
    assert work_status(replace(work, work_state="done")).headline == "Done"


def test_a_turn_only_reading_adds_nothing_to_working():
    """#2590: Idle says only what the turn already does."""
    work = _work(pull=True, chats=(_chat("c:a:worker", "Worker", working=True),))
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
    quiet = _work()
    working = _work(pull=True, chats=(_chat("c:a:worker", "Worker", working=True),))
    asking = _work(pull=True, attention=(_ask(),))
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
    detail = WorkDetail(card=rows[10].card, workspace=None, chats=(),
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
    plain = WorkDetail(card=rows[10].card, workspace=None, chats=(),
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
    detail = WorkDetail(card=rows[10].card, workspace=None, chats=(),
                        verifications=(), tree=tree)
    section = dict(render_work_sections(templates, detail))["tree"]
    assert "Map" in section and "1/3 done" in section and "data-work-tree-child" not in section
    assert 'href="/works/github_issue:eerovil%2Fagentdeck%2312"' in section
    assert section.count("data-wm-box") == 5 and "wm-box wm-fog" in section
    assert "data-wayfinder-map-partial" in section and "Destination" in section


@pytest.mark.asyncio
async def test_a_maps_page_draws_held_reads_at_once_and_refreshes_behind_it():
    """#3739: a map's held rows are drawn even when out of date, so opening it never waits
    on GitHub; one re-read per Work runs behind the page and redraws open pages only when
    GitHub's answer moved. A read with nothing held still waits."""
    import asyncio
    from types import SimpleNamespace

    from agentdeck import wayfinder_map as wm
    from agentdeck.web import work_view

    item = {"repository": "eerovil/agentdeck", "number": 11, "title": "Old", "state": "open"}
    held = {"sub_issues": ((item,), False), "issue_body": ("## Destination\n\nShip.", True),
            "blocked_by": None}
    fetched = {"sub_issues": ({**item, "title": "New"},), "issue_body": "## Destination\n\nShip.",
               "blocked_by": ()}
    # The page's own read waits on ``gate``; the re-read behind it on ``behind``.
    calls, gate, behind = [], asyncio.Event(), asyncio.Event()

    def holder(name):
        return lambda *args: held[name] or (None, False)

    def reader(name):
        async def read(*args):
            calls.append(name)
            await (behind if name == "sub_issues" else gate).wait()
            return fetched[name]

        return read

    issues = SimpleNamespace(
        **{f"held_{name}": holder(name) for name in fetched},
        **{name: reader(name) for name in fetched},
        cached_issues=lambda refs: {},
    )
    published = []
    app = SimpleNamespace(state=SimpleNamespace(
        issues=issues, work_overview=None, control_db=None,
        app_state=SimpleNamespace(bus=SimpleNamespace(publish=published.append)),
    ))
    ref = WorkRef("eerovil/agentdeck", 10)
    record = {"labels": ["wayfinder:map"], "subIssues": {"total": 1, "completed": 0}}

    # Nothing held for #11's "blocked by": that read waits; the rest come from the cache.
    pending = asyncio.ensure_future(work_view._work_tree(app, ref, record))
    for _ in range(5):
        await asyncio.sleep(0)
    assert calls == ["blocked_by"] and not pending.done()
    gate.set()
    tree = await pending
    assert tree.children[0].title == "Old" and tree.diagram is not None
    assert not tree.diagram.partial
    held["blocked_by"] = ((), True)  # the cache holds what that read stored

    generation = wm.generation()
    task = app.state.work_tree_refreshes[ref.work_id]
    await work_view._work_tree(app, ref, record)  # drawn again at once: no second refresh
    assert app.state.work_tree_refreshes[ref.work_id] is task and "sub_issues" in calls
    behind.set()
    await task
    assert wm.generation() == generation + 1 and published == ["works"]

    # A re-read that finds nothing new redraws nothing.
    fetched["sub_issues"] = (item,)
    await work_view._work_tree(app, ref, record)
    await app.state.work_tree_refreshes.get(ref.work_id, task)
    assert wm.generation() == generation + 1 and published == ["works"]


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
                         question=("Lists: which to keep. Which lists does the deck keep?\n"
                                   "What I found:\n- The deck keeps three lists. - Two are "
                                   "never read."), interaction="question",
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
    # Only the sentence that asks, not what the agent found (#3736).
    assert asked.line == "Lists: Which lists does the deck keep?"
    assert asked.href == "/chats/t11#pending-interaction" and asked.title == "Decide lists"
    # A ticket the owner hid keeps its own row for the Hidden list: listed, so not blocked.
    put = work_tree.fold(list(rows.values()), parents=parents, counts={map_id: (1, 5)},
                         hidden={ticket})
    shown = next(item for item in put if item.card.work_id == map_id)
    assert shown.progress == "1/5 done · 1 working · 1 blocked"
    # A ticket no agent started waits on the open ones, not on the owner (#3736): it neither
    # leads the row nor counts as Not started, until it is all that is left.
    unstarted = front_row(replace(rows[13].card, start_label="agent"), current=False)
    assert unstarted.attention.category == "unstarted"
    held = {**rows, 13: unstarted}
    [row13] = work_tree.fold(list(held.values()), parents=parents, counts={map_id: (1, 5)})
    assert row13.lead.card.work_id == ticket
    assert row13.progress == "1/5 done · 1 asks you · 1 working · 2 blocked"
    # Nor is it a Needs you of its own: no top-bar entry, push or needs-you wake.
    assert [item.card.work_id for item in front_sections([row13]).needs_you_works] == [ticket]
    quiet = {10: rows[10], 13: unstarted}
    [alone] = work_tree.fold(list(quiet.values()), parents=parents, counts={map_id: (1, 5)})
    assert alone.lead.card.work_id == unstarted.card.work_id
    # A deck-run ticket asking in its own chat, with no Deckhand card, asks too.
    from agentdeck.work_derived import QUESTION, DerivedStatus

    chat_asks = front_row(replace(rows[13].card, derived=DerivedStatus(
        QUESTION, "Needs you — Which?", summary="Which?", chat_href="/chats/t12")),
        current=True)
    [derived] = work_tree.fold([rows[10], chat_asks], parents=parents, counts={map_id: (1, 5)})
    assert [(q.line, q.href) for q in derived.map.questions] == [
        ("Which?", "/chats/t12#pending-interaction")
    ]

    templates = Jinja2Templates(directory=str(app_mod._TEMPLATES_DIR))
    render_mod.register_filters(templates)
    overview = WorkOverview(front=front_sections([row]))
    html = render_work_front(templates, overview, "needs")
    assert "Make wayfinder great" in html and "Issue 10" not in html
    assert "Lists: Which lists does the deck keep?" in html and "never read" not in html
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
        return [{"repository": "eerovil/agentdeck", "number": 11}] if number == 13 else []

    async def issue_body(repository, number):
        return "## Destination\n\nShip it.\n"

    issues = SimpleNamespace(blocked_by=blocked_by, issue_body=issue_body,
                             cached_issues=lambda refs: {})
    page_app = SimpleNamespace(state=SimpleNamespace(issues=issues, work_overview=None))
    items = [{"repository": "eerovil/agentdeck", "number": n, "title": f"Issue {n}",
              "state": "open", "labels": ["wayfinder:research"]} for n in (11, 12, 13)]
    diagram = await work_view._wayfinder_diagram(
        page_app, WorkRef("eerovil/agentdeck", 10), {"labels": ["wayfinder:map"]}, items,
        {ticket: rows[11], unstarted.card.work_id: unstarted},
    )
    box = {item.key.rsplit("#", 1)[-1]: item for item in diagram.boxes}
    # Not started, and blocked by #11's open question: it waits, it does not need you.
    assert unstarted.section == "needs_you" and box["13"].state == "waiting"
    assert " ".join(box["11"].lines) == "Asks: Lists · Issue 11"
    assert box["11"].href == asked.href and box["12"].href.startswith("/works/")
    assert [item.work_id for item in diagram.questions] == [ticket]
    tree = work_tree.WorkTree(counts=(1, 5), diagram=diagram)
    detail = WorkDetail(card=rows[10].card, workspace=None, chats=(),
                        verifications=(), tree=tree)
    section = dict(work_view.render_work_sections(templates, detail))["tree"]
    assert "Waiting for you" in section and f'data-wm-question="{ticket}"' in section
    assert asked.line in section and "never read" not in section
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
        rows[10].status, kind="waiting_job"))
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
