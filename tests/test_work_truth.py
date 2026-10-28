"""Every Work card tells the truth (#2226).

A card claims something only when the deck has evidence for it. These cases are built from
the real state shapes the 2026-09-25 audit found: pull requests that name their issue through
GitHub's closing reference, through their branch, or only through the chat that registered
them; and issue records cached before ``closedAt``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentdeck.work import WorkRef
from agentdeck.work_facts import (
    FOUND_BY_BRANCH,
    FOUND_BY_CHAT,
    FOUND_BY_CLOSING,
    Registration,
    work_facts,
    work_finished_at,
)
from agentdeck.work_overview import WorkChat, build_work_card, work_title
from agentdeck.work_story import (
    FINISHED,
    QUIET,
    front_row,
    front_sections,
    work_status,
)

NOW = datetime(2026, 9, 25, 13, 25, tzinfo=UTC)
ORG = "Northwind"
HEAD = "7957ce96b80f6085ddfa24f281b96fcf0dbc3180"


def _pull(repo: str, number: int) -> str:
    return f"https://github.com/{ORG}/{repo}/pull/{number}"


def _row(state: str = "OPEN", head: str = HEAD, merged: bool = False) -> dict:
    """A cached pull request row in ``git_context._PR_FIELDS``' shape."""
    return {
        "state": "MERGED" if merged else state,
        "mergedAt": "2026-09-25T12:00:00Z" if merged else None,
        "headRefOid": head,
    }


# --- which pull requests are the Work's ------------------------------------------------------


def _outputs(backlog, repo: str, number: int, **fields):
    """The Work's outputs as its facts find them, from the cached open list."""
    ref = WorkRef(f"{ORG}/{repo}", number)
    open_pulls = backlog.pulls_for_work().get(f"{ref.repository}#{number}".lower(), ())
    return work_facts(ref, open_pulls=open_pulls, **fields).outputs


def test_a_work_takes_the_open_pull_request_that_closes_it():
    backlog = SimpleNamespace(
        pulls_for_work=lambda: {
            f"{ORG}/store#3358".lower(): ({"url": _pull("store", 3499), "number": 3499},),
        },
    )
    (found,) = _outputs(backlog, "store", 3358)
    assert found.url == _pull("store", 3499) and found.found_by == FOUND_BY_BRANCH
    assert _outputs(backlog, "store", 3359) == ()


def test_an_issues_work_finds_the_pull_request_in_another_repository_that_closes_it():
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
    def pulls(repo, number):
        return tuple(item.url for item in _outputs(backlog, repo, number))

    assert pulls("issues", 453) == (f"https://github.com/{ORG}/store/pull/3366",)
    assert _outputs(backlog, "issues", 453)[0].found_by == FOUND_BY_CLOSING
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


CLOSED = {"state": "closed", "closedAt": "2026-09-25T13:20:36Z", "title": "Closed one"}


def test_a_closed_issue_finishes_the_work():
    assert work_finished_at(CLOSED) == datetime(2026, 9, 25, 13, 20, 36, tzinfo=UTC)
    assert work_finished_at({**CLOSED, "is_pr": True}) is None
    assert work_finished_at({"state": "open"}) is None

    ref = WorkRef("eerovil/agentdeck", 2052)
    work = build_work_card(ref, issue=None, title="t", facts=work_facts(ref, record=CLOSED))
    row = front_row(work, current=True)
    assert work.work_state == "done" and row.section == FINISHED


# --- finished Works: counted right, and saying "Merged" ----------------------------------------


def test_a_finished_row_says_merged_and_the_work_page_names_the_pull_request():
    ref = WorkRef("eerovil/agentdeck", 2224)
    url = "https://github.com/eerovil/agentdeck/pull/2225"
    rows = {url: _row(merged=True)}
    facts = work_facts(ref, record=CLOSED, rows=rows, registered=(Registration(url, True),))
    work = build_work_card(ref, issue=None, title="t", facts=facts)

    assert front_row(work, current=False).reason == "Merged"
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

    def item(number):
        return SimpleNamespace(ref=WorkRef("eerovil/agentdeck", number))

    items = [item(1), item(2), item(3), item(4), item(6)]
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


