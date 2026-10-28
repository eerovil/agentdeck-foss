"""Work is the dashboard's primary object (#2147).

One card per Work however many chats or legs it has; attention, workspace, verification and
outputs at Work level; chats and sessions kept reachable but secondary; activity no Work owns
kept visible.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from public_copy import private_feature

from agentdeck import chat_review as _chat_review
from agentdeck.models import (
    BACKGROUND_TASK_ACTIVITY,
    IssueRef,
    PullRequestRef,
    Session,
    SessionStatus,
)
from agentdeck.poller_engine.workspace import WorkspaceSnapshot
from agentdeck.poller_ops import BoardScope, BoardView
from agentdeck.triage import AssistantInsight, AssistantView
from agentdeck.work import WorkRef
from agentdeck.work_overview import (
    VerificationSummary,
    attribute_insights,
    build_work_card,
    compact_workspace,
    summarize_verification,
    work_href,
)
from agentdeck.work_workspace import (
    RepositoryRole,
    RunVerification,
    WorkPullRequest,
    WorkRepositoryContext,
    WorkspaceEvidence,
    WorkspaceProvenance,
    WorkWorkspace,
)
from agentdeck.work_workspace_claims import WorkspaceClaim

WORK = WorkRef("eerovil/agentdeck", 2147)
OTHER = WorkRef("eerovil/agentdeck", 2150)
UNSTARTED = WorkRef("eerovil/agentdeck", 2160)  # an open issue no agent works on yet
NOW = datetime.now(UTC)
_EMPTY = hashlib.sha256(b"").hexdigest()


def _session(key: str, **fields) -> Session:
    account, _, session_id = key.rpartition(":")
    fields.setdefault("status", SessionStatus.IDLE)
    fields.setdefault("last_activity", NOW - timedelta(minutes=5))
    return Session(key=key, account_key=account, session_id=session_id, **fields)


def _board() -> BoardView:
    return BoardView(scopes=(BoardScope("agentdeck", org="eerovil"),))


def _app(tmp_path, *, board: BoardView | None = None):
    from test_web import _app_with_state

    app = _app_with_state(tmp_path)
    app.state.issues.read_board = lambda: board or BoardView()
    # No issue record unless a test gives one: the metadata cache is the host's own.
    app.state.issues.cached_issues = lambda refs: {}
    return app


def _closed(app, when_by_number: dict, *, updated: dict | None = None) -> None:
    """Cached issue records for closed issues: ``closedAt`` is GitHub's close time, and
    ``updatedAt`` may be later, as a comment on a closed issue makes it."""
    updated = updated or {}

    def lookup(refs):
        return {
            (repository, number): {
                "title": f"Issue {number}",
                "state": "closed",
                "is_pr": False,
                "closedAt": when_by_number[number].isoformat(),
                "updatedAt": updated.get(number, when_by_number[number]).isoformat(),
            }
            for repository, number in refs
            if number in when_by_number
        }

    app.state.issues.cached_issues = lookup


def _add(app, key: str, *issues: WorkRef, **fields) -> str:
    state = app.state.app_state
    state.update_session(_session(key, **fields))
    if issues:
        state.set_issues(key, tuple(IssueRef(w.repository, w.number) for w in issues))
    return key


def _insight(key: str, kind: str = "needs_input") -> AssistantInsight:
    return AssistantInsight(session_key=key, kind=kind, headline=f"{key} needs you")


def _attention(app, *insights: AssistantInsight) -> None:
    app.state.assistant.config.enabled = True
    app.state.assistant.view = AssistantView(state="ready", insights=tuple(insights))


def _snap(repository: str, head: str) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        root=f"/w/{repository.rsplit('/', 1)[-1]}",
        head=head,
        staged_sha256=_EMPTY,
        tracked_diff_sha256=_EMPTY,
        untracked_sha256=_EMPTY,
        repository=repository,
    )


def _check(run_id: str, *, verified: bool, head: str = "a" * 40, state: str = "done"):
    return RunVerification(
        job_id=f"job-{run_id}-{head[:3]}",
        run_id=run_id,
        state=state,
        verified=verified,
        tested=(("eerovil/agentdeck", _snap("eerovil/agentdeck", head)),),
    )


def _workspace(*heads: str, pulls=()) -> WorkWorkspace:
    return WorkWorkspace(
        work_id=WORK.work_id,
        repositories=(
            WorkRepositoryContext(
                repository="eerovil/agentdeck",
                roles=(RepositoryRole.SOURCE,),
                heads=heads,
                pull_requests=tuple(pulls),
            ),
        ),
    )


async def _overview(app):
    from agentdeck.web.work_view import load_work_overview

    return await load_work_overview(app, now=NOW)


def _card_of(overview, work: WorkRef):
    return next(card for card in overview.cards if card.work_id == work.work_id)


# --- one card per Work ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_work_with_many_chats_is_one_card(tmp_path):
    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", WORK, thinking=True)
    _add(app, "codex:test:review", WORK)
    _add(app, "claude_code:test:helper", WORK)

    overview = await _overview(app)

    cards = [card for card in overview.cards if card.work_id == WORK.work_id]
    assert len(cards) == 1
    assert (cards[0].chat_count, cards[0].working_count) == (3, 1)
    assert cards[0].href == "/works/github_issue:eerovil%2Fagentdeck%232147"


@pytest.mark.asyncio
async def test_a_command_left_running_in_the_background_is_not_the_work_working(tmp_path):
    """#2783: a dev server a chat started keeps the chat "Background task", never the Work."""
    app = _app(tmp_path)
    _add(app, "claude_code:test:server", WORK, thinking=True, title="Dev server",
         activity=BACKGROUND_TASK_ACTIVITY)
    _add(app, "claude_code:test:worker", WORK, thinking=True, title="Worker",
         activity="Using tools")

    card = _card_of(await _overview(app), WORK)
    assert card.working_count == 1
    assert [title for title, _ in card.working_chats] == ["Worker"]

    app.state.app_state.update_session(
        _session("claude_code:test:worker", thinking=False)
    )
    assert _card_of(await _overview(app), WORK).working_count == 0


@pytest.mark.asyncio
async def test_a_helper_agent_keeps_the_work_working_beside_a_background_command(tmp_path):
    """#2783 review: the chat's own label names the dev server, but its helper agent works."""
    app = _app(tmp_path)
    parent = _add(app, "claude_code:test:server", WORK, thinking=True, title="Dev server",
                  activity=BACKGROUND_TASK_ACTIVITY, status=SessionStatus.LIVE)
    _add(app, "claude_code:test:helper", thinking=True, activity="Using tools",
         parent_session_key=parent, status=SessionStatus.LIVE)

    card = _card_of(await _overview(app), WORK)
    assert card.working_count == 1
    assert [title for title, _ in card.working_chats] == ["Dev server"]

    app.state.app_state.update_session(_session("claude_code:test:helper", thinking=False,
                                                parent_session_key=parent))
    assert _card_of(await _overview(app), WORK).working_count == 0


@pytest.mark.asyncio
async def test_without_a_cached_issue_the_title_is_the_dispatched_chats(tmp_path):
    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", title="Promote Work to the UI",
         issue_url="https://github.com/eerovil/agentdeck/issues/2147")
    _add(app, "codex:test:helper", WORK, title="A helper", last_activity=NOW)

    assert _card_of(await _overview(app), WORK).title == "Promote Work to the UI"


@pytest.mark.asyncio
async def test_one_chat_in_two_works_shows_on_both_but_owns_neither_attention(tmp_path):
    app = _app(tmp_path)
    shared = _add(app, "claude_code:test:shared", WORK, OTHER)
    _attention(app, _insight(shared))

    overview = await _overview(app)

    assert {card.work_id for card in overview.cards} == {WORK.work_id, OTHER.work_id}
    # The chat cannot say which Work its problem is about, so neither card claims it.
    assert _card_of(overview, WORK).attention == ()
    assert _card_of(overview, OTHER).attention == ()
    projection = app.state.app_state.work_projection()
    assert attribute_insights([_insight(shared)], projection, {}) == {}


@pytest.mark.asyncio
async def test_a_job_its_chat_queued_lists_the_work_under_waiting(tmp_path, monkeypatch):
    """#2573: the live queue reaches the card through the chat that submitted the job; a
    verification job (which reads as Verifying on its own Work), a finished one or another
    chat's does not."""
    from agentdeck.web import work_view

    app = _app(tmp_path, board=_board())
    mine = _add(app, "claude_code:test:mine", WORK, title="Implement it")
    _add(app, "claude_code:test:other", OTHER, title="Something else")
    stamp = NOW.timestamp()
    queue = [
        {"id": "j1", "state": "queued", "session_key": mine, "command": ["pytest", "-q"],
         "submitted_at": stamp - 300, "purpose": ""},
        {"id": "j2", "state": "running", "session_key": mine, "command": ["pytest"],
         "purpose": "verification", "work_id": OTHER.work_id, "run_id": "g"},
        {"id": "j3", "state": "done", "session_key": "claude_code:test:other",
         "command": ["pytest"], "purpose": ""},
    ]

    async def live_queue(_app):
        return queue

    async def with_evidence(_app, _work_ids, *, queue=None):
        return list(queue if queue is not None else await live_queue(_app))

    # No Runtime here: the queue is what both the classification and the hydration read.
    monkeypatch.setattr(work_view, "runtime_queue", live_queue)
    monkeypatch.setattr(work_view, "runtime_jobs", with_evidence)

    async def current(_app, _queue):
        # Its turn ended: something current keeps a Work the deck does not run itself listed.
        return frozenset({WORK.work_id})

    monkeypatch.setattr(work_view, "current_work_ids", current)
    overview = await _overview(app)

    work = _card_of(overview, WORK)
    assert [(job.state, job.label) for job in work.jobs] == [("queued", "pytest -q")]
    assert _card_of(overview, OTHER).jobs == ()
    assert WORK.work_id in {row.card.work_id for row in overview.front.waiting}
    row = next(row for row in overview.front.waiting if row.card.work_id == WORK.work_id)
    assert row.status.headline == "Job queued"


@pytest.mark.asyncio
async def test_a_pass_reads_the_deckhand_states_once_for_every_card(tmp_path, monkeypatch):
    """#2841: the states are the same for every Work, so a pass walks the insights once —
    not once per card — and each card still gets its own chat's attention."""
    from agentdeck.web import render

    app = _app(tmp_path, board=_board())
    mine = _add(app, "claude_code:test:mine", WORK, title="Implement it")
    theirs = _add(app, "claude_code:test:theirs", OTHER, title="Something else")
    _add(app, "claude_code:test:third", WorkRef("eerovil/agentdeck", 2151), title="Third")
    _attention(app, _insight(mine, "waiting"), _insight(theirs, "finished"))
    real = render.assistant_insight_states
    calls: list[int] = []

    def counting(assistant):
        calls.append(1)
        return real(assistant)

    monkeypatch.setattr(render, "assistant_insight_states", counting)
    overview = await _overview(app)

    assert len(calls) == 1
    assert [item.session_key for item in _card_of(overview, WORK).attention] == [mine]
    assert [item.session_key for item in _card_of(overview, OTHER).attention] == [theirs]


@pytest.mark.asyncio
async def test_a_pass_reads_files_and_databases_off_the_event_loop(tmp_path, monkeypatch):
    """#2841: the board, backlog and control-DB reads of a rebuild run in a thread, so they
    do not hold every page request behind them; the in-memory snapshot stays on the loop."""
    import threading

    from agentdeck.web import work_view

    app = _app(tmp_path, board=_board())
    _add(app, "claude_code:test:mine", WORK, title="Implement it")
    loop_thread = threading.get_ident()
    read_on: list[int] = []
    real = work_view._board_and_backlog

    def recording(app_):
        read_on.append(threading.get_ident())
        return real(app_)

    monkeypatch.setattr(work_view, "_board_and_backlog", recording)
    overview = await _overview(app)

    assert read_on and loop_thread not in read_on
    assert _card_of(overview, WORK).title == "Implement it"


@pytest.mark.asyncio
async def test_a_works_attention_is_one_item_naming_its_chats(tmp_path):
    app = _app(tmp_path)
    first = _add(app, "claude_code:test:first", WORK)
    second = _add(app, "codex:test:second", WORK)
    alone = _add(app, "claude_code:test:alone")
    insights = (_insight(first), _insight(alone), _insight(second, "stalled"))
    _attention(app, *insights)

    work = _card_of(await _overview(app), WORK)
    assert [item.session_key for item in work.attention] == [first, second]
    assert work.needs_attention


@pytest.mark.asyncio
async def test_a_works_chats_in_two_projects_are_still_one_item(tmp_path):
    from pathlib import Path

    app = _app(tmp_path)
    here = _add(app, "claude_code:test:here", WORK, cwd=Path("/srv/code/agentdeck"))
    # A helper the Work's chat delegated to, working in another project's checkout.
    there = _add(app, "claude_code:test:there", cwd=Path("/srv/code/bazaar"))
    app.state.app_state.mark_delegated_session(there, here)
    other = _add(app, "claude_code:test:other", cwd=Path("/srv/code/bazaar"))
    insights = (_insight(there), _insight(other), _insight(here))
    _attention(app, *insights)
    assert app.state.app_state.sessions[there].project_name != (
        app.state.app_state.sessions[here].project_name
    )

    # The Work owns both chats' attention whatever project each works in; the loose chat
    # is its own.
    work = _card_of(await _overview(app), WORK)
    assert [item.session_key for item in work.attention] == [there, here]
    assert other not in {item.session_key for item in work.attention}


# --- workspace, verification, outputs ---------------------------------------------------------


def _claim(repository: str, purpose: str = "code", **fields) -> WorkspaceClaim:
    fields.setdefault("confirmed_at", "2026-09-24T08:00:00Z")
    return WorkspaceClaim(
        WORK.work_id, repository, purpose, f"/w/{repository}-{purpose}", branch="agent/x", **fields
    )


def test_the_compact_workspace_is_multi_repository_from_the_claims():
    lines, note = compact_workspace(
        [
            _claim("eerovil/agentdeck"),
            _claim("eerovil/agent-evidence", "evidence", confirmed_at=""),
            _claim("eerovil/trailhead", last_error="clone failed"),
        ],
    )
    assert [(line.repository, line.state, line.problem) for line in lines] == [
        ("eerovil/agent-evidence", "reserved", False),
        ("eerovil/agentdeck", "claimed", False),
        ("eerovil/trailhead", "error", True),
    ]
    assert note == ""  # a card's own worktree list is not read (#2195)
    assert compact_workspace(None) == ((), "workspace claims could not be read")


@pytest.mark.asyncio
async def test_the_card_shows_every_claimed_repository(tmp_path, monkeypatch):
    import agentdeck.web.work_view as view

    app = _app(tmp_path)
    # Working, so it is a listed Active row: only listed rows read their claims (#2219).
    _add(app, "claude_code:test:worker", WORK, thinking=True)
    claims = [_claim("eerovil/agentdeck"), _claim("eerovil/trailhead", "code")]
    monkeypatch.setattr(view, "read_claims", lambda db, work_id: claims)

    work = _card_of(await _overview(app), WORK)

    assert [line.repository for line in work.workspace] == [
        "eerovil/agentdeck",
        "eerovil/trailhead",
    ]


def test_only_the_current_runs_verified_job_says_verified():
    current = summarize_verification([_check("gen-2", verified=True)], "gen-2")
    assert (current.status, current.label) == (
        "verified", f"Verified eerovil/agentdeck@{'a' * 7}",
    )

    # An earlier Run's green job never covers the current Run.
    earlier = summarize_verification([_check("gen-1", verified=True)], "gen-2")
    assert earlier.status == "earlier"
    assert "the current Run has not" in earlier.label

    # The current Run's own newest job wins, even when an older Run's job was green.
    ours = summarize_verification(
        [_check("gen-2", verified=False, state="running"), _check("gen-1", verified=True)], "gen-2"
    )
    assert ours.status == "running"

    unproven = summarize_verification([_check("gen-2", verified=False)], "gen-2")
    assert unproven.status == "unproven" and "could not be proven" in unproven.label
    assert summarize_verification([], "gen-2") is None


def test_a_verified_commit_the_checkout_has_moved_past_is_stale():
    moved = summarize_verification(
        [_check("gen-2", verified=True, head="a" * 40)], "gen-2", _workspace("b" * 40)
    )
    assert moved.status == "stale"
    assert moved.moved == (("eerovil/agentdeck", "b" * 7),)
    assert "has moved to eerovil/agentdeck@bbbbbbb" in moved.label

    same = summarize_verification(
        [_check("gen-2", verified=True, head="a" * 40)], "gen-2", _workspace("a" * 40)
    )
    assert same.status == "verified"
    # An uninspected checkout proves nothing either way; the label still names the commit.
    unknown = summarize_verification([_check("gen-2", verified=True)], "gen-2", _workspace())
    assert isinstance(unknown, VerificationSummary) and unknown.status == "verified"


def test_a_verified_commit_the_pull_request_has_moved_past_names_both_heads():
    """abacus#1951 (#2327): GitHub merged the base into the branch three times; the checkout
    stayed at the tested commit, so only the pull request says the head moved."""
    from agentdeck.work_facts import OutputFact

    url = "https://github.com/Eerovil/Agentdeck/pull/7"
    moved_on = OutputFact(url, "Eerovil/Agentdeck", 7, "card", "open", head="b" * 40)
    found = summarize_verification(
        [_check("gen-2", verified=True, head="a" * 40)], "gen-2", _workspace("a" * 40),
        (moved_on,),
    )
    assert found.status == "verified"
    # The repository as its pull request spells it, not the lowercased Work id.
    assert found.tested == (("Eerovil/Agentdeck", "a" * 7),)
    assert found.superseded == (("Eerovil/Agentdeck", "b" * 7),)
    assert found.label == (
        "Verified Eerovil/Agentdeck@aaaaaaa; the pull request's head is now "
        "Eerovil/Agentdeck@bbbbbbb"
    )

    for same in (OutputFact(url, "Eerovil/Agentdeck", 7, "card", "open", head="a" * 40),
                 OutputFact(url, "Eerovil/Agentdeck", 7, "card", "open", head="A" * 7),
                 OutputFact(url, "Eerovil/Agentdeck", 7, "card", "open", head=""),
                 OutputFact(url, "Eerovil/Agentdeck", 7, "card", "merged", head="b" * 40),
                 OutputFact(url, "Eerovil/other", 7, "card", "open", head="b" * 40),
                 OutputFact(url, "Eerovil/Agentdeck", 7, "card", "open", head="b" * 5)):
        plain = summarize_verification(
            [_check("gen-2", verified=True, head="a" * 40)], "gen-2", None, (same,)
        )
        assert (plain.superseded, plain.label.startswith("Verified ")) == ((), True), same
        assert "head is now" not in plain.label, same

    # Two open pull requests in the repository: one still on the tested commit is enough.
    both = summarize_verification(
        [_check("gen-2", verified=True, head="a" * 40)], "gen-2", None,
        (moved_on, OutputFact(url + "8", "Eerovil/Agentdeck", 8, "card", "open", head="a" * 40)),
    )
    assert both.superseded == ()

    # A moved checkout is still stale, and says so rather than naming the pull request.
    stale = summarize_verification(
        [_check("gen-2", verified=True, head="a" * 40)], "gen-2", _workspace("c" * 40),
        (moved_on,),
    )
    assert (stale.status, stale.superseded) == ("stale", ())


