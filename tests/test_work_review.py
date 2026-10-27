"""A Work says where its review has got to without opening a chat (#2216).

The summary is read from the Poller card's own release facts: a review queued, running,
passed or asking for another automatic round is progress; only a review that cannot move
without a person is attention.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from public_copy import private_feature

from agentdeck import poller_nudge as _poller_nudge
from agentdeck import poller_ops
from agentdeck.poller_nudge import ExternalReviews
from agentdeck.poller_ops import BoardCard, BoardDispatch, BoardScope, BoardView
from agentdeck.run_trace import (
    FINISHED,
    NEXT_HUMAN,
    NEXT_REVIEW,
    PREPARING,
    RUNNING,
    UNCERTAIN,
    WAITING,
    RunView,
)
from agentdeck.work import WorkRef
from agentdeck.work_facts import FOUND_BY_CARD, OutputFact, work_facts
from agentdeck.work_overview import (
    REVIEW_BLOCKED,
    REVIEW_FAILED,
    REVIEW_HUMAN,
    REVIEW_PASSED,
    REVIEW_PENDING,
    REVIEW_RUNNING,
    build_work_card,
    summarize_review,
    work_href,
)

WORK = WorkRef("eerovil/agentdeck", 2147)
PR = "https://github.com/eerovil/agentdeck/pull/2300"


def _card(**fields) -> BoardCard:
    fields.setdefault("status", "In progress")
    fields.setdefault("ownership", True)
    fields.setdefault("pull_request_url", PR)
    return BoardCard("eerovil/agentdeck", WORK.number, **fields)


def _reviewing(**fields) -> BoardCard:
    """A card whose worker finished and whose reviewer is armed."""
    return _card(review_mode="merge-review", generation="gen-review", **fields)


def _in_review(**fields) -> BoardCard:
    return _card(status="In review", in_review=True, status_role="in_review", **fields)


def _work(card: BoardCard, run_state: str = "", next_expected: str = ""):
    trace = None
    if run_state:
        run = RunView(
            run_id=card.generation or "gen-1",
            state=run_state,
            next_expected=next_expected,
            current=True,
        )
        trace = SimpleNamespace(current=run, preflight="", preflight_next="", unreadable=())
    return build_work_card(
        WORK, issue=None, title="Review progress", facts=work_facts(WORK, card=card), trace=trace
    )


# --- the acceptance cases ---------------------------------------------------------------------


def test_implementation_done_and_review_running_is_progress_not_attention():
    work = _work(_reviewing(), RUNNING, "worker activity")

    review = work.review
    assert review is not None
    assert (review.state, review.label) == (REVIEW_RUNNING, "Review running")
    assert review.implemented and not review.needs_human
    # Provenance: the reviewer's own Run and the pull request under review.
    assert (review.run_id, review.pull_request) == ("gen-review", PR)
    assert not work.needs_attention


@pytest.mark.parametrize("run_state", [PREPARING, WAITING, ""])
def test_an_armed_reviewer_that_has_not_started_is_queued(run_state):
    review = summarize_review(_reviewing(), "gen-review", run_state)

    assert review is not None and review.state == REVIEW_PENDING
    assert review.label == "Review queued" and not review.needs_human


def test_a_finished_reviewer_waits_for_the_poller_to_apply_its_verdict():
    review = summarize_review(_reviewing(), "gen-review", FINISHED)

    assert review is not None and review.state == REVIEW_RUNNING
    assert review.next_expected == "verdict to apply"


def test_a_clean_review_is_passed_whether_merging_or_resting():
    merging = summarize_review(
        _card(review_mode="merge", review_passed_heads=("a" * 40,)), "gen-review", RUNNING
    )
    resting = summarize_review(_in_review(review_passed_heads=("b" * 40,)))

    assert merging is not None and resting is not None
    assert (merging.state, merging.label) == (REVIEW_PASSED, "Review passed at aaaaaaa; merging")
    assert (resting.state, resting.label) == (REVIEW_PASSED, "Review passed at bbbbbbb")
    assert resting.heads == ("b" * 40,)
    assert not merging.needs_human and not resting.needs_human


def test_a_merge_card_whose_pull_request_merged_no_longer_says_merging():
    """#2316: the card stays in merge mode until the deploy closes it."""
    card = _card(review_mode="merge", review_passed_heads=("a" * 40,))
    pr = "https://github.com/eerovil/agentdeck/pull/2300"
    merged = OutputFact(pr, "eerovil/agentdeck", 2300, FOUND_BY_CARD, "merged")
    still_open = OutputFact(pr[:-1] + "1", "eerovil/agentdeck", 2301, FOUND_BY_CARD, "open")

    done = summarize_review(card, outputs=(merged,))
    partial = summarize_review(card, outputs=(merged, still_open))

    assert done is not None and done.label == "Review passed at aaaaaaa; merged"
    assert done.next_expected == "merge"
    assert partial is not None and partial.label == "Review passed at aaaaaaa; merging"


