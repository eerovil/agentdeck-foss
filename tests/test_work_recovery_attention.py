"""Needs you means a person, never a failure AgentDeck is still recovering by itself (#2235).

Each test is one misroute #2234 found, built from the engine record's own shape and read
through ``poller_ops.read_board`` where the fact comes from the Poller: a clean dispatch park
with its free retry due, a dead Run with restarts left, a failed reviewer Run, a stopped worker
chat on a card the Poller can restart, a card waiting on named blockers, and a question form.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agentdeck import poller_ops
from agentdeck.models import Session, SessionStatus
from agentdeck.poller_engine.revive import MAX_REVIVES
from agentdeck.poller_engine.state import EnginePaths, StateStore
from agentdeck.quota_retry import message_for
from agentdeck.run_trace import RunTraceFacts, StoredOutcome, build_run_trace
from agentdeck.web.work_view import _attention
from agentdeck.work import WorkRef
from agentdeck.work_facts import work_facts
from agentdeck.work_overview import WorkAttention, build_work_card
from agentdeck.work_story import (
    ACTIVE,
    CHAT_STOPPED,
    DISPATCH_FAILED,
    NEEDS_YOU,
    RETRIES_ITSELF,
    front_row,
    work_status,
)

ORG = "eerovil"
WORK = WorkRef(f"{ORG}/agentdeck", 5)
PARKED_AT = "2026-09-25T10:00:00+00:00"


def _board(tmp_path, **cards: dict) -> poller_ops.BoardView:
    """The deck's reading of an instance whose engine records are exactly ``cards``."""
    base = tmp_path / "instances" / "agentdeck"
    base.mkdir(parents=True, exist_ok=True)
    (base / "poller.toml").write_text(
        f'contract = 1\n\n[board]\norg = "{ORG}"\nproject_number = 1\n'
    )
    store = StateStore(EnginePaths.from_base(base / "state.json"))
    with store.transaction() as engine:
        engine["issues"] = {key: dict(record) for key, record in cards.items()}
    poller_ops.forget_durable_reads()
    return poller_ops.read_board(tmp_path)


def _card(tmp_path, record: dict) -> poller_ops.BoardCard:
    card = _board(tmp_path, **{"agentdeck#5": record}).card(WORK.repository, WORK.number)
    assert card is not None
    return card


def _work(card, *, outcome: str = "", attention=(), trace=None):
    if trace is None:
        outcomes = (
            (StoredOutcome(card.generation, outcome, "", observed_at=time.time()),)
            if outcome
            else ()
        )
        trace = build_run_trace(RunTraceFacts(work=WORK, card=card, outcomes=outcomes))
    return build_work_card(
        WORK,
        issue=None,
        title="Work",
        facts=work_facts(WORK, card=card),
        trace=trace,
        attention=attention,
    )


def _in_progress(**extra) -> dict:
    return {
        "status": "In progress",
        "engine_claim": True,
        "started": True,
        "started_at": PARKED_AT,
        "generation": "gen-2",
        **extra,
    }


# --- the facts the engine already keeps, projected ------------------------------------------


def test_the_board_projects_revives_and_the_dispatch_park_as_the_engine_wrote_them(tmp_path):
    card = _card(tmp_path, _in_progress(revives=1))
    assert (card.revives, card.revives_left) == (1, MAX_REVIVES - 1)
    assert _card(tmp_path, _in_progress(revives=MAX_REVIVES)).revives_left == 0
    assert _card(tmp_path, _in_progress(revives="x")).revives == 0

    parked = _card(
        tmp_path,
        _in_progress(
            status="Blocked", dispatch_failed=True, dispatch_failed_clean=True,
            dispatch_failed_at=PARKED_AT, dispatch_failed_refusal="at_capacity",
            dispatch_failed_refusal_final=True,
        ),
    )
    work = _work(parked)
    assert (work.dispatch_failed, work.dispatch_rearms) == (True, True)


# --- M1: a clean dispatch park with its free retry still due --------------------------------


def _park(**extra) -> dict:
    return _in_progress(
        status="Blocked",
        dispatch_failed=True,
        dispatch_failed_at=PARKED_AT,
        dispatch_failed_refusal="at_capacity",
        **extra,
    )


