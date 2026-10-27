"""The Work's read-only Run trace (#2141)."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from agentdeck import poller_ops
from agentdeck.db import Db, DeliveryRoute
from agentdeck.models import Session, SessionStatus
from agentdeck.poller_engine.github import write_receipt
from agentdeck.poller_engine.revive import MAX_REVIVES
from agentdeck.poller_ops import BoardCard, BoardDispatch, BoardScope, BoardView
from agentdeck.run_execution import ExecutionOutcome, OutcomeStore
from agentdeck.run_trace import (
    FAILED,
    FINISHED,
    HISTORICAL,
    NEEDS_ATTENTION,
    NEXT_ACTIVITY,
    NEXT_BLOCKERS,
    NEXT_CAPACITY,
    NEXT_FIRST_ACTIVITY,
    NEXT_HUMAN,
    NEXT_RETRY,
    NEXT_REVIEW,
    NEXT_RUNTIME_ACCEPTANCE,
    NEXT_SESSION,
    NEXT_STOP,
    NEXT_VERIFICATION,
    PREPARING,
    RUNNING,
    SUPERSEDED,
    UNCERTAIN,
    WAITING,
    RunTraceFacts,
    StoredOutcome,
    build_run_trace,
    gather_run_trace,
    read_evidence_since,
    read_outcomes,
)
from agentdeck.state import AppState
from agentdeck.work import WorkRef

WORK = WorkRef("eerovil/agentdeck", 2141)
OTHER = WorkRef("eerovil/agentdeck", 9)
NOW = time.time()


def _card(**fields) -> BoardCard:
    fields.setdefault("status", "In progress")
    return BoardCard("eerovil/agentdeck", 2141, **fields)


def _dispatch(run_id: str, **fields) -> BoardDispatch:
    return BoardDispatch(id=f"d-{run_id}", run_id=run_id, work_id=WORK.work_id, **fields)


def _route(run_id: str, *, work: WorkRef = WORK, account: str = "main") -> DeliveryRoute:
    return DeliveryRoute(
        f"poller:d-{run_id}", "fp", work.work_id, run_id, "claude_code", account,
        "agentdeck#2141", "2026-09-24T08:00:00+00:00",
    )


class _Binding:
    def __init__(self, session_key: str, run_id: str, *, work: WorkRef = WORK) -> None:
        self.session_key, self.work_id, self.run_id = session_key, work.work_id, run_id
        self.first_delivery_id = f"poller:d-{run_id}"
        self.bound_at = "2026-09-24T08:00:05+00:00"


def _outcome(run_id: str, state: str, reason: str = "", **fields) -> StoredOutcome:
    return StoredOutcome(run_id, state, reason, observed_at=NOW, **fields)


def _trace(**fields):
    return build_run_trace(RunTraceFacts(work=WORK, **fields))


# --- identity -----------------------------------------------------------------------------


def test_the_current_run_is_the_card_generation():
    trace = _trace(card=_card(generation="gen-1"), routes=(_route("gen-1"),))
    assert trace.current.run_id == "gen-1"
    assert trace.current.current and trace.previous == ()
    assert trace.work_id == "github_issue:eerovil/agentdeck#2141"


def test_a_generation_bump_is_a_new_run_and_the_old_one_is_superseded():
    trace = _trace(
        card=_card(generation="gen-2", dispatch=_dispatch("gen-2", sent_at="2026-09-24T09:00:00Z")),
        routes=(_route("gen-1"),),
        bindings=(_Binding("claude_code:main:s-1", "gen-1"),),
        outcomes=(
            _outcome("gen-1", "stopped", stop_reason="superseded_by:gen-2", stopped_at=NOW,
                     stop_requested_at=NOW),
        ),
    )
    assert trace.current.run_id == "gen-2"
    assert trace.current.state == WAITING
    assert trace.current.next_expected == NEXT_RUNTIME_ACCEPTANCE
    (old,) = trace.previous
    assert (old.run_id, old.state) == ("gen-1", SUPERSEDED)
    assert not old.sparse


def test_another_works_dispatch_is_never_attributed():
    card = _card(generation="gen-1", dispatch=BoardDispatch(id="x", run_id="gen-1",
                                                           work_id=OTHER.work_id))
    trace = _trace(card=card)
    assert not any(step.kind == "dispatch" for step in trace.current.milestones)


# --- delivery ------------------------------------------------------------------------------


def test_a_prepared_dispatch_waits_for_the_runtime():
    trace = _trace(card=_card(generation="g", dispatch=_dispatch("g")))
    assert (trace.current.state, trace.current.next_expected) == (
        PREPARING, NEXT_RUNTIME_ACCEPTANCE,
    )


def test_a_capacity_refusal_is_a_waiting_state_with_its_reason():
    trace = _trace(
        card=_card(generation="g", dispatch=_dispatch("g", sent_at="2026-09-24T09:00:00Z")),
        deferral="at_capacity",
    )
    run = trace.current
    assert (run.state, run.next_expected) == (WAITING, NEXT_CAPACITY)
    assert "at capacity" in run.reason
    assert any(step.kind == "refused" and step.problem for step in run.milestones)


def test_a_prior_run_still_executing_waits_for_it_to_stop():
    trace = _trace(
        card=_card(generation="g", dispatch=_dispatch("g", sent_at="x")),
        deferral="prior_run_executing",
    )
    assert trace.current.next_expected == NEXT_STOP


def test_a_revive_refused_by_its_own_chats_active_turn_waits_rather_than_failing():
    """#2902: the Runtime records ``not_started`` for a revive it refused because the chat on
    the card was mid-turn. The Poller still holds that dispatch and sends it again next wake, so
    the Run is waiting, not failed, and the Work does not land in Needs retry."""
    trace = _trace(
        card=_card(generation="g", dispatch=_dispatch("g", sent_at="x", refusal="active_turn")),
        deferral="active_turn",
        outcomes=(_outcome("g", "not_started", "no_execution"),),
    )
    run = trace.current
    assert (run.state, run.next_expected) == (WAITING, NEXT_STOP)
    assert "active turn" in run.reason


def test_a_not_started_run_with_no_dispatch_left_is_still_failed():
    trace = _trace(
        card=_card(generation="g"),
        outcomes=(_outcome("g", "not_started", "no_execution"),),
    )
    assert (trace.current.state, trace.current.reason) == (FAILED, "refused before it started")


def test_a_stale_refusal_does_not_hide_a_later_placement():
    trace = _trace(
        card=_card(generation="g", dispatch=_dispatch("g", sent_at="x", refusal="at_capacity")),
        routes=(_route("g"),),
    )
    assert (trace.current.state, trace.current.next_expected) == (WAITING, NEXT_SESSION)


def test_a_placed_run_without_a_bound_session_is_not_attached():
    """A receipt left at prepared_before_write never binds, so a route alone is not a session."""
    trace = _trace(card=_card(generation="g"), routes=(_route("g"),))
    run = trace.current
    assert run.sessions == ()
    assert (run.state, run.next_expected) == (WAITING, NEXT_SESSION)
    assert run.provider == "claude_code" and run.account == "main"


def test_a_clean_park_waits_for_the_automatic_retry():
    trace = _trace(card=_card(generation="g", dispatch_failed=True, dispatch_failed_clean=True))
    assert (trace.current.state, trace.current.next_expected) == (NEEDS_ATTENTION, NEXT_RETRY)


def test_an_ambiguous_park_needs_a_person():
    trace = _trace(card=_card(generation="g", dispatch_failed=True))
    run = trace.current
    assert (run.state, run.next_expected) == (NEEDS_ATTENTION, NEXT_HUMAN)
    assert "unknown" in run.reason


# --- Runtime execution ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "reason", "expected", "next_expected"),
    [
        ("running", "turn_active", RUNNING, NEXT_ACTIVITY),
        ("running", "turn_pending", RUNNING, NEXT_FIRST_ACTIVITY),
        ("waiting_human", "pending_interaction", NEEDS_ATTENTION, NEXT_HUMAN),
        ("ended", "success", FINISHED, ""),
        ("ended", "refused:usage_limit", FAILED, ""),
        # The CLI calls a turn its usage limit cut short a success (#2862).
        ("ended", "usage_limit", FAILED, ""),
        ("died", "process_gone", FAILED, ""),
        ("abandoned", "no_activity", NEEDS_ATTENTION, NEXT_STOP),
        ("stopped", "worker_released", FINISHED, ""),
        ("uncertain", "runtime_restarted", UNCERTAIN, ""),
        ("not_started", "no_execution", FAILED, ""),
    ],
)
def test_runtime_execution_state_drives_the_run(state, reason, expected, next_expected):
    # Restarts spent, so the Runtime's word alone decides what comes next.
    trace = _trace(
        card=_card(generation="g", revives=MAX_REVIVES),
        routes=(_route("g"),),
        bindings=(_Binding("claude_code:main:s-1", "g"),),
        outcomes=(_outcome("g", state, reason),),
    )
    assert (trace.current.state, trace.current.next_expected) == (expected, next_expected)


@pytest.mark.parametrize(
    ("state", "reason"),
    [("died", "process_gone"), ("abandoned", "no_activity"), ("ended", "usage_limit")],
)
def test_a_dead_run_with_restarts_left_retries_itself(state, reason):
    """The engine restarts a worker that died, was abandoned or hit its usage limit while
    restarts are left (#2235, #2862)."""

    def run(**fields):
        return _trace(
            card=_card(generation="g", **fields),
            routes=(_route("g"),),
            bindings=(_Binding("claude_code:main:s-1", "g"),),
            outcomes=(_outcome("g", state, reason),),
        ).current

    assert run(revives=MAX_REVIVES - 1).next_expected == NEXT_RETRY
    assert run().next_expected == NEXT_RETRY
    # Spent, resting in a review or parked column, or done: nothing restarts it.
    assert run(revives=MAX_REVIVES).next_expected != NEXT_RETRY
    assert run(status="In review", in_review=True).next_expected != NEXT_RETRY
    assert run(status="Needs More Info", status_role="needs_more_info").next_expected != NEXT_RETRY
    assert run(done=True).next_expected != NEXT_RETRY


def test_a_run_that_ended_cleanly_is_not_retried():
    current = _trace(
        card=_card(generation="g"),
        routes=(_route("g"),),
        bindings=(_Binding("claude_code:main:s-1", "g"),),
        outcomes=(_outcome("g", "ended", "success"),),
    ).current
    assert current.next_expected != NEXT_RETRY


def test_a_requested_stop_waits_for_the_worker_to_stop():
    trace = _trace(
        card=_card(generation="g"),
        outcomes=(_outcome("g", "running", "turn_active", stop_requested_at=NOW),),
    )
    assert trace.current.next_expected == NEXT_STOP


def test_a_previous_run_still_running_stays_visible_as_running():
    trace = _trace(
        card=_card(generation="gen-2", dispatch=_dispatch("gen-2", sent_at="x")),
        deferral="prior_run_executing",
        outcomes=(_outcome("gen-1", "running", "turn_active"),),
    )
    (old,) = trace.previous
    assert (old.state, old.next_expected) == (RUNNING, NEXT_STOP)


def test_an_attached_session_without_an_outcome_is_not_called_running():
    trace = _trace(card=_card(generation="g"), bindings=(_Binding("claude_code:main:s", "g"),))
    assert (trace.current.state, trace.current.next_expected) == (WAITING, NEXT_ACTIVITY)


def test_a_held_run_with_no_runtime_fact_is_uncertain_not_running():
    assert _trace(card=_card(generation="g")).current.state == UNCERTAIN


# --- verification and review ----------------------------------------------------------------


def _job(job_id: str, run_id: str, *, state: str = "done", work: WorkRef = WORK) -> dict:
    return {
        "id": job_id, "purpose": "verification", "work_id": work.work_id, "run_id": run_id,
        "state": state, "submitted_at": NOW - 60, "finished_at": NOW if state == "done" else None,
    }


def test_verification_attaches_to_its_own_run_only():
    trace = _trace(
        card=_card(generation="gen-2"),
        outcomes=(_outcome("gen-2", "ended", "success"), _outcome("gen-1", "ended", "success")),
        jobs=(_job("j1", "gen-1"), _job("j2", "gen-2", state="running"),
              _job("j3", "gen-2", work=OTHER)),
    )
    assert [check.job_id for check in trace.current.verifications] == ["j2"]
    assert (trace.current.state, trace.current.next_expected) == (WAITING, NEXT_VERIFICATION)
    (old,) = trace.previous
    assert [check.job_id for check in old.verifications] == ["j1"]


def test_a_finished_turn_in_review_waits_for_the_review():
    trace = _trace(
        card=_card(generation="g", in_review=True, status="In review"),
        outcomes=(_outcome("g", "ended", "success"),),
    )
    assert (trace.current.state, trace.current.next_expected) == (WAITING, NEXT_REVIEW)
    assert any(step.kind == "review" for step in trace.current.milestones)


def test_a_blocked_card_explains_its_preflight():
    trace = _trace(card=_card(blocked_by=("eerovil/agentdeck#2159",)))
    assert trace.current is None
    assert "2159" in trace.preflight and trace.preflight_next == NEXT_BLOCKERS


# --- history ------------------------------------------------------------------------------------


def test_old_history_with_only_a_binding_is_labelled_sparse():
    trace = _trace(
        card=_card(generation="gen-new", dispatch=_dispatch("gen-new")),
        bindings=(_Binding("claude_code:main:old", "gen-old"),),
    )
    (old,) = trace.previous
    assert old.sparse and old.state == SUPERSEDED
    assert not trace.current.sparse


def test_unreadable_outcomes_are_named_not_read_as_absent():
    trace = _trace(card=_card(generation="g"), outcomes_readable=False)
    assert trace.unreadable == ("execution outcomes",)


# --- gathering from real stores -------------------------------------------------------------------


def _session(key: str) -> Session:
    account, _, session_id = key.rpartition(":")
    return Session(key=key, account_key=account, session_id=session_id, status=SessionStatus.IDLE)


@pytest.fixture
def state(tmp_path):
    db = Db(tmp_path / "control.db")
    try:
        yield AppState(db=db, budget_db=db)
    finally:
        db.close()


def test_m2m_chat_lends_each_work_only_its_own_run(state):
    shared, only_other, bystander = "claude_code:main:s-1", "claude_code:main:b-1", "codex:w:x-1"
    for key in (shared, only_other, bystander):
        state.sessions[key] = _session(key)
    state.set_issues(shared, (_issue(WORK), _issue(OTHER)))
    state.set_issues(bystander, (_issue(WORK),))  # membership alone is not participation
    state.bind_session_run(shared, WORK.work_id, "gen-a")
    state.bind_session_run(shared, OTHER.work_id, "gen-b")
    state.bind_session_run(only_other, OTHER.work_id, "gen-b")

    mine = build_run_trace(gather_run_trace(state, WORK, outcomes=[]))
    theirs = build_run_trace(gather_run_trace(state, OTHER, outcomes=[]))

    assert [(run.run_id, [s.session_key for s in run.sessions]) for run in mine.previous] == [
        ("gen-a", [shared])
    ]
    assert [(run.run_id, sorted(s.session_key for s in run.sessions))
            for run in theirs.previous] == [("gen-b", sorted([shared, only_other]))]


def _issue(work: WorkRef):
    from agentdeck.models import IssueRef

    return IssueRef(work.repository, work.number)


def test_routes_come_from_the_control_store(state):
    state.budget_db.freeze_delivery_route(_route("gen-1"))
    state.budget_db.freeze_delivery_route(_route("gen-9", work=OTHER))
    trace = build_run_trace(gather_run_trace(state, WORK, outcomes=[]))
    assert [run.run_id for run in trace.previous] == ["gen-1"]


def test_outcomes_are_read_from_the_runtime_table(tmp_path):
    path = tmp_path / "history.db"
    assert read_outcomes(path, WORK.work_id) == []
    store = OutcomeStore(path)
    store.record(ExecutionOutcome(WORK.work_id, "g", "died", "process_gone", observed_at=NOW))
    store.record(ExecutionOutcome(OTHER.work_id, "g", "running", observed_at=NOW))
    store.close()
    (row,) = read_outcomes(path, WORK.work_id)
    assert (row.run_id, row.execution_state, row.reason_code) == ("g", "died", "process_gone")


def test_the_board_carries_the_dispatch_and_the_host_refusal(tmp_path):
    from test_issue_context import _poller_root

    root = _poller_root(
        tmp_path,
        state={
            "issues": {
                "agentdeck#2141": {
                    "status": "In progress",
                    "generation": "gen-7",
                    "dispatch": {
                        "id": "disp-7", "action": "start", "generation": "gen-7",
                        "run_id": "gen-7", "work_id": WORK.work_id, "attempts": 2,
                        "sent_at": "2026-09-24T09:00:00+00:00",
                    },
                }
            }
        },
    )
    card = poller_ops.read_board(root).card("eerovil/agentdeck", 2141)
    assert card.generation == "gen-7"
    dispatch = card.dispatch
    assert (dispatch.id, dispatch.run_id, dispatch.attempts) == ("disp-7", "gen-7", 2)
    state_dir = Path(card.state_path).parent
    write_receipt(state_dir / "dispatch-deferrals", "agentdeck", 2141, "disp-7",
                  now=datetime.now(UTC), extra={"reason": "at_capacity"})
    # A refusal of another dispatch of the same card is not this one's.
    write_receipt(state_dir / "dispatch-deferrals", "agentdeck", 2141, "disp-6",
                  now=datetime.now(UTC), extra={"reason": "routing_unavailable"})

    from agentdeck.run_trace import read_deferral

    assert read_deferral(card) == "at_capacity"


# --- the loader and the rendered section ------------------------------------------------------


def trace_app(tmp_path):
    from test_web import _app_with_state

    app = _app_with_state(tmp_path)
    state = app.state.app_state
    worker = "claude_code:test:worker-1"
    state.update_session(_session(worker))
    state.set_issues(worker, (_issue(WORK),))
    state.bind_session_run(worker, WORK.work_id, "gen-1")
    state.budget_db.freeze_delivery_route(_route("gen-1", account="test"))
    store = OutcomeStore(app.state.config.history.db_path)
    store.record(ExecutionOutcome(WORK.work_id, "gen-1", "stopped", "worker_released",
                                  stop_reason="superseded_by:gen-2", stop_requested_at=NOW - 90,
                                  stopped_at=NOW - 80, observed_at=NOW - 80))
    store.close()
    card = _card(
        generation="gen-2",
        dispatch=_dispatch("gen-2", action="start", attempts=3,
                           sent_at=datetime.now(UTC).isoformat()),
        pull_request_url="https://github.com/eerovil/agentdeck/pull/2190",
    )
    board = BoardView(
        cards={card.identity: card},
        scopes=(BoardScope("agentdeck", org="eerovil", cards=(card,)),),
    )
    app.state.issues.read_board = lambda: board
    return app


@pytest.fixture
def capacity(monkeypatch):
    import agentdeck.run_trace as module

    monkeypatch.setattr(module, "read_deferral", lambda card: "at_capacity")


async def _section_html(app) -> str:
    from agentdeck.web.run_trace_view import load_run_trace, render_run_trace

    trace = await load_run_trace(app, WORK)
    return render_run_trace(app.state.templates, trace)


def _digest(paths) -> dict[str, str]:
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in paths
        for path in sorted(Path(root).rglob("*"))
        # SQLite's shared-memory index changes under any reader's locks; it holds no data.
        if path.is_file() and not path.name.endswith("-shm")
    }