def test_outputs_are_the_works_facts_whether_or_not_a_workspace_was_inspected():
    """The Work page inspects the checkout and the front page never does, so an inspected
    workspace must not add outputs one page shows and the other cannot (#2229): its pull
    requests stay in the workspace's own section."""
    from agentdeck.work_facts import work_facts

    claimed = WorkPullRequest(
        "eerovil/agent-evidence", 7, "https://github.com/eerovil/agent-evidence/pull/7",
        provenance=(WorkspaceProvenance(WorkspaceEvidence.WORK_CLAIM, WORK.work_id),),
    )
    chat_only = WorkPullRequest(
        "eerovil/agentdeck", 9, "https://github.com/eerovil/agentdeck/pull/9",
        provenance=(WorkspaceProvenance(WorkspaceEvidence.CHAT_SESSION, "claude_code:test:x"),),
    )
    open_pulls = [
        {"url": "https://github.com/eerovil/agentdeck/pull/2200", "closes": [WORK.source_key]}
    ]
    facts = work_facts(WORK, open_pulls=open_pulls)
    inspected = build_work_card(
        WORK, issue=None, title="t", facts=facts, workspace=_workspace(pulls=(claimed, chat_only))
    )
    front = build_work_card(WORK, issue=None, title="t", facts=facts)
    assert [output.label for output in inspected.outputs] == ["eerovil/agentdeck#2200"]
    assert inspected.outputs == front.outputs


@pytest.mark.asyncio
async def test_a_chat_local_pull_request_is_the_chats_not_the_works(tmp_path):
    app = _app(tmp_path)
    shared = _add(app, "claude_code:test:shared", WORK, OTHER)
    app.state.app_state.set_pull_requests(shared, (PullRequestRef("eerovil/agentdeck", 9),))

    overview = await _overview(app)

    assert _card_of(overview, WORK).outputs == ()
    assert _card_of(overview, OTHER).outputs == ()
    from agentdeck.web.work_view import load_work_detail

    detail = await load_work_detail(app, WORK, workspace=False)
    (chat,) = detail.chats
    assert chat.pull_requests == ("https://github.com/eerovil/agentdeck/pull/9",)
    assert chat.other_works == 1


@pytest.mark.asyncio
async def test_the_card_reads_verification_per_work_and_names_the_newest_job(tmp_path):
    from test_run_workspace_evidence import _job as verified_job
    from test_run_workspace_evidence import _snap as evidence_snap

    app = _app(tmp_path, board=_board())
    _add(app, "claude_code:test:worker", WORK, thinking=True)
    asked: list[str] = []
    evidence = {WORK.work_id: [verified_job(evidence_snap("eerovil/agentdeck", "c" * 40),
                                            id="j-old", work_id=WORK.work_id, run_id="gen-1")]}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/job-evidence":
            work_id = request.url.params.get("work_id", "")
            asked.append(work_id)
            return httpx.Response(200, json={"evidence": evidence.get(work_id, [])})
        return httpx.Response(200, json={"jobs": []})

    app.state.runtime_http = httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://agentdeck-runtime"
    )
    work = _card_of(await _overview(app), WORK)
    # One evidence read, for this Work alone: never the whole ledger.
    assert asked == [WORK.work_id]
    assert work.verification.status == "earlier"

    evidence[WORK.work_id].append(verified_job(
        evidence_snap("eerovil/agentdeck", "d" * 40),
        id="j-new", work_id=WORK.work_id, run_id="gen-2", submitted_at=2.0,
    ))
    work = _card_of(await _overview(app), WORK)
    # No Run is current any more (#3741): the newest job speaks only for the commit it tested.
    assert (work.verification.status, work.verification.job_id) == ("earlier", "j-new")
    assert work.verification.label == "An earlier Run verified eerovil/agentdeck@ddddddd"


@pytest.mark.asyncio
async def test_the_work_page_reads_only_its_own_rows_of_the_queue(tmp_path):
    """The whole queue is over a megabyte of finished jobs; one Work's page asks for its own
    rows (#2837), and a Runtime from before the filter, which sends everything, gives the same."""
    from test_run_workspace_evidence import _job as verified_job
    from test_run_workspace_evidence import _snap as evidence_snap

    from agentdeck.web.work_view import load_work_detail, runtime_queue

    app = _app(tmp_path, board=_board())
    _add(app, "claude_code:test:worker", WORK)
    mine = verified_job(evidence_snap("eerovil/agentdeck", "c" * 40),
                        id="j-mine", work_id=WORK.work_id, run_id="gen-2")
    queue = [
        mine,
        {"id": "j-other", "state": "done", "work_id": "github_issue:eerovil/agentdeck#1"},
        {"id": "j-plain", "state": "failed", "work_id": ""},
        {"id": "j-run", "state": "done", "work_id": "", "run_id": "gen-2"},
        {"id": "j-busy", "state": "running", "work_id": ""},
    ]
    asked: list[str | None] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/job-evidence":
            return httpx.Response(200, json={"evidence": []})
        asked.append(request.url.params.get("work_id"))
        return httpx.Response(200, json={"jobs": queue})  # an old Runtime: no filter

    app.state.runtime_http = httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://agentdeck-runtime"
    )
    rows = await runtime_queue(app, work_id=WORK.work_id)
    assert [row["id"] for row in rows] == ["j-mine", "j-run", "j-busy"]

    asked.clear()
    detail = await load_work_detail(app, WORK, workspace=False)
    assert asked == [WORK.work_id]
    assert [check.job_id for check in detail.verifications] == ["j-mine"]


# --- activity no Work owns ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_manual_chat_stays_visible_outside_every_work(tmp_path):
    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", WORK)
    manual = _add(app, "claude_code:test:manual", thinking=True)
    stale = _add(app, "claude_code:test:ancient", last_activity=NOW - timedelta(days=3))

    overview = await _overview(app)

    assert [card.work_id for card in overview.cards] == [WORK.work_id]
    assert [chat.session_key for chat in overview.other] == [manual]
    assert overview.other[0].working
    assert stale not in {chat.session_key for chat in overview.other}


# --- the canonical route ----------------------------------------------------------------------


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_the_canonical_work_route_and_its_lookups(tmp_path):
    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", WORK)
    client = _client(app)

    page = client.get(work_href(WORK.work_id))
    assert page.status_code == 200
    assert 'data-work-id="github_issue:eerovil/agentdeck#2147"' in page.text
    # Its own stream (#3228), told each live section's digest; the page's build is stored as
    # the stream's first, so a connect with nothing moved builds nothing.
    stream = re.search(r'sse-connect="(/events/works/[^"]+)"', page.text).group(1)
    assert "?sig=status-" in stream and "message-" not in stream
    assert 'sse-swap="work-status"' in page.text and 'sse-swap="work-message"' not in page.text

    for spelling in ("eerovil/agentdeck/2147", "github_issue:EeroVil/AgentDeck#2147"):
        moved = client.get("/works/" + quote(spelling, safe=":"), follow_redirects=False)
        assert moved.status_code == 307, spelling
        assert moved.headers["location"] == work_href(WORK.work_id)

    assert client.get(work_href(WorkRef("eerovil/agentdeck", 1).work_id)).status_code == 404
    assert client.get("/works/not-a-work").status_code == 404


def test_the_work_page_says_where_its_time_went(tmp_path, monkeypatch, caplog):
    """A Server-Timing header on every open, and a log line for a slow one (#2654)."""
    from agentdeck.web import routes_works

    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", WORK)
    client = _client(app)
    monkeypatch.setattr(routes_works, "SLOW_WORK_PAGE_MS", 1e9)

    page = client.get(work_href(WORK.work_id))
    steps = dict(
        part.strip().split(";dur=") for part in page.headers["Server-Timing"].split(",")
    )
    assert set(steps) == {
        "lag", "pass", "jobs", "stores", "workspace", "facts", "card", "tree", "rest", "render",
        "total",
    }
    assert all(float(value) >= 0 for value in steps.values())
    assert "slow work page" not in caplog.text

    monkeypatch.setattr(routes_works, "SLOW_WORK_PAGE_MS", -1.0)
    with caplog.at_level("WARNING", logger=routes_works.logger.name):
        client.get(work_href(WORK.work_id))
    assert f"slow work page {WORK.work_id} total=" in caplog.text
    assert " lag=" in caplog.text and " pass=" in caplog.text and " render=" in caplog.text


def test_work_page_lag_counts_the_wait_before_the_handler(monkeypatch):
    """Time queued between the request reaching the app and the handler starting is
    ``lag``, and it counts in the total (#2654)."""
    from agentdeck.web import work_view

    now = 100.0
    monkeypatch.setattr(work_view.time, "perf_counter", lambda: now)
    times = work_view.StepTimes(received=92.0)
    assert times.steps["lag"] == 8000.0
    assert times.total_ms == 8000.0
    assert times.server_timing().startswith("lag;dur=8000.0")
    assert work_view.StepTimes().steps["lag"] == 0.0
    # A stamp from the future (a clock oddity) is no negative lag.
    assert work_view.StepTimes(received=105.0).steps["lag"] == 0.0


def test_an_ambiguous_delegation_is_shown_not_placed_on_each_candidate_work(tmp_path):
    app = _app(tmp_path)
    state = app.state.app_state
    parent = _add(app, "claude_code:test:parent", WORK, OTHER, title="Parent chat")
    child = _add(app, "claude_code:test:child", title="Helper chat")
    state.mark_delegated_session(child, parent)
    client = _client(app)

    for work in (WORK, OTHER):
        html = client.get(work_href(work.work_id)).text
        chats = html.split('id="work-chats"', 1)[1].split("</details>", 1)[0]
        # Folded by default, so the summary line says it too.
        assert "1 not placed" in chats.split("</summary>", 1)[0]
        members = chats.split("data-wd-unplaced-note", 1)[0]
        assert "Parent chat" in members and "Helper chat" not in members
        assert f'data-wd-unplaced="{state.chat_identity_key(child)}"' in chats
        assert "one of 2 candidate Works" in chats
    assert state.work_projection().works_for_chat(state.chat_identity_key(child)) == ()


def test_the_work_page_answers_the_operators_questions_in_order(tmp_path):
    app = _app(tmp_path, board=_board())
    state = app.state.app_state
    worker = _add(app, "claude_code:test:worker", WORK, title="Worker chat")
    state.sessions[worker].question = "Merge now?"
    _attention(app, _insight(worker))

    html = _client(app).get(work_href(WORK.work_id)).text

    # #2218's hierarchy: the story and the message box first, history and debug folded last.
    order = [
        'id="work-sec-status"',
        'id="work-sec-message"',
        'id="work-sec-lifecycle"',
        'id="work-sec-result"',
        'id="work-sec-workspace"',
        'id="work-sec-chats"',
        'id="work-sec-inspector"',
    ]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)
    # The Runs section went with the Poller's Runs (#3741).
    assert 'id="work-sec-runs"' not in html and 'id="run-trace"' not in html
    assert f'data-wd-attention="{worker}"' in html
    # A chat's question is the Work's human dependency: said at the top, not in a folded chat.
    status = html.split('id="work-sec-status"', 1)[1].split('id="work-sec-message"', 1)[0]
    assert 'data-wd-tone="attention"' in status and "Worker chat asked: Merge now?" in status
    # Chats and Technical details are folded shut by default.
    for fold in ("work-chats", "work-inspector"):
        opening = re.search(rf'<details[^>]*id="{fold}"[^>]*>', html)
        assert opening is not None and " open" not in opening.group(0), fold
    # Session deep links: the chat row and the inspector reach the leg.
    session = state.sessions[worker]
    assert f'href="{session.detail_path}"' in html
    assert f'href="/sessions/{worker}"' in html
    # Provider and session ids are debug detail: only ever inside a collapsed disclosure.
    assert f"<code>{worker}</code>" in html.split('id="work-sec-inspector"', 1)[1]
    visible = html
    while True:
        shut = re.sub(r"<details(?![^>]*\sopen[\s>=])[^>]*>(?:(?!<details).)*?</details>", "",
                      visible, flags=re.S)
        if shut == visible:
            break
        visible = shut
    text = re.sub(r"<[^>]+>", " ", visible.split("<main", 1)[1])  # what a reader sees
    assert worker not in text


def test_the_front_page_is_work_first(tmp_path):
    app = _app(tmp_path, board=_board())
    _add(app, "claude_code:test:worker", WORK, thinking=True)
    manual = _add(app, "claude_code:test:manual", thinking=True)
    client = _client(app)

    html = client.get("/").text

    # The counts, then the groups in the order a Work moves through them (#3578), other
    # activity, and the board with the links off the page.
    order = [
        'id="front-summary"',
        'id="front-needs"',
        'id="works-section"',
        'id="front-stuck"',
        'id="front-waiting"',
        'id="front-release"',
        'id="front-live"',
        'id="front-cleanup"',
        'id="front-finished"',
        'id="front-paused"',
        'id="front-stopped"',
        'id="work-other"',
        'id="front-board"',
    ]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert f'data-work-id="{WORK.work_id}"' in html
    assert "Other activity" in html and f'data-session-key="{manual}"' in html
    # No Deckhand, no chat list, no board card list: Works are the page.
    for absent in ('id="assistant-panel"', 'id="chat-list"', 'id="sessions"', 'id="board-strip"'):
        assert absent not in html, absent
    board = html.split('id="front-board"', 1)[1].split("</p>", 1)[0]
    # No card counts any more (#3741): only the links off the page.
    assert "data-board-count" not in board and 'href="/pollers"' in board
    # Nothing is waiting on a person: one calm line, not an empty card.
    needs = html.split('id="works-needs"', 1)[1].split("</div>", 1)[0]
    assert "data-front-empty" in needs and "Nothing needs you" in needs
    # The front page's stream is told to send no Deckhand.
    assert 'sse-connect="/events?view=works"' in html

    # All chats opens the chats view: the board strip and the chat list (#2256).
    assert '<a href="/chats" data-all-chats>' in html
    chats = client.get("/chats").text
    for present in ('id="board-strip"', 'id="chat-list"'):
        assert present in chats, present
    # Rendered at load, on the topbar's stream alone (#2603).
    assert '<a class="session' in chats and 'sse-connect="/events"' in chats
    # The old way to open the front page's chat list goes there.
    moved = client.get("/?chats=1", follow_redirects=False)
    assert moved.status_code == 307 and moved.headers["location"] == "/chats"


def test_a_chat_page_names_the_work_that_owns_it(tmp_path):
    """#2346: a chat page loaded on its own names its Work for the desktop columns; a chat
    no Work owns names none, and the in-page open, which has its columns, is not told."""
    app = _app(tmp_path)
    worker = _add(app, "claude_code:test:worker", WORK)
    manual = _add(app, "claude_code:test:manual")
    client = _client(app)

    page = client.get(f"/sessions/{worker}").text
    sidebar = page.split('<aside class="session-sidebar"', 1)[1].split(">", 1)[0]
    assert f'data-work-path="{work_href(WORK.work_id)}"' in sidebar
    assert 'id="board-strip"' not in page
    unowned = client.get(f"/sessions/{manual}").text
    assert "data-work-path" not in unowned
    detail = client.get(
        f"/sessions/{worker}", headers={"x-agentdeck-session-navigation": "detail"}
    ).text
    assert "data-work-path" not in detail and "session-sidebar" not in detail
    # The chat's header links up to its Work however it was opened (#2512): a phone's
    # front-page row opens the chat in place, so this is its way to the Work.
    link = f'<a class="session-work-link" href="{work_href(WORK.work_id)}"'
    assert link in page and link in detail
    assert "session-work-link" not in unowned
    # The pane names its Work, so the top bar's list marks it read while it is open (#3289).
    named = f'data-chat-work-id="{WORK.work_id}"'
    assert named in page and named in detail and "data-chat-work-id" not in unowned


def test_a_chat_two_works_share_names_neither(tmp_path):
    app = _app(tmp_path)
    shared = _add(app, "claude_code:test:shared", WORK, OTHER)
    page = _client(app).get(f"/sessions/{shared}").text
    assert "data-work-path" not in page and "session-work-link" not in page


def test_a_shared_chat_names_the_work_it_was_opened_from(tmp_path):
    """#2567: a chat two Works share links up to the Work it was opened from, which its
    address names; a Work the chat is not part of, or a garbled one, names nothing."""
    app = _app(tmp_path)
    shared = _add(app, "claude_code:test:shared", WORK, OTHER)
    client = _client(app)
    link = f'<a class="session-work-link" href="{work_href(OTHER.work_id)}"'
    opened = f"/sessions/{shared}?work={quote(OTHER.work_id, safe=':')}"
    page = client.get(opened).text
    assert link in page and f'data-work-path="{work_href(OTHER.work_id)}"' in page
    detail = client.get(opened, headers={"x-agentdeck-session-navigation": "detail"}).text
    assert link in detail
    # Spelled with the repository's case changed, it is still the same Work.
    shouted = OTHER.work_id.replace("eerovil/agentdeck", "Eerovil/AgentDeck")
    assert link in client.get(f"/sessions/{shared}?work={quote(shouted, safe=':')}").text
    for wanted in (UNSTARTED.work_id, "nonsense", ""):
        page = client.get(f"/sessions/{shared}?work={quote(wanted, safe=':')}").text
        assert "session-work-link" not in page, wanted


def test_a_shared_chat_names_the_work_its_run_was_for(tmp_path):
    """#2567: opened with no Work in its address, a shared chat names the Work its latest
    Run was for, and a Work its address names still wins."""
    app = _app(tmp_path)
    state = app.state.app_state
    shared = _add(app, "claude_code:test:shared", WORK, OTHER)
    client = _client(app)
    state.bind_session_run(shared, OTHER.work_id, "gen-1")
    page = client.get(f"/sessions/{shared}").text
    assert f'<a class="session-work-link" href="{work_href(OTHER.work_id)}"' in page
    state.bind_session_run(shared, WORK.work_id, "gen-2")
    page = client.get(f"/sessions/{shared}").text
    assert f'<a class="session-work-link" href="{work_href(WORK.work_id)}"' in page
    page = client.get(f"/sessions/{shared}?work={quote(OTHER.work_id, safe=':')}").text
    assert f'<a class="session-work-link" href="{work_href(OTHER.work_id)}"' in page


