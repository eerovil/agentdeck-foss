"""Every Work card tells the truth (#2226).

A card claims something only when the deck has evidence for it. These cases are built from
the real state shapes the 2026-09-25 audit found: the chatgpt-nudge extension's ``seen``,
``stalled``, ``missing_receipts`` and ``review_misses``; Poller cards that name their pull
request through ``comment_links`` only, through ``review_prs`` only, or not at all; closed
issues whose card the engine has not moved yet; and issue records cached before ``closedAt``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from public_copy import private_feature

from agentdeck import poller_nudge as _poller_nudge
from agentdeck import poller_ops
from agentdeck.poller_ops import BoardCard
from agentdeck.work import WorkRef
from agentdeck.work_facts import (
    FOUND_BY_BRANCH,
    FOUND_BY_CHAT,
    FOUND_BY_CLOSING,
    Registration,
    work_facts,
    work_finished_at,
)
from agentdeck.work_overview import (
    CAUSE_HUMAN_REVIEW,
    CAUSE_NO_OUTPUT,
    REVIEW_HUMAN,
    REVIEW_PENDING,
    REVIEW_RUNNING,
    WorkChat,
    build_work_card,
    summarize_review,
    work_title,
)
from agentdeck.work_story import (
    DISPATCH_FAILED,
    FINISHED,
    IN_PROGRESS,
    NEEDS_YOU,
    QUIET,
    FrontRow,
    front_row,
    front_sections,
    work_status,
)

NOW = datetime(2026, 9, 25, 13, 25, tzinfo=UTC)
ORG = "Northwind"
HEAD = "7957ce96b80f6085ddfa24f281b96fcf0dbc3180"
MOVED = "05d522c27a2c63b087451e0613d65b3a44dbec5c"
EXTENSIONS = Path(__file__).resolve().parent.parent / "poller" / "extensions"


def _pull(repo: str, number: int) -> str:
    return f"https://github.com/{ORG}/{repo}/pull/{number}"


def _row(state: str = "OPEN", head: str = HEAD, merged: bool = False) -> dict:
    """A cached pull request row in ``git_context._PR_FIELDS``' shape."""
    return {
        "state": "MERGED" if merged else state,
        "mergedAt": "2026-09-25T12:00:00Z" if merged else None,
        "headRefOid": head,
    }


# --- the reviewer's own state, read the way the board reads it ---------------------------------


def _trailhead(tmp_path, nudge: dict, cards: dict) -> poller_ops.BoardView:
    """An trailhead-shaped instance: the extension watches three repositories."""
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    base = tmp_path / "instances" / "trailhead"
    base.mkdir(parents=True)
    (base / "poller.toml").write_text(
        f'contract = 1\n\n[board]\norg = "{ORG}"\nproject_number = 1\n\n'
        "[extensions.chatgpt-nudge]\n"
        f'repos = ["{ORG}/store", "{ORG}/abacus", "{ORG}/metso"]\n'
    )
    state_file = base / "chatgpt-nudge.json"
    (base / "overlay.toml").write_text(
        f'[extensions.chatgpt-nudge]\nstate_file = "{state_file}"\n'
        'review_program = ["agentdeck", "chat-review"]\n'
    )
    state_file.write_text(json.dumps(nudge))
    store = StateStore(EnginePaths.from_base(base / "state.json"))
    with store.transaction() as engine:
        engine["issues"] = {
            key: {"status": "In review", "last_live_check": "2026-09-25T13:20:00+00:00", **card}
            for key, card in cards.items()
        }
    poller_ops.forget_durable_reads()
    return poller_ops.read_board(tmp_path)


# The literal shapes from the live state files (redacted to what matters).
NUDGE = {
    "policy": {"schema": 1, "max_review_attempts": 3, "receipt_deadline_s": 1200},
    "awaiting": {},
    "seen": {f"{ORG}/metso#52": f"{HEAD}@2"},
    "stalled": {
        f"{ORG}/store#3295": {
            "advances": 2,
            "sha": HEAD,
            "why": "reviewed, up to date with its base, and still not merged",
        }
    },
    "missing_receipts": {f"{ORG}/abacus#1853": {"count": 3, "sha": HEAD}},
    "review_misses": {
        f"{ORG}/store#3343": {
            "count": 3, "settled": "given_up", "sha": HEAD,
            "why": "the closing note did not say the review ran",
        }
    },
}

CARDS = {
    # comment_links only
    "issues#397": {"comment_links": {"pull_request_url": _pull("store", 3295)}},
    # review_prs only
    "metso#51": {"review_prs": ["metso#52"]},
    "issues#1": {"review_prs": ["docker#56"]},
    "issues#2": {"review_prs": ["abacus#1853"]},
    "issues#3": {"review_prs": ["store#3343"]},
    "issues#4": {"review_prs": ["store#3400"]},
    "issues#5": {"comment_links": {"pull_request_url": _pull("store", 3401)}},
    "issues#6": {"comment_links": {"pull_request_url": _pull("store", 3402)}},
    # neither: nothing names its pull request
    "store#3358": {},
}