@pytest.mark.asyncio
async def test_the_loader_reads_without_writing(tmp_path, capacity):
    from agentdeck.web.run_trace_view import load_run_trace

    app = trace_app(tmp_path)
    methods: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/job-evidence":
            return httpx.Response(200, json={"evidence": [_job("j1", "gen-1")]})
        return httpx.Response(200, json={"jobs": []})

    app.state.runtime_http = httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://agentdeck-runtime"
    )
    before = _digest([tmp_path])
    assert await load_run_trace(app, WorkRef("eerovil/agentdeck", 999)) is None
    trace = await load_run_trace(app, WORK)
    assert _digest([tmp_path]) == before
    assert set(methods) == {"GET"}

    assert trace.current.run_id == "gen-2"
    assert (trace.current.state, trace.current.next_expected) == (WAITING, NEXT_CAPACITY)
    (old,) = trace.previous
    assert old.state == SUPERSEDED and [c.job_id for c in old.verifications] == ["j1"]
    assert [s.session_key for s in old.sessions] == ["claude_code:test:worker-1"]


def test_the_trace_is_served_only_inside_the_work_page(tmp_path):
    """#2147's canonical Work routes are the trace's only public surface."""
    from test_work_ui import _route_paths

    paths = _route_paths(trace_app(tmp_path).routes)
    assert not any("run-trace" in path for path in paths)
    assert "/works/{work_id:path}" in paths


