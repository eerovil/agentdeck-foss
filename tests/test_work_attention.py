"""The operator's category for a Work, and its pull requests' size (#2236).

Every case is a Work shape the front page can put in Needs you, and the one operator category
and action list it gets, or no category at all: a chat's own wait, a Work the deck runs itself
(its derived status), an issue that closed and a merge that waits on a check that never ran.
"""

from __future__ import annotations

import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentdeck import work_actions
from agentdeck.webhook_status_cache import WebhookStatusCacheService
from agentdeck.work import WorkRef
from agentdeck.work_attention import (
    ISSUE_CLOSED,
    LABELS,
    NEEDS_ANSWER,
    NEEDS_RETRY,
    READY_FOR_MERGE,
    RELEASE_BLOCKED,
    WORK_ERROR,
    operator_attention,
)
from agentdeck.work_derived import DerivedStatus
from agentdeck.work_facts import (
    DiffStat,
    OutputFact,
    Registration,
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
    {
        "path": "src/agentdeck/web/templates/b.html",
        "additions": 10,
        "deletions": 0,
        "changeType": "ADDED",
    },
    {"path": "tests/test_a.py", "additions": 50, "deletions": 5},
    {"path": ".claude/codebase/a.md", "additions": 4, "deletions": 1},
)


def _work(*, rows=None, attention=(), verifications=(), derived=None, record=None):
    """A Work whose one pull request its chat registered, read as the deck reads it."""
    rows = {PR.lower(): _row()} if rows is None else rows
    facts = work_facts(WORK, record=record, rows=rows, registered=(Registration(PR, True),))
    work = build_work_card(
        WORK,
        issue=None,
        title="Work",
        facts=facts,
        attention=attention,
        verifications=verifications,
    )
    return replace(work, derived=derived) if derived is not None else work


def _derived(kind: str, summary: str = "", **fields) -> DerivedStatus:
    return DerivedStatus(kind, summary, summary=summary, **fields)


def _ask(interaction: str = "", *, asks: bool = True) -> WorkAttention:
    return WorkAttention(
        "claude_code:main:worker",
        "Worker chat",
        "waiting",
        "/chats/w",
        asks=asks,
        question="Which branch?",
        interaction=interaction,
    )


STOPPED_CHAT = WorkAttention("claude_code:main:m", "Manual", "stalled", "/chats/m")
REVIEW_ACTIONS = ("agent_review", "chatgpt_review", "review")

# (id, work kwargs, category, actions). ``hide`` ends every list and is added below.
CASES = [
    # Needs answer: a question in the chat's text, a form, an approval, a browser step.
    ("text-question", {"attention": (_ask(),)}, NEEDS_ANSWER, ("answer",)),
    ("question-form", {"attention": (_ask("mcp_form"),)}, NEEDS_ANSWER, ("open",)),
    ("approval", {"attention": (_ask("permission", asks=False),)}, NEEDS_ANSWER, ("open",)),
    ("browser-step", {"attention": (_ask("mcp_url", asks=False),)}, NEEDS_ANSWER, ("open",)),
    ("chat-stopped", {"attention": (STOPPED_CHAT,)}, NEEDS_RETRY, ("inspect",)),
    # A Work the deck runs itself: its derived status alone (#2944).
    (
        "derived-question",
        {"derived": _derived("question", "Asked: which branch?")},
        NEEDS_ANSWER,
        ("open",),
    ),
    (
        "derived-ready",
        {"derived": _derived("ready", "Reviewed and tested")},
        READY_FOR_MERGE,
        ("merge", "open"),
    ),
    (
        "derived-held",
        {"derived": _derived("pr_held", "Held for a person")},
        RELEASE_BLOCKED,
        ("open", "inspect"),
    ),
    (
        "derived-review-stuck",
        {"derived": _derived("review_stuck", "No reviewer runs")},
        NEEDS_RETRY,
        ("open", *REVIEW_ACTIONS),
    ),
]