def _judged(board, key: str, pulls: dict | None = None):
    repository, _, number = key.partition("#")
    card = board.card(f"{ORG}/{repository}", int(number))
    assert card is not None, key
    ref = WorkRef(card.repository, card.number)
    rows = {url.lower(): row for url, row in (pulls or {}).items()}
    work = build_work_card(
        ref, issue=None, title=key, facts=work_facts(ref, card=card, rows=rows)
    )
    return work, front_row(work, current=True)


@pytest.fixture
def board(tmp_path):
    return _trailhead(tmp_path, NUDGE, CARDS)


def test_the_board_names_every_pull_request_a_card_has(board):
    assert board.card(f"{ORG}/issues", 397).pull_requests == (_pull("store", 3295),)
    assert board.card(f"{ORG}/metso", 51).pull_requests == (_pull("metso", 52),)
    assert board.card(f"{ORG}/store", 3358).pull_requests == ()


@private_feature(_poller_nudge)
@pytest.mark.parametrize(
    ("key", "pulls", "label"),
    [
        (
            "issues#397",
            {_pull("store", 3295): _row()},
            "Not merged: reviewed, up to date with its base, and still not merged",
        ),
        ("metso#51", {_pull("metso", 52): _row()}, "Reviewed at 7957ce9; ready for a person"),
        ("issues#1", {}, f"The reviewer doesn't watch {ORG}/docker"),
        ("issues#2", {_pull("abacus", 1853): _row()}, "The reviewer stopped reporting back"),
        ("issues#3", {_pull("store", 3343): _row()}, "External review gave up"),
        (
            "issues#5",
            {_pull("store", 3401): _row(state="CLOSED")},
            "Its pull request was closed without merging",
        ),
        (
            "issues#6",
            {_pull("store", 3402): _row(merged=True)},
            f"Merged {ORG}/store#3402; the issue is still open",
        ),
    ],
)
def test_a_review_nobody_automatic_will_give_is_needs_you_with_its_reason(
    board, key, pulls, label
):
    """Section 1's table: each of these said "Review queued · reviewer to start" and sat in
    Active, though no reviewer would ever start."""
    work, row = _judged(board, key, pulls)

    assert work.review is not None and work.review.state == REVIEW_HUMAN, work.review
    assert work.review.label.startswith(label), work.review.label
    assert row.section == NEEDS_YOU and row.reason.startswith(label)


@private_feature(_poller_nudge)
def test_a_review_is_queued_only_when_the_reviewer_will_ask(board):
    work, row = _judged(board, "issues#4", {_pull("store", 3400): _row()})
    assert work.review.state == REVIEW_PENDING and not work.review.needs_human
    assert row.section == IN_PROGRESS and row.reason == "Next: reviewer to start"


@private_feature(_poller_nudge)
def test_an_entry_about_an_older_head_says_nothing_about_the_current_one(board):
    # The extension forgets an entry once the head moves; until it does, the deck's own
    # cached head says the stalled note is about a commit that is no longer the PR's.
    work, _ = _judged(board, "issues#397", {_pull("store", 3295): _row(head=MOVED)})
    assert work.review.state == REVIEW_PENDING


@private_feature(_poller_nudge)
def test_a_card_is_judged_by_all_of_its_pull_requests(tmp_path):
    board = _trailhead(
        tmp_path,
        {**NUDGE, "awaiting": {f"{ORG}/abacus#9": {
            "sha": HEAD, "repository": "eerovil/review-chats", "chat": 2054, "answer": HEAD,
        }}},
        {"issues#7": {"comment_links": {"pull_request_url": _pull("store", 3295)},
                      "review_prs": ["abacus#9"]}},
    )
    work, row = _judged(board, "issues#7", {_pull("store", 3295): _row()})
    # The second pull request's review is running: that is progress, not a person's job.
    assert work.review.state == REVIEW_RUNNING
    assert work.review.label == "External review running in eerovil/review-chats#2054"
    assert row.section == IN_PROGRESS


def _outputs(board, backlog, repo: str, number: int, **fields):
    """The Work's outputs as its facts find them, from the board and the cached open list."""
    ref = WorkRef(f"{ORG}/{repo}", number)
    open_pulls = backlog.pulls_for_work().get(f"{ref.repository}#{number}".lower(), ())
    card = board.card(ref.repository, number)
    return work_facts(ref, card=card, open_pulls=open_pulls, **fields).outputs


def test_a_card_with_no_link_takes_the_open_pull_request_that_closes_it(board):
    backlog = SimpleNamespace(
        pulls_for_work=lambda: {
            f"{ORG}/store#3358".lower(): ({"url": _pull("store", 3499), "number": 3499},),
            f"{ORG}/issues#397".lower(): ({"url": _pull("store", 3500), "number": 3500},),
        },
    )
    (found,) = _outputs(board, backlog, "store", 3358)
    assert found.url == _pull("store", 3499) and found.found_by == FOUND_BY_BRANCH
    # One already naming its pull request keeps exactly that.
    assert [item.url for item in _outputs(board, backlog, "issues", 397)] == [_pull("store", 3295)]
    # The card itself is never rewritten to carry what was found.
    assert board.card(f"{ORG}/store", 3358).pull_requests == ()