@pytest.mark.asyncio
async def test_the_section_leads_with_state_and_keeps_ids_in_details(tmp_path, capacity):
    html = await _section_html(trace_app(tmp_path))
    assert 'id="run-trace"' in html
    assert 'data-rt-state="waiting"' in html
    assert "Waiting for worker capacity" in html
    assert "Runtime refused: at capacity" in html
    assert "/pull/2190" in html
    import re

    compact = re.sub(r'<details class="rt-inspector">.*?</details>', "", html, flags=re.S)
    compact = re.sub(r'data-[a-z-]+="[^"]*"', "", compact)
    assert "gen-1" not in compact and "gen-2" not in compact and "worker-1" not in compact
    assert "gen-1" in html and "worker-1" in html  # still there, in the collapsed detail
    assert '<details class="rt-previous">' in html and "1 earlier Run" in html


SECTION_URL = "/__test__/run-trace"  # served by Playwright, not by the deck


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_run_section_fits_a_phone_screen(tmp_path, capacity):
    """The rendered section, swapped by HTMX into the real deck page and its stylesheet."""
    from conftest import async_playwright
    from test_web import _live_server

    app = trace_app(tmp_path)
    html = await _section_html(app)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.route(
            base + SECTION_URL,
            lambda route: route.fulfill(status=200, content_type="text/html", body=html),
        )
        await page.goto(base + "/")
        await page.evaluate(
            """async (url) => {
                const host = document.createElement('div');
                host.id = 'rt-host';
                (document.querySelector('main') || document.body).prepend(host);
                await htmx.ajax('GET', url, {target: '#rt-host', swap: 'innerHTML'});
            }""",
            SECTION_URL,
        )
        section = page.locator("#run-trace")
        await section.wait_for()
        state = page.locator('[data-rt-run="current"] [data-rt-state]')
        assert await state.get_attribute("data-rt-state") == "waiting"
        assert await state.is_visible()
        assert await page.locator("[data-rt-next]").first.is_visible()
        assert await page.locator(".rt-previous").evaluate("el => el.open") is False
        assert await page.locator(".rt-inspector").first.evaluate("el => el.open") is False
        overflow = await section.evaluate(
            """el => [...el.querySelectorAll('*')].filter(
                node => node.getBoundingClientRect().right > window.innerWidth + 1
            ).map(node => node.className || node.tagName)"""
        )
        assert overflow == []
        await browser.close()