@pytest.mark.asyncio
async def test_a_shared_chats_address_names_the_work_it_is_listed_under(tmp_path):
    """#2567: the front-page row and the Work page open a shared chat with that Work in its
    address; a chat only one Work owns keeps its plain address."""
    app = _app(tmp_path)
    shared = _add(app, "claude_code:test:shared", WORK, OTHER, thinking=True)
    _add(app, "claude_code:test:own", OTHER)
    await app.state.work_overview.refresh()
    overview = app.state.work_overview.overview
    detail_path = app.state.app_state.sessions[shared].detail_path
    for work in (WORK, OTHER):
        assert _card_of(overview, work).chat_href == (
            f"{detail_path}?work={quote(work.work_id, safe=':')}"
        )
    page = _client(app).get(work_href(WORK.work_id)).text
    assert f'href="{detail_path}?work={quote(WORK.work_id, safe=":")}"' in page
    own = app.state.app_state.sessions["claude_code:test:own"].detail_path
    assert f'href="{own}"' in _client(app).get(work_href(OTHER.work_id)).text


def _visible_text(html: str) -> str:
    """What a reader of the page body sees: no tags, attributes, scripts or comments."""
    body = html.split("<main", 1)[1]
    body = re.sub(r"<script\b.*?</script>", " ", body, flags=re.S)
    return re.sub(r"<[^>]+>", " ", body)


def test_all_chats_lists_only_chats_outside_every_work(tmp_path):
    app = _app(tmp_path, board=_board())
    worker = _add(app, "claude_code:test:worker", WORK, show_when_idle=True)
    manual = _add(app, "claude_code:test:manual", show_when_idle=True)
    # Deckhand still triages both, but the old attention panel is shown for neither.
    _attention(app, _insight(worker), _insight(manual))
    client = _client(app)

    pages = {}
    for url in ("/chats", "/chats?chats=1"):
        page = client.get(url)
        assert page.status_code == 200, url
        html = pages[url] = page.text
        # No legacy Attention view, its stream topic or its wording, shut or open.
        assert 'id="assistant-panel"' not in html and 'sse-swap="assistant"' not in html, url
        assert "attention" not in _visible_text(html).lower(), url
        assert "Chats outside Works" in html
        assert f'data-session-key="{worker}"' not in html, url
    # The front page's link opens the list, and it holds only the chat no Work owns.
    assert f'data-session-key="{manual}"' in pages["/chats"]

    # Old deck deep links land on the chats view, never on the old deck.
    moved = client.get("/?chats=1")
    assert moved.url.path == "/chats" and 'id="assistant-panel"' not in moved.text
    assert f'data-session-key="{worker}"' not in moved.text
    assert client.get("/partials/assistant").status_code == 404

    # A chat page carries no list at all (#2603), so closing it never reveals the old deck.
    chat = client.get("/sessions/claude_code:test:sid1").text
    assert 'id="assistant-panel"' not in chat and 'sse-swap="assistant"' not in chat
    sidebar = chat.split('class="session-sidebar"', 1)[1].split("</aside>", 1)[0]
    assert "data-list-empty" in sidebar and "data-session-key" not in sidebar


@private_feature(_chat_review)
@pytest.mark.asyncio
async def test_the_chat_list_follows_the_overview_s_work_membership(tmp_path):
    from agentdeck.web.render import session_list_context
    from agentdeck.web.work_view import WorkOverview, work_chat_ids

    app = _app(tmp_path)
    worker = _add(app, "claude_code:test:worker", WORK, show_when_idle=True)
    manual = _add(app, "claude_code:test:manual", show_when_idle=True)
    # A deck-started reviewer's chat is in no Work and still left out (#3101).
    reviewer = _add(
        app,
        "claude_code:test:reviewer",
        show_when_idle=True,
        initial_prompt="You are AgentDeck's Codex reviewer. ChatGPT reviews first on this host",
    )
    state = app.state.app_state

    # Before the refresher's first snapshot the projection itself answers.
    app.state.work_overview.overview = None
    assert work_chat_ids(app) == {state.chat_identity_key(worker)}
    # The snapshot carries the same answer, so a list render builds no projection.
    overview = await _overview(app)
    assert overview.work_chat_ids == {state.chat_identity_key(worker)}
    app.state.work_overview.overview = overview
    assert work_chat_ids(app) is overview.work_chat_ids
    # A chat joining a Work changes the overview, which is what republishes the list.
    assert overview != WorkOverview(**{**overview.__dict__, "work_chat_ids": frozenset()})

    context = session_list_context(
        [],
        state.session_presentation(),
        injector=app.state.injector,
        assistant=app.state.assistant,
        work_chat_ids=work_chat_ids(app),
    )
    listed = [session.key for session in context["sessions"]]
    assert manual in listed and worker not in listed and reviewer not in listed
    assert context["session_total_count"] == len(listed)


def _section_of(overview, work: WorkRef) -> str:
    found = [
        row.section
        for rows in (
            overview.front.needs_you,
            overview.front.active,
            overview.front.waiting,
            overview.front.finished,
        )
        for row in rows
        if row.card.work_id == work.work_id
    ]
    assert len(found) <= 1, found  # a Work is never in two primary sections
    return found[0] if found else ""


@pytest.mark.asyncio
async def test_each_work_is_in_one_section_by_its_human_dependency(tmp_path):
    from agentdeck.work_story import FINISHED, NEEDS_YOU

    question, done, handoff = (WorkRef("eerovil/agentdeck", n) for n in (3001, 3004, 3006))
    app = _app(tmp_path, board=_board())
    asker = _add(app, "claude_code:test:asker", question)
    _add(app, f"claude_code:test:w{done.number}", done)
    finisher = _add(app, "claude_code:test:finisher", handoff)
    # A chat that really asks: a waiting card alone is not a question (#2224).
    app.state.app_state.sessions[asker].question = "Which branch should this go on?"
    _attention(app, _insight(asker, "waiting"), _insight(finisher, "finished"))
    _closed(app, {3004: NOW - timedelta(hours=1)})

    overview = await _overview(app)

    assert _section_of(overview, question) == NEEDS_YOU
    assert _section_of(overview, done) == FINISHED
    # A finished turn nobody acknowledged is not a dependency.
    assert _section_of(overview, handoff) != NEEDS_YOU
    needs = {row.card.work_id: row for row in overview.front.needs_you}
    assert list(needs) == [question.work_id]
    assert needs[question.work_id].action == "Answer"

    page = _client(app).get("/").text
    rows = re.findall(r'<article class="fr fr-(\w+)[^"]*"\s+data-work-id="([^"]+)"', page)
    # Each Work is in one section; Recently stopped only repeats Needs you rows (#2283).
    listed = [work_id for section, work_id in rows if section != "stopped"]
    assert len(set(listed)) == len(listed)
    assert {work_id for section, work_id in rows if section == "stopped"} == set(needs)
    # Recently stopped is listed last (#3578): everything before it lists each Work once.
    html = page.split('id="works-stopped"', 1)[0]
    # Rows never name the chat or session that raised the question.
    ask_row = html.split(f'data-work-id="{question.work_id}"', 1)[1].split("</article>", 1)[0]
    # ...and sit under the operator's category (#2236, grouped by #2254).
    assert asker not in ask_row and 'data-op-category="needs_answer"' in ask_row
    assert html.index('data-front-group="needs_answer"') < html.index(
        f'data-work-id="{question.work_id}"'
    )


@pytest.mark.asyncio
async def test_recently_finished_is_bounded_and_newest_first(tmp_path):
    from agentdeck.work_story import MAX_FINISHED

    refs = [WorkRef("eerovil/agentdeck", 3100 + n) for n in range(MAX_FINISHED + 2)]
    app = _app(tmp_path, board=_board())
    for age, ref in enumerate(refs):
        # Chat activity runs the other way round, so only the close time can give this order.
        # Every chat went quiet before its close: a later one would be continued (#2357).
        _add(app, f"claude_code:test:f{ref.number}", ref,
             last_activity=NOW - timedelta(hours=2 * len(refs) - age))
    _closed(app, {ref.number: NOW - timedelta(hours=age + 1) for age, ref in enumerate(refs)})

    overview = await _overview(app)

    assert [row.card.work_id for row in overview.front.finished] == [
        ref.work_id for ref in refs[:MAX_FINISHED]
    ]
    assert overview.front.finished_more == 2
    assert not overview.front.needs_you and not overview.front.active


@pytest.mark.asyncio
async def test_an_old_question_is_not_crowded_out_by_newer_automation(tmp_path):
    """#2219 review: sections first, limits after. With more current Works than the Active
    section shows, the oldest of them — the only one waiting on a person — is still listed,
    and only Active is cut, by its own display limit."""
    from agentdeck.work_overview import MAX_DASHBOARD_WORKS
    from agentdeck.work_story import NEEDS_YOU

    busy = [WorkRef("eerovil/agentdeck", 3200 + n) for n in range(MAX_DASHBOARD_WORKS + 5)]
    parked = WorkRef("eerovil/agentdeck", 3199)
    app = _app(tmp_path, board=_board())
    for age, ref in enumerate(busy):
        _add(app, f"claude_code:test:b{ref.number}", ref, thinking=True,
             last_activity=NOW - timedelta(minutes=age + 1))
    # Older than every busy Work: ranked last by raw activity, past the 20th place.
    asker = _add(app, "claude_code:test:parked", parked, last_activity=NOW - timedelta(days=2))
    app.state.app_state.sessions[asker].question = "Which branch?"
    _attention(app, _insight(asker, "waiting"))

    overview = await _overview(app)

    assert _section_of(overview, parked) == NEEDS_YOU
    assert [row.card.work_id for row in overview.front.needs_you] == [parked.work_id]
    # Active shows its limit and counts the rest; nothing was cut before sectioning.
    assert len(overview.front.active) == MAX_DASHBOARD_WORKS
    assert overview.front.active_more == len(busy) - MAX_DASHBOARD_WORKS
    assert len(overview.cards) == len(busy) + 1
    html = _client(app).get("/").text
    assert f'data-work-id="{parked.work_id}"' in html.split('id="works-section"', 1)[0]
    assert f"{len(busy) - MAX_DASHBOARD_WORKS} more active" in html


@pytest.mark.asyncio
async def test_only_rendered_rows_are_hydrated_and_an_old_question_is_still_found(
    tmp_path, monkeypatch
):
    """#2223 review: sectioning is one cheap pass, and every expensive Work-scoped reader —
    the workspace claims and the retained job evidence — runs only for the rows the page
    renders. With far more
    Works than the Active limit, the old parked Work past the raw recency boundary is still
    found, and the healthy Works the page does not list are never hydrated."""
    import agentdeck.web.work_view as work_view
    from agentdeck.work_overview import MAX_DASHBOARD_WORKS
    from agentdeck.work_story import NEEDS_YOU

    busy = [WorkRef("eerovil/agentdeck", 3400 + n) for n in range(MAX_DASHBOARD_WORKS + 20)]
    quiet = [WorkRef("eerovil/agentdeck", 3500 + n) for n in range(15)]
    parked = WorkRef("eerovil/agentdeck", 3399)
    app = _app(tmp_path, board=_board())
    for age, ref in enumerate(busy):
        _add(app, f"claude_code:test:e{ref.number}", ref, thinking=True,
             last_activity=NOW - timedelta(minutes=age + 1))
    for age, ref in enumerate(quiet):
        _add(app, f"claude_code:test:q{ref.number}", ref,
             last_activity=NOW - timedelta(hours=age + 1))
    # Older than every busy and quiet Work: past the 20th place by raw recency.
    asker = _add(app, "claude_code:test:parked", parked, last_activity=NOW - timedelta(days=2))
    app.state.app_state.sessions[asker].question = "Which branch?"
    _attention(app, _insight(asker, "waiting"))

    reads: dict[str, list[str]] = {"claims": [], "evidence": []}

    def counted(name, real, key=lambda *a, **k: a[1] if len(a) > 1 else a[0].work_id):
        def wrapper(*args, **kwargs):
            reads[name].append(key(*args, **kwargs))
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        work_view, "read_claims",
        counted("claims", work_view.read_claims, key=lambda db, work_id: work_id),
    )
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/job-evidence":
            reads["evidence"].append(request.url.params.get("work_id", ""))
            return httpx.Response(200, json={"evidence": []})
        return httpx.Response(200, json={"jobs": []})

    app.state.runtime_http = httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://agentdeck-runtime"
    )

    overview = await _overview(app)

    rendered = {
        row.card.work_id
        for row in (*overview.front.needs_you, *overview.front.active, *overview.front.finished)
    }
    # The old question is found and rendered; Active shows its limit and counts the rest.
    assert _section_of(overview, parked) == NEEDS_YOU
    assert len(rendered) == MAX_DASHBOARD_WORKS + 1
    assert overview.front.active_more == len(busy) - MAX_DASHBOARD_WORKS
    assert overview.front.quiet == len(quiet)
    # Every expensive per-Work reader ran exactly once for each rendered row, and never for
    # the 20 busy Works past the Active limit or the 15 quiet ones.
    for name, ids in reads.items():
        assert sorted(ids) == sorted(rendered), name
    assert len(overview.cards) == len(busy) + len(quiet) + 1


@pytest.mark.asyncio
async def test_recently_finished_goes_by_when_the_work_finished(tmp_path):
    """#2219 review: a Work closed today is Recently finished even when its chats went quiet
    a fortnight ago; rows are ordered and dated by GitHub's close time; and a comment on an
    issue closed long ago, which moves ``updatedAt``, does not make it newly finished."""
    from agentdeck.work_story import FINISHED

    quiet_long, busy_late = WorkRef("eerovil/agentdeck", 3301), WorkRef("eerovil/agentdeck", 3302)
    old_close, commented = WorkRef("eerovil/agentdeck", 3303), WorkRef("eerovil/agentdeck", 3304)
    app = _app(tmp_path)
    _add(app, "claude_code:test:q", quiet_long, last_activity=NOW - timedelta(days=14))
    _add(app, "claude_code:test:l", busy_late, last_activity=NOW - timedelta(minutes=5))
    _add(app, "claude_code:test:o", old_close, last_activity=NOW - timedelta(minutes=10))
    _add(app, "claude_code:test:c", commented, last_activity=NOW - timedelta(days=20))
    closed = {
        3301: NOW - timedelta(hours=1),
        3302: NOW - timedelta(hours=3),
        3303: NOW - timedelta(days=9),  # finished long ago: off the front page
        3304: NOW - timedelta(days=20),
    }
    # Someone commented on #3304 ten minutes ago, three weeks after it closed.
    _closed(app, closed, updated={3304: NOW - timedelta(minutes=10)})

    overview = await _overview(app)

    # #2357: a chat carried on well after the close goes by that chat instead, so #3302
    # (chat five minutes ago) leads and #3303, closed nine days ago, is listed again.
    assert [row.card.work_id for row in overview.front.finished] == [
        busy_late.work_id,
        old_close.work_id,
        quiet_long.work_id,
    ]
    assert _section_of(overview, quiet_long) == FINISHED
    assert _section_of(overview, old_close) == FINISHED
    assert _section_of(overview, commented) == ""
    row = overview.front.finished[2]
    assert row.card.finished_at == closed[3301] and row.card.title == "Issue 3301"
    assert row.continued is None and overview.front.finished[0].continued is not None
    html = _client(app).get("/").text
    finished = html.split('id="works-finished"', 1)[1].split("</section>", 1)[0]
    # Dated by the closing, not by the chat that went quiet two weeks ago.
    assert closed[3301].isoformat() in finished


def test_a_finish_time_is_the_close_time_and_nothing_else():
    from agentdeck.work_facts import work_finished_at

    closed = {"state": "closed", "closedAt": "2026-09-01T10:00:00Z",
              "updatedAt": "2026-09-25T10:00:00Z"}
    assert work_finished_at(closed) == datetime(2026, 9, 1, 10, tzinfo=UTC)
    # A record cached before it carried ``closedAt`` has no finish time, not ``updatedAt``.
    assert work_finished_at({"state": "closed", "updatedAt": "2026-09-25T10:00:00Z"}) is None
    assert work_finished_at({**closed, "state": "open"}) is None
    assert work_finished_at({**closed, "is_pr": True}) is None


# --- live updates -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_refresher_publishes_only_a_real_change(tmp_path):
    from agentdeck.web.work_view import WorkOverviewService

    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", WORK)
    bus = app.state.app_state.bus
    service = WorkOverviewService(app)

    assert await service.refresh() is True
    before = bus.revision("works")
    assert await service.refresh() is False
    assert bus.revision("works") == before

    _add(app, "claude_code:test:worker", WORK, thinking=True)
    assert await service.refresh() is True
    assert bus.revision("works") != before


@pytest.mark.asyncio
async def test_a_work_change_streams_without_rendering_the_chat_list(tmp_path, monkeypatch):
    import agentdeck.web.routes_sse as sse

    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", WORK)
    await app.state.work_overview.refresh()
    # No stream renders a chat list at all any more (#2603); the stream is checked for
    # the event below.

    class _Request:
        def __init__(self):
            self.app = app
            self.query_params = {"view": "works"}
            self.headers = {}
            self.cookies = {}
            self.url = httpx.URL("http://deck/events?view=works")

        async def is_disconnected(self):
            return False

    stream = sse._stream(_Request())
    primed = []
    while not any(chunk.startswith("event: works") for chunk in primed):
        primed.append(await asyncio.wait_for(anext(stream), 5))
    # The front page's stream sends no Deckhand (#2219).
    assert not any(chunk.startswith("event: assistant") for chunk in primed)
    _add(app, "claude_code:test:worker", WORK, thinking=True)
    await app.state.work_overview.refresh()
    chunk = await asyncio.wait_for(anext(stream), 5)
    while not chunk.startswith("event: works"):
        assert not chunk.startswith("event: sessions")
        chunk = await asyncio.wait_for(anext(stream), 5)
    active = chunk.split("event: works-active", 1)[1].split("event: ", 1)[0]
    # The working chat is named (#2487), not counted.
    assert f'data-work-id="{WORK.work_id}"' in active and "worker is working" in active
    await stream.aclose()