def test_a_card_with_no_chat_still_has_an_age():
    ref = WorkRef("eerovil/agentdeck", 234)
    work = build_work_card(ref, issue=None, title="t", issue_updated_at="2026-09-01T00:00:00Z")

    assert work.last_activity == datetime(2026, 9, 1, tzinfo=UTC)
    assert build_work_card(ref, issue=None, title="t").last_activity is None


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
    assert front_row(work, current=False).reason == "Merged"
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
    assert front.work_state == page.work_state and front.finished_at == page.finished_at
    front_line, page_line = front_row(front, current=True), front_row(page, current=True)
    assert (front_line.section, front_line.reason) == (page_line.section, page_line.reason)
    assert work_status(front).next == work_status(page).next


def _trailhead_app(tmp_path, *, rows, backlog=None):
    from test_work_ui import _app

    app = _app(tmp_path)
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


def test_a_cross_repository_pull_request_reads_the_same_on_both_pages(tmp_path):
    """issues#453 → store#3366 (#2228): GitHub's closing reference links them across
    repositories. Both pages must say the same, once."""
    from test_work_ui import _add

    backlog = _store_list({
        "number": 3366, "title": "Returns guidance", "url": STORE_3366,
        "headRefName": "agent/issue-453-returns-guidance", "baseRefName": "master",
        "isDraft": False,
        "closingIssuesReferences": [
            {"number": 453, "repository": {"name": "issues", "owner": {"login": ORG}}}],
    })
    app = _trailhead_app(tmp_path / "deck", rows={STORE_3366.lower(): _row()}, backlog=backlog)
    ref = WorkRef(f"{ORG}/issues", 453)
    _add(app, "claude_code:test:worker", ref)

    front, page = _both_pages(app, ref)

    _agree(front, page)
    assert [(item.label, item.found_by) for item in front.outputs] == [
        (f"{ORG}/store#3366", FOUND_BY_CLOSING)]


def test_a_work_finished_by_a_merged_pull_request_reads_merged_on_both_pages(tmp_path):
    """agentdeck#2207 after its card left the board (#2228): the only link to #2221 is the
    chat that registered it, and both pages must call the Work merged from that one fact."""
    from test_work_ui import _add, _closed

    from agentdeck.models import PullRequestRef

    ref = WorkRef("eerovil/agentdeck", 2207)
    app = _trailhead_app(tmp_path, rows={_registered(2221).lower(): MERGED_2221})
    _closed(app, {2207: datetime.now(UTC) - timedelta(hours=1)})
    chat = _add(app, "claude_code:test:worker", ref)
    app.state.app_state.set_pull_requests(chat, (PullRequestRef("eerovil/agentdeck", 2221),))

    front, page = _both_pages(app, ref)

    _agree(front, page)
    assert front.work_state == "done"
    assert front_row(front, current=False).reason == "Merged"
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
    app = _trailhead_app(tmp_path, rows={STORE_3366.lower(): merged})
    _closed(app, {453: datetime.now(UTC) - timedelta(hours=1)})
    chat = _add(app, "claude_code:test:worker", ref)
    app.state.app_state.set_pull_requests(chat, (PullRequestRef(f"{ORG}/store", 3366),))

    front, page = _both_pages(app, ref)

    _agree(front, page)
    assert front_row(front, current=False).reason == "Merged"

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


def test_a_work_takes_its_worker_chats_open_pull_requests():
    """agentdeck#2272 (#2286): the open list not yet re-read since #2284 opened, the Work said
    "no pull request is linked" while its own worker chat had registered three."""
    ref = WorkRef("eerovil/agentdeck", 2272)
    alone = tuple(Registration(url, exclusive=True) for url in (PR_2284, CHAT_2198, CHAT_2197))

    facts = work_facts(ref, rows=_rows_2272(merged=False), registered=alone)

    assert [(item.label, item.state, item.found_by) for item in facts.outputs] == [
        ("eerovil/agentdeck#2284", "open", FOUND_BY_CHAT),
        ("eerovil/review-chats#2198", "open", FOUND_BY_CHAT),
        ("eerovil/review-chats#2197", "open", FOUND_BY_CHAT),
    ]

    # Once #2284 merged, the review-chats pull requests still hold the Work open.
    facts = work_facts(ref, rows=_rows_2272(merged=True), registered=alone)
    assert [(item.number, item.state) for item in facts.outputs] == [
        (2284, "merged"), (2198, "open"), (2197, "open")]

    # A chat in another Work too cannot say which one a branch naming no issue was for; the
    # branch naming this issue in this repository still speaks.
    shared = tuple(Registration(item.url, exclusive=False) for item in alone)
    facts = work_facts(ref, rows=_rows_2272(merged=False), registered=shared)
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