def test_a_clean_dispatch_park_with_its_free_retry_due_is_automatic_progress(tmp_path):
    record = _park(dispatch_failed_clean=True, dispatch_failed_refusal_final=True)
    work = _work(_card(tmp_path, record))
    story = work_status(work)

    assert (story.tone, story.you) == (ACTIVE, RETRIES_ITSELF)
    assert story.kind != DISPATCH_FAILED
    assert front_row(work, current=True).section != NEEDS_YOU


def test_a_chat_that_stopped_still_shows_while_the_start_is_retried(tmp_path):
    record = _park(dispatch_failed_clean=True)
    manual = WorkAttention("claude_code:main:manual", "Manual", "stalled", "/chats/m")
    work = _work(_card(tmp_path, record), attention=(manual,))

    assert work_status(work).kind == CHAT_STOPPED
    assert front_row(work, current=True).section == NEEDS_YOU


def test_a_side_chat_waiting_for_its_quota_reset_is_not_needs_you(tmp_path):
    item = WorkAttention(
        "claude_code:main:side", "Side chat", "waiting", "/chats/side",
        recovering=True, cause="turn_failed",
    )
    work = _work(
        _card(
            tmp_path,
            _park(dispatch_failed_clean=True, dispatch_failed_refusal_final=True),
        ),
        attention=(item,),
    )

    assert work_status(work).kind != CHAT_STOPPED
    assert front_row(work, current=True).section != NEEDS_YOU


def test_a_pending_provider_retry_marks_the_chat_as_recovering():
    key = "claude_code:main:side"
    session = Session(
        key=key,
        account_key="claude_code:main",
        session_id="side",
        status=SessionStatus.LIVE,
        turn_failed=True,
        automatic_retry_at=datetime(2026, 10, 12, 17, 50, tzinfo=UTC),
    )
    retry = message_for(session)
    assert retry is not None
    state = SimpleNamespace(sessions={key: session})
    app = SimpleNamespace(state=SimpleNamespace(app_state=state, assistant=None))
    insight = SimpleNamespace(session_key=key, kind="waiting", cause="turn_failed")

    (item,) = _attention(app, (insight,), None, {}, {retry.id})

    assert item.recovering is True


def test_a_quota_retry_that_is_no_longer_pending_needs_attention():
    key = "claude_code:main:side"
    session = Session(
        key=key,
        account_key="claude_code:main",
        session_id="side",
        status=SessionStatus.LIVE,
        turn_failed=True,
        automatic_retry_at=datetime(2026, 10, 12, 17, 50, tzinfo=UTC),
    )
    retry = message_for(session)
    assert retry is not None
    state = SimpleNamespace(sessions={key: session})
    app = SimpleNamespace(state=SimpleNamespace(app_state=state, assistant=None))
    insight = SimpleNamespace(session_key=key, kind="waiting", cause="turn_failed")

    (item,) = _attention(app, (insight,), None, {}, frozenset())

    assert item.recovering is False


@pytest.mark.parametrize(
    "park",
    [
        # The free retry is spent: only a new instruction starts it again.
        {"dispatch_failed_clean": True, "dispatch_rearmed": True},
        # An attempt's outcome is unknown: retrying could start a second worker.
        {},
    ],
    ids=["retry-spent", "ambiguous"],
)
def test_a_dispatch_park_nothing_retries_still_needs_you(tmp_path, park):
    work = _work(_card(tmp_path, _park(**park)))

    assert work.dispatch_rearms is False
    assert work_status(work).kind == DISPATCH_FAILED
    assert front_row(work, current=True).section == NEEDS_YOU


# --- M2: a Run that died or was abandoned while restarts remain -----------------------------


@pytest.mark.parametrize("ending", ["died", "abandoned"])
def test_a_dead_run_with_restarts_left_is_not_needs_you(tmp_path, ending):
    work = _work(_card(tmp_path, _in_progress(revives=MAX_REVIVES - 1)), outcome=ending)

    assert work.stuck and work_status(work).you == RETRIES_ITSELF
    assert front_row(work, current=True).section != NEEDS_YOU


@pytest.mark.parametrize("ending", ["died", "abandoned"])
def test_a_dead_run_past_its_restarts_needs_you(tmp_path, ending):
    work = _work(_card(tmp_path, _in_progress(revives=MAX_REVIVES)), outcome=ending)

    assert work_status(work).headline == "Run needs a look"
    assert front_row(work, current=True).section == NEEDS_YOU