@pytest.mark.asyncio
async def test_an_open_work_page_builds_only_when_its_own_work_moved(tmp_path, monkeypatch):
    """#3228: the Work page's stream builds the Work again only when something of its own
    moved, once for every tab, and sends only the sections that changed. Another Work's chat
    or record costs it a key read."""
    import agentdeck.web.routes_sse as sse
    from agentdeck import work_counter
    from agentdeck.web import routes_works
    from agentdeck.web.work_view import WORK_INPUTS_TOPIC

    app = _app(tmp_path, board=_board())
    _add(app, "claude_code:test:worker", WORK)
    _add(app, "claude_code:test:cleanup", OTHER)
    await app.state.work_overview.refresh()
    monkeypatch.setattr(sse, "HEARTBEAT_S", 0.05)
    monkeypatch.setattr(routes_works, "WORK_PAGE_MIN_INTERVAL_S", 0.0)
    # The sections print ages ("5m ago") from the wall clock; a fresh render a minute boundary
    # later would differ from the kept one for no fault of the build (#3360).
    from agentdeck.web import render

    monkeypatch.setattr(render, "_now", lambda: NOW)
    builds = routes_works.work_page_builds(app)
    # What the page load stores and tells its stream (``work_page``).
    page = await builds.current(app, WORK, app.state.templates)
    shown = {name: digest for name, (digest, _html) in page.sections.items()}

    class _Request:
        def __init__(self):
            self.app = app

        async def is_disconnected(self):
            return False

    tabs = [routes_works._work_stream(_Request(), WORK, dict(shown)) for _ in range(2)]

    async def next_of(tab):
        return await asyncio.wait_for(anext(tab), 5)

    with work_counter.counting() as spent:
        # Nothing moved since the page was built: no build, only the ping.
        for tab in tabs:
            assert await next_of(tab) == ": ping\n\n"
    assert spent[work_counter.WORK_PAGE_BUILD] == 0

    with work_counter.counting() as spent:
        _add(app, "claude_code:test:worker", WORK, thinking=True)
        await app.state.work_overview.refresh()
        for tab in tabs:
            chunk = await next_of(tab)
            assert chunk.startswith("event: work-")
            while chunk != ": ping\n\n":
                assert chunk.startswith("event: work-") and "event: work-message" not in chunk
                chunk = await next_of(tab)
    assert spent[work_counter.WORK_PAGE_BUILD] == 1  # once, for both tabs
    # Only the sections whose inputs moved are rendered again: a chat starting to work moves
    # the card and its chats, never the resolution or the workspace.
    assert 0 < spent[work_counter.WORK_SECTION] < len(routes_works.LIVE_SECTIONS)
    rebuilt = builds._builds[WORK.work_id]
    for name in ("resolution", "workspace"):
        assert rebuilt.inputs[name] is page.inputs[name], name  # kept, not rendered
    # The workspace fragment is built only for the section that shows it and was rendered
    # again: the details.
    owed = {"inspector": 1}
    assert spent[work_counter.WORK_FRAGMENT] == sum(
        count for name, count in owed.items() if rebuilt.inputs[name] is not page.inputs[name]
    )
    # What was kept is what a fresh render shows: no section reads more than its inputs say.
    from agentdeck.web.work_view import load_work_detail, render_work_sections

    detail = await load_work_detail(app, WORK)
    fresh = dict(render_work_sections(app.state.templates, detail))
    for name, (_digest, html) in rebuilt.sections.items():
        assert html == fresh[name], name

    # A change only one section reads re-renders that section, with what a fresh render shows:
    # the workspace's rows from the card's own claims, the status's hide control.
    from dataclasses import replace

    from agentdeck.web.work_view import render_changed_sections
    from agentdeck.work_hidden import HiddenWork
    from agentdeck.work_overview import WorkspaceLine

    templates = app.state.templates
    before = render_changed_sections(templates, detail, previous={})
    claimed = replace(
        detail,
        card=replace(
            detail.card,
            workspace=(WorkspaceLine("eerovil/agentdeck", "issue-2147", "claimed"),),
        ),
    )
    hidden = replace(detail, hidden=HiddenWork(WORK.work_id, "2026-10-17T06:00:00+00:00"))
    for changed, name in ((claimed, "workspace"), (hidden, "status")):
        after = render_changed_sections(templates, changed, previous=before)
        assert after[name] is not before[name], name
        assert after[name][1] == dict(render_work_sections(templates, changed))[name], name
    assert render_changed_sections(templates, hidden, previous=before)["resolution"] is (
        before["resolution"]
    )

    with work_counter.counting() as spent:
        _add(app, "claude_code:test:cleanup", OTHER, thinking=True)
        await app.state.work_overview.refresh()
        app.state.app_state.bus.publish(WORK_INPUTS_TOPIC)  # another Work's record
        for tab in tabs:
            assert await next_of(tab) == ": ping\n\n"
    assert spent[work_counter.WORK_PAGE_BUILD] == 0
    for tab in tabs:
        await tab.aclose()


def _route_paths(routes) -> set[str]:
    """Every route path, through FastAPI's included-router wrappers."""
    found: set[str] = set()
    for route in routes:
        found.add(getattr(route, "path", ""))
        inner = getattr(getattr(route, "original_router", None), "routes", None)
        found |= _route_paths(inner or getattr(route, "routes", None) or ())
    return found


def test_the_older_slices_routes_are_the_work_page_now(tmp_path):
    paths = _route_paths(_app(tmp_path).routes)
    assert {p for p in paths if "works" in p} == {
        "/works",
        "/works/{work_id:path}",
        "/works/{work_id:path}/message",
        "/works/{work_id:path}/action",  # the one Work action route (#2237)
        "/works/{work_id:path}/visibility",  # Hide and Bring back (#2238)
        "/works/{work_id:path}/star",  # Star and Unstar (#3116)
        "/api/works/{work_id:path}/star",  # the same for agents (#3116)
        "/api/works/{work_id:path}/map-layout",  # a wayfinder map's moved boxes (#3627)
        "/api/works/{work_id:path}/question",  # its question boxes, for agents (#3481)
        "/works/{work_id:path}/title",  # the Work's own name on this deck (#2328)
        "/works/{work_id:path}/new-chat",  # a side chat for the Work (#2388)
        "/works/new",  # file a new Work's issue from the deck (#2415)
        "/works/new/guess-project",  # New work's project guessed from its text (#3367)
        "/works/new/warm",  # an open New work form keeps a Work spare (#3590)
        "/works/{work_id:path}/chat",  # the New work receipt's link to the Work's chat (#3590)
        "/works/seen",  # mark Works read from the top bar's list (#3246)
        "/api/works/filed",  # an issue the deck filed starts its Work now (#3371)
        "/api/works/population",  # read-only rows for staging comparisons (#2250)
        "/api/works/status-log",  # every logged status change (#2751)
        "/api/works/status-log/flaps",  # the short trips somewhere and back (#2751)
        "/partials/works/{work_id:path}/diff",  # the Work's diff, read on open (#2379)
        "/events/works/{work_id:path}",  # the Work page's own stream (#3228)
    }
    assert not any("run-trace" in path for path in paths)


# --- Active, Recent and Archived Works (#2201) ---------------------------------------------------


def _candidate(work: WorkRef = WORK, *, age: timedelta | None, live: bool = False):
    from agentdeck.work_overview import WorkCandidate

    at = NOW - age if age is not None else None
    return WorkCandidate(work, None, at, live)


def test_a_work_is_active_on_evidence_recent_by_activity_and_archived_otherwise():
    from agentdeck.work_overview import ACTIVE, ARCHIVED, RECENT, RECENT_WORK, classify_work

    assert classify_work(_candidate(age=timedelta(days=90), live=True), now=NOW) == ACTIVE
    assert classify_work(_candidate(age=timedelta(hours=30)), now=NOW) == RECENT
    assert classify_work(_candidate(age=RECENT_WORK), now=NOW) == RECENT
    assert classify_work(_candidate(age=RECENT_WORK + timedelta(seconds=1)), now=NOW) == ARCHIVED
    # A Work nothing has ever timed is not recent by default.
    assert classify_work(_candidate(age=None), now=NOW) == ARCHIVED


def _old(app, key: str, work: WorkRef) -> str:
    return _add(app, key, work, last_activity=NOW - timedelta(days=30))


def _active(app, key: str, work: WorkRef) -> str:
    """An old Work still current: its chat is mid-turn."""
    return _add(app, key, work, thinking=True, last_activity=NOW - timedelta(days=30))


def _candidates(app, *, current=()):
    from agentdeck.web.work_view import gather_pass
    from agentdeck.work_overview import bucket_works, work_candidates

    snapshot = gather_pass(app)
    found = work_candidates(
        snapshot.state,
        snapshot.projection,
        owned_insights=snapshot.owned,
        legs_by_chat=snapshot.legs,
        current_work_ids=current,
        presentation=snapshot.presentation,
    )
    buckets = bucket_works(found, now=NOW)
    return {
        item.ref: name
        for name in ("active", "recent", "archived")
        for item in getattr(buckets, name)
    }


def test_current_evidence_keeps_an_old_work_active(tmp_path):
    works = tuple(WorkRef("eerovil/agentdeck", n) for n in (1, 3, 5))
    working, attended, runtime = works
    app = _app(tmp_path, board=_board())
    _add(app, "claude_code:test:working", working, thinking=True,
         last_activity=NOW - timedelta(days=30))
    for key, work in (("attended", attended), ("runtime", runtime)):
        _old(app, f"claude_code:test:{key}", work)
    _attention(app, _insight("claude_code:test:attended"))
    _old(app, "claude_code:test:archived", OTHER)

    buckets = _candidates(app, current={runtime.work_id})

    for work in works:
        assert buckets[work] == "active", work
    assert buckets[OTHER] == "archived"


def test_a_background_command_alone_does_not_keep_an_old_work_active(tmp_path):
    """#2783 review: a dev server left running is not the Work live; its helper agent is."""
    server_only, with_helper = (WorkRef("eerovil/agentdeck", n) for n in (21, 22))
    app = _app(tmp_path)
    old = NOW - timedelta(days=30)
    _add(app, "claude_code:test:server", server_only, thinking=True, last_activity=old,
         activity=BACKGROUND_TASK_ACTIVITY, status=SessionStatus.LIVE)
    parent = _add(app, "claude_code:test:parent", with_helper, thinking=True,
                  last_activity=old, activity=BACKGROUND_TASK_ACTIVITY,
                  status=SessionStatus.LIVE)
    _add(app, "claude_code:test:helper", thinking=True, last_activity=old,
         activity="Using tools", parent_session_key=parent, status=SessionStatus.LIVE)

    buckets = _candidates(app)

    assert buckets[server_only] == "archived"
    assert buckets[with_helper] == "active"


@pytest.mark.asyncio
async def test_a_running_verification_makes_a_work_current(tmp_path):
    from agentdeck.run_execution import ExecutionOutcome, OutcomeStore
    from agentdeck.web.work_view import current_work_ids

    executing, verifying = (WorkRef("eerovil/agentdeck", n) for n in (11, 13))
    app = _app(tmp_path)
    # A Poller Run's open outcome no longer makes its Work current (#3741).
    store = OutcomeStore(app.state.config.history.db_path)
    store.record(ExecutionOutcome(executing.work_id, "g1", "running",
                                  observed_at=NOW.timestamp()))
    store.close()
    queue = [
        {"id": "j1", "work_id": verifying.work_id.replace("eerovil", "Eerovil"), "run_id": "g",
         "state": "running",
         "purpose": "verification"},
        {"id": "j2", "work_id": OTHER.work_id, "run_id": "g", "state": "running", "purpose": ""},
        {"id": "j3", "work_id": WORK.work_id, "run_id": "g", "state": "done",
         "purpose": "verification"},
    ]

    assert await current_work_ids(app, queue) == {verifying.work_id}


def test_the_works_index_shows_active_and_recent_and_counts_the_archive(tmp_path):
    recent = WorkRef("eerovil/agentdeck", 3)
    app = _app(tmp_path, board=_board())
    _active(app, "claude_code:test:worker", WORK)
    _add(app, "claude_code:test:recent", recent, last_activity=NOW - timedelta(days=3))
    _old(app, "claude_code:test:old", OTHER)
    _old(app, "claude_code:test:older", WorkRef("eerovil/trailhead", 7))
    # Closed issues are in no open-issue list: their titles come from the cached record (#3257).
    _closed(app, {3: NOW - timedelta(hours=1), OTHER.number: NOW - timedelta(days=30)})

    html = _client(app).get("/works").text

    active = html.split('data-works-bucket="active"')[1].split('data-works-bucket="recent"')[0]
    recent_part = html.split('data-works-bucket="recent"')[1]
    assert f'data-work-id="{WORK.work_id}"' in active
    assert f'data-work-id="{recent.work_id}"' in recent_part
    assert "Issue 3" in recent_part
    assert f'data-work-id="{OTHER.work_id}"' not in html
    assert "Archived Works (2)" in html
    assert f"Issue {OTHER.number}" in _client(app).get("/works?view=archived").text


@pytest.mark.asyncio
async def test_the_works_index_streams_only_its_changed_rows(tmp_path, monkeypatch):
    """#3228: ``/works`` keeps its list current, and a push carries only the rows that moved."""
    from test_change_budget import _EventsRequest, _until

    import agentdeck.web.routes_sse as sse

    recent = WorkRef("eerovil/agentdeck", 3)
    app = _app(tmp_path, board=_board())
    _active(app, "claude_code:test:worker", WORK)
    _add(app, "claude_code:test:recent", recent, last_activity=NOW - timedelta(days=3))
    await app.state.work_overview.refresh()
    assert 'sse-connect="/events?view=works-index&amp;repo="' in _client(app).get("/works").text

    # A change between the page load and the stream connecting still reaches the tab.
    _add(app, "claude_code:test:recent", recent, title="Moved before the stream connected")
    stream = sse._stream(_EventsRequest(app, view="works-index"))
    primed = await _until(stream, "event: works-index-")
    assert f'data-work-id="{recent.work_id}"' in primed and "data-work-same" not in primed
    assert "Moved before the stream connected" in primed
    assert "event: works-index-recent-count\ndata: 1\n" in primed

    # A chat change alone moves it: no overview pass, no ``works`` publication.
    monkeypatch.setattr(sse, "INDEX_MIN_INTERVAL_S", 0.0)
    _add(app, "claude_code:test:recent", recent, title="Renamed in its chat")
    pushed = await _until(stream, "event: works-index-")
    rows = pushed.split("event: works-index-recent\n", 1)[1].split("event: ", 1)[0]
    assert f'data-work-id="{recent.work_id}"' in rows and "data-work-same" not in rows
    assert f'<li hidden data-work-same data-work-id="{WORK.work_id}"' in pushed
    await stream.aclose()


def test_the_works_index_filters_by_project(tmp_path):
    trailhead = WorkRef("eerovil/trailhead", 7)
    recent = WorkRef("eerovil/agentdeck", 3)
    app = _app(tmp_path, board=_board())
    _active(app, "claude_code:test:worker", WORK)
    _add(app, "claude_code:test:recent", recent, last_activity=NOW - timedelta(days=3))
    _add(app, "claude_code:test:trailhead", trailhead, last_activity=NOW - timedelta(days=2))
    _old(app, "claude_code:test:old", OTHER)
    client = _client(app)

    everything = client.get("/works").text
    assert {WORK.work_id, recent.work_id, trailhead.work_id} <= set(_archived_ids(everything))
    assert re.findall(r'data-works-project="([^"]+)"', everything) == [
        "eerovil/agentdeck", "eerovil/trailhead"
    ]
    assert "All projects" in everything and "Archived Works (1)" in everything

    # Any spelling of the repository picks its Works in both buckets, and says so while shut.
    only = client.get("/works?repo=EEROVIL/Trailhead").text
    assert _archived_ids(only) == [trailhead.work_id]
    assert 'data-works-project-summary>eerovil/trailhead<' in only
    assert 'href="/works?view=archived&amp;repo=eerovil%2Ftrailhead"' in only
    assert "No other Work moved this week." not in only
    agentdeck = client.get("/works?repo=eerovil/agentdeck").text
    assert set(_archived_ids(agentdeck)) == {WORK.work_id, recent.work_id}

    # A repository with nothing Active or Recent is still offered, so it can be cleared.
    gone = client.get("/works?repo=eerovil/nothing").text
    assert _archived_ids(gone) == []
    assert 'data-works-project="eerovil/nothing"' in gone and 'href="/works"' in gone


def test_one_project_offers_no_filter(tmp_path):
    app = _app(tmp_path, board=_board())
    _active(app, "claude_code:test:worker", WORK)

    html = _client(app).get("/works").text

    assert "data-works-project" not in html


def test_an_archived_work_keeps_its_canonical_page(tmp_path):
    app = _app(tmp_path)
    _old(app, "claude_code:test:old", OTHER)

    response = _client(app).get(work_href(OTHER.work_id))

    assert response.status_code == 200
    assert f'data-work-id="{OTHER.work_id}"' in response.text


def _archive(tmp_path, count: int):
    app = _app(tmp_path)
    for number in range(1, count + 1):
        repo = "eerovil/trailhead" if number % 10 == 0 else "eerovil/agentdeck"
        _add(app, f"claude_code:test:w{number}", WorkRef(repo, number),
             title=f"Task number {number}", last_activity=NOW - timedelta(days=30, hours=number))
    return app


def _archived_ids(html: str) -> list[str]:
    return re.findall(r'<li class="wd-row" data-work-id="([^"]+)"', html)


def _backfilled_inventory(app, monkeypatch, tmp_path, *works: WorkRef):
    """A deck upgraded from before the ``works`` table: the Works exist only as old
    ``session_issues`` rows of chats no scan loads any more, and startup backfills the
    inventory from them, stamping every row with the backfill's own time (#2140)."""
    import sqlite3

    from agentdeck.db import Db
    from agentdeck.state import AppState

    path = tmp_path / "upgraded.db"
    old = Db(path)
    for work in works:
        old.record_session_issue(
            f"claude_code:old:{work.number}", IssueRef(work.repository, work.number)
        )
    old.close()
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM works")  # the table did not exist before #2140
    conn.commit()
    conn.close()

    db = Db(path)
    AppState(db=db, budget_db=db)  # startup: the backfill
    backfilled = db.load_works()
    assert {work.work_id for work in works} <= set(backfilled)
    # The backfill is today, although nothing about these Works happened today.
    assert all(
        datetime.fromisoformat(row[2]) >= NOW - timedelta(minutes=5)
        for row in backfilled.values()
    )
    for name in ("load_works", "count_works", "known_work_ids"):
        monkeypatch.setattr(app.state.app_state.db, name, getattr(db, name), raising=False)
    return db


@pytest.mark.asyncio
async def test_a_backfilled_work_only_the_inventory_remembers_is_archived_and_found(
    tmp_path, monkeypatch
):
    remembered = WorkRef("eerovil/trailhead", 412)
    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", WORK)
    # WORK is in the inventory too, and still has a loaded chat: its chat's facts win.
    _backfilled_inventory(app, monkeypatch, tmp_path, remembered, WORK)
    client = _client(app)

    index = client.get("/works").text
    # Backfilled today, but nothing current or recent names it: Archived, not Recent.
    assert f'data-work-id="{remembered.work_id}"' not in index
    assert f'data-work-id="{WORK.work_id}"' in index
    assert "Archived Works (1)" in index
    archive = client.get("/works?view=archived&q=trailhead%23412").text
    assert _archived_ids(archive) == [remembered.work_id]
    assert _archived_ids(client.get("/works?view=archived&repo=eerovil/trailhead").text) == [
        remembered.work_id
    ]
    page = client.get(work_href(remembered.work_id))
    assert page.status_code == 200 and f'data-work-id="{remembered.work_id}"' in page.text
    overview = await _overview(app)
    assert [card.work_id for card in overview.cards] == [WORK.work_id]
    assert overview.archived == 1