def test_a_review_only_a_person_can_move_is_attention():
    work = _work(_in_review(), WAITING, NEXT_REVIEW)

    review = work.review
    assert review is not None and review.state == REVIEW_HUMAN
    assert review.needs_human and work.needs_attention
    # The Run waits on "review decision"; the review knows that decision is a person's.
    assert work.next_expected == NEXT_HUMAN


@pytest.mark.parametrize(
    ("role", "status", "label"),
    [
        ("ready_to_merge", "Ready to merge", "nothing reviewed the current head"),
        ("human_review_pending", "Human review pending", "Waiting for a human review"),
    ],
)
def test_other_resting_columns_name_what_the_person_is_for(role, status, label):
    review = summarize_review(_card(status=status, status_role=role))

    assert review is not None and review.state == REVIEW_HUMAN and label in review.label


def test_a_reviewer_run_that_failed_or_went_quiet_is_a_review_failure():
    review = summarize_review(_reviewing(), "gen-review", UNCERTAIN)

    assert review is not None and review.state == REVIEW_FAILED
    assert review.label == "Review uncertain"


def test_a_blocked_release_or_a_parked_reviewed_card_needs_a_person():
    blocked = summarize_review(_reviewing(release_blocked="the repair was exhausted"))
    parked = summarize_review(
        _card(status="Blocked", status_role="blocked", review_passed_heads=("a" * 40,))
    )

    assert blocked is not None and parked is not None
    assert (blocked.state, blocked.label) == (
        REVIEW_BLOCKED,
        "Release blocked: the repair was exhausted",
    )
    assert parked.state == REVIEW_BLOCKED and parked.needs_human


def test_no_review_in_play_says_nothing():
    assert summarize_review(None) is None
    assert summarize_review(_card()) is None
    assert summarize_review(_card(status="Blocked", status_role="blocked")) is None
    assert summarize_review(_in_review(done=True)) is None


# --- review findings on #2217 -----------------------------------------------------------------


def _reviews(**fields) -> ExternalReviews:
    fields.setdefault("awaiting", {})
    fields.setdefault("given_up", frozenset())
    return ExternalReviews(None, **fields)


def test_an_open_external_review_chat_is_a_running_review_not_a_person():
    card = _in_review(
        reviewer=_reviews(awaiting={"eerovil/agentdeck#2300": "eerovil/review-chats#2054"})
    )
    work = _work(card, WAITING, NEXT_REVIEW)

    review = work.review
    assert review is not None and review.state == REVIEW_RUNNING
    assert review.label == "External review running in eerovil/review-chats#2054"
    assert not review.needs_human and not work.needs_attention
    assert work.next_expected == "review verdict"


def _held_work(holds, chat="eerovil/review-chats#2054"):
    card = _in_review(reviewer=_reviews(awaiting={"eerovil/agentdeck#2300": chat}))
    return build_work_card(
        WORK, issue=None, title="Review progress",
        facts=work_facts(WORK, card=card, chat_holds=holds),
    )