# --- review findings on #2190 --------------------------------------------------------------------


def test_a_park_for_capacity_keeps_its_refusal_as_the_reason():
    trace = _trace(card=_card(generation="g", dispatch_failed=True, dispatch_failed_clean=True,
                              dispatch_failed_refusal="at_capacity"))
    run = trace.current
    assert (run.state, run.refusal, run.next_expected) == (
        NEEDS_ATTENTION, "at_capacity", NEXT_RETRY,
    )
    assert "Runtime refused: at capacity" in run.reason
    assert any(step.kind == "refused" for step in run.milestones)


def test_a_park_after_the_free_retry_was_spent_needs_a_person():
    trace = _trace(card=_card(generation="g", dispatch_failed=True, dispatch_failed_clean=True,
                              dispatch_rearmed=True, dispatch_failed_refusal="at_capacity"))
    assert trace.current.next_expected == NEXT_HUMAN


def test_the_engine_keeps_the_last_refusal_with_the_park_and_the_board_reads_it(tmp_path):
    from test_issue_context import _poller_root

    from agentdeck.poller_engine import dispatch as engine_dispatch

    rec = {"generation": "gen-3"}
    engine_dispatch.mark_given_up(rec, datetime.now(UTC), clean=True, refusal="at_capacity")
    assert rec["dispatch_failed_refusal"] == "at_capacity"
    root = _poller_root(tmp_path, state={"issues": {"agentdeck#2141": dict(rec)}})
    card = poller_ops.read_board(root).card("eerovil/agentdeck", 2141)
    assert (card.dispatch_failed, card.dispatch_failed_refusal) == (True, "at_capacity")

    engine_dispatch.mark_rearmed(rec)
    assert "dispatch_failed_refusal" not in rec
    engine_dispatch.mark_given_up(rec, datetime.now(UTC), clean=False)
    assert "dispatch_failed_refusal" not in rec  # no refusal this time: nothing stale kept
    engine_dispatch.mark_given_up(rec, datetime.now(UTC), clean=True, refusal="x")
    engine_dispatch.clear_rearm(rec)
    assert "dispatch_failed_refusal" not in rec