@pytest.mark.parametrize(
    ("extra", "category", "actions"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_every_needs_you_shape_gets_exactly_one_operator_category(extra, category, actions):
    work = _work(**extra)
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


def test_a_closed_issue_waits_for_its_finish():
    """The issue closed by itself, and only a person's Finish lets the Work go (#2510)."""
    work = replace(
        _work(rows={PR.lower(): _row("merged")}), work_state="done", awaiting_finish=True
    )

    found = operator_attention(work, work_status(work))

    assert (found.category, found.reason, found.actions) == (
        ISSUE_CLOSED,
        "Merged",
        ("finish", "answer", "hide"),
    )


def test_a_merge_waiting_on_a_check_that_never_ran_is_blocked():
    """Auto-merge waits on a required check that never reported (#2275)."""
    work = _work()
    work = replace(work, outputs=(replace(work.outputs[0], never_ran=("ci / tests",)),))

    found = operator_attention(work, work_status(work))

    assert (found.category, found.actions) == (RELEASE_BLOCKED, ("review", "inspect", "hide"))
    assert work_status(work).summary == "Waits for ci / tests, which never ran."


def test_a_work_with_nothing_waiting_on_a_person_gets_no_category():
    quota = WorkAttention(
        "claude_code:main:side",
        "Side chat",
        "waiting",
        "/chats/side",
        recovering=True,
        cause="turn_failed",
    )
    for work in (_work(), _work(attention=(quota,))):
        assert operator_attention(work, work_status(work)) is None
        assert front_row(work, current=True).section != NEEDS_YOU


@pytest.mark.parametrize(
    ("attention", "bug"),
    [
        # A chat whose turn died is a bug; one that only went quiet is not.
        (
            WorkAttention(
                "claude_code:main:m", "Manual", "waiting", "/chats/m", cause="died_mid_turn"
            ),
            "chat_stopped",
        ),
        (
            WorkAttention(
                "claude_code:main:m", "Manual", "waiting", "/chats/m", cause="reported_state"
            ),
            "",
        ),
        # #3354: an expired sign-in is the account's, not AgentDeck's.
        (
            WorkAttention(
                "claude_code:alt:m", "Manual", "waiting", "/chats/m", cause="login_expired"
            ),
            "",
        ),
    ],
    ids=["chat-died", "chat-quiet", "login-expired"],
)
def test_a_dead_chat_is_a_bug_only_when_agentdeck_broke(attention, bug):
    work = _work(attention=(attention,))
    found = operator_attention(work, work_status(work))

    assert (found.category, found.bug) == (NEEDS_RETRY, bug)


def test_a_shape_nothing_explains_is_a_work_error():
    work = _work()
    story = replace(work_status(work), tone="attention", summary="Something odd.")

    found = operator_attention(work, story)

    assert (found.category, found.reason, found.actions) == (
        WORK_ERROR,
        "Something odd",
        ("inspect", "hide"),
    )


def test_with_no_reviewer_only_a_persons_review_is_offered(monkeypatch):
    """The public copy has no reviewer (#3623): its two review buttons would only refuse."""
    from agentdeck import chat_review, review_refs

    monkeypatch.setattr(chat_review, "PUBLIC_STUB", False, raising=False)  # the real module
    assert review_refs.reviewer_available()
    monkeypatch.setattr(chat_review, "PUBLIC_STUB", True, raising=False)  # the stand-in's mark
    assert not review_refs.reviewer_available()
    work = _work(derived=_derived("review_stuck", "No reviewer runs"))

    found = operator_attention(work, work_status(work))

    assert found is not None and found.actions == ("open", "review", "hide")


SIDE_CHAT = ("Side chat", "/chats/side")


def _mid_turn(work, *chats):
    """``work`` while the given chats are mid-turn (#2481)."""
    return replace(work, working_count=len(chats), working_chats=tuple(chats))


def test_a_stopped_work_is_working_while_a_turn_on_it_runs():
    """#2487: a turn on the Work proves it has not stopped, so it reads Working, says what it
    will say when the turn ends (#2590), and asks nobody anything until then."""
    work = _work(rows={}, attention=(STOPPED_CHAT,))
    idle = work_status(work)
    assert idle.headline == "Chat needs a look"

    busy = _mid_turn(work, SIDE_CHAT)
    story = work_status(busy)
    steps = {step.key: (step.state, step.note) for step in story.milestones}

    assert (story.tone, story.headline, story.kind) == ("active", "Working", "")
    assert (story.summary, story.where, story.where_href) == ("Side chat is working.", *SIDE_CHAT)
    assert story.detail.startswith(f"When it finishes: {idle.headline}: ")
    assert steps["implement"] == ("active", "Working")
    assert "attention" not in {step.state for step in story.milestones}
    assert operator_attention(busy, story) is None
    assert front_row(busy, current=True).section != NEEDS_YOU


@pytest.mark.parametrize(
    ("chats", "summary"),
    [
        ((SIDE_CHAT,), "Side chat is working."),
        ((SIDE_CHAT, ("Worker", "/chats/w")), "Side chat and 1 more chat are working."),
    ],
    ids=["chat", "two-chats"],
)
def test_a_working_work_names_who_is_working(chats, summary):
    story = work_status(_mid_turn(_work(), *chats))

    assert (story.headline, story.summary) == ("Working", summary)


@pytest.mark.parametrize("interaction", ["", "permission"])
def test_a_chat_waiting_on_a_person_waits_while_another_runs(interaction):
    """#2590 (reversing #2572's exception): a question or an approval in one chat while
    another chat on the Work is mid-turn is Working too, with the question in the detail.
    It asks for a person again when the turn ends."""
    work = _work(rows={}, attention=(_ask(interaction, asks=not interaction),))
    busy = _mid_turn(work, SIDE_CHAT)
    story, idle = work_status(busy), work_status(work)

    assert idle.kind in {"chat_question", "chat_action"}
    assert front_row(work, current=True).section == NEEDS_YOU
    assert (story.tone, story.headline, story.kind) == ("active", "Working", "")
    assert story.detail.startswith(f"When it finishes: {idle.headline}: ")
    assert operator_attention(busy, story) is None
    assert front_row(busy, current=True).section != NEEDS_YOU


@pytest.mark.parametrize("state", ["running", "queued"])
def test_a_calm_reading_is_working_too_while_a_chat_runs(state):
    """#2590: no exceptions: a verification in progress is said in the detail as well."""
    work = _work(verifications=(RunVerification("job-1", "run-1", state, False, ()),))
    assert work_status(work).headline == "Verifying"

    story = work_status(_mid_turn(work, SIDE_CHAT))
    assert (story.headline, story.next) == ("Working", "")
    assert story.detail.startswith("When it finishes: Verifying: ")


def test_a_merge_is_refused_while_a_chat_on_the_work_is_working():
    """#2481: the turn may still push, so a Merge pressed meanwhile is refused."""
    busy = _mid_turn(_work(), SIDE_CHAT)
    with pytest.raises(work_actions.Refused, match="not offered"):
        work_actions.plan(work_actions.MERGE, busy, operator_attention(busy, work_status(busy)))


def test_needs_you_is_grouped_by_category_in_the_front_pages_order():
    from agentdeck.work_story import front_sections

    ready = _work(derived=_derived("ready", "Reviewed and tested"))
    answer = _work(attention=(_ask(),))
    retry = _work(attention=(STOPPED_CHAT,))
    closed = replace(
        _work(rows={PR.lower(): _row("merged")}), work_state="done", awaiting_finish=True
    )

    works = (closed, answer, retry, ready)
    front = front_sections([front_row(work, current=True) for work in works])

    # Most needing the owner first (#3818), each at its place in the page's flow.
    assert [group.category for group in front.groups] == [
        NEEDS_ANSWER,
        READY_FOR_MERGE,
        NEEDS_RETRY,
        ISSUE_CLOSED,
    ]
    assert [group.glyph for group in front.groups] == ["?", "↑", "↻", "☐"]
    assert [
        [group.category for group in front.groups_in(place)]
        for place in ("start", "release", "stuck", "cleanup")
    ] == [[NEEDS_ANSWER], [READY_FOR_MERGE], [NEEDS_RETRY], [ISSUE_CLOSED]]
    assert dict((key, count) for key, count, _ in front.summary) == {
        "merge": 1,
        "stuck": 1,
        "answer": 1,
        "cleanup": 1,
    }


def test_a_closed_issue_is_not_listed_as_recently_stopped():
    """Issue closed only waits for Finish: Needs you lists it, Recently stopped does not (#3388)."""
    from datetime import UTC, datetime

    from agentdeck.work_story import front_sections

    at = datetime(2026, 10, 22, 12, tzinfo=UTC)
    rows = [
        replace(front_row(_work(attention=(_ask(),)), current=True), stopped_at=at)
        for _ in range(2)
    ]
    closed = replace(
        rows[1],
        card=replace(rows[1].card, work_id="github_issue:owner/repo#2"),
        attention=replace(rows[1].attention, category=ISSUE_CLOSED),
    )

    front = front_sections([rows[0], closed])

    assert closed in front.needs_you and front.stopped == (rows[0],)


def test_each_needs_you_category_lists_its_most_recent_work_first():
    """Recency wins inside a category, over which part of the Work waits (#2267)."""
    from datetime import UTC, datetime, timedelta

    from agentdeck.work_story import front_sections

    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    stopped = _work(attention=(STOPPED_CHAT,))
    answer = _work(attention=(_ask(),))

    def at(work, number, hours):
        when = None if hours is None else now - timedelta(hours=hours)
        return replace(work, work_id=f"github_issue:owner/repo#{number}", last_activity=when)

    works = (
        at(stopped, 1, 30),
        at(stopped, 2, None),
        at(stopped, 3, 1),
        at(stopped, 4, 5),
        at(answer, 5, 48),
        at(answer, 6, 2),
    )
    front = front_sections([front_row(work, current=True) for work in works])

    assert [
        (group.category, [row.card.work_id.rpartition("#")[2] for row in group.rows])
        for group in front.groups
    ] == [(NEEDS_ANSWER, ["6", "5"]), (NEEDS_RETRY, ["3", "4", "1", "2"])]


# --- the pull requests' size -----------------------------------------------------------------


def _output(
    n: int, state: str = "open", *, base: str = "master", size=(10, 2, 1), new=(0, 0)
) -> OutputFact:
    additions, deletions, files = size
    return OutputFact(
        f"https://github.com/{ORG}/r{n}/pull/{n}",
        f"{ORG}/r{n}",
        n,
        "chat_registration",
        state,
        base=base,
        additions=additions,
        deletions=deletions,
        files=files,
        new_lines=new[0],
        new_files=new[1],
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
    work = _work(rows=rows)

    assert work.diffstat == DiffStat(40, 3, 2, 10, 1) and work.targets == ("release",)
    asked = replace(work, attention=(_ask(),))
    assert operator_attention(asked, work_status(asked)).target == "release"
    unsized = _work(rows={PR.lower(): _row(sized=False)})
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
    work = _work(rows=rows, derived=_derived("ready", "Reviewed and tested"))
    overview = WorkOverview(front=front_sections([front_row(work, current=True)]))

    assert work.diffstat == DiffStat(0, 0, 0, 0, 0)
    html = render_work_front(_templates(tmp_path), overview, "release")
    assert '<span class="fr-size" data-work-diffstat>no source change</span>' in html


def test_the_webhook_write_through_keeps_the_size_gh_pr_view_stores():
    from agentdeck.git_context import _PR_FIELDS

    assert {"additions", "deletions", "changedFiles", "files"} <= set(_PR_FIELDS.split(","))
    pull = {
        "number": 7,
        "title": "t",
        "html_url": PR,
        "state": "open",
        "updated_at": AT,
        "head": {"ref": "b", "sha": HEAD},
        "base": {"ref": "master"},
        "additions": 40,
        "deletions": 3,
        "changed_files": 2,
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
    for moved in (
        {"headRefOid": "b" * 40, "baseRefName": "master", **size},
        {"headRefOid": HEAD, "baseRefName": "release", **size},
        {"headRefOid": None, "baseRefName": "master", **size},
        # The base branch moved under the same name and head: the totals say so.
        {"headRefOid": HEAD, "baseRefName": "master", **size, "changedFiles": 5},
        {"headRefOid": HEAD, "baseRefName": "master", **size, "additions": 90},
        {"headRefOid": HEAD, "baseRefName": "master"},
    ):
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

    app = SimpleNamespace(
        state=SimpleNamespace(
            issues=SimpleNamespace(
                refresh_issue_records=issues, refresh_pull_rows=pulls, stale_pull_rows=stale
            )
        )
    )
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

    held = _work(rows={PR.lower(): _row(sized=False)})
    sized = _work()
    never = _work(rows={})
    # A list that disagrees with the file count is stale, and is read again; one gh cut at its
    # limit has no size either, but reading it again would only cut it short again.
    stale = _work(rows={PR.lower(): _row(changedFiles=5)})
    cut = {PR.lower(): _row(changedFiles=101, files=[dict(_FILES[0])] * 100)}
    long = _work(rows=cut)

    assert long.diffstat is None and stale.diffstat is None
    assert unsized_pulls([held, sized, never, held, long]) == ((f"{ORG}/agentdeck", 7, HEAD),)
    assert unsized_pulls([stale]) == ((f"{ORG}/agentdeck", 7, HEAD),)


def test_an_open_pull_request_row_ages_out_into_the_backfill(tmp_path):
    """A merge no webhook delivered left a finished Work's pull request open for good (#3217)."""
    from agentdeck.github_cache import GitHubMetadata, GitHubMetadataCache
    from agentdeck.issue_context import IssueResolver
    from agentdeck.web.work_view import open_pull_rows

    held = _work()
    merged = _work(rows={PR.lower(): _row(state="merged")})
    never = _work(rows={})
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

    rows = {PR.lower(): _row(baseRefName="release")}
    work = _work(rows=rows, derived=_derived("pr_held", "Held for a person"))
    overview = WorkOverview(front=front_sections([front_row(work, current=True)]))

    html = render_work_front(_templates(tmp_path), overview, "release")

    assert 'data-op-category="release_blocked"' in html
    assert 'data-op-actions="open inspect hide"' in html
    # The category heads its group; the row gives the reason (#2254).
    assert 'data-front-group="release_blocked"' in html and ">Release blocked<" in html
    assert '<span class="fr-reason">Held for a person</span>' in html
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


def test_a_ready_row_opens_the_work_and_master_is_not_named(tmp_path):
    from agentdeck.web.work_view import WorkOverview, render_work_front
    from agentdeck.work_story import front_sections

    work = _work(derived=_derived("ready", "Reviewed and tested"))
    overview = WorkOverview(front=front_sections([front_row(work, current=True)]))

    html = render_work_front(_templates(tmp_path), overview, "release")

    assert 'data-front-group="ready_for_merge"' in html and ">Ready for merge<" in html
    assert "data-front-action" not in html and f'href="{PR}"' not in html
    assert "data-work-diffstat" in html and "data-work-target" not in html
    assert "data-work-staging" not in html

    # Staging already runs its head (#3180): the row says so; the links are on the page.
    from agentdeck.staging_deploys import StagingCopy

    staged = replace(work, staging=StagingCopy("s1", (("Store", "https://s1.example/"),)))
    overview = WorkOverview(front=front_sections([front_row(staged, current=True)]))
    html = render_work_front(_templates(tmp_path), overview, "release")
    assert (
        '<span class="fr-staging" data-work-staging>ready to test</span> · <span class="fr-reason'
    ) in html
    assert "https://s1.example/" not in html


def test_the_work_page_shows_the_same_category_above_the_technical_story(tmp_path):
    from agentdeck.web.work_view import WorkDetail, render_work_sections

    work = _work(attention=(_ask(),))
    detail = WorkDetail(card=work, workspace=None, chats=(), verifications=())

    sections = dict(render_work_sections(_templates(tmp_path), detail))

    status = sections["status"]
    assert 'data-op-category="needs_answer"' in status
    assert 'data-op-actions="answer hide"' in status
    assert status.index("Needs answer") < status.index("wd-status-head")
    # The technical story is still there, word for word.
    assert detail.status.headline in status
    assert "+40/−3 · 2 files" in sections["result"]
    assert "data-wd-staging" not in sections["result"]

    from agentdeck.staging_deploys import StagingCopy

    links = (("Store", "https://s1.example/"), ("Admin", "https://admin-s1.example/"))
    staged = replace(work, staging=StagingCopy("s1", links))
    detail = WorkDetail(card=staged, workspace=None, chats=(), verifications=())
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
        detail = WorkDetail(
            card=replace(work, outputs=outputs),
            workspace=None,
            chats=(),
            verifications=(),
            resolution=resolution,
        )
        html = dict(render_work_sections(_templates(tmp_path), detail))["resolution"]
        assert ("written before the latest commit (<time" in html) is behind
        assert html.count('<p class="rt-note wd-resolution-when">') == 1


def test_facts_mark_follows_the_facts_and_nothing_else():
    """#2332: a stop is dated by whether this moved, so a rename or a clock must not move it."""
    from datetime import UTC, datetime

    from agentdeck.work_stops import facts_mark

    work = _work()
    pushed = _work(rows={PR.lower(): _row(head="f" * 40)})
    later = datetime(2026, 9, 28, tzinfo=UTC)

    assert facts_mark(work) == facts_mark(replace(work, last_activity=later, title="Renamed"))
    assert facts_mark(work) != facts_mark(pushed)