@pytest.mark.asyncio
async def test_the_recurring_views_never_read_the_whole_inventory(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", WORK)
    _backfilled_inventory(app, monkeypatch, tmp_path, *(WorkRef("eerovil/old", n)
                                                        for n in range(1, 40)))

    def forbidden():
        raise AssertionError("the whole inventory was read outside the archive view")

    monkeypatch.setattr(app.state.app_state.db, "load_works", forbidden, raising=False)
    overview = await _overview(app)
    assert overview.archived == 39
    assert "Archived Works (39)" in _client(app).get("/works").text
    assert _client(app).get(work_href("github_issue:eerovil/old#7")).status_code == 200


def test_the_archive_is_paged_so_a_long_history_stays_a_short_page(tmp_path):
    from agentdeck.web.routes_works import ARCHIVE_PAGE_SIZE

    app = _archive(tmp_path, 120)
    client = _client(app)

    first = client.get("/works?view=archived").text
    assert "Archived Works" in first and 'data-archived-count>120<' in first
    assert len(_archived_ids(first)) == ARCHIVE_PAGE_SIZE
    assert "Page 1 of 3" in first and 'rel="next"' in first and 'rel="prev"' not in first

    last = client.get("/works?view=archived&page=3").text
    assert len(_archived_ids(last)) == 120 - 2 * ARCHIVE_PAGE_SIZE
    # Newest first across pages, and no Work on two pages.
    seen = _archived_ids(first) + _archived_ids(client.get("/works?view=archived&page=2").text)
    seen += _archived_ids(last)
    assert len(set(seen)) == 120
    assert seen[0] == WorkRef("eerovil/agentdeck", 1).work_id
    # A page past the end shows the last page rather than nothing.
    assert _archived_ids(client.get("/works?view=archived&page=99").text) == _archived_ids(last)


def test_the_archive_searches_and_filters_by_repository(tmp_path):
    app = _archive(tmp_path, 30)
    client = _client(app)

    # Every word must appear, in any case.
    found = _archived_ids(client.get("/works?view=archived&q=TASK number 7").text)
    assert set(found) == {WorkRef("eerovil/agentdeck", n).work_id for n in (7, 17, 27)}

    trailhead = client.get("/works?view=archived&repo=EEROVIL/Trailhead")
    assert set(_archived_ids(trailhead.text)) == {
        WorkRef("eerovil/trailhead", n).work_id for n in (10, 20, 30)
    }
    assert "3 of 3 matching" in trailhead.text.replace("1–", "")
    # The issue spelling finds the Work too.
    assert _archived_ids(client.get("/works?view=archived&q=trailhead%2310").text) == [
        WorkRef("eerovil/trailhead", 10).work_id
    ]
    empty = client.get("/works?view=archived&q=nothing-like-this").text
    assert _archived_ids(empty) == [] and "No Archived Work matches." in empty


@pytest.mark.asyncio
async def test_the_dashboard_links_the_archive_with_its_count(tmp_path):
    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", WORK)
    _old(app, "claude_code:test:old", OTHER)
    _old(app, "claude_code:test:older", WorkRef("eerovil/trailhead", 7))

    overview = await _overview(app)

    assert [card.work_id for card in overview.cards] == [WORK.work_id]
    assert (overview.hidden, overview.archived) == (0, 2)
    from agentdeck.web.work_view import render_work_front

    html = render_work_front(app.state.templates, overview, "board")
    assert 'href="/works?view=archived"' in html and "Archived (2)" in html
    assert "older Works" not in html


def test_viewing_and_classifying_works_writes_nothing(tmp_path):
    from pathlib import Path

    from agentdeck.run_execution import ExecutionOutcome, OutcomeStore

    app = _archive(tmp_path, 12)
    db = Path(app.state.config.history.db_path)
    store = OutcomeStore(db)
    store.record(ExecutionOutcome(WORK.work_id, "g", "running", observed_at=NOW.timestamp()))
    store.close()

    def files() -> dict[str, bytes]:
        # The database and its write-ahead log; ``-shm`` is a reader's shared index, not data.
        return {p.name: p.read_bytes() for p in (db, db.with_name(db.name + "-wal")) if p.exists()}

    before = files()
    client = _client(app)
    for path in ("/works", "/works?view=archived&q=task", work_href(OTHER.work_id)):
        client.get(path)
    assert files() == before


# --- in the browser -------------------------------------------------------------------------------


def _browser_app(tmp_path, *unstarted: WorkRef):
    """Two Works and a manual chat, with a chat-local pull request; each of ``unstarted`` is
    an open issue no agent works on yet: a Work with no chat (#3558)."""
    app = _app(tmp_path, board=_board())
    if unstarted:
        from agentdeck.issue_context import IssueBacklog, IssueContext, RepositoryIssues

        issues = tuple(
            IssueContext(
                ref.repository, ref.number, f"Issue {ref.number}",
                f"https://github.com/{ref.repository}/issues/{ref.number}", "open",
                created_at=(NOW - timedelta(hours=1)).isoformat(),
            )
            for ref in unstarted
        )
        scope = BoardScope("agentdeck", org="eerovil", ownership_labels=("agent",),
                           issue_repositories=("eerovil/agentdeck",))
        backlog = IssueBacklog(
            repositories=(RepositoryIssues("eerovil/agentdeck", issues, present=True, age_s=60.0),),
            board=BoardView(scopes=(scope,)),
        )
        app.state.issues.read_board = lambda: backlog.board
        app.state.issues.cached = lambda: backlog
    # Mid-turn: something current keeps a Work the deck does not run itself listed.
    _add(app, "claude_code:test:worker", WORK, title="Promote Work to the UI", thinking=True)
    _add(app, "codex:test:review", WORK, title="Review the Work UI")
    _add(app, "claude_code:test:cleanup", OTHER, title="Remove legacy paths")
    _add(app, "claude_code:test:manual", thinking=True, title="A chat started by hand")
    return app


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_chat_page_loaded_on_its_own_opens_in_the_front_page_columns(tmp_path):
    """#2346: on a desktop, a chat page loaded on its own is laid out the way front page →
    Work → chat lays it out — the front page, the chat's Work and the chat side by side, each
    column on its own stream, and Back stepping chat → Work → front page. A chat no Work
    owns gets the front page and an empty Work column."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    await app.state.work_overview.refresh()
    work_path = work_href(WORK.work_id)
    chat_path = "/sessions/claude_code:test:worker"
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900},
                                      service_workers="block")
        await page.add_init_script(_SOURCE_LOG)
        await page.goto(base + chat_path)
        await page.wait_for_function(
            f"""() => document.querySelector('.front-columns > .session-sidebar #front-needs')
              && document.querySelector(
                '.front-columns > .work-column .work-main[data-work-id="{WORK.work_id}"]')
              && document.querySelector('.front-columns > .session-detail')"""
        )
        assert await page.evaluate("location.pathname") == chat_path
        await page.evaluate("() => { window.__samePage = true; }")
        # The chats list, its head and the board strip are gone from the page.
        for gone in ("#chat-list", "#board-strip", "#sessions"):
            assert await page.locator(gone).count() == 0, gone
        assert await page.locator(
            f'.front-columns > .session-sidebar .fr.is-open[data-work-id="{WORK.work_id}"]'
        ).count() == 1
        boxes = await page.evaluate(_column_boxes())
        assert boxes["front"]["right"] <= boxes["work"]["left"]
        assert boxes["work"]["right"] <= boxes["chat"]["left"]
        assert boxes["chat"]["right"] <= 1440
        await page.wait_for_function(
            f"""() => {{ const open = ({_OPEN_STREAMS})();
              return open.filter(u => u === '/events?view=works').length === 1
                && open.filter(u => u.startsWith('/events/works/')).length === 1
                && open.filter(u => u.startsWith('/events/sessions/')).length === 1; }}"""
        )
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-2346-chat-loaded-on-its-own.png"))

        # Back to the Work shows it with its own chat beside it, if any (#2345).
        await page.go_back()
        await page.wait_for_function(
            f"""() => {{
              const work = document.querySelector('.front-columns > .work-column .work-main');
              const chat = document.querySelector('.session-layout > .session-detail');
              return location.pathname === {work_path!r} && work
                && (!chat || chat.dataset.sessionKey === work.dataset.openChatKey); }}"""
        )
        await page.go_back()
        await page.wait_for_function(
            "() => location.pathname === '/' && !document.querySelector('.session-layout')"
        )
        await page.locator("main > #front-needs").wait_for(state="attached")
        assert await page.evaluate("window.__samePage") is True
        assert await page.evaluate("window.__doubled") == []

        # A reload keeps the entries under it rather than stacking them again.
        await page.go_forward()
        await page.go_forward()
        await page.wait_for_function(f"location.pathname === {chat_path!r}")
        length = await page.evaluate("history.length")
        await page.reload()
        await page.wait_for_function(
            "() => document.querySelector('.front-columns > .work-column .work-main')"
        )
        assert await page.evaluate("history.length") == length
        await page.close()

        page = await browser.new_page(viewport={"width": 1440, "height": 900},
                                      service_workers="block")
        await page.goto(base + "/sessions/claude_code:test:manual")
        await page.wait_for_function(
            "() => document.querySelector('.front-columns > .session-sidebar #front-needs')"
            " && document.querySelector('.front-columns > .work-column .work-column-empty')"
        )
        await page.go_back()
        await page.wait_for_function(
            "() => location.pathname === '/' && !document.querySelector('.session-layout')"
        )
        await page.close()

        # A phone keeps the chat page as it was: the chat over an empty slot, since a chat
        # page carries no list (#2603).
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.goto(base + chat_path)
        await page.wait_for_function("window.htmx")
        assert await page.locator(".front-columns").count() == 0
        assert await page.locator(".session-sidebar[data-list-empty]").count() == 1
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_work_left_behind_closes_its_stream(tmp_path):
    """#2359: switching Works from a chat page leaves one Work stream open, the shown one's,
    and a chat opened after a Work still has a working diff control.

    ``htmx.remove`` runs no cleanup, and a Work's stream sends only comments while the Work
    is unchanged, so every Work opened and left kept a stream rebuilding its page on the
    server — and every later click waited behind all of them."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    await app.state.work_overview.refresh()

    def row(work):
        return page.locator(f'.front-columns > .session-sidebar [data-work-id="{work.work_id}"]'
                            " a.fr-body").first

    async def shown(work):
        await page.wait_for_function(
            f"""() => document.querySelector(
              '.front-columns > .work-column .work-main[data-work-id="{work.work_id}"]')"""
        )

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900},
                                      service_workers="block")
        await page.add_init_script(_SOURCE_LOG)
        await page.goto(base + "/sessions/claude_code:test:worker")
        await shown(WORK)
        for work in (OTHER, WORK, OTHER):
            await row(work).click(no_wait_after=True)
            await shown(work)
        # Each Work has its stream again (#3228), and a Work left behind closes its own.
        other = quote(OTHER.work_id, safe=":")
        await page.wait_for_function(
            f"""() => {{ const open = ({_OPEN_STREAMS})()
                .filter(u => u.startsWith('/events/works/'));
              return open.length === 1 && open[0].includes({other!r}); }}"""
        )

        # Opening the Work dropped the chat's diff control; the next chat's brings a fresh
        # one, and that one opens the diff without a reload.
        await row(WORK).click(no_wait_after=True)
        await shown(WORK)
        await page.locator(".work-column #work-chats > summary").click()
        await page.locator(
            '.work-column #work-chats a[data-session-key="claude_code:test:worker"]'
        ).click(no_wait_after=True)
        await page.locator(".front-columns > .session-detail").wait_for()
        await page.locator("[data-session-diff-open]").click()
        await page.wait_for_function("document.body.classList.contains('diff-open')")
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_work_push_swaps_only_the_changed_card(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    from conftest import async_playwright
    from test_web import _live_server

    from agentdeck.web.work_view import row_sig

    app = _browser_app(tmp_path)
    await app.state.work_overview.refresh()
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        # The deck's own default: the chat list shut (conftest opens it for older tests).
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.add_init_script(
            "document.addEventListener('htmx:sseOpen', () => { window.__sseOpen = true; }, true)"
        )
        await page.add_init_script(_SOURCE_LOG)
        await page.goto(base + "/")
        this = page.locator(f'[data-work-id="{WORK.work_id}"]')
        other = page.locator(f'[data-work-id="{OTHER.work_id}"]')
        await this.wait_for()
        await page.wait_for_function("() => window.__sseOpen === true")
        await other.evaluate("el => { el.__kept = true; }")
        await this.evaluate("el => { el.__before = true; }")
        before = await this.get_attribute("data-work-rev")
        await other.locator(".fr-body").focus()

        _add(app, "codex:test:review", WORK, thinking=True, title="Review the Work UI")
        await app.state.work_overview.refresh()

        # The changed Work's row is replaced by one at its new revision...
        await page.wait_for_function(
            "([id, rev]) => { const el = document.querySelector(`[data-work-id=\"${id}\"]`);"
            " return el && !el.__before && el.dataset.workRev !== rev; }",
            arg=[WORK.work_id, before],
        )
        # ...and the unchanged one is the very same element, focus and all.
        assert await other.evaluate("el => el.__kept === true")
        assert await page.evaluate(
            "() => document.activeElement"
            " && document.activeElement.classList.contains('fr-body')"
            " && document.activeElement.closest('[data-work-id]').dataset.workId"
        ) == OTHER.work_id
        # The chat list stays shut and unrendered through it.
        assert await page.locator("#sessions a.session").count() == 0

        # #3228: a row being typed into is held, and swapped once the person is done.
        await this.evaluate(
            "el => { const box = document.createElement('textarea'); box.value = 'half';"
            " el.appendChild(box); box.focus(); el.__held = true; }"
        )
        held_rev = await this.get_attribute("data-work-rev")
        _add(app, "codex:test:review", WORK, title="Review the Work UI")
        await app.state.work_overview.refresh()
        await page.wait_for_function(
            "id => document.querySelector(`[data-work-id=\"${id}\"]`)._agentdeckOwed",
            arg=WORK.work_id,
        )
        assert await this.evaluate("el => el.__held === true")
        assert await this.get_attribute("data-work-rev") == held_rev
        # Done typing: the next push brings the owed row, though it is only a placeholder.
        await this.evaluate("el => { el.querySelector('textarea').remove(); }")
        _add(app, "claude_code:test:cleanup", OTHER, thinking=True, title="Remove legacy paths")
        await app.state.work_overview.refresh()
        await page.wait_for_function(
            "([id, rev]) => { const el = document.querySelector(`[data-work-id=\"${id}\"]`);"
            " return el && !el.__held && el.dataset.workRev !== rev; }",
            arg=[WORK.work_id, held_rev],
        )
        # A row the page lost comes back: its placeholder reopens the stream, which sends it.
        await this.evaluate("el => el.remove()")
        _add(app, "claude_code:test:cleanup", OTHER, title="Remove legacy paths")
        await app.state.work_overview.refresh()
        await this.wait_for()
        assert await page.locator("[data-work-same]").count() == 0
        # The same swap for any keyed list (#3228): `keyed-rows` on `data-key` and `data-rev`.
        kept = await page.evaluate("""() => {
          const list = document.createElement('div');
          list.innerHTML = '<p data-key="a" data-rev="1">a</p><p data-key="b" data-rev="1">b</p>';
          document.body.appendChild(list);
          const [a, b] = list.children;
          htmx.swap(list,
                    '<p data-key="b" data-rev="2">b2</p>'
                    + '<p data-key="a" data-rev="1" data-same></p>',
                    {swapStyle: 'keyed-rows'}, {contextElement: list});
          return [list.children[1] === a, list.children[0] !== b,
                  list.children[0].textContent, list.children.length];
        }""")
        assert kept == [True, True, "b2", 2]
        # The reconnect named the rows the page still showed, and not the one it lost.
        other_rev = await other.get_attribute("data-work-rev")
        last = await page.evaluate("() => window.__sources[window.__sources.length - 1].url")
        sigs = parse_qs(urlsplit(last).query)["sig"][0].split(".")
        assert row_sig(OTHER.work_id, other_rev) in sigs
        assert all(len(sig) == 8 for sig in sigs) and len(sigs) == len(set(sigs))
        # #3511: a Work shown in one part arrives as a placeholder in another, as one that moved
        # part does. The reconnect leaves it unnamed, so it comes whole, and a second loss
        # waits out the gap instead of reopening the stream at once.
        count = await page.evaluate("() => window.__sources.length")
        swap = (
            "id => { const part = document.getElementById('works-waiting');"
            " htmx.swap(part, part.innerHTML"
            " + `<article hidden data-work-same data-work-id=\"${id}\""
            " data-work-rev=\"x\"></article>`,"
            " {swapStyle: 'keyed-works'}, {contextElement: part}); }"
        )
        await page.evaluate(swap, OTHER.work_id)
        await page.wait_for_function("n => window.__sources.length > n", arg=count)
        last = await page.evaluate("() => window.__sources[window.__sources.length - 1].url")
        assert row_sig(OTHER.work_id, other_rev) not in parse_qs(urlsplit(last).query).get(
            "sig", [""]
        )[0]
        await page.evaluate(swap, OTHER.work_id)
        await page.wait_for_timeout(300)
        assert await page.evaluate("() => window.__sources.length") == count + 1
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_an_open_disclosure_survives_its_section_changing(tmp_path):
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.goto(base + work_href(WORK.work_id))
        inspector = page.locator("#work-inspector")
        await inspector.locator(":scope > summary").click()
        assert await inspector.evaluate("el => el.open")

        _add(app, "claude_code:test:third", WORK, title="A third chat")
        # The inspector is redrawn live (#3228) or on the next load; the disclosure has to
        # survive either redraw.
        await page.reload()

        await page.locator('#work-inspector [data-wd-leg="claude_code:test:third"]').wait_for(
            state="attached", timeout=15000
        )
        await page.wait_for_function(
            "() => document.querySelector('#work-inspector').open === true"
        )
        # Another Work's page in the same tab keeps its own disclosure shut...
        await page.goto(base + work_href(OTHER.work_id))
        assert await page.locator("#work-inspector").evaluate("el => el.open") is False
        # ...and this Work's comes back open.
        await page.goto(base + work_href(WORK.work_id))
        await page.wait_for_function(
            "() => document.querySelector('#work-inspector').open === true"
        )
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "work"])
async def test_the_work_ui_fits_a_320px_phone(tmp_path, path):
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    url = work_href(WORK.work_id) if path == "work" else path
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 640},
                                      service_workers="block")
        await page.goto(base + url)
        root = page.locator("main" if path == "/" else "main.work-main")
        await root.wait_for()
        state = page.locator(f'[data-work-id="{WORK.work_id}"] .fr-line').first
        if path == "work":
            state = page.locator("#work-status[data-wk-run-state]")
        assert await state.is_visible()
        overflow = await root.evaluate(
            """el => [...el.querySelectorAll('*')].filter(
                node => node.getClientRects().length
                  && node.getBoundingClientRect().right > window.innerWidth + 1
            ).map(node => node.className || node.tagName)"""
        )
        assert overflow == []
        assert await page.evaluate("() => document.documentElement.scrollWidth") <= 320
        if path == "work":
            # Provider and session detail stays collapsed on a phone, and so do Runs and Chats.
            for fold in ("#work-inspector", "#work-runs", "#work-chats"):
                assert await page.locator(fold).evaluate("el => el.open") is False, fold
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_front_page_row_grows_to_fit_its_title_and_detail_line(tmp_path):
    """#2526 gave the title the row's whole width; #3386 lets the row grow until all its text
    fits: the whole title shows, the detail line wraps rather than ending in "...", and the age
    and repo# still end the detail line on the right. A short row stays as tall as before."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.goto(base + "/")
        row = page.locator(f'article.fr[data-work-id="{WORK.work_id}"]').first
        await row.wait_for()
        assert await row.locator(".fr-side").count() == 0
        assert await row.locator(".fr-line > .fr-when > .fr-age").is_visible()
        assert await row.locator(".fr-line > .fr-when > .fr-ref").is_visible()
        one_line = await row.evaluate("el => el.offsetHeight")
        layout = await row.evaluate(
            """el => {
                const title = el.querySelector('.fr-title');
                const line = el.querySelector('.fr-line');
                const reason = el.querySelector('.fr-reason');
                const when = el.querySelector('.fr-when');
                const titleRight = (() => {
                  title.textContent = 'Front-page Work titles cut off on a phone: give the title'
                    + ' the whole row and two lines';
                  return title.getBoundingClientRect().right;
                })();
                title.textContent = title.textContent.repeat(3);
                reason.textContent = 'The reviewer asked for changes: '.padEnd(200, ' word');
                const titleHeight = parseFloat(getComputedStyle(title).lineHeight);
                const lineHeight = parseFloat(getComputedStyle(line).lineHeight);
                const box = el.getBoundingClientRect();
                const at = when.getBoundingClientRect();
                return {
                  titleClipped: title.scrollHeight > title.clientHeight + 1,
                  titleLines: Math.round(title.clientHeight / titleHeight),
                  lineClipped: line.scrollHeight > line.clientHeight + 1
                    || line.scrollWidth > line.clientWidth + 1,
                  detailLines: Math.round(line.clientHeight / lineHeight),
                  titleRight,
                  whenInside: at.right <= box.right + 1 && at.bottom <= box.bottom + 1,
                  whenRight: box.right - at.right,
                  rowRight: box.right,
                };
            }"""
        )
        assert layout["titleClipped"] is False, layout
        assert layout["titleLines"] > 2, layout
        assert layout["lineClipped"] is False, layout
        assert layout["detailLines"] > 1, layout
        assert layout["rowRight"] - layout["titleRight"] < 8, layout
        assert layout["whenInside"] and layout["whenRight"] < 8, layout
        assert await row.locator(".fr-line > .fr-when > .fr-age").is_visible()
        assert await row.evaluate("el => el.offsetHeight") > one_line
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_house_reaches_the_front_page_from_a_chat_opened_over_a_work(tmp_path):
    """A chat opened from a Work page layers over that Work. On a phone its arrow goes to the
    front page, skipping the Work (#2878); on a desktop the house goes there: it used to be
    swallowed as "reveal the list", which was already showing, so the tap did nothing
    (#2266)."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    work_path = work_href(WORK.work_id)
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for width, height in ((390, 844), (1280, 900)):
            page = await browser.new_page(viewport={"width": width, "height": height},
                                          service_workers="block")
            await page.goto(base + work_path)
            await page.wait_for_function("window.htmx")
            # The Work's own chat list, folded shut on a phone (#2218).
            await page.locator("#work-chats > summary").click()
            # Opened in place over the Work, so there is no page load to wait for.
            await page.locator(
                '#work-chats a[data-session-key="claude_code:test:worker"]'
            ).click(no_wait_after=True)
            await page.wait_for_function(
                "document.body.classList.contains('session-page')"
                " && !!document.querySelector('.session-layout > .session-detail')"
            )
            if width < 1000:
                await page.wait_for_function(
                    "document.body.classList.contains('mobile-session-stack-ready')"
                )
                await page.locator("a.back").click()
            else:
                await page.locator("a.brand").click()
            await page.wait_for_function(
                "location.pathname === '/' && !!document.body"
                " && !document.body.classList.contains('session-page')"
            )
            await page.locator("#front-needs").wait_for(state="attached")
            if width < 1000 and screenshot_dir:
                await page.screenshot(
                    path=str(Path(screenshot_dir) / "issue-2266-front-after-house.png")
                )
            await page.close()
        await browser.close()


def test_work_page_disclosures_are_keyed_by_work(tmp_path):
    app = _app(tmp_path, board=_board())
    _add(app, "claude_code:test:worker", WORK)
    html = _client(app).get(work_href(WORK.work_id)).text
    keys = re.findall(r'data-keep-open="([^"]+)"', html)
    assert f"work-inspector:{WORK.work_id}" in keys and f"work-chats:{WORK.work_id}" in keys
    # No disclosure on the page shares a key another Work's page could also use.
    assert all(WORK.work_id in key for key in keys)


_SOURCE_LOG = """
(() => {
  const Native = window.EventSource;
  window.__sources = [];
  window.EventSource = function (url, init) {
    const source = new Native(url, init);
    window.__sources.push(source);
    return source;
  };
  window.EventSource.prototype = Native.prototype;
  Object.assign(window.EventSource, {CONNECTING: 0, OPEN: 1, CLOSED: 2});
  // A message a stream delivers is one event object; seeing it twice means a live part was
  // bound to that stream twice and swapped the same push twice.
  window.__handled = new Map();
  window.__doubled = [];
  document.addEventListener('htmx:sseBeforeMessage', e => {
    const seen = (window.__handled.get(e.detail) || 0) + 1;
    window.__handled.set(e.detail, seen);
    if (seen > 1) window.__doubled.push(e.detail.type);
  }, true);
})();
"""

# Each open stream's path and query, less the rows a Works stream names on connecting (#3228).
_OPEN_STREAMS = """() => window.__sources.filter(s => s.readyState !== 2)
  .map(s => { const url = new URL(s.url); url.searchParams.delete('sig');
              return url.pathname + url.search; })"""


def _column_boxes():
    return """() => Object.fromEntries(
      [['front', '.front-columns > .session-sidebar'], ['work', '.front-columns > .work-column'],
       ['chat', '.front-columns > .session-detail'], ['diff', '.front-columns > .session-diff']]
      .map(([name, sel]) => {
        const el = document.querySelector(sel);
        const box = el && el.getBoundingClientRect();
        return [name, box && box.width > 0 && getComputedStyle(el).visibility !== 'hidden'
          ? {left: box.left, right: box.right, width: box.width} : null];
      }))"""


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_desktop_shows_the_front_page_a_work_and_a_chat_side_by_side(tmp_path):
    """#2290: on a desktop, opening a Work from the front page puts it beside the front page,
    and its chat beside both (#2345) — no page load, each column on its own live stream, Back
    stepping chat → Work → front page, and the diff as a fourth column."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    _add(app, "claude_code:test:worker", WORK, title="Promote Work to the UI", cwd=tmp_path,
         thinking=True)
    await app.state.work_overview.refresh()
    work_path = work_href(WORK.work_id)
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")

    async def shot(page, name):
        if screenshot_dir:
            await page.screenshot(path=str(Path(screenshot_dir) / f"issue-2290-{name}.png"))

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900},
                                      service_workers="block")
        await page.add_init_script(_SOURCE_LOG)
        await page.goto(base + "/")
        await page.wait_for_function("window.htmx")
        # Gone if anything below loads a page instead of opening a column.
        await page.evaluate("() => { window.__samePage = true; }")
        row = page.locator(f'#front-needs [data-work-id="{WORK.work_id}"] a.fr-body, '
                           f'.front-rows [data-work-id="{WORK.work_id}"] a.fr-body').first
        await row.click(no_wait_after=True)
        await page.wait_for_function(
            f"""() => document.querySelector(
              '.front-columns > .work-column .work-main[data-work-id="{WORK.work_id}"]')"""
        )
        assert await page.evaluate("location.pathname") == work_path
        assert await page.evaluate("window.__samePage") is True
        # The front page stays, live, with the open Work's row marked.
        assert await page.locator(".front-columns > .session-sidebar #front-needs").count() == 1
        assert await page.locator(
            f'.front-columns > .session-sidebar .fr.is-open[data-work-id="{WORK.work_id}"]'
        ).count() == 1
        boxes = await page.evaluate(_column_boxes())
        assert boxes["front"] and boxes["work"] and boxes["front"]["right"] <= boxes["work"]["left"]
        # The Work's own chat opens beside it (#2345), at the Work's address and title. No chat
        # takes a message here, so it is the one working.
        await page.wait_for_function(
            """() => document.querySelector(
              '.front-columns > .session-detail[data-session-key="claude_code:test:worker"]')"""
        )
        assert await page.evaluate("location.pathname") == work_path
        assert (await page.title()).startswith("eerovil/agentdeck#2147 · ")
        assert await page.evaluate("window.__samePage") is True
        boxes = await page.evaluate(_column_boxes())
        assert boxes["work"]["right"] <= boxes["chat"]["left"]
        await page.wait_for_function(
            f"""() => {{ const open = ({_OPEN_STREAMS})();
              return open.filter(u => u === '/events?view=works').length === 1
                && open.filter(u => u.startsWith('/events/works/')).length === 1
                && open.filter(u => u.startsWith('/events/sessions/')).length === 1; }}"""
        )
        await shot(page, "front-work-and-its-chat")

        # A chat of that Work opened by hand takes the third column; the other two stay put.
        await page.locator(".work-column #work-chats > summary").click()
        await page.locator(
            '.work-column #work-chats a[data-session-key="claude_code:test:worker"]'
        ).click(no_wait_after=True)
        # The auto-opened chat is already showing, so wait for the hand-opened one's address.
        await page.wait_for_function(
            "() => document.querySelector('.front-columns > .session-detail')"
            " && /^\\/(chats|sessions)\\//.test(location.pathname)"
        )
        assert await page.evaluate("window.__samePage") is True
        boxes = await page.evaluate(_column_boxes())
        assert boxes["front"]["right"] <= boxes["work"]["left"]
        assert boxes["work"]["right"] <= boxes["chat"]["left"]
        assert boxes["chat"]["right"] <= 1440
        # Front page and Work keep their own streams; the body's follows the chat.
        await page.wait_for_function(
            f"""() => {{ const open = ({_OPEN_STREAMS})();
              return open.filter(u => u === '/events?view=works').length === 1
                && open.filter(u => u.startsWith('/events/works/')).length === 1
                && open.filter(u => u.startsWith('/events/sessions/')).length === 1; }}"""
        )
        await shot(page, "front-work-chat")

        # The diff is a fourth column, and the chat stays readable beside it.
        await page.locator("[data-session-diff-open]").click()
        await page.wait_for_function("document.body.classList.contains('diff-open')")
        boxes = await page.evaluate(_column_boxes())
        assert boxes["chat"] and boxes["diff"]
        assert boxes["work"]["right"] <= boxes["chat"]["left"]
        assert boxes["chat"]["right"] <= boxes["diff"]["left"]
        assert boxes["diff"]["right"] <= 1440
        await shot(page, "with-diff")

        # Back shuts the diff, then returns to the Work with its own chat still beside it, then
        # to the front page alone.
        await page.go_back()
        await page.wait_for_function("!document.body.classList.contains('diff-open')")
        await page.go_back()
        await page.wait_for_function(
            f"""() => location.pathname === {work_path!r}
              && document.querySelector('.front-columns > .work-column .work-main')"""
        )
        await page.locator(
            '.front-columns > .session-detail[data-session-key="claude_code:test:worker"]'
        ).wait_for(state="attached")
        assert await page.locator(".front-columns > .session-detail").count() == 1
        assert (await page.title()).startswith("eerovil/agentdeck#2147 · ")
        await page.go_back()
        await page.wait_for_function(
            "() => location.pathname === '/' && !document.querySelector('.session-layout')"
            " && !document.body.classList.contains('session-page')"
        )
        await page.locator("main > #front-needs").wait_for(state="attached")
        assert await page.evaluate("window.__samePage") is True

        # A chat no Work owns opens in the chat column and leaves the Work column empty.
        # "Other activity" is open by default since #2283.
        await page.locator(
            '#work-other a[data-session-key="claude_code:test:manual"]'
        ).click(no_wait_after=True)
        await page.wait_for_function(
            "() => document.querySelector('.front-columns > .session-detail')"
        )
        assert await page.locator(".front-columns > .work-column .work-column-empty").count() == 1
        boxes = await page.evaluate(_column_boxes())
        assert boxes["front"]["right"] <= boxes["work"]["left"]
        assert boxes["work"]["right"] <= boxes["chat"]["left"]
        # The house returns to the front page alone.
        await page.locator("a.brand").click()
        await page.wait_for_function(
            "() => location.pathname === '/' && !document.querySelector('.session-layout')"
        )
        assert await page.evaluate("window.__samePage") is True
        # One binding per live part on every stream, all along.
        assert await page.evaluate("window.__doubled") == []
        await page.close()

        # A Work page loaded on its own gets the front page and its chat beside it, and Back
        # under it.
        page = await browser.new_page(viewport={"width": 1440, "height": 900},
                                      service_workers="block")
        await page.add_init_script(_SOURCE_LOG)
        await page.goto(base + work_path)
        await page.wait_for_function(
            "() => document.querySelector('.front-columns > .session-sidebar #front-needs')"
            " && document.querySelector('.front-columns > .work-column .work-main')"
            " && document.querySelector('.front-columns > .session-detail')"
        )
        assert await page.evaluate("location.pathname") == work_path
        await page.wait_for_function(
            f"""() => {{ const open = ({_OPEN_STREAMS})();
              return open.filter(u => u === '/events?view=works').length === 1
                && open.filter(u => u.startsWith('/events/works/')).length === 1
                && open.filter(u => u.startsWith('/events/sessions/')).length === 1; }}"""
        )
        await page.go_back()
        await page.wait_for_function(
            "() => location.pathname === '/' && !document.querySelector('.session-layout')"
        )
        await page.locator("main > #front-needs").wait_for(state="attached")
        assert await page.evaluate("window.__doubled") == []
        # A phone builds no columns: its row opens the Work's chat over the front page
        # (#2512, ``test_a_phone_opens_a_works_chat_straight_from_the_front_page``).
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_stream_restart_leaves_an_unchanged_other_activity_list_in_place(tmp_path):
    """#2739: every stream the front page opens pushes Other activity at once, and Back to the
    front page opens one. Replacing an unchanged list under a press loses the click, so a push
    that matches what is shown leaves the very same link in place; a real change still swaps."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    await app.state.work_overview.refresh()
    link = '#work-other a[data-session-key="claude_code:test:manual"]'

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900},
                                      service_workers="block")
        await page.add_init_script(
            """document.addEventListener('htmx:sseBeforeMessage', e => {
                 if (e.target.id === 'work-other') window.__otherPushes += 1; }, true);
               window.__otherPushes = 0;"""
        )
        await page.goto(base + "/")
        await page.wait_for_function("window.htmx")
        await page.evaluate(f"() => {{ window.__link = document.querySelector('{link}'); }}")
        # The stream's first push, then a restart's, each the list the page already shows.
        await page.wait_for_function("window.__otherPushes >= 1")
        await page.evaluate("() => htmx.reconnectSSE(document.body, true)")
        await page.wait_for_function("window.__otherPushes >= 2")
        assert await page.evaluate("window.__link.isConnected") is True
        # A fold the reader made is theirs, and does not make a push look new.
        await page.locator("#work-other-fold > summary").click()
        await page.evaluate("() => htmx.reconnectSSE(document.body, true)")
        await page.wait_for_function("window.__otherPushes >= 3")
        assert await page.evaluate("window.__link.isConnected") is True
        assert await page.evaluate(
            "document.getElementById('work-other-fold').open") is False
        await page.locator("#work-other-fold > summary").click()
        # A chat's new title is a change: the list is swapped and shows it.
        _add(app, "claude_code:test:manual", thinking=True, title="Renamed by hand")
        await app.state.work_overview.refresh()
        await page.locator(link, has_text="Renamed by hand").wait_for()
        assert await page.evaluate("window.__link.isConnected") is False
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_clicking_a_work_row_again_while_it_loads_still_opens_it(tmp_path):
    """#2422: pressing a Work's row again before the Work arrives used to start a new request
    and throw the first answer away, so a reader clicking faster than the Work loaded never
    got it open. A repeat press joins the load already under way, on a desktop's column and
    on a phone's page load alike, and the first press is answered at once: a desktop's Work
    column says it is opening, and a phone's row spins."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    _add(app, "claude_code:test:worker", WORK, title="Promote Work to the UI", cwd=tmp_path)
    await app.state.work_overview.refresh()
    work_path = work_href(WORK.work_id)
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for viewport in ({"width": 1440, "height": 900}, {"width": 390, "height": 844}):
            page = await browser.new_page(viewport=viewport, service_workers="block")
            await page.goto(base + "/")
            await page.wait_for_function("window.htmx")
            requests = []
            release = asyncio.Event()

            def held(requests, release):
                async def slow_work(route):
                    requests.append(route.request.url)
                    await release.wait()
                    await route.continue_()
                return slow_work

            await page.route(f"**{work_path}", held(requests, release))
            # Let go of a held request whatever happens, or the page never closes.
            page.on("close", lambda _page, release=release: release.set())
            # Clicked from inside the page, and not awaited there: Playwright's own click and
            # evaluate both wait out the page load the first press starts on a phone, which
            # is the wait a reader does not do. The page reports what the row showed.
            seen = asyncio.Queue()
            await page.expose_function(f"reportRow{viewport['width']}", seen.put_nowait)
            await page.evaluate(
                """([id, report]) => { setTimeout(async () => {
                  const row = document.querySelector(`[data-work-id="${id}"] a.fr-body`);
                  // A phone opens a row's chat rather than its Work (#2512); a row with no
                  // chat yet still loads the Work page, which is the load this repeats.
                  if (innerWidth < 1000) delete row.dataset.chatHref;
                  for (let i = 0; i < 4; i++) {
                    row.click();
                    await new Promise(r => setTimeout(r, 50));
                  }
                  window[report](row.closest('.fr').classList.contains('fr-opening'));
                }, 0); }""",
                [WORK.work_id, f"reportRow{viewport['width']}"],
            )
            opening = await asyncio.wait_for(seen.get(), 10)
            if viewport["width"] > 1000:
                # The press is answered before the Work arrives.
                loading = page.locator(
                    ".front-columns > .work-column[data-work-loading] [role=status]"
                )
                title = await page.locator(
                    f'[data-work-id="{WORK.work_id}"] .fr-title').first.inner_text()
                assert await loading.inner_text() == f"Opening {title.strip()}…"
                assert await page.evaluate("location.pathname") == work_path
                assert await page.locator(
                    f'.front-columns > .session-sidebar .fr.is-open[data-work-id="{WORK.work_id}"]'
                ).count() == 1
                if screenshot_dir:
                    await page.screenshot(path=str(Path(screenshot_dir) / "issue-2422-loading.png"))
            else:
                assert opening is True
            assert len(requests) == 1, (viewport, requests)
            release.set()
            await page.wait_for_function(f"location.pathname === {work_path!r}")
            await page.locator(f'.work-main[data-work-id="{WORK.work_id}"]').wait_for()
            assert len(requests) == 1, (viewport, requests)
            await page.close()
        await browser.close()


_SET_VISIBILITY = """state => {
  Object.defineProperty(document, 'hidden', {get: () => state === 'hidden', configurable: true});
  Object.defineProperty(document, 'visibilityState', {get: () => state, configurable: true});
  document.dispatchEvent(new Event('visibilitychange'));
}"""


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_desktop_columns_stay_live_after_the_tab_was_hidden(tmp_path):
    """#2406: hiding the tab closes every stream, the front page's and the Work's columns
    included; showing it again has to reopen all three, not only the body's (the chat's), or
    the front page's cards never move again until a reload."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    # A working chat, so opening the Work opens it beside the Work (#2345): three columns.
    _add(app, "claude_code:test:worker", WORK, title="Promote Work to the UI", cwd=tmp_path,
         thinking=True)
    await app.state.work_overview.refresh()
    three_open = f"""() => {{ const open = ({_OPEN_STREAMS})();
      return open.length === 3 && open.filter(u => u === '/events?view=works').length === 1
        && open.filter(u => u.startsWith('/events/works/')).length === 1
        && open.filter(u => u.startsWith('/events/sessions/')).length === 1; }}"""
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900},
                                      service_workers="block")
        await page.add_init_script(_SOURCE_LOG)
        await page.goto(base + "/")
        await page.wait_for_function("window.htmx")
        await page.locator(f'.front-rows [data-work-id="{WORK.work_id}"] a.fr-body').first.click(
            no_wait_after=True
        )
        await page.wait_for_function(three_open)

        await page.evaluate(_SET_VISIBILITY, "hidden")
        await page.wait_for_function(f"() => ({_OPEN_STREAMS})().length === 0")
        await page.evaluate(_SET_VISIBILITY, "visible")
        await page.wait_for_function(three_open)

        # And the front page column really follows its reopened stream: a Work change moves it.
        row = page.locator(f'.front-columns > .session-sidebar [data-work-id="{OTHER.work_id}"]')
        await row.first.evaluate("el => { el.__before = true; }")
        before = await row.first.get_attribute("data-work-rev")
        _add(app, "codex:test:other-review", OTHER, thinking=True, title="Review the cleanup")
        await app.state.work_overview.refresh()
        await page.wait_for_function(
            "([id, rev]) => { const el = document.querySelector("
            "`.front-columns > .session-sidebar [data-work-id=\"${id}\"]`);"
            " return el && !el.__before && el.dataset.workRev !== rev; }",
            arg=[OTHER.work_id, before],
        )
        # One binding per live part on every stream, reopened or not.
        assert await page.evaluate("window.__doubled") == []
        await browser.close()