def test_the_trace_keeps_every_retained_verification_across_older_runs():
    jobs = tuple(
        {**_job(f"j{n}", f"gen-{n % 4}"), "submitted_at": NOW - 1000 + n} for n in range(9)
    )
    trace = _trace(card=_card(generation="gen-0"), jobs=jobs)
    runs = [trace.current, *trace.previous]
    assert sorted(run.run_id for run in runs) == ["gen-0", "gen-1", "gen-2", "gen-3"]
    shown = sorted(check.job_id for run in runs for check in run.verifications)
    assert shown == sorted(f"j{n}" for n in range(9))
    # The Work card keeps its own five.
    from agentdeck.work_workspace import run_verifications

    assert len(run_verifications(jobs, WORK.work_id)) == 5


# --- second review round on #2190 ----------------------------------------------------------------


def _steer(**fields) -> BoardDispatch:
    return BoardDispatch(id="s-1", action="followup", run_id="g", work_id=WORK.work_id,
                         steer=True, **fields)


@pytest.mark.parametrize(
    ("steer", "deferral", "expected", "reason", "next_expected"),
    [
        (_steer(), "", PREPARING, "follow-up prepared", NEXT_RUNTIME_ACCEPTANCE),
        (_steer(sent_at="2026-09-24T09:00:00Z"), "", WAITING, "follow-up sent",
         NEXT_RUNTIME_ACCEPTANCE),
        (_steer(sent_at="2026-09-24T09:00:00Z"), "worker_inactive", WAITING,
         "Runtime refused the follow-up: worker inactive", NEXT_RUNTIME_ACCEPTANCE),
        (_steer(sent_at="2026-09-24T09:00:00Z"), "at_capacity", WAITING,
         "Runtime refused the follow-up: at capacity", NEXT_CAPACITY),
    ],
)
def test_an_outstanding_follow_up_beats_the_runs_ended_outcome(
    steer, deferral, expected, reason, next_expected
):
    trace = _trace(
        card=_card(generation="g", dispatch=steer),
        deferral=deferral,
        routes=(_route("g"),),
        bindings=(_Binding("claude_code:main:s-1", "g"),),
        outcomes=(_outcome("g", "ended", "success"),),
    )
    run = trace.current
    assert (run.state, run.next_expected) == (expected, next_expected)
    assert reason in run.reason
    assert run.refusal == deferral
    # The turn before the follow-up stays in the history.
    assert any(step.kind == "execution" and step.label == "Execution ended"
               for step in run.milestones)