def test_an_issues_card_finds_the_pull_request_in_another_repository_that_closes_it(tmp_path):
    """#2227 review: issues#453's pull request is store#3366, whose branch
    ``agent/issue-453-…`` names an issue number only. GitHub's closing reference is what links
    them; the branch alone would have pointed at store#453, a different issue."""
    from agentdeck.issue_context import IssueBacklog, RepositoryPulls, _pull

    def gh_row(number, branch, closes=()):
        return {
            "number": number, "title": f"PR {number}", "url": _pull_url(number, branch),
            "headRefName": branch, "baseRefName": "master", "isDraft": False,
            "closingIssuesReferences": [
                {"number": n, "repository": {"name": repo, "owner": {"login": ORG}}}
                for repo, n in closes
            ],
        }

    def _pull_url(number, branch):
        repo = "abacus" if number == 1970 else "store"
        return f"https://github.com/{ORG}/{repo}/pull/{number}"

    store = [gh_row(3366, "agent/issue-453-returns-guidance", [("issues", 453)]),
             gh_row(3370, "agent/issue-3358")]
    abacus = [gh_row(1970, "agent/issue-449", [("Issues", 449)])]
    backlog = IssueBacklog(pulls=(
        RepositoryPulls(f"{ORG}/store", tuple(_pull(f"{ORG}/store", row) for row in store)),
        RepositoryPulls(f"{ORG}/abacus", tuple(_pull(f"{ORG}/abacus", row) for row in abacus)),
    ))
    board = _trailhead(tmp_path, NUDGE, {"issues#453": {}, "issues#449": {}, "store#3358": {},
                                       "issues#3358": {}})

    def pulls(repo, number):
        return tuple(item.url for item in _outputs(board, backlog, repo, number))

    assert pulls("issues", 453) == (f"https://github.com/{ORG}/store/pull/3366",)
    assert _outputs(board, backlog, "issues", 453)[0].found_by == FOUND_BY_CLOSING
    # The closing reference is compared case-insensitively, as GitHub names are.
    assert pulls("issues", 449) == (f"https://github.com/{ORG}/abacus/pull/1970",)
    # A branch still links a pull request to an issue in its own repository …
    assert pulls("store", 3358) == (f"https://github.com/{ORG}/store/pull/3370",)
    # … and never to the same number in another one.
    assert pulls("issues", 3358) == ()
    # A pull request that closes issues#453 is not store#453's, whatever its branch says, and
    # a card-less store#453 Work gets nothing from it either (#2231 review).
    assert f"{ORG}/store#453".lower() not in backlog.pulls_for_work()
    stale = {f"{ORG}/store#453".lower(): backlog.pulls_for_work()[f"{ORG}/issues#453".lower()]}
    assert work_facts(WorkRef(f"{ORG}/store", 453), open_pulls=stale[f"{ORG}/store#453".lower()]
                      ).outputs == ()
    # The branch-only index its other readers use is unchanged.
    assert f"{ORG}/issues#453".lower() not in backlog.pulls_by_issue()


# --- a closed issue leaves Needs you and Active at once ----------------------------------------


def _card(**fields) -> BoardCard:
    fields.setdefault("status", "Needs More Info")
    fields.setdefault("status_role", "needs_more_info")
    fields.setdefault("ownership", True)
    return BoardCard("eerovil/agentdeck", 2052, **fields)


CLOSED = {"state": "closed", "closedAt": "2026-09-25T13:20:36Z", "title": "Closed one"}


def test_an_issue_closed_after_the_engine_last_looked_finishes_the_work():
    before = _card(last_live_check="2026-09-25T13:15:00+00:00")
    after = _card(last_live_check="2026-09-25T13:25:00+00:00")

    finished = work_finished_at(before, CLOSED)
    assert finished == datetime(2026, 9, 25, 13, 20, 36, tzinfo=UTC)
    # The engine looked after the close and still holds the card: the card is the truth.
    assert work_finished_at(after, CLOSED) is None

    ref = WorkRef("eerovil/agentdeck", 2052)
    work = build_work_card(
        ref, issue=None, title="t", facts=work_facts(ref, card=before, record=CLOSED)
    )
    row = front_row(work, current=True)
    assert work.work_state == "done" and row.section == FINISHED


# --- finished Works: counted right, and saying "Merged" ----------------------------------------


def test_a_finished_row_says_which_pull_request_merged_and_the_work_page_agrees():
    card = BoardCard(
        "eerovil/agentdeck", 2224, status="Done", done=True,
        pull_request_url="https://github.com/eerovil/agentdeck/pull/2225",
    )
    ref = WorkRef("eerovil/agentdeck", 2224)
    rows = {"https://github.com/eerovil/agentdeck/pull/2225": _row(merged=True)}
    work = build_work_card(
        ref, issue=None, title="t", facts=work_facts(ref, card=card, record=CLOSED, rows=rows)
    )

    assert front_row(work, current=False).reason == "Merged eerovil/agentdeck#2225"
    story = work_status(work)
    assert story.detail == "Merged eerovil/agentdeck#2225."
    assert [step.note for step in story.milestones if step.key == "merge"] == ["Merged"]