def test_a_review_chat_that_waits_on_a_person_is_not_a_running_review():
    """#2442: the reviewer still awaits chat #2054, but the chat is marked blocked."""
    work = _held_work({"eerovil/review-chats#2054": ("blocked",)})

    review = work.review
    assert review is not None and review.state == REVIEW_HUMAN and review.needs_human
    assert review.label == (
        "External review is blocked in eerovil/review-chats#2054; waiting for a person"
    )
    assert review.pull_request == PR
    assert work.outputs[0].review == "held"
    assert work.outputs[0].review_held == ("blocked",)
    assert work.needs_attention
    # The reviewer names the chat as its settings spell it; the board's keys are casefolded.
    folded = _held_work({"eerovil/review-chats#2054": ("blocked",)}, "EeroVil/Review-Chats#2054")
    assert folded.review is not None and folded.review.state == REVIEW_HUMAN

    both = _held_work({"eerovil/review-chats#2054": ("needs-more-info", "blocked")}).review
    assert both is not None and both.label.startswith(
        "External review is needs-more-info, blocked in eerovil/review-chats#2054"
    )


def test_only_the_awaited_chat_itself_holds_a_review():
    # Unknown to the board, the same number in another repository, or no holds at all: running.
    for holds in (None, {}, {"other/review-chats#2054": ("blocked",)},
                  {"eerovil/review-chats#2055": ("blocked",)}):
        review = _held_work(holds).review
        assert review is not None and review.state == REVIEW_RUNNING, holds
    # An awaited review whose chat is unnamed has nothing to look up.
    card = _in_review(reviewer=_reviews(awaiting={"eerovil/agentdeck#2300": ""}))
    facts = work_facts(WORK, card=card, chat_holds={"": ("blocked",)})
    assert facts.outputs[0].review == "awaiting"


def test_a_held_review_chat_outranks_another_that_is_still_running():
    card = _in_review()
    held = OutputFact(
        PR, "eerovil/agentdeck", 2300, FOUND_BY_CARD, "open", "a" * 40, "held",
        "eerovil/review-chats#1", review_held=("blocked",),
    )
    running = OutputFact(
        PR.replace("2300", "2301"), "eerovil/agentdeck", 2301, FOUND_BY_CARD, "open", "b" * 40,
        "awaiting", "eerovil/review-chats#2",
    )
    for outputs in ((running, held), (held, running)):
        review = summarize_review(card, outputs=outputs)
        assert review is not None and review.state == REVIEW_HUMAN
        assert review.pull_request == PR and "review-chats#1" in review.label


def test_an_external_reviewer_with_no_chat_open_is_queued_and_one_that_gave_up_is_a_person():
    queued = summarize_review(_in_review(reviewer=_reviews()))
    given_up = summarize_review(
        _in_review(reviewer=_reviews(given_up=frozenset({"eerovil/agentdeck#2300"})))
    )

    assert queued is not None and queued.state == REVIEW_PENDING and not queued.needs_human
    assert given_up is not None and given_up.state == REVIEW_HUMAN and given_up.needs_human


def test_a_plain_merge_does_not_claim_a_review_passed():
    assert summarize_review(_card(review_mode="merge"), "gen-merge", RUNNING) is None
    for evidence in ({"auto_release": True}, {"review_approved": True}):
        review = summarize_review(_card(review_mode="merge", **evidence), "gen-merge", RUNNING)
        assert review is not None and review.state == REVIEW_PASSED, evidence


def test_a_parked_release_outranks_the_pass_it_kept():
    # _block_release keeps release_review_pass and clears blocked_pending.
    heads = ("c" * 40,)
    ready = summarize_review(
        _card(status="Ready to merge", status_role="ready_to_merge", review_passed_heads=heads,
              auto_release=True)
    )
    blocked = summarize_review(
        _card(status="Blocked", status_role="blocked", review_passed_heads=heads)
    )

    assert ready is not None and ready.state == REVIEW_HUMAN and ready.needs_human
    assert "the release did not land" in ready.label
    assert blocked is not None and blocked.state == REVIEW_BLOCKED and blocked.needs_human