# --- M3: a failed reviewer Run is the review's to report ------------------------------------


@pytest.mark.parametrize("ending", ["died", "uncertain"])
def test_a_failed_reviewer_run_keeps_the_reviews_automatic_retry(tmp_path, ending):
    card = _card(
        tmp_path,
        _in_progress(mode="merge-review", revives=MAX_REVIVES),
    )
    work = _work(card, outcome=ending)
    story = work_status(work)

    assert work.stuck and work.review is not None and work.review.state == "failed"
    assert (story.headline, story.you) == ("Review stopped", RETRIES_ITSELF)
    assert front_row(work, current=True).section != NEEDS_YOU


# --- M4: a stopped worker chat on a card the Poller can still restart ------------------------


WORKER = "claude_code:main:worker-2"


class _Binding:
    """The Runtime's exact record of a session taking part in a Run."""

    def __init__(self, run_id: str) -> None:
        self.session_key, self.work_id, self.run_id = WORKER, WORK.work_id, run_id
        self.first_delivery_id = f"poller:d-{run_id}"
        self.bound_at = "2026-09-25T10:00:05+00:00"


def _worker_work(record: dict, ending: str, *, tmp_path, key: str = WORKER):
    """The card's current Run, its worker chat bound to it, and a stopped Deckhand card."""
    card = _card(tmp_path, record)
    outcome = StoredOutcome(card.generation, ending, "", observed_at=time.time())
    facts = RunTraceFacts(
        work=WORK, card=card, outcomes=(outcome,), bindings=(_Binding(card.generation),)
    )
    trace = build_run_trace(facts)
    stopped = WorkAttention(key, "A chat", "stalled", "/chats/x")
    return _work(card, trace=trace, attention=(stopped,))


@pytest.mark.parametrize("ending", ["died", "abandoned"])
def test_a_stopped_worker_chat_is_not_needs_you_while_the_poller_restarts_its_run(
    tmp_path, ending
):
    work = _worker_work(_in_progress(revives=1), ending, tmp_path=tmp_path)

    assert work.attention[0].recovering
    assert work_status(work).kind != CHAT_STOPPED
    assert front_row(work, current=True).section != NEEDS_YOU


@pytest.mark.parametrize(
    ("record", "ending", "key"),
    [
        # The current Run is still going: nothing ended, so nothing is being restarted.
        (_in_progress(), "running", WORKER),
        # A chat that is not one of the Work's own Runs: Deckhand's reading stands.
        (_in_progress(), "died", "claude_code:main:manual"),
        # Restarts spent: nothing recovers the card by itself.
        (_in_progress(revives=MAX_REVIVES), "died", WORKER),
        # Parked for a person, so the Poller is not recovering it.
        (_in_progress(status="Blocked"), "died", WORKER),
    ],
    ids=["run-still-going", "manual-chat", "restarts-spent", "parked"],
)
def test_a_stopped_chat_still_needs_you_when_nothing_recovers_it(tmp_path, record, ending, key):
    work = _worker_work(record, ending, tmp_path=tmp_path, key=key)

    assert not work.attention[0].recovering
    assert work_status(work).kind == CHAT_STOPPED
    assert front_row(work, current=True).section == NEEDS_YOU


# --- M5: named blockers beat a retained review or release bit --------------------------------


@pytest.mark.parametrize(
    "record",
    [
        # A worker's `finish blocked --blocked-by` on a card that kept its pass.
        _in_progress(
            status="Blocked", blocked_until=[f"{ORG}/agentdeck#1"],
            release_review_pass={"heads": {"agentdeck": "abc1234"}},
        ),
        # A release about to block, on a card that also waits on another one.
        _in_progress(
            status="Ready to merge", blocked_until=[f"{ORG}/agentdeck#1"],
            blocked_pending={"why": "checks failed", "merged": [], "steps": {}},
        ),
        # A card resting in Blocked on a native dependency the engine resumes it from (#2241).
        _in_progress(status="Blocked", blocked_by=["agentdeck#1"]),
        _in_progress(
            status="Blocked", blocked_by=["agentdeck#1"],
            release_review_pass={"heads": {"agentdeck": "abc1234"}},
        ),
    ],
    ids=["retained-pass", "pending-block", "native-dependency", "native-dependency-pass"],
)
def test_named_blockers_wait_automatically_rather_than_asking_for_a_release(tmp_path, record):
    card = _card(tmp_path, record)
    # The worker's turn ended with its report; only the card's bits are left.
    work = _work(card, outcome="ended")

    assert card.blocked_by and work.review is None and not work.parked
    assert work_status(work).headline != "Release needs your decision"
    assert front_row(work, current=True).section != NEEDS_YOU