def test_a_closed_issue_whose_close_time_is_unknown_is_finished_never_quiet():
    closed = build_work_card(
        WorkRef("eerovil/agentdeck", 1), issue=None, title="t", issue_state="closed"
    )
    quiet = build_work_card(WorkRef("eerovil/agentdeck", 2), issue=None, title="t")
    rows = [front_row(closed, current=False), front_row(quiet, current=False)]

    assert [row.section for row in rows] == [FINISHED, QUIET]
    sections = front_sections(rows, now=NOW)
    assert sections.finished == () and sections.finished_more == 1 and sections.quiet == 1


def _done(number: int, *, closed: datetime, moved: datetime | None):
    from dataclasses import replace

    card = build_work_card(
        WorkRef("eerovil/agentdeck", number), issue=None, title="t", issue_state="closed"
    )
    return front_row(replace(card, finished_at=closed, last_activity=moved), current=False)


def test_a_chat_carried_on_in_a_done_work_dates_and_sorts_its_finished_row():
    """#2357: a chat moving well after the close puts the Work at the top of Recently
    finished, brings one closed long ago back from quiet, and the worker's own wrap-up just
    after the close changes nothing."""
    continued = _done(1, closed=NOW - timedelta(days=21), moved=NOW - timedelta(minutes=3))
    wrap_up = _done(2, closed=NOW - timedelta(hours=1), moved=NOW - timedelta(minutes=50))
    newer = _done(3, closed=NOW - timedelta(minutes=30), moved=NOW - timedelta(days=2))
    stale = _done(4, closed=NOW - timedelta(days=21), moved=NOW - timedelta(days=8))

    assert continued.continued == NOW - timedelta(minutes=3)
    assert wrap_up.continued is None and newer.continued is None
    assert stale.continued == NOW - timedelta(days=8)  # continued, but not recently
    sections = front_sections([stale, newer, wrap_up, continued], now=NOW)
    assert [row.card.work_id for row in sections.finished] == [
        row.card.work_id for row in (continued, newer, wrap_up)
    ]
    assert sections.quiet == 1
    # Nothing new: the same order on every pass.
    again = front_sections([continued, wrap_up, stale, newer], now=NOW)
    assert again.finished == sections.finished


def test_only_the_records_that_cannot_say_are_backfilled():
    from agentdeck.web.work_view import unresolved_issues

    def item(number, card=None):
        return SimpleNamespace(ref=WorkRef("eerovil/agentdeck", number), card=card)

    items = [item(1), item(2), item(3), item(4), item(5, _card()), item(6)]
    records = {
        items[0].ref.work_id: {"state": "closed"},  # cached before closedAt
        items[1].ref.work_id: CLOSED,
        items[2].ref.work_id: {"state": "open"},
    }
    backlog = SimpleNamespace(issue=lambda repo, number: object() if number == 6 else None)

    assert unresolved_issues(items, records, backlog) == (
        ("eerovil/agentdeck", 1),
        ("eerovil/agentdeck", 4),  # never resolved, and nothing vouches for it being open
    )


@pytest.mark.asyncio
async def test_the_backfill_is_bounded_and_rotates_past_what_it_already_tried():
    from agentdeck.web.work_view import WorkOverview, WorkOverviewService

    calls: list[list] = []

    async def refresh(refs, *, limit):
        calls.append(list(refs))
        return 0

    app = SimpleNamespace(state=SimpleNamespace(issues=SimpleNamespace(
        refresh_issue_records=refresh)))
    service = WorkOverviewService(app)
    refs = tuple(("eerovil/agentdeck", n) for n in range(10))
    service.overview = WorkOverview(unresolved=refs)

    await service.backfill(0.0)
    await service.backfill(30.0)  # within the minute: nothing
    await service.backfill(61.0)
    await service.backfill(122.0)
    await service.backfill(183.0)  # every one tried once: nothing until the hour is up
    await service.backfill(3700.0)

    assert calls == [list(refs[0:4]), list(refs[4:8]), list(refs[8:10]), list(refs[0:4])]
    # The unresolved list is not part of what the page shows.
    assert WorkOverview(unresolved=refs) == WorkOverview()


# --- the title is the GitHub issue title ---------------------------------------------------------