async def _open_worker_chat_over_the_work(page, base, work_path):
    await page.goto(base + work_path)
    await page.wait_for_function("window.htmx")
    await page.locator("#work-chats > summary").click()
    await page.locator(
        '#work-chats a[data-session-key="claude_code:test:worker"]'
    ).click(no_wait_after=True)
    await page.wait_for_function(
        "document.body.classList.contains('mobile-session-stack-ready')"
        " && !!document.querySelector('.session-layout > .session-detail')"
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_back_after_a_reload_over_a_work_shows_the_work(tmp_path):
    """A chat opened over a Work, then reloaded — a deploy, the phone restoring the app — is
    a chat page of its own, whose built-in list is the chats view. Back used to reveal that
    list under another page's address (#2281); it loads the front page, skipping the Work
    the chat took the place of (#2878)."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    work_path = work_href(WORK.work_id)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await _open_worker_chat_over_the_work(page, base, work_path)
        chat_path = await page.evaluate("location.pathname")
        await page.reload()
        await page.wait_for_function(
            "document.body.classList.contains('mobile-session-stack-ready')"
        )
        assert await page.evaluate("location.pathname") == chat_path
        await page.go_back()
        await page.wait_for_function(
            "location.pathname === '/' && !!document.body"
            " && !document.body.classList.contains('session-page')"
        )
        await page.locator("#front-needs").wait_for(state="attached")
        assert await page.locator('.session-sidebar[data-list-path="/chats"]').count() == 0
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_back_from_a_chat_opened_over_a_work_reaches_the_front_page(tmp_path):
    """#2878: on a phone, Back from a chat opened over a Work is the front page, not the
    Work. (Before, it closed in place to the Work, whose ← had to stay visible, #2281.)"""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    work_path = work_href(WORK.work_id)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await _open_worker_chat_over_the_work(page, base, work_path)
        await page.go_back()
        await page.wait_for_function(
            "location.pathname === '/' && !!document.body"
            " && !document.body.classList.contains('session-page')"
        )
        await page.locator("#front-needs").wait_for(state="attached")
        await browser.close()



@private_feature(_chat_review)
def test_other_activity_counts_only_chats_a_person_can_act_on_here(tmp_path):
    """#2332: three 22-hour-old probe chats whose worktree was gone read "3 need you", and a
    finished handoff was marked as needing attention though the count left it out."""
    here = tmp_path / "here"
    here.mkdir()
    app = _app(tmp_path)
    live = _add(app, "claude_code:test:live", cwd=here)
    gone = _add(app, "claude_code:test:gone", cwd=tmp_path / "removed-worktree")
    done = _add(app, "claude_code:test:done", cwd=here)
    # #3101: a waiting reviewer's chat is no chat of a person's, so it is neither row nor count.
    review = _add(
        app,
        "claude_code:test:review",
        cwd=here,
        initial_prompt="You are AgentDeck's Claude reviewer. ChatGPT reviews are switched off",
    )
    _attention(
        app,
        _insight(live, "waiting"),
        _insight(gone, "waiting"),
        _insight(done, "finished"),
        _insight(review, "waiting"),
    )

    html = _client(app).get("/").text

    assert 'data-other-waiting="1"' in html, html[html.find('id="work-other"'):][:3000]
    classes = {
        key: cls
        for cls, key in re.findall(
            r'<li class="work-other-row([^"]*)"[^>]*>\s*<a href="[^"]*" data-session-key="([^"]+)"',
            html,
        )
    }
    assert "needs-attention" in classes[live]
    assert "needs-attention" not in classes[gone]
    assert "needs-attention" not in classes[done]
    assert review not in classes


def test_a_chat_in_no_work_holding_a_background_command_stays_working_in_other_activity(
    tmp_path,
):
    """#2783 review: only a Work leaves a background command out; the chat row keeps it."""
    app = _app(tmp_path)
    server = _add(app, "claude_code:test:server", thinking=True, title="Dev server",
                  activity=BACKGROUND_TASK_ACTIVITY, status=SessionStatus.LIVE,
                  last_activity=NOW - timedelta(days=2))

    html = _client(app).get("/").text

    classes = {
        key: cls
        for cls, key in re.findall(
            r'<li class="work-other-row([^"]*)"[^>]*>\s*<a href="[^"]*" data-session-key="([^"]+)"',
            html,
        )
    }
    assert "is-working" in classes[server], html[html.find('id="work-other"'):][:3000]


def test_the_work_page_names_the_chat_a_desktop_opens_beside_it(tmp_path):
    """#2345: the Work page names one chat for a desktop to open beside the Work — the one a
    message would go to, else the Work's first chat — and a Work with no chat names none."""
    app = _browser_app(tmp_path, UNSTARTED)
    client = _client(app)
    html = client.get(work_href(WORK.work_id)).text
    main = re.search(r'<main class="work-main"[^>]*>', html).group(0)
    key = re.search(r'data-open-chat-key="([^"]+)"', main).group(1)
    path = re.search(r'data-open-chat="([^"]+)"', main).group(1)
    assert key in {"claude_code:test:worker", "codex:test:review"}
    target = re.search(r'data-wd-message-target="([^"]+)"', html)
    if target:
        assert key == target.group(1)
    assert path.startswith(("/chats/", "/sessions/"))
    assert f'href="{path}" data-session-key="{key}"' in html

    html = client.get(work_href(UNSTARTED.work_id)).text
    assert "data-open-chat" not in html


def test_a_front_page_row_names_the_chat_its_work_opens_into(tmp_path):
    """#2512: a front-page row carries the chat its Work opens into — the same one the Work
    page names for a desktop (#2345) — and still links to the Work; a Work with no chat yet
    names none."""
    app = _browser_app(tmp_path, UNSTARTED)
    client = _client(app)
    front = client.get("/").text
    page = client.get(work_href(WORK.work_id)).text
    opens = re.search(r'data-open-chat="([^"]+)"', page).group(1)

    def body(work):
        row = re.search(rf'<article[^>]*data-work-id="{re.escape(work.work_id)}".*?</article>',
                        front, flags=re.S)
        assert row is not None, work
        return re.search(r'<a class="fr-body"[^>]*>', row.group(0)).group(0)

    link = body(WORK)
    assert f'href="{work_href(WORK.work_id)}"' in link
    assert f'data-chat-href="{opens}"' in link
    assert "data-chat-href" not in body(UNSTARTED)


def test_opening_a_works_chat_counts_as_opening_the_work(tmp_path):
    """#2512: a phone's row opens the chat instead of the Work page, so opening a chat one
    Work owns is what clears that Work's new mark (#2283); a shared chat marks neither."""
    app = _app(tmp_path)
    worker = _add(app, "claude_code:test:worker", WORK)
    shared = _add(app, "claude_code:test:shared", WORK, OTHER)
    control = app.state.control_db
    client = _client(app)

    client.get(f"/sessions/{shared}")
    assert WORK.work_id not in control.works_opened()
    assert OTHER.work_id not in control.works_opened()
    client.get(f"/sessions/{worker}", headers={"x-agentdeck-session-navigation": "detail"})
    assert WORK.work_id in control.works_opened()
    assert OTHER.work_id not in control.works_opened()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_phone_opens_a_shared_chat_with_the_work_it_tapped(tmp_path):
    """#2567: a chat two Works share, opened from one Work's front-page row on a phone, links
    up to that Work — and still does after a reload and after Back and Forward. Back steps
    through the tapped Work to the front page (#2854)."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    shared = _add(app, "claude_code:test:shared", WORK, OTHER, title="Spec weekly SEO audit",
                  cwd=tmp_path, thinking=True)
    await app.state.work_overview.refresh()
    chat_path = app.state.app_state.sessions[shared].detail_path
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.goto(base + "/")
        await page.wait_for_function("window.htmx")
        link = page.locator(".session-detail-head a.session-work-link")
        for work in (OTHER, WORK):
            await page.locator(f'[data-work-id="{work.work_id}"] a.fr-body').first.click()
            await page.wait_for_function(
                "path => location.pathname === path && location.search.startsWith('?work=')"
                " && !!document.querySelector('.session-layout > .session-detail')",
                arg=chat_path,
            )
            assert await link.get_attribute("href") == work_href(work.work_id)
            assert await link.is_visible()
            if screenshot_dir and work == OTHER:
                await page.screenshot(
                    path=str(Path(screenshot_dir) / "issue-2567-shared-chat-work-link.png")
                )
            # Back is the front page, in place, not the Work (#2878).
            await page.go_back()
            await page.wait_for_function(
                "location.pathname === '/'"
                " && document.body.classList.contains('mobile-list-open')"
            )
            await page.wait_for_function("document.readyState === 'complete'")
        await page.go_forward()
        await page.wait_for_function(
            "path => location.pathname === path && !!document.querySelector('.session-detail')"
            " && !document.body.classList.contains('mobile-list-open')",
            arg=chat_path,
        )
        assert await link.get_attribute("href") == work_href(WORK.work_id)
        await page.reload()
        await link.wait_for()
        assert await link.get_attribute("href") == work_href(WORK.work_id)
        await link.click()
        await page.wait_for_function(f"location.pathname === {work_href(WORK.work_id)!r}")
        await page.locator("body.work-page main.work-main").wait_for()
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-2567-tapped-work-page.png")
            )
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_phone_opens_a_works_chat_straight_from_the_front_page(tmp_path):
    """#2512: on a phone almost every visit to a Work is to its chat. A row opens its Work's
    chat over the front page, without a page load; the chat's header reaches the Work page,
    at no extra height; Back from the chat, and from the Work its header reaches, is the
    front page (#2878). A Work with no chat yet still opens its page."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path, UNSTARTED)
    # A working chat, so it is the one the Work opens into, whatever order the two idle
    # chats would tie in.
    _add(app, "claude_code:test:worker", WORK, title="Promote Work to the UI", cwd=tmp_path,
         thinking=True)
    await app.state.work_overview.refresh()
    work_path = work_href(WORK.work_id)
    opens = re.search(
        r'data-open-chat="([^"]+)"', _client(app).get(work_path).text
    ).group(1)
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.goto(base + "/")
        await page.wait_for_function("window.htmx")
        await page.evaluate("window.__samePage = true")
        if screenshot_dir:
            await page.screenshot(path=str(Path(screenshot_dir) / "issue-2512-front-page.png"))
        await page.locator(f'[data-work-id="{WORK.work_id}"] a.fr-body').first.click()
        await page.wait_for_function(
            "path => location.pathname === path"
            " && document.body.classList.contains('mobile-session-stack-ready')"
            " && !!document.querySelector('.session-layout > .session-detail')",
            arg=opens,
        )
        assert await page.evaluate("window.__samePage") is True
        link = page.locator(".session-detail-head a.session-work-link")
        assert await link.is_visible()
        assert await link.get_attribute("href") == work_path
        head = page.locator(".session-detail-head")
        with_link = (await head.bounding_box())["height"]
        if screenshot_dir:
            await page.screenshot(path=str(Path(screenshot_dir) / "issue-2512-chat-from-row.png"))
        await link.evaluate("el => { el.hidden = true; el.style.display = 'none'; }")
        assert (await head.bounding_box())["height"] == with_link
        await link.evaluate("el => { el.hidden = false; el.style.display = ''; }")

        # Back is the front page, in place: the Work is not in between (#2878).
        await page.go_back()
        await page.wait_for_function(
            "location.pathname === '/' && document.body.classList.contains('mobile-list-open')"
        )
        assert await page.evaluate("window.__samePage") is True
        assert not await page.locator("body.work-page").count()
        if screenshot_dir:
            await page.wait_for_function("document.getAnimations().length === 0")
            await page.screenshot(path=str(Path(screenshot_dir) / "issue-2878-back-to-front.png"))
        # The arrow is the same step.
        await page.go_forward()
        await page.wait_for_function(
            "path => location.pathname === path"
            " && !document.body.classList.contains('mobile-list-open')",
            arg=opens,
        )
        await page.locator("a.back").click()
        await page.wait_for_function(
            "location.pathname === '/' && document.body.classList.contains('mobile-list-open')"
        )
        assert not await page.locator("body.work-page").count()

        # The chat's header reaches the Work page, and Back from that Work is the front
        # page, not the chat again (#2878).
        await page.go_forward()
        await page.wait_for_function(
            "path => location.pathname === path && !!document.querySelector('.session-detail')"
            " && !document.body.classList.contains('mobile-list-open')",
            arg=opens,
        )
        await page.locator(".session-detail-head a.session-work-link").click()
        await page.wait_for_function(
            "path => location.pathname === path"
            " && !!document.querySelector('body.work-page main.work-main')"
            " && !document.querySelector('.session-detail')"
            " && document.readyState === 'complete'",
            arg=work_path,
        )
        if screenshot_dir:
            await page.screenshot(path=str(Path(screenshot_dir) / "issue-2878-work-page.png"))
        await page.go_back()
        await page.wait_for_function(
            "location.pathname === '/' && !document.querySelector('.session-detail')"
            " && !!document.querySelector('[data-work-id] a.fr-body')"
            " && document.readyState === 'complete'"
        )
        await page.close()

        # A Work no chat has joined yet opens its page, as before.
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.goto(base + "/")
        await page.wait_for_function("window.htmx")
        await page.locator(f'[data-work-id="{UNSTARTED.work_id}"] a.fr-body').first.click()
        await page.wait_for_function(
            f"location.pathname === {work_href(UNSTARTED.work_id)!r}"
        )
        await page.locator("body.work-page main.work-main").wait_for()
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_phone_steps_back_from_a_linked_chat_to_the_front_page(tmp_path):
    """#2878: a chat a phone opens from outside the deck (a notification, a shared link)
    steps back to the front page, and so does a Work opened over one of its chats."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    worker = _add(app, "claude_code:test:worker", WORK, title="Promote Work to the UI",
                  cwd=tmp_path, thinking=True)
    await app.state.work_overview.refresh()
    chat_path = app.state.app_state.sessions[worker].detail_path
    work_path = work_href(WORK.work_id)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.goto(base + chat_path)
        await page.wait_for_function(
            "document.body.classList.contains('mobile-session-stack-ready')"
        )
        await page.locator("a.back").click()
        await page.wait_for_function(
            "location.pathname === '/' && !!document.querySelector('[data-work-id] a.fr-body')"
            " && !document.querySelector('.session-detail')"
        )
        assert not await page.locator("body.work-page").count()
        await page.close()

        # A Work opened from its own link, then a chat from that Work's page: Back from the
        # chat skips the Work too, and a Work linked straight in steps back to the front page.
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.goto(base + work_path)
        await page.locator("body.work-page main.work-main").wait_for()
        await page.wait_for_function("window.htmx && document.readyState === 'complete'")
        await page.locator("#work-chats > summary").click()
        await page.locator(f'#work-chats a[data-session-key][href="{chat_path}"]').click()
        await page.wait_for_function(
            "path => location.pathname === path"
            " && !!document.querySelector('.session-layout > .session-detail')",
            arg=chat_path,
        )
        await page.go_back()
        await page.wait_for_function(
            "location.pathname === '/' && !!document.querySelector('[data-work-id] a.fr-body')"
            " && !document.querySelector('.session-detail')"
            " && !document.querySelector('body.work-page')"
            " && document.readyState === 'complete'"
        )
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_house_skips_the_work_beneath_a_phones_chat(tmp_path):
    """#2876: the house is the front page: it closes a phone's chat in place, whether the
    chat came from a row or from a link, and leaves no Work beneath (#2878)."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _browser_app(tmp_path)
    worker = _add(app, "claude_code:test:worker", WORK, title="Promote Work to the UI",
                  cwd=tmp_path, thinking=True)
    await app.state.work_overview.refresh()
    chat_path = app.state.app_state.sessions[worker].detail_path
    work_path = work_href(WORK.work_id)
    opens = re.search(
        r'data-open-chat="([^"]+)"', _client(app).get(work_path).text
    ).group(1)
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.goto(base + "/")
        await page.wait_for_function("window.htmx")
        await page.evaluate("window.__samePage = true")
        await page.locator(f'[data-work-id="{WORK.work_id}"] a.fr-body').first.click()
        await page.wait_for_function(
            "path => location.pathname === path"
            " && document.body.classList.contains('mobile-session-stack-ready')",
            arg=opens,
        )
        house = page.locator("header.topbar a.brand")
        assert await house.is_visible()
        if screenshot_dir:
            await page.screenshot(path=str(Path(screenshot_dir) / "issue-2876-chat.png"))
        await house.click()
        await page.wait_for_function(
            "location.pathname === '/'"
            " && document.body.classList.contains('mobile-list-open')"
        )
        assert await page.evaluate("window.__samePage") is True
        assert not await page.locator("body.work-page").count()
        if screenshot_dir:
            await page.screenshot(path=str(Path(screenshot_dir) / "issue-2876-house-to-front.png"))
        # The chat is one step forward: nothing was added.
        await page.go_forward()
        await page.wait_for_function(
            "path => location.pathname === path"
            " && !document.body.classList.contains('mobile-list-open')",
            arg=opens,
        )
        await page.close()

        # A chat opened from a link has the same entries beneath it.
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.goto(base + chat_path)
        await page.wait_for_function(
            "document.body.classList.contains('mobile-session-stack-ready')"
        )
        await page.locator("header.topbar a.brand").click()
        await page.wait_for_function(
            "location.pathname === '/' && !!document.querySelector('[data-work-id] a.fr-body')"
            " && !document.querySelector('.session-detail')"
        )
        assert not await page.locator("body.work-page").count()
        await browser.close()


# --- the Work's diff (#2379) ------------------------------------------------------------------


def _work_with_a_checkout(tmp_path):
    from test_web import _checkout_with_a_change

    app = _app(tmp_path, board=_board())
    _add(app, "claude_code:test:worker", WORK, title="Worker chat",
         cwd=_checkout_with_a_change(tmp_path))
    return app


def _diff_url(work: WorkRef = WORK) -> str:
    return "/partials" + work_href(work.work_id) + "/diff"


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
def test_the_work_page_opens_its_diff_without_a_chat(tmp_path):
    """#2379: the Work page reaches the Work's diff directly. The fold is shut, reads nothing
    until opened, and the fragment is the chat's diff over the Work's own checkouts."""
    client = _client(_work_with_a_checkout(tmp_path))
    html = client.get(work_href(WORK.work_id)).text

    fold = re.search(r'<details[^>]*id="work-diff"[^>]*>', html)
    assert fold is not None and " open" not in fold.group(0)
    assert f'hx-get="{_diff_url()}"' in fold.group(0)
    assert 'hx-trigger="toggle[this.open]"' in fold.group(0)
    # Under the workspace, above the folded history, and outside every swapped section.
    assert html.index('id="work-sec-workspace"') < html.index('id="work-diff"')
    assert html.index('id="work-diff"') < html.index('id="work-sec-chats"')
    assert "greeting.txt" not in html

    fragment = client.get(_diff_url())
    assert fragment.status_code == 200
    assert "greeting.txt" in fragment.text and "notes.md" in fragment.text
    assert "agent/issue-1114" in fragment.text and "vs master" in fragment.text
    assert '<div class="diff-line diff-add">+deck</div>' in fragment.text


def test_the_work_page_links_its_conversation_to_github(tmp_path):
    """#2591: the issue conversation is a plain link to GitHub, not an in-deck reader."""
    app = _app(tmp_path, board=_board())
    _add(app, "claude_code:test:worker", WORK, title="Worker chat")
    client = _client(app)
    html = client.get(work_href(WORK.work_id)).text

    link = re.search(r'<p[^>]*id="work-conversation"[^>]*>\s*<a ([^>]*)>([^<]*)</a>', html)
    assert link is not None
    assert link.group(2) == "Open the conversation on GitHub ↗"
    assert f'href="https://github.com/{WORK.repository}/issues/{WORK.number}"' in link.group(1)
    assert 'target="_blank"' in link.group(1)
    assert html.index('id="work-sec-workspace"') < html.index('id="work-conversation"')
    assert html.index('id="work-conversation"') < html.index('id="work-sec-chats"')
    assert "/conversation" not in html and "wd-conv-body" not in html
    assert client.get(f"/partials/works/{WORK.work_id}/conversation").status_code == 404


def test_a_work_with_no_checkout_here_offers_no_diff(tmp_path):
    app = _app(tmp_path, board=_board())
    _add(app, "claude_code:test:worker", WORK, title="Worker chat")
    client = _client(app)

    assert 'id="work-diff"' not in client.get(work_href(WORK.work_id)).text
    # Asked anyway, it says why rather than showing an empty pane.
    fragment = client.get(_diff_url())
    assert fragment.status_code == 200
    assert "No checkout of this Work is on this host" in fragment.text
    assert client.get("/partials/works/not-a-work/diff").status_code == 404
    assert client.get(_diff_url(WorkRef("eerovil/agentdeck", 1))).status_code == 404


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_opening_the_works_diff_fold_shows_the_change(tmp_path):
    from conftest import async_playwright
    from test_web import _live_server

    app = _work_with_a_checkout(tmp_path)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        await page.goto(base + work_href(WORK.work_id))
        fold = page.locator("#work-diff")
        await fold.wait_for()
        assert not [url for url in requests if url.endswith("/diff")]

        await fold.locator(":scope > summary").click()
        await fold.locator(".diff-file .diff-path", has_text="greeting.txt").wait_for(
            timeout=15000
        )
        assert await fold.locator(".diff-line.diff-add", has_text="+deck").count() == 1
        # A live update of the Work (#3228) leaves the diff being read where it is: the
        # rename shows in the status heading, and the fold stays open on its file.
        app.state.control_db.set_work_title(WORK.work_id, "Renamed while reading")
        await app.state.work_overview.refresh()  # as the rename route does
        await page.locator("#work-sec-status .wd-title", has_text="Renamed while reading").wait_for(
            timeout=15000
        )
        assert await fold.evaluate("el => el.open")
        assert await fold.locator(".diff-path", has_text="greeting.txt").count() == 1
        await browser.close()


def test_each_checkout_is_diffed_against_its_own_pull_request():
    """#2379 review: a repository can hold several worktrees and pull requests, so a checkout
    takes the pull request whose head is its branch, and a guess only when it cannot be wrong."""
    from agentdeck.web.work_view import work_checkouts
    from agentdeck.work_workspace import PathObservation, WorktreeObservation

    def tree(path: str, branch: str = "") -> WorktreeObservation:
        seen = PathObservation(path, exists=True, toplevel=path, branch=branch)
        return WorktreeObservation(path, True, seen, ())

    def pull(number: int, head: str, base: str = "master") -> WorkPullRequest:
        return WorkPullRequest("eerovil/agentdeck", number, f"https://x/{number}",
                               head_branch=head, base_branch=base)

    def pairs(worktrees, pulls):
        workspace = WorkWorkspace(
            work_id=WORK.work_id,
            repositories=(WorkRepositoryContext(
                repository="eerovil/agentdeck", roles=(RepositoryRole.SOURCE,),
                worktrees=tuple(worktrees), pull_requests=tuple(pulls)),),
        )
        return {path: p.number if p else None for path, p in work_checkouts(workspace)}

    two = [pull(1, "agent/a", "master"), pull(2, "agent/b", "release")]
    assert pairs([tree("/a", "agent/a"), tree("/b", "agent/b"), tree("/c", "other")], two) == {
        "/a": 1, "/b": 2, "/c": None,
    }
    # Unknown branch beside two pull requests: ambiguous, so none.
    assert pairs([tree("/d")], two) == {"/d": None}
    # One pull request: a detached checkout takes it, a checkout on another branch does not.
    one = [pull(3, "agent/c")]
    assert pairs([tree("/d"), tree("/e", "agent/x"), tree("/f", "agent/c")], one) == {
        "/d": 3, "/e": None, "/f": 3,
    }
    # A pull request with no known base is not something to diff against.
    assert pairs([tree("/g", "agent/g")], [pull(4, "agent/g", "")]) == {"/g": None}


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
def test_the_work_diff_reads_the_worktree_its_pull_requests_branch_is_in(tmp_path, monkeypatch):
    """#2993: the chat made its own worktree for the pull request's branch and nobody recorded
    it. Git knows it, so the Work's diff finds it by the branch instead of showing only the
    clean main checkout."""
    import subprocess
    from types import SimpleNamespace

    from agentdeck import work_start
    from agentdeck.web import routes_works
    from agentdeck.work_workspace import PathObservation, WorktreeObservation

    main = tmp_path / "main"
    main.mkdir()
    for args in (
        ["init", "--initial-branch=master"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["remote", "add", "origin", "https://github.com/eerovil/agentdeck.git"],
        ["commit", "--allow-empty", "-m", "base"],
        ["worktree", "add", "-b", "agent/issue-2993", str(tmp_path / "wt")],
    ):
        subprocess.run(["git", "-C", str(main), *args], check=True, capture_output=True)
    (tmp_path / "wt" / "fix.py").write_text("FIXED = True\n")
    # #3129: the code for the same Work is in another repository the Work has no checkout
    # of; its pull request's worktree is found from that repository's local clone.
    store = tmp_path / "store"
    store.mkdir()
    for args in (
        ["init", "--initial-branch=master"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["remote", "add", "origin", "git@github.com:Shop/Store.git"],
        ["commit", "--allow-empty", "-m", "base"],
        ["worktree", "add", "-b", "hotfix/715-part", str(tmp_path / "store-wt")],
    ):
        subprocess.run(["git", "-C", str(store), *args], check=True, capture_output=True)
    (tmp_path / "store-wt" / "transfer.py").write_text("DAY = 1\n")

    seen = PathObservation(str(main), exists=True, toplevel=str(main), branch="master")
    workspace = WorkWorkspace(
        work_id=WORK.work_id,
        repositories=(WorkRepositoryContext(
            repository="eerovil/agentdeck", roles=(RepositoryRole.SOURCE,),
            worktrees=(WorktreeObservation(str(main), True, seen, ()),),
            pull_requests=(WorkPullRequest(
                "eerovil/agentdeck", 2994, "https://x/2994",
                head_branch="agent/issue-2993", base_branch="master"),),
        ), WorkRepositoryContext(
            repository="shop/store", roles=(RepositoryRole.SOURCE,),
            pull_requests=(WorkPullRequest(
                "shop/store", 3567, "https://x/3567",
                head_branch="hotfix/715-part", base_branch="master"),),
        )),
    )

    async def detail(_app, _ref):
        return SimpleNamespace(workspace=workspace)

    monkeypatch.setattr(routes_works, "load_work_detail", detail)
    monkeypatch.setattr(work_start, "checkout_candidates", lambda _app: [str(main), str(store)])
    fragment = _client(_app(tmp_path, board=_board())).get(_diff_url())

    assert fragment.status_code == 200
    assert "fix.py" in fragment.text and "agent/issue-2993" in fragment.text
    assert "transfer.py" in fragment.text and "hotfix/715-part" in fragment.text
    assert "Nothing has changed here yet." not in fragment.text


@pytest.mark.asyncio
async def test_the_work_cards_tell_the_live_window_which_chats_to_keep(tmp_path):
    """#2841: a Work still going keeps its chats live whatever their age; a finished
    Work's chats are let go. Registered chats count even when the deck holds none of
    them, so a Work going again finds its chats."""
    from agentdeck.live_window import LiveWindow

    app = _app(tmp_path, board=_board())
    state = app.state.app_state
    state.live_window = LiveWindow(7)
    going = _add(app, "claude_code:test:going", WORK, title="Still going")
    done = _add(app, "claude_code:test:done", OTHER, title="Shipped")
    # A chat registered to the open Work that the deck no longer holds.
    state.set_issues("claude_code:test:let-go", (IssueRef(WORK.repository, WORK.number),))
    _closed(app, {OTHER.number: NOW - timedelta(hours=1)})

    scans: list[str] = []

    async def scan_now(account_key):
        scans.append(account_key)
        return True

    app.state.collector.scan_now = scan_now
    await _overview(app)
    await asyncio.sleep(0)

    assert {going, "claude_code:test:let-go"} <= state.live_window.pinned
    assert done in state.live_window.finished
    assert done not in state.live_window.pinned
    # The registered chat the deck does not hold is asked for once, not on every rebuild.
    assert scans == ["claude_code:test"]
    await _overview(app)
    await asyncio.sleep(0)
    assert scans == ["claude_code:test"]

    # #3189: the next scan lets the finished Work's chat go, and the Work stays built and
    # its chat stays let go, scan after scan, instead of flipping with each one.
    for _ in range(2):
        account = [s for s in state.sessions.values() if s.account_key == "claude_code:test"]
        state.replace_account_sessions("claude_code:test", [*account, _session(done)])
        assert done not in state.sessions
        overview = await _overview(app)
        assert _card_of(overview, OTHER).work_id in {
            row.card.work_id for row in overview.front.finished
        }
        assert done in state.live_window.finished

    # #3229: the all-works list keeps it under Recent too, and opening its page loads the
    # chat the window let go, so the page has a chat to open and to message.
    from agentdeck import live_chats

    live_chats._failed.clear()
    loaded: list[tuple[str, set[str]]] = []

    async def load_sessions(account_key, session_ids):
        loaded.append((account_key, set(session_ids)))
        state.update_session(_session(done))
        return True

    app.state.collector.load_sessions = load_sessions
    # A chat that joined only through a Run of the Work, and whose transcript is gone.
    state.bind_session_run("claude_code:test:ran", OTHER.work_id, "gen-1")
    client = _client(app)
    assert f'data-work-id="{OTHER.work_id}"' in client.get("/works").text
    page = client.get(work_href(OTHER.work_id))
    assert loaded == [("claude_code:test", {"done", "ran"})]
    assert f'data-open-chat-key="{done}"' in page.text
    client.get(work_href(OTHER.work_id))
    assert len(loaded) == 1, "one held, the other just failed: no second load"

    # One that finished long ago but that its chat carried on in since is shown by that
    # chat's activity, so its chat is kept, like a Work still going.
    _closed(app, {OTHER.number: NOW - timedelta(days=10)})
    for _ in range(2):
        await _overview(app)
        assert done in state.live_window.pinned
        account = [s for s in state.sessions.values() if s.account_key == "claude_code:test"]
        state.replace_account_sessions("claude_code:test", [*account, _session(done)])
        assert done in state.sessions
        overview = await _overview(app)
        assert OTHER.work_id in {row.card.work_id for row in overview.front.finished}



def _tree_app(tmp_path):
    """A map issue #40 with one sub-issue #50, neither with an agent (#3622)."""
    api = "https://api.github.com/repos/eerovil/agentdeck/issues/{}"
    records = {
        40: {"title": "The map", "state": "open", "is_pr": False,
             "subIssues": {"total": 1, "completed": 0}},
        50: {"title": "A ticket", "state": "open", "is_pr": False, "parent": api.format(40)},
    }
    app = _app(tmp_path)
    app.state.issues.cached_issues = lambda refs: {
        (repository, number): records[number] for repository, number in refs if number in records
    }

    async def unchanged(_refs):
        return False

    async def listed(repository, number):
        return ({"repository": "eerovil/agentdeck", "number": 50, "title": "A ticket",
                 "state": "open"},) if number == 40 else ()

    app.state.issues.refresh_stale_issue_records = unchanged
    app.state.issues.sub_issues = listed
    return app


def test_a_sub_issue_and_its_parent_open_as_works_with_no_agent(tmp_path):
    """#3622: the parent's page lists its sub-issues and each opens, a never-started one
    included (it is reached only from its parent's page), and it says what it is part of.
    Its arrow, and Back beneath it, lead to the parent's Work; the parent's to `/` (#3691)."""
    parent, child = WorkRef("eerovil/agentdeck", 40), WorkRef("eerovil/agentdeck", 50)
    client = _client(_tree_app(tmp_path))

    page = client.get(work_href(parent.work_id))
    assert page.status_code == 200
    assert f'data-work-tree-child="{child.work_id}"' in page.text and "A ticket" in page.text
    assert '<a class="back" href="/"' in page.text and "data-parent-work" not in page.text
    ticket = client.get(work_href(child.work_id))
    assert ticket.status_code == 200 and "Part of" in ticket.text and "#40 The map" in ticket.text
    parent_href = work_href(parent.work_id)
    assert f'<a class="back" href="{parent_href}" aria-label="back to #40">' in ticket.text
    assert f'data-parent-work="{parent_href}"' in ticket.text


@pytest.mark.browser
@pytest.mark.asyncio
async def test_back_from_a_sub_issues_work_is_its_parents_work(tmp_path):
    """#3691: on a phone, a sub-issue's Work opened from anywhere steps back to its parent's
    Work, then the front page; opened from its parent, Back is the parent once, no loop. On a
    desktop the same history holds across the front page's columns."""
    from conftest import async_playwright
    from test_web import _live_server

    app = _tree_app(tmp_path)
    parent_path = work_href(WorkRef("eerovil/agentdeck", 40).work_id)
    child_path = work_href(WorkRef("eerovil/agentdeck", 50).work_id)
    loaded = (
        "path => location.pathname === path && document.readyState === 'complete'"
        " && !!document.querySelector('main.work-main')"
    )
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        # From a link: the parent's Work is beneath it, and the front page beneath that.
        await page.goto(base + child_path)
        await page.wait_for_function(loaded, arg=child_path)
        assert await page.locator("a.back").get_attribute("href") == parent_path
        await page.go_back()
        await page.wait_for_function(loaded, arg=parent_path)
        assert await page.locator("a.back").get_attribute("href") == "/"
        await page.go_back()
        await page.wait_for_function(
            "location.pathname === '/' && document.readyState === 'complete'"
        )
        # The arrow goes to the parent too.
        await page.goto(base + child_path)
        await page.wait_for_function(loaded, arg=child_path)
        await page.locator("a.back").click()
        await page.wait_for_function(loaded, arg=parent_path)

        # From the parent: Back is the parent, once.
        await page.goto(base + parent_path)
        await page.wait_for_function(loaded, arg=parent_path)
        await page.locator(f'[data-work-tree-child] a[href="{child_path}"]').first.click()
        await page.wait_for_function(loaded, arg=child_path)
        await page.go_back()
        await page.wait_for_function(loaded, arg=parent_path)
        await page.go_back()
        await page.wait_for_function(
            "location.pathname === '/' && document.readyState === 'complete'"
        )

        # From the front page: the parent's Work, then that front page's own entry, once.
        await page.goto(base + "/")
        await page.wait_for_function("document.readyState === 'complete'")
        await page.evaluate("history.replaceState({marker: 'front'}, '', '/')")
        await page.evaluate(f"location.href = {child_path!r}")
        await page.wait_for_function(loaded, arg=child_path)
        await page.go_back()
        await page.wait_for_function(loaded, arg=parent_path)
        await page.go_back()
        await page.wait_for_function(
            "location.pathname === '/' && document.readyState === 'complete'"
        )
        assert await page.evaluate("history.state && history.state.marker") == "front"
        await page.close()

        # A desktop: the Work opens beside the front page, and Back walks the same way.
        page = await browser.new_page(viewport={"width": 1400, "height": 900},
                                      service_workers="block")
        await page.goto(base + child_path)
        await page.wait_for_function(
            "path => document.querySelector('.front-columns > .work-column')"
            "?.dataset.workPath === path",
            arg=child_path,
        )
        await page.go_back()
        await page.wait_for_function(
            "path => location.pathname === path && document.querySelector("
            "'.front-columns > .work-column')?.dataset.workPath === path",
            arg=parent_path,
        )
        await page.go_back()
        await page.wait_for_function("location.pathname === '/'")
        await browser.close()