def test_once_the_follow_up_settles_the_outcome_speaks_again():
    trace = _trace(card=_card(generation="g"), outcomes=(_outcome("g", "ended", "success"),))
    assert trace.current.state == FINISHED


def test_an_earlier_refusal_is_not_the_cause_of_an_ambiguous_park():
    """Refused once, then a later attempt timed out: the park's cause is unknown."""
    trace = _trace(card=_card(generation="g", dispatch_failed=True,
                              dispatch_failed_refusal="at_capacity"))
    run = trace.current
    assert run.refusal == ""
    assert (run.state, run.next_expected) == (NEEDS_ATTENTION, NEXT_HUMAN)
    assert "outcome is unknown" in run.reason
    assert "an earlier attempt was refused: at capacity" in run.reason
    (step,) = [step for step in run.milestones if step.kind == "refused"]
    assert step.detail == "an earlier attempt"


def test_a_final_refusal_is_the_cause_even_of_an_ambiguous_park():
    """A refusal for good parks ambiguously, yet the final attempt was that refusal."""
    trace = _trace(card=_card(generation="g", dispatch_failed=True,
                              dispatch_failed_refusal="delivery_id_conflict",
                              dispatch_failed_refusal_final=True))
    run = trace.current
    assert run.refusal == "delivery_id_conflict"
    assert "Runtime refused: delivery id conflict" in run.reason


def test_the_engine_records_whether_the_final_attempt_was_the_refusal():
    from agentdeck.poller_engine import dispatch as engine_dispatch

    attempt = {"id": "d", "attempts": 1}
    engine_dispatch.observe_attempt(attempt, clean=True, refusal="at_capacity")
    engine_dispatch.observe_attempt(attempt, clean=False)  # a later attempt timed out
    rec: dict = {}
    engine_dispatch.mark_given_up(
        rec, datetime.now(UTC), clean=engine_dispatch.provably_unwritten(attempt),
        refusal=engine_dispatch.last_refusal(attempt), final_refused=False,
    )
    assert rec["dispatch_failed_refusal"] == "at_capacity"
    assert "dispatch_failed_refusal_final" not in rec

    engine_dispatch.mark_given_up(rec, datetime.now(UTC), clean=False, refusal="x",
                                  final_refused=True)
    assert rec["dispatch_failed_refusal_final"] is True
    engine_dispatch.clear_rearm(rec)
    assert "dispatch_failed_refusal_final" not in rec


# --- third review round on #2190 -----------------------------------------------------------------


def test_participation_beats_an_ambiguous_park_and_names_the_contradiction():
    """An unacknowledged attempt may have started a worker whose acknowledgement was lost."""
    trace = _trace(
        card=_card(generation="g", dispatch_failed=True, dispatch_failed_refusal="at_capacity"),
        bindings=(_Binding("claude_code:main:s-1", "g"),),
        outcomes=(_outcome("g", "running", "turn_active"),),
    )
    run = trace.current
    assert (run.state, run.next_expected) == (RUNNING, NEXT_ACTIVITY)
    assert "never landed" in run.reason and "Runtime shows it took part" in run.reason
    assert run.refusal == ""
    assert [s.session_key for s in run.sessions] == ["claude_code:main:s-1"]
    kinds = [step.kind for step in run.milestones]
    assert "parked" in kinds and "session" in kinds and "execution" in kinds


def test_a_binding_alone_beats_an_ambiguous_park():
    trace = _trace(
        card=_card(generation="g", dispatch_failed=True),
        bindings=(_Binding("claude_code:main:s-1", "g"),),
    )
    run = trace.current
    assert (run.state, run.next_expected) == (WAITING, NEXT_ACTIVITY)
    assert "Runtime shows it took part" in run.reason


def test_an_ended_outcome_beats_an_ambiguous_park():
    trace = _trace(
        card=_card(generation="g", dispatch_failed=True),
        outcomes=(_outcome("g", "died", "process_gone"),),
    )
    run = trace.current
    assert run.state == FAILED
    assert "process gone" in run.reason and "Runtime shows it took part" in run.reason