def test_the_title_is_the_issues_own_record_before_a_list_or_a_chat():
    ref = WorkRef("eerovil/agentdeck", 1940)
    chat = WorkChat("c", "k", "agentdeck#1940 · Duplicate of #1941 chore", "/c", False, None,
                    ("issue_url",))
    record = {"title": "Agentdeck can be used for chore in this case", "state": "open"}
    listed = SimpleNamespace(issue=lambda *_: SimpleNamespace(title="Older title", state="open"))
    empty = SimpleNamespace(issue=lambda *_: None)

    assert work_title(ref, backlog=listed, chats=(chat,), record=record) == (
        "Agentdeck can be used for chore in this case", "open")
    # The open list is gone (an issues webhook), or the issue closed: the record still names it.
    assert work_title(ref, backlog=empty, chats=(chat,), record=record)[0] == record["title"]
    assert work_title(ref, backlog=empty, chats=(chat,), record={**record, "state": "closed"}) == (
        record["title"], "closed")
    assert work_title(ref, backlog=listed, chats=(chat,)) == ("Older title", "open")
    assert work_title(ref, backlog=empty, chats=(chat,))[0] == chat.title
    # A chat no longer loaded is titled by its id: that is no title at all (#3257).
    unloaded = WorkChat("0bb0b893998d45cfb120e18bce5105a6", "k", "0bb0b893998d45cfb120e18bce5105a6",
                        "/c", False, None, ("issue_url",))
    assert work_title(ref, backlog=empty, chats=(unloaded,))[0] == ""
    assert work_title(ref, backlog=empty, chats=(unloaded, chat))[0] == chat.title


# --- every card has an age ------------------------------------------------------------------------


def test_a_card_with_no_chat_or_run_still_has_an_age():
    ref = WorkRef("eerovil/agentdeck", 234)

    def age(**card_fields) -> datetime | None:
        card = _card(**card_fields) if card_fields else None
        return build_work_card(
            ref, issue=None, title="t", facts=work_facts(ref, card=card),
            issue_updated_at="2026-09-01T00:00:00Z",
        ).last_activity

    assert age(started_at="2026-08-31T09:45:45+00:00", dispatch_failed_at="2026-09-02T00:00:00Z") \
        == datetime(2026, 9, 2, tzinfo=UTC)
    assert age(started_at="2026-08-31T09:45:45+00:00") == datetime(
        2026, 8, 31, 9, 45, 45, tzinfo=UTC)
    assert age() == datetime(2026, 9, 1, tzinfo=UTC)


# --- a failed dispatch is not "needs clarification" ---------------------------------------------


def test_a_card_whose_worker_never_started_says_so_not_needs_clarification():
    card = _card(started=True, dispatch_failed=True, started_at="2026-08-31T09:45:45+00:00")
    ref = WorkRef("Northwind/issues", 234)
    work = build_work_card(ref, issue=None, title="t", facts=work_facts(ref, card=card))
    story = work_status(work)

    assert story.kind == DISPATCH_FAILED
    assert story.summary == "The worker never started; press Retry to start it again."
    assert "clarification" not in (story.detail + story.summary).lower()
    row = front_row(work, current=True)
    assert row.section == NEEDS_YOU
    assert row.reason == "The worker never started; press Retry to start it again"
    assert isinstance(row, FrontRow) and work.last_activity is not None


# --- the reviewer's closing note is read by the commit it names ----------------------------------