@private_feature(_poller_nudge)
def test_the_board_reads_the_external_reviewer_for_each_cards_pull_request(tmp_path):
    import json

    from agentdeck.poller_engine.state import EnginePaths, StateStore

    base = tmp_path / "instances" / "agentdeck"
    base.mkdir(parents=True)
    (base / "poller.toml").write_text(
        'contract = 1\n\n[board]\norg = "eerovil"\nproject_number = 1\n'
    )
    nudge = base / "chatgpt-nudge.json"
    (base / "overlay.toml").write_text(
        f'[extensions.chatgpt-nudge]\nstate_file = "{nudge}"\n'
        'review_program = ["agentdeck", "chat-review"]\n'
    )
    nudge.write_text(json.dumps({
        "awaiting": {"EeroVil/AgentDeck#11": {"sha": "a" * 40, "repository":
                     "eerovil/review-chats", "chat": 2054}},
        "review_misses": {"eerovil/agentdeck#12": {"sha": "b" * 40, "count": 3},
                          "eerovil/agentdeck#13": {"sha": "b" * 40, "count": 1}},
    }))

    def pr(number: int) -> dict:
        url = f"https://github.com/eerovil/agentdeck/pull/{number}"
        return {"status": "In review", "comment_links": {"pull_request_url": url}}

    store = StateStore(EnginePaths.from_base(base / "state.json"))
    with store.transaction() as state:
        state["issues"] = {
            "agentdeck#1": pr(11),
            "agentdeck#2": pr(12),
            "agentdeck#3": {**pr(13), "auto_release": True, "review_heads": {"x": "a"}},
            "agentdeck#4": {"status": "In review"},
        }
    poller_ops.forget_durable_reads()

    board = poller_ops.read_board(tmp_path)

    def card(number: int) -> BoardCard:
        return board.card("eerovil/agentdeck", number)

    assert (card(1).external_review, card(1).external_review_chat) == (
        "awaiting",
        "eerovil/review-chats#2054",
    )
    assert card(2).external_review == "given_up"
    assert card(3).external_review == "configured"
    assert (card(3).auto_release, card(3).review_approved) == (True, True)
    assert card(4).external_review == ""  # no pull request: nothing to review

    # Without a review_program the instance has no external reviewer at all.
    (base / "overlay.toml").write_text(f'[extensions.chatgpt-nudge]\nstate_file = "{nudge}"\n')
    poller_ops.forget_durable_reads()
    assert poller_ops.read_board(tmp_path).card("eerovil/agentdeck", 1).external_review == ""


def _reviewer(tmp_path, state_file, state: dict, cards: dict) -> None:
    import json

    from agentdeck.poller_engine.state import EnginePaths, StateStore

    base = tmp_path / "instances" / "agentdeck"
    base.mkdir(parents=True, exist_ok=True)
    (base / "poller.toml").write_text(
        'contract = 1\n\n[board]\norg = "eerovil"\nproject_number = 1\n'
    )
    (base / "overlay.toml").write_text(
        f'[extensions.chatgpt-nudge]\nstate_file = "{state_file}"\n'
        'review_program = ["agentdeck", "chat-review"]\n'
    )
    state_file.write_text(json.dumps(state))
    store = StateStore(EnginePaths.from_base(base / "state.json"))
    with store.transaction() as engine:
        engine["issues"] = {
            key: {
                "status": "In review",
                "comment_links": {
                    "pull_request_url": f"https://github.com/eerovil/agentdeck/pull/{pr}"
                },
            }
            for key, pr in cards.items()
        }


@private_feature(_poller_nudge)
def test_the_board_honours_the_settlement_the_reviewer_recorded(tmp_path):
    # The extension persists its own policy and each miss's settlement (#1910); a count at
    # the old cap of three is not "given up" when the reviewer settled it as a retry.
    _reviewer(
        tmp_path,
        tmp_path / "nudge.json",
        {
            "policy": {"schema": 1, "max_review_attempts": 9, "receipt_deadline_s": 1200},
            "review_misses": {
                "eerovil/agentdeck#11": {"sha": "a", "count": 3, "settled": "retry"},
                "eerovil/agentdeck#12": {"sha": "b", "count": 1, "settled": "given_up"},
                "eerovil/agentdeck#13": {"sha": "c", "count": 4},  # policy cap 9, unsettled
            },
        },
        {"agentdeck#1": 11, "agentdeck#2": 12, "agentdeck#3": 13},
    )
    poller_ops.forget_durable_reads()

    board = poller_ops.read_board(tmp_path)

    assert [board.card("eerovil/agentdeck", n).external_review for n in (1, 2, 3)] == [
        "configured",
        "given_up",
        "configured",
    ]