# --- M6: only a question in the chat's text is answered by a message --------------------------


@pytest.mark.parametrize(
    ("interaction", "action"), [("", "Answer"), ("question", "Open"), ("mcp_form", "Open")]
)
def test_the_front_page_answers_only_what_a_message_answers(tmp_path, interaction, action):
    ask = WorkAttention(
        "claude_code:main:worker", "Worker", "waiting", "/chats/w",
        asks=True, question="Which branch?", interaction=interaction,
    )
    row = front_row(_work(_card(tmp_path, _in_progress()), attention=(ask,)), current=True)

    assert (row.section, row.action) == (NEEDS_YOU, action)


# --- the two lasting engine facts, stale-safe --------------------------------------------------


def _release_park(**extra) -> dict:
    parked = {"why": "Auto-merge is disabled", "status": "Blocked", "generation": "gen-2"}
    return _in_progress(**{
        "status": "Blocked", "release_blocked": parked, **extra,
    })


def test_the_release_park_reason_is_read_only_while_that_park_is_current(tmp_path):
    card = _card(tmp_path, _release_park())
    assert card.release_parked == "Auto-merge is disabled"
    assert _work(card).release_parked == "Auto-merge is disabled"

    stale = {
        "moved": _release_park(status="In progress"),
        "later Run": _release_park(generation="gen-3"),
        "dispatching": _release_park(dispatch={"id": "d-3", "run_id": "gen-3"}),
        "releasing again": _release_park(mode="merge"),
        "blocking again": _release_park(blocked_pending={"why": "x", "steps": {}}),
    }
    for why, record in stale.items():
        assert _card(tmp_path, record).release_parked == "", why


def test_the_human_review_fallback_is_read_only_while_the_card_rests_in_needs_more_info(tmp_path):
    marked = _in_progress(status="Needs More Info", parked_as="human_review_pending")
    card = _card(tmp_path, marked)
    assert card.parked_as == "human_review_pending"
    assert _work(card).parked_as == "human_review_pending"

    assert _card(tmp_path, {**marked, "status": "In progress"}).parked_as == ""
    assert _card(tmp_path, {**marked, "parked_as": "something_else"}).parked_as == ""


# --- #2896: a worker waiting on a person's prompt is not a Run that failed --------------------


def _prompt_parked(**extra) -> dict:
    return _in_progress(
        status="Needs More Info", prompt_parked="gen-2", parked_as="prompt", **extra
    )


@pytest.mark.parametrize("record", [_prompt_parked(), _in_progress()])
def test_a_worker_waiting_on_a_prompt_no_chat_shows_asks_for_an_answer(tmp_path, record):
    from agentdeck.work_attention import NEEDS_ANSWER, operator_attention
    from agentdeck.work_story import CLARIFICATION

    work = _work(_card(tmp_path, record), outcome="waiting_human")
    story = work_status(work)
    attention = operator_attention(work, story)

    assert not work.stuck
    assert work.parked == "needs_more_info"
    assert story.kind == CLARIFICATION
    assert (attention.category, attention.bug) == (NEEDS_ANSWER, "")


def test_a_prompt_its_chat_already_shows_is_not_asked_twice(tmp_path):
    from agentdeck.work_story import CHAT_QUESTION

    ask = WorkAttention(
        "claude_code:main:worker", "Worker", "waiting", "/chats/w",
        asks=True, question="Which branch?", interaction="question",
    )
    work = _work(_card(tmp_path, _prompt_parked()), outcome="waiting_human", attention=(ask,))

    assert (work.stuck, work.parked) == (False, "")
    assert work_status(work).kind == CHAT_QUESTION


def test_an_abandoned_run_is_still_stuck(tmp_path):
    from agentdeck.work_attention import NEEDS_RETRY, operator_attention

    work = _work(_card(tmp_path, _in_progress(revives=MAX_REVIVES)), outcome="abandoned")
    attention = operator_attention(work, work_status(work))

    assert (work.stuck, work.parked) == (True, "")
    assert attention.category == NEEDS_RETRY