def _core():
    spec = importlib.util.spec_from_file_location(
        "chatgpt_nudge_truth_under_test", EXTENSIONS / "chatgpt_nudge_core.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@private_feature(_poller_nudge)
@pytest.mark.parametrize(
    "note",
    [
        # review-chats#1898
        f"Reviewed Northwind/store#3343 at `{HEAD[:12]}…`: clean. Clean-status "
        "comment posted on Northwind/issues#434.",
        # review-chats#2068
        f"Reviewed Northwind/metso#52 at `{HEAD[:7]}`: clean; the repository profile "
        "does not permit this reviewer to merge.",
        # review-chats#2071
        f"The exact head `{HEAD[:10]}…` was already handled cleanly by an earlier chat.",
    ],
)
def test_the_reviewers_own_closing_note_is_a_review_of_the_head_it_names(note):
    core = _core()
    assert core._review_verdict(note, (HEAD,)) == (True, "")
    assert core._says_something(note, (HEAD,))
    # Without the head it is about, the same words prove nothing.
    assert core._review_verdict(note)[0] is False
    # And a note naming another commit does not settle this one.
    assert core._review_verdict(note, (MOVED,))[0] is False


@private_feature(_poller_nudge)
def test_a_note_naming_the_head_still_refuses_a_run_that_did_nothing():
    core = _core()
    note = f"Reviewed nothing at `{HEAD[:7]}`: the run stopped before taking any action."
    assert core._review_verdict(note, (HEAD,)) == (False, "the run said it took no action")


# --- #2228: the regressions the live front page still showed after #2227 ----------------------


def _registered(number: int) -> str:
    return f"https://github.com/eerovil/agentdeck/pull/{number}"


# ``pr-ref:v1:eerovil/agentdeck:2221`` as the webhook write-through left it on 2026-09-25.
MERGED_2221 = {
    "baseRefName": "master", "headRefName": "agent/issue-2207",
    "headRefOid": "722184ea0000000000000000000000000000beef", "isDraft": False,
    "mergedAt": "2026-09-25T13:18:58Z", "number": 2221, "state": "closed",
    "title": "fix(work): publish the backfill rows", "updatedAt": "2026-09-25T13:18:58Z",
    "url": _registered(2221),
}


def test_a_finished_row_whose_card_is_gone_says_merged_from_its_chats_pull_request():
    """agentdeck#2207 after its card left the board: the Work page listed #2221 under its chat,
    merged, and the Finished row said nothing (#2228)."""
    other = dict(MERGED_2221, headRefName="agent/issue-2206", number=2220, url=_registered(2220))
    opened = dict(MERGED_2221, headRefName="agent/issue-2207", number=2222, url=_registered(2222),
                  mergedAt=None, state="open")
    rows = {row["url"].lower(): row for row in (MERGED_2221, other, opened)}
    ref = WorkRef("eerovil/agentdeck", 2207)
    # A chat that serves another Work too: only the branch in this repository speaks.
    shared = tuple(
        Registration(_registered(number), exclusive=False) for number in (2221, 2220, 2222)
    )

    facts = work_facts(ref, record=CLOSED, rows=rows, registered=shared)
    # The chat also merged another Work's pull request: that is not this Work's merge. Its own
    # #2222 still open after the issue closed stays an output, so the Work shows it (#3187).
    assert [(item.number, item.state) for item in facts.outputs] == [
        (2221, "merged"), (2222, "open")]
    assert facts.outputs[0].found_by == FOUND_BY_CHAT
    facts = work_facts(ref, record=CLOSED, rows=rows, registered=shared[:2])
    work = build_work_card(ref, issue=None, title="t", facts=facts)
    assert front_row(work, current=False).reason == "Merged eerovil/agentdeck#2221"
    assert work_status(work).detail == "Merged eerovil/agentdeck#2221."
    # A pull request in another repository never counts by branch alone.
    assert work_facts(WorkRef("eerovil/other", 2207), rows=rows, registered=shared).outputs == ()


def test_the_open_pull_request_list_is_never_served_in_an_older_shape(tmp_path):
    """issues#453 → store#3366: for ten minutes after #2227 the resolver served store's list as
    cached before ``closingIssuesReferences`` was asked for, so the join found nothing."""
    from agentdeck.github_cache import GitHubMetadata, GitHubMetadataCache
    from agentdeck.issue_context import IssueResolver

    cache = GitHubMetadataCache(tmp_path / "cache.sqlite3")
    resolver = IssueResolver(poller_root=tmp_path, metadata_cache=cache)
    row = {
        "number": 3366, "title": "Returns guidance", "isDraft": False,
        "url": f"https://github.com/{ORG}/store/pull/3366",
        "headRefName": "agent/issue-453-returns-guidance", "baseRefName": "master",
    }
    # The row the old key held: no closing references at all.
    cache.offer(f"pull-list:v1:{ORG.lower()}/store", GitHubMetadata([row]))
    assert resolver._cached_pulls(f"{ORG}/store").pulls == ()

    closing = {"number": 453, "repository": {"name": "issues", "owner": {"login": ORG}}}
    cache.offer(
        resolver._pull_cache_key(f"{ORG}/store"),
        GitHubMetadata([dict(row, closingIssuesReferences=[closing])]),
    )
    (pull,) = resolver._cached_pulls(f"{ORG}/store").pulls
    assert pull["closes"] == (f"{ORG}/issues#453".lower(),)


# --- #2229: one Work, one reading, on every surface ----------------------------------------------


# The live shape #2228 found for store#3343: the extension had settled the head as given up
# under the old rule, while the reviewer's own note said "Reviewed … at `7957ce96`: clean".
REVIEWED_AFTER_GIVE_UP = {**NUDGE, "seen": {**NUDGE["seen"], f"{ORG}/store#3343": f"{HEAD}@2"}}


@private_feature(_poller_nudge)
def test_a_clean_review_of_the_exact_head_outranks_an_old_give_up(tmp_path):
    board = _trailhead(tmp_path, REVIEWED_AFTER_GIVE_UP, CARDS)

    work, row = _judged(board, "issues#3", {_pull("store", 3343): _row()})
    assert work.review.label == "Reviewed at 7957ce9; ready for a person to decide"
    assert row.section == NEEDS_YOU and "gave up" not in row.reason
    # It is read, not asked for: the reviewer is not queued for another round.
    assert work.review.state == REVIEW_HUMAN and work.review.next_expected != "reviewer to start"


@private_feature(_poller_nudge)
def test_a_give_up_stands_when_the_head_is_not_known_or_the_review_is_of_another_head(tmp_path):
    board = _trailhead(tmp_path, {**REVIEWED_AFTER_GIVE_UP, "seen": {
        f"{ORG}/store#3343": f"{MOVED}@1"}}, CARDS)
    other_head, _ = _judged(board, "issues#3", {_pull("store", 3343): _row()})
    assert other_head.review.label.startswith("External review gave up")

    board = _trailhead(tmp_path / "unknown", REVIEWED_AFTER_GIVE_UP, CARDS)
    unknown, _ = _judged(board, "issues#3", {})
    # No cached head: nothing can be exact, so the extension's own order stands.
    assert unknown.review.label.startswith("External review gave up")


@private_feature(_poller_nudge)
def test_a_stall_the_reviewer_recorded_still_says_why(tmp_path):
    # A stall is the reviewer's own reason about a head it reviewed: it keeps its words.
    nudge = {**NUDGE, "seen": {f"{ORG}/store#3295": f"{HEAD}@1"}}
    board = _trailhead(tmp_path, nudge, CARDS)
    work, _ = _judged(board, "issues#397", {_pull("store", 3295): _row()})
    assert work.review.label.startswith("Not merged")


def _both_pages(app, ref):
    """The same Work read by the front page and by its own page."""
    from agentdeck.web.work_view import load_work_detail, load_work_overview
    from agentdeck.work_workspace import parse_work_id

    ref = parse_work_id(ref.work_id)  # the page's own spelling: the route redirects to it

    async def read():
        overview = await load_work_overview(app, now=datetime.now(UTC))
        front = next(card for card in overview.cards if card.work_id == ref.work_id)
        detail = await load_work_detail(app, ref, workspace=False)
        return front, detail.card

    return asyncio.run(read())


def _agree(front, page):
    assert front.outputs == page.outputs
    assert front.review == page.review
    assert front.work_state == page.work_state and front.finished_at == page.finished_at
    front_line, page_line = front_row(front, current=True), front_row(page, current=True)
    assert (front_line.section, front_line.reason) == (page_line.section, page_line.reason)
    assert work_status(front).next == work_status(page).next


def _trailhead_app(tmp_path, board, *, rows, backlog=None):
    from test_work_ui import _app

    app = _app(tmp_path, board=board)
    app.state.issues.cached = lambda: backlog or SimpleNamespace(
        pulls_for_work=dict, issue=lambda *_: None
    )
    app.state.issues.cached_pulls = lambda refs: {
        ref: rows[f"https://github.com/{ref[0]}/pull/{ref[1]}".lower()]
        for ref in refs
        if f"https://github.com/{ref[0]}/pull/{ref[1]}".lower() in rows
    }
    return app


def _store_list(*rows):
    from agentdeck.issue_context import IssueBacklog, RepositoryPulls, _pull

    return IssueBacklog(pulls=(
        RepositoryPulls(f"{ORG}/store", tuple(_pull(f"{ORG}/store", row) for row in rows)),
    ))


STORE_3366 = f"https://github.com/{ORG}/store/pull/3366"


@private_feature(_poller_nudge)
def test_a_cross_repository_pull_request_reads_the_same_on_both_pages(tmp_path):
    """issues#453 → store#3366 (#2228): the card names no pull request, GitHub's closing
    reference links them across repositories, and store#3366 was reviewed clean at its head
    after the extension had given it up. Both pages must say the same, once."""
    nudge = {**NUDGE, "seen": {f"{ORG}/store#3366": f"{HEAD}@1"}, "review_misses": {
        f"{ORG}/store#3366": {"count": 3, "settled": "given_up", "sha": HEAD,
                              "why": "the closing note did not say the review ran"}}}
    board = _trailhead(tmp_path, nudge, {"issues#453": {"ownership": True}})
    backlog = _store_list({
        "number": 3366, "title": "Returns guidance", "url": STORE_3366,
        "headRefName": "agent/issue-453-returns-guidance", "baseRefName": "master",
        "isDraft": False,
        "closingIssuesReferences": [
            {"number": 453, "repository": {"name": "issues", "owner": {"login": ORG}}}],
    })
    app = _trailhead_app(tmp_path / "deck", board, rows={STORE_3366.lower(): _row()},
                       backlog=backlog)

    front, page = _both_pages(app, WorkRef(f"{ORG}/issues", 453))

    _agree(front, page)
    assert [(item.label, item.found_by) for item in front.outputs] == [
        (f"{ORG}/store#3366", FOUND_BY_CLOSING)]
    assert front.review.label == "Reviewed at 7957ce9; ready for a person to decide"
    assert front_row(front, current=True).section == NEEDS_YOU
    # The decision is made on the pull request the facts found, not on the card's empty link.
    assert front.review.pull_request == STORE_3366
    assert work_status(page).where == f"PR {ORG}/store#3366"


def test_a_work_finished_by_a_merged_pull_request_reads_merged_on_both_pages(tmp_path):
    """agentdeck#2207 after its card left the board (#2228): the only link to #2221 is the
    chat that registered it, and both pages must call the Work merged from that one fact."""
    from test_work_ui import _add, _closed

    from agentdeck.models import PullRequestRef

    ref = WorkRef("eerovil/agentdeck", 2207)
    app = _trailhead_app(tmp_path, None, rows={_registered(2221).lower(): MERGED_2221})
    _closed(app, {2207: datetime.now(UTC) - timedelta(hours=1)})
    chat = _add(app, "claude_code:test:worker", ref)
    app.state.app_state.set_pull_requests(chat, (PullRequestRef("eerovil/agentdeck", 2221),))

    front, page = _both_pages(app, ref)

    _agree(front, page)
    assert front.work_state == "done"
    assert front_row(front, current=False).reason == "Merged eerovil/agentdeck#2221"
    assert work_status(page).detail == "Merged eerovil/agentdeck#2221."


def test_a_merged_pull_request_in_another_repository_finishes_its_issue_on_both_pages(tmp_path):
    """issues#453 once store#3366 merged: the card is gone and the open list no longer has the
    pull request. The worker chat that registered it is in this Work alone, so its branch
    names this issue whichever repository the pull request lives in."""
    from test_work_ui import _add, _closed

    from agentdeck.models import PullRequestRef

    ref = WorkRef(f"{ORG}/issues", 453)
    merged = {"state": "MERGED", "mergedAt": "2026-09-25T14:00:00Z", "headRefOid": HEAD,
              "headRefName": "agent/issue-453-returns-guidance", "url": STORE_3366}
    app = _trailhead_app(tmp_path, None, rows={STORE_3366.lower(): merged})
    _closed(app, {453: datetime.now(UTC) - timedelta(hours=1)})
    chat = _add(app, "claude_code:test:worker", ref)
    app.state.app_state.set_pull_requests(chat, (PullRequestRef(f"{ORG}/store", 3366),))

    front, page = _both_pages(app, ref)

    _agree(front, page)
    assert front_row(front, current=False).reason == f"Merged {ORG}/store#3366"

    # The same chat serving another Work as well cannot say which one store#3366 was for.
    _add(app, chat, ref, WorkRef(f"{ORG}/issues", 999))
    front, page = _both_pages(app, ref)
    _agree(front, page)
    assert front.outputs == ()


# agentdeck#2272 on 2026-09-27, as its worker chat registered them: #2284 said only "Part of
# #2272", and the two review-chats pull requests carry branches that name no issue.
PR_2284 = "https://github.com/eerovil/agentdeck/pull/2284"
CHAT_2197 = "https://github.com/eerovil/review-chats/pull/2197"
CHAT_2198 = "https://github.com/eerovil/review-chats/pull/2198"


def _rows_2272(*, merged: bool) -> dict:
    agentdeck = dict(MERGED_2221, headRefName="agent/issue-2272", number=2284, url=PR_2284)
    if not merged:
        agentdeck.update(mergedAt=None, state="OPEN")
    verdict = dict(MERGED_2221, headRefName="agentdeck-2272-reviewed-verdict", number=2197,
                   url=CHAT_2197, mergedAt=None, state="OPEN", baseRefName="main")
    cutover = dict(verdict, headRefName="agentdeck-2272-profile-cutover", number=2198,
                   url=CHAT_2198, isDraft=True, baseRefName="agentdeck-2272-reviewed-verdict")
    return {row["url"].lower(): row for row in (agentdeck, verdict, cutover)}


def test_a_card_that_names_no_pull_request_takes_its_worker_chats_open_ones():
    """agentdeck#2272 (#2286): in review, its card naming no pull request and the open list not
    yet re-read since #2284 opened, the Work said "no pull request is linked" while its own
    worker chat had registered three."""
    ref = WorkRef("eerovil/agentdeck", 2272)
    card = BoardCard("eerovil/agentdeck", 2272, status="In review", status_role="in_review")
    alone = tuple(Registration(url, exclusive=True) for url in (PR_2284, CHAT_2198, CHAT_2197))

    facts = work_facts(ref, card=card, rows=_rows_2272(merged=False), registered=alone)

    assert [(item.label, item.state, item.found_by) for item in facts.outputs] == [
        ("eerovil/agentdeck#2284", "open", FOUND_BY_CHAT),
        ("eerovil/review-chats#2198", "open", FOUND_BY_CHAT),
        ("eerovil/review-chats#2197", "open", FOUND_BY_CHAT),
    ]
    assert summarize_review(card, outputs=facts.outputs).cause == CAUSE_HUMAN_REVIEW
    assert summarize_review(card, outputs=()).cause == CAUSE_NO_OUTPUT  # what it said before

    # Once #2284 merged, the review-chats pull requests still hold the Work open: it is not
    # "Merged, issue still open".
    facts = work_facts(ref, card=card, rows=_rows_2272(merged=True), registered=alone)
    assert [(item.number, item.state) for item in facts.outputs] == [
        (2284, "merged"), (2198, "open"), (2197, "open")]

    # A chat in another Work too cannot say which one a branch naming no issue was for; the
    # branch naming this issue in this repository still speaks.
    shared = tuple(Registration(item.url, exclusive=False) for item in alone)
    facts = work_facts(ref, card=card, rows=_rows_2272(merged=False), registered=shared)
    assert [item.url for item in facts.outputs] == [PR_2284]


def test_the_open_list_and_the_chats_registrations_speak_together():
    """#2284 in the open list by its branch does not silence the review-chats pull requests
    the same worker chat registered (#2286)."""
    ref = WorkRef("eerovil/agentdeck", 2272)
    listed = ({"url": PR_2284, "closes": ()},)
    alone = tuple(Registration(url, exclusive=True) for url in (PR_2284, CHAT_2197))

    facts = work_facts(ref, rows=_rows_2272(merged=False), open_pulls=listed, registered=alone)

    assert [(item.url, item.found_by) for item in facts.outputs] == [
        (PR_2284, FOUND_BY_BRANCH), (CHAT_2197, FOUND_BY_CHAT)]