@private_feature(_poller_nudge)
def test_a_reviewer_state_file_outside_the_instance_still_updates_the_board(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    state_file = elsewhere / "reviews.json"
    _reviewer(tmp_path, state_file, {}, {"agentdeck#1": 11})
    poller_ops.forget_durable_reads()
    assert poller_ops.read_board(tmp_path).card("eerovil/agentdeck", 1).external_review == (
        "configured"
    )

    # The review starts: only the extension's own file changes, and nothing is forgotten.
    import json

    state_file.write_text(json.dumps({"awaiting": {"eerovil/agentdeck#11": {
        "sha": "a" * 40, "repository": "eerovil/review-chats", "chat": 2054}}}))

    card = poller_ops.read_board(tmp_path).card("eerovil/agentdeck", 1)
    assert (card.external_review, card.external_review_chat) == (
        "awaiting",
        "eerovil/review-chats#2054",
    )


# --- the facts come from the engine's own state ----------------------------------------------


def test_the_board_reads_the_release_facts_and_the_boards_own_column_names(tmp_path):
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    base = tmp_path / "instances" / "agentdeck"
    base.mkdir(parents=True)
    (base / "poller.toml").write_text(
        'contract = 1\n\n[board]\norg = "eerovil"\nproject_number = 1\n\n'
        '[board.statuses]\nready_to_merge = "Approved, not merged"\n'
    )
    store = StateStore(EnginePaths.from_base(base / "state.json"))
    with store.transaction() as state:
        state["issues"] = {
            "agentdeck#1": {
                "status": "In progress",
                "mode": "merge-review",
            },
            "agentdeck#2": {
                "status": "approved, NOT merged",
                "blocked_pending": {"why": "deploy failed", "merged": [], "steps": {}},
            },
            "agentdeck#3": {"status": "In review", "blocked_pending": 1},
        }
    poller_ops.forget_durable_reads()

    board = poller_ops.read_board(tmp_path)

    first = board.card("eerovil/agentdeck", 1)
    assert (first.review_mode, first.status_role) == ("merge-review", "")
    second = board.card("eerovil/agentdeck", 2)
    assert (second.status_role, second.release_blocked) == ("ready_to_merge", "deploy failed")
    # Malformed engine state says nothing rather than breaking the board.
    third = board.card("eerovil/agentdeck", 3)
    assert (third.status_role, third.release_blocked) == ("in_review", "")


# --- the Work UI renders it, and a change reaches it ------------------------------------------


def _app(tmp_path, card: BoardCard):
    from test_web import _app_with_state

    app = _app_with_state(tmp_path)
    board = BoardView(
        cards={card.identity: card},
        scopes=(BoardScope("agentdeck", org="eerovil", cards=(card,)),),
    )
    app.state.issues.read_board = lambda: board
    return app


def test_the_work_page_shows_review_progress_outside_every_disclosure(tmp_path):
    from fastapi.testclient import TestClient

    app = _app(tmp_path, _card(review_offer="rejected"))
    with TestClient(app) as client:
        html = client.get(work_href(WORK.work_id)).text

    # The Work page's top status (#2218) tells the worker's fix round, calmly.
    section = html.split('id="work-status"', 1)[1].split("</section>", 1)[0]
    assert 'data-wd-review="changes"' in section and 'data-wd-tone="active"' in section
    assert "Fixing review findings" in section
    assert "the worker is fixing them" in section
    assert "Nothing needed" in section
    assert "<details" not in html.split('id="work-status"', 1)[0].split("<main", 1)[1]


@pytest.mark.asyncio
async def test_a_review_transition_changes_the_cards_revision(tmp_path):
    from agentdeck.web.work_view import load_work_overview

    now = datetime.now(UTC)
    dispatch = BoardDispatch(
        id="d-review", action="merge-review", run_id="gen-review", work_id=WORK.work_id
    )
    running = await load_work_overview(_app(tmp_path / "a", _reviewing(dispatch=dispatch)), now=now)
    human = await load_work_overview(_app(tmp_path / "b", _in_review()), now=now)

    before = next(card for card in running.cards if card.work_id == WORK.work_id)
    after = next(card for card in human.cards if card.work_id == WORK.work_id)
    assert before.review.state == REVIEW_PENDING  # dispatched, not yet accepted: queued
    assert not before.needs_attention
    assert after.review.state == REVIEW_HUMAN
    assert before.revision != after.revision