def test_a_park_with_only_a_route_is_still_a_park():
    """A route is frozen before the host is called, so it is not participation."""
    trace = _trace(card=_card(generation="g", dispatch_failed=True), routes=(_route("g"),))
    assert (trace.current.state, trace.current.next_expected) == (NEEDS_ATTENTION, NEXT_HUMAN)


def test_a_not_started_outcome_is_not_participation():
    trace = _trace(
        card=_card(generation="g", dispatch_failed=True),
        outcomes=(_outcome("g", "not_started", "no_execution"),),
    )
    run = trace.current
    assert (run.state, run.next_expected) == (NEEDS_ATTENTION, NEXT_HUMAN)
    assert "took part" not in run.reason


# --- Runs from before the Runtime recorded anything (#2201) -------------------------------------

_RECORDING = datetime(2026, 9, 23, 21, 44, tzinfo=UTC)


def test_a_current_run_from_before_recording_is_history_not_a_warning():
    card = _card(generation="gen-old", status="In review",
                 started_at="2026-09-13T18:26:55+00:00")
    trace = _trace(card=card, evidence_since=_RECORDING)
    assert trace.current.state == HISTORICAL
    assert trace.current.reason == "execution details unavailable"
    assert trace.current.next_expected == "" and trace.current.sparse
    # Never dressed up as an outcome it does not have.
    assert trace.current.state not in {FINISHED, FAILED, UNCERTAIN}


@pytest.mark.parametrize(
    ("started_at", "evidence_since"),
    [
        ("2026-09-24T08:00:00+00:00", _RECORDING),  # began after recording: a real gap
        ("", _RECORDING),  # the Run's start is unknown
        ("2026-09-13T18:26:55+00:00", None),  # nothing was ever recorded, or unreadable
        ("not a time", _RECORDING),
    ],
)
def test_a_current_run_missing_its_facts_stays_uncertain(started_at, evidence_since):
    card = _card(generation="gen-1", started_at=started_at)
    trace = _trace(card=card, evidence_since=evidence_since)
    assert trace.current.state == UNCERTAIN
    assert trace.current.reason == "the Poller holds this Run but no Runtime fact names it"


def test_an_old_run_with_real_facts_is_read_from_them_not_called_history():
    card = _card(generation="gen-1", started_at="2026-09-01T00:00:00+00:00")
    running = _trace(card=card, evidence_since=_RECORDING,
                     outcomes=(_outcome("gen-1", "running", "turn_active"),))
    assert running.current.state == RUNNING
    dispatched = _trace(card=_card(generation="gen-1", started_at="2026-09-01T00:00:00+00:00",
                                   dispatch=_dispatch("gen-1")), evidence_since=_RECORDING)
    assert dispatched.current.state == PREPARING


def test_recording_began_with_the_earliest_runtime_fact(tmp_path):
    path = tmp_path / "control.db"
    assert read_evidence_since(path) is None
    db = Db(path)
    try:
        assert read_evidence_since(path) is None
        db.freeze_delivery_route(_route("gen-1"))  # decided 2026-09-24T08:00
        assert read_evidence_since(path) == datetime(2026, 9, 24, 8, tzinfo=UTC)
        store = OutcomeStore(path)
        store.record(ExecutionOutcome(WORK.work_id, "g", "running",
                                      observed_at=datetime(2026, 9, 20, tzinfo=UTC).timestamp()))
        store.close()
        assert read_evidence_since(path) == datetime(2026, 9, 20, tzinfo=UTC)
    finally:
        db.close()


def test_the_board_reads_when_the_card_started(tmp_path):
    from test_issue_context import _poller_root

    root = _poller_root(tmp_path, state={"issues": {"agentdeck#2141": {
        "status": "In review", "generation": "g", "started_at": "2026-09-13T18:26:55+00:00",
    }}})
    card = poller_ops.read_board(root).card("eerovil/agentdeck", 2141)
    assert card.started_at == "2026-09-13T18:26:55+00:00"


def _placed(db, run_id: str, at: str, *, work: WorkRef = WORK) -> None:
    db.freeze_delivery_route(DeliveryRoute(
        f"poller:{work.number}-{run_id}-{at}", "fp", work.work_id, run_id, "claude_code", "main",
        "w", at,
    ))


def test_a_later_reobservation_of_an_older_run_does_not_hide_the_running_one(tmp_path):
    from agentdeck.run_trace import read_current_executions

    path = tmp_path / "history.db"
    db = Db(path)
    store = OutcomeStore(path)
    try:
        _placed(db, "g1", "2026-09-24T08:00:00+00:00")
        _placed(db, "g2", "2026-09-24T09:00:00+00:00")
        store.record(ExecutionOutcome(WORK.work_id, "g2", "running", observed_at=NOW))
        # The Runtime observes Runs newest first, so the older, ended Run is written last.
        store.record(ExecutionOutcome(WORK.work_id, "g1", "ended", observed_at=NOW + 5))
        assert read_current_executions(path) == {WORK.work_id}

        # A Run a later Run replaced stops counting, however recently it was re-observed.
        store.record(ExecutionOutcome(WORK.work_id, "g1", "running", observed_at=NOW + 9))
        store.record(ExecutionOutcome(WORK.work_id, "g2", "ended", observed_at=NOW + 1))
        assert read_current_executions(path) == frozenset()

        # A Run with no route cannot be ordered, so it keeps its Work current.
        store.record(ExecutionOutcome(OTHER.work_id, "x", "waiting_human", observed_at=NOW))
        assert read_current_executions(path) == {OTHER.work_id}
        # A stopped Run is not executing, whatever its last state said.
        store.record(ExecutionOutcome(OTHER.work_id, "x", "running", observed_at=NOW,
                                      stopped_at=NOW))
        assert read_current_executions(path) == frozenset()
    finally:
        store.close()
        db.close()


def test_without_routes_every_open_outcome_counts(tmp_path):
    from agentdeck.run_trace import read_current_executions

    path = tmp_path / "history.db"
    assert read_current_executions(path) == frozenset()
    store = OutcomeStore(path)
    store.record(ExecutionOutcome(WORK.work_id, "g1", "running", observed_at=NOW))
    store.record(ExecutionOutcome(WORK.work_id, "g2", "ended", observed_at=NOW + 5))
    store.close()
    assert read_current_executions(path) == {WORK.work_id}


def test_the_runtime_refreshes_exactly_the_runs_the_trace_reads_as_executing(tmp_path):
    """#2586: the Runtime's refresh and the deck's read share one selection."""
    from agentdeck.run_trace import read_current_executions

    path = tmp_path / "history.db"
    db = Db(path)
    store = OutcomeStore(path)
    try:
        other = "github_issue:eerovil/agentdeck#2586"
        for index, run_id in enumerate(("g1", "g2")):
            assert db.freeze_delivery_route(DeliveryRoute(
                f"poller:{run_id}", "f", WORK.work_id, run_id, "claude_code", "main", "k",
                datetime.fromtimestamp(NOW + index, UTC).isoformat(),
            ))
        store.record(ExecutionOutcome(WORK.work_id, "g1", "running", observed_at=NOW))
        store.record(ExecutionOutcome(WORK.work_id, "g2", "uncertain", observed_at=NOW))
        store.record(ExecutionOutcome(other, "r1", "ended", observed_at=NOW))
        store.record(ExecutionOutcome(other, "r2", "running", observed_at=NOW,
                                      stopped_at=NOW))

        assert store.open_runs() == [(WORK.work_id, "g2")]
        assert read_current_executions(path) == {WORK.work_id}
    finally:
        store.close()
        db.close()


def test_current_executions_read_only_the_open_outcomes(tmp_path):
    import sqlite3

    from agentdeck.run_execution import OPEN_OUTCOMES, REPLACED_RUNS

    path = tmp_path / "history.db"
    Db(path).close()
    OutcomeStore(path).close()
    conn = sqlite3.connect(path)
    try:
        plan = " | ".join(row[-1] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT o.work_id FROM run_execution_outcomes AS o"
            f" WHERE {OPEN_OUTCOMES}{REPLACED_RUNS}"
        ))
    finally:
        conn.close()
    # Only the partial index of open outcomes is read, never the table's history, and each
    # Run's routes are found through the route index.
    assert plan.startswith("SCAN o USING INDEX run_execution_outcomes_open "), plan
    assert "SEARCH later USING INDEX idx_delivery_routes_work" in plan, plan
    assert "SEARCH mine USING INDEX idx_delivery_routes_work" in plan, plan


def test_the_new_readers_open_a_configured_home_path_not_a_literal_tilde(tmp_path, monkeypatch):
    from agentdeck.run_trace import read_current_executions

    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    configured = "~/.local/share/agentdeck/agentdeck.db"
    store = OutcomeStore(home / ".local/share/agentdeck/agentdeck.db")
    store.record(ExecutionOutcome(WORK.work_id, "g", "running", observed_at=NOW))
    store.close()

    assert read_current_executions(configured) == {WORK.work_id}
    assert read_evidence_since(configured) == datetime.fromtimestamp(NOW, UTC)
    # Nothing was read from, or created at, a literal "~" under the working directory.
    assert not (cwd / "~").exists()


# --- a Run is dated by what happened to it, never by the last look (#2332) ------------------


def test_an_ended_run_is_dated_by_its_result_and_not_by_the_last_sweep():
    ended = NOW - 86400 * 3
    first = _trace(card=_card(generation="g"), outcomes=(
        StoredOutcome("g", "ended", "", terminal_result_at=ended, observed_at=NOW),
    ))
    again = _trace(card=_card(generation="g"), outcomes=(
        StoredOutcome("g", "ended", "", terminal_result_at=ended, observed_at=NOW + 600),
    ))

    assert first.current.latest_at.timestamp() == pytest.approx(ended)
    assert first.current == again.current


def test_a_run_that_never_started_has_no_date_of_its_own():
    trace = _trace(card=_card(generation="g"), outcomes=(_outcome("g", "not_started"),))

    assert trace.current.latest_at is None


def test_a_stopped_run_is_dated_by_its_stop_and_a_running_one_by_its_activity():
    stopped = _trace(card=_card(generation="g"), outcomes=(
        _outcome("g", "stopped", stopped_at=NOW - 500, terminal_result_at=NOW - 400),
    ))
    running = _trace(card=_card(generation="g"), outcomes=(
        _outcome("g", "running", last_activity_at=NOW - 30),
    ))

    assert stopped.current.latest_at.timestamp() == pytest.approx(NOW - 500)
    assert running.current.latest_at.timestamp() == pytest.approx(NOW - 30)
