"""A Work whose auto-merge waits on a required check that never ran needs a person (#2275).

The live shape is Northwind/store#3369: `/merge` armed auto-merge, `master` requires
``continuous-integration/drone/pr``, only ``continuous-integration/drone/push`` ever reported,
and the front page listed the Work under Working now for the fifty minutes until the Poller
gave up on the merge.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentdeck import merge_checks
from agentdeck.webhook_status_cache import WebhookStatusCacheService
from agentdeck.work import WorkRef
from agentdeck.work_attention import RELEASE_BLOCKED, operator_attention
from agentdeck.work_facts import work_facts
from agentdeck.work_overview import build_work_card
from agentdeck.work_story import IN_PROGRESS, NEEDS_YOU, front_row, work_status

ORG = "eerovil"
WORK = WorkRef(f"{ORG}/agentdeck", 5)
PR = f"https://github.com/{ORG}/agentdeck/pull/7"
HEAD = "abc1234def5678abc1234def5678abc1234def56"


def _row(state: str = "open", *, head: str = HEAD, **extra) -> dict:
    """The cached pull request row ``git_context`` and the webhook write-through keep."""
    row = {"state": state, "headRefOid": head, "baseRefName": "master", **extra}
    if state == "merged":
        row.update(state="closed", mergedAt="2026-09-25T10:00:00+00:00")
    return row

ENABLED = datetime(2026, 9, 26, 19, 19, 33, tzinfo=UTC)
REQUIRED = "continuous-integration/drone/pr"
RAN = "continuous-integration/drone/push"


def _answer(
    *,
    head: str = HEAD,
    auto: bool = True,
    reported=(RAN,),
    required=(REQUIRED,),
    more: bool = False,
    rollup: bool = True,
    state: str = "OPEN",
    mergeable: str | None = None,
    merge_state: str | None = None,
) -> dict:
    """The GraphQL answer, in the shape store#3369 returned it."""
    contexts = {
        "pageInfo": {"hasNextPage": more},
        "nodes": [
            {"__typename": "StatusContext", "context": name} for name in reported
        ],
    }
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "state": state,
                    "headRefOid": head,
                    "mergeable": mergeable,
                    "mergeStateStatus": merge_state,
                    "autoMergeRequest": {"enabledAt": "2026-09-26T19:19:33Z"} if auto else None,
                    "baseRef": {
                        "refUpdateRule": {"requiredStatusCheckContexts": []},
                        "branchProtectionRule": {"requiredStatusCheckContexts": list(required)},
                    },
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "oid": head,
                                    "committedDate": "2026-09-26T19:02:00Z",
                                    "statusCheckRollup": {"contexts": contexts} if rollup else None,
                                }
                            }
                        ]
                    },
                }
            }
        }
    }


def _checked(after: timedelta = timedelta(minutes=30), **answer) -> dict:
    row = merge_checks.parse(_answer(**answer), checked_at=ENABLED + after)
    assert row is not None
    return row


ARMED = {"autoMergeRequest": {"enabledAt": "2026-09-26T19:19:33Z"}}


# --- the read ----------------------------------------------------------------------------------


def test_a_required_check_with_no_report_is_missing_and_one_that_ran_is_not():
    row = _checked()
    assert row["missing"] == [REQUIRED] and row["head"] == HEAD
    assert _checked(reported=(RAN, REQUIRED))["missing"] == []
    # A check run counts as a report just like a commit status.
    answer = _answer()
    nodes = answer["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]
    run = {"__typename": "CheckRun", "name": REQUIRED}
    nodes["statusCheckRollup"]["contexts"]["nodes"].append(run)
    assert merge_checks.parse(answer, checked_at=ENABLED)["missing"] == []


def test_a_cut_off_report_list_never_calls_a_check_missing():
    assert _checked(more=True)["missing"] == []


def test_a_commit_nothing_reported_on_misses_every_required_check():
    assert _checked(rollup=False, required=(REQUIRED, "lint"))["missing"] == [REQUIRED, "lint"]


def test_an_unreadable_answer_is_no_row():
    for value in (None, {}, {"data": {"repository": None}}, {"errors": [{"message": "x"}]}):
        assert merge_checks.parse(value, checked_at=ENABLED) is None


# --- the decision ------------------------------------------------------------------------------


def test_only_past_the_grace_does_a_missing_check_count_as_never_run():
    row = _row(**ARMED)
    just = merge_checks.GRACE - timedelta(seconds=1)
    assert merge_checks.never_ran(row, _checked(after=just)) == ()
    assert merge_checks.never_ran(row, _checked(after=merge_checks.GRACE)) == (REQUIRED,)


def test_a_new_head_a_disarmed_row_or_a_settled_pull_request_is_not_blocked():
    checks = _checked()
    assert merge_checks.never_ran(_row(**ARMED), checks) == (REQUIRED,)
    # A push restarts CI: the read was about the old head.
    assert merge_checks.never_ran(_row(head="f" * 40, **ARMED), checks) == ()
    # The webhook said auto-merge was turned off after the read.
    assert merge_checks.never_ran(_row(autoMergeRequest=None), checks) == ()
    assert merge_checks.never_ran(_row(), checks) == ()
    assert merge_checks.never_ran(_row(**ARMED), _checked(state="MERGED")) == ()
    assert merge_checks.never_ran(_row(**ARMED), None) == ()


def test_auto_merge_turned_off_and_on_again_on_the_same_head_gets_its_own_grace():
    # The read was made thirty minutes into the first arming; auto-merge was then turned off
    # and on again, and the webhook rewrote the row before the read's cache entry aged out.
    checks = _checked()
    rearmed = {"autoMergeRequest": {"enabledAt": "2026-09-26T19:49:00Z"}}
    assert merge_checks.never_ran(_row(**rearmed), checks) == ()
    # The same arming spelled the other way round still matches.
    same = {"autoMergeRequest": {"enabledAt": "2026-09-26T19:19:33+00:00"}}
    assert merge_checks.never_ran(_row(**same), checks) == (REQUIRED,)
    # A webhook row that carries no time falls back to the read's own.
    assert merge_checks.never_ran(_row(autoMergeRequest={"enabledAt": None}), checks) == (
        REQUIRED,
    )


# --- the Work ----------------------------------------------------------------------------------


def _work(tmp_path, *, row: dict, checks: dict | None):
    facts = work_facts(
        WORK,
        rows={PR.lower(): row},
        open_pulls=[{"url": PR, "closes": [WORK.source_key]}],
        checks={PR.lower(): checks} if checks is not None else None,
    )
    return build_work_card(WORK, issue=None, title="Work", facts=facts)


def test_the_store_3368_shape_leaves_working_now_for_release_blocked(tmp_path):
    work = _work(tmp_path, row=_row(**ARMED), checks=_checked())
    story = work_status(work)

    assert story.headline == "Merge blocked"
    assert REQUIRED in story.summary and "never ran" in story.summary
    row = front_row(work, current=True)
    assert row.section == NEEDS_YOU
    attention = operator_attention(work, story)
    assert attention.category == RELEASE_BLOCKED
    assert attention.actions == ("review", "inspect", "hide")
    assert attention.review_url == PR


def test_an_armed_work_whose_checks_all_reported_stays_in_progress(tmp_path):
    for checks in (None, _checked(reported=(RAN, REQUIRED)), _checked(after=timedelta(0))):
        work = _work(tmp_path, row=_row(**ARMED), checks=checks)
        assert front_row(work, current=True).section == IN_PROGRESS
        assert work.outputs[0].auto_merge


# --- keeping it current ------------------------------------------------------------------------


def test_the_pull_request_row_carries_auto_merge_from_both_writers():
    from agentdeck.git_context import _PR_FIELDS

    assert {"autoMergeRequest", "body"} <= set(_PR_FIELDS.split(","))
    pull = {
        "number": 3369, "title": "t", "html_url": PR, "state": "open",
        "updated_at": "2026-09-26T19:19:34Z", "head": {"ref": "b", "sha": HEAD},
        "base": {"ref": "master"},
        "auto_merge": {"enabled_at": "2026-09-26T19:19:33Z", "merge_method": "merge"},
    }
    value = WebhookStatusCacheService._pull_value(pull)
    assert value["autoMergeRequest"] == {"enabledAt": "2026-09-26T19:19:33Z"}
    assert WebhookStatusCacheService._pull_value({**pull, "auto_merge": None})[
        "autoMergeRequest"
    ] is None
    bare = {key: item for key, item in pull.items() if key != "auto_merge"}
    assert "autoMergeRequest" not in WebhookStatusCacheService._pull_value(bare)


def test_only_armed_open_outputs_are_checked(tmp_path):
    from agentdeck.web.work_view import merge_watch_pulls

    armed = _work(tmp_path, row=_row(**ARMED), checks=None)
    plain = _work(tmp_path, row=_row(), checks=None)
    merged = _work(tmp_path, row=_row("merged", **ARMED), checks=None)

    assert merge_watch_pulls([armed, plain, merged, armed]) == ((f"{ORG}/agentdeck", 7),)


@pytest.mark.asyncio
async def test_the_service_reads_armed_checks_at_most_once_a_minute():
    from agentdeck.web.work_view import WorkOverview, WorkOverviewService

    calls: list[list] = []

    async def refresh(refs, *, limit):
        calls.append(list(refs))
        return len(refs)

    issues = SimpleNamespace(refresh_merge_checks=refresh)
    app = SimpleNamespace(state=SimpleNamespace(issues=issues))
    service = WorkOverviewService(app)
    service.overview = WorkOverview(merge_watch=((f"{ORG}/agentdeck", 7),))

    assert await service.check_merges(0.0) == 1
    assert await service.check_merges(30.0) == 0
    assert await service.check_merges(61.0) == 1
    assert calls == [[(f"{ORG}/agentdeck", 7)]] * 2
    service.overview = WorkOverview()
    assert await service.check_merges(200.0) == 0


# --- GitHub's merge answer, for exactly the head the deck holds (#2332) ----------------------


def test_the_row_carries_githubs_merge_answer_and_is_read_only_for_its_own_head():
    row = merge_checks.parse(
        _answer(mergeable="CONFLICTING", merge_state="dirty"), checked_at=ENABLED
    )

    assert (row["mergeable"], row["mergeState"]) == ("CONFLICTING", "DIRTY")
    assert merge_checks.merge_answer(row, HEAD) == ("CONFLICTING", "DIRTY")
    assert merge_checks.merge_answer(row, "0" * 40) == ("", ""), "an answer about another head"
    assert merge_checks.merge_answer(None, HEAD) == ("", "")
    assert merge_checks.parse(_answer(), checked_at=ENABLED)["mergeable"] == ""
    # And when that head was committed (#3199), likewise only for its own head.
    assert merge_checks.head_committed_at(row, HEAD) == datetime(2026, 9, 26, 19, 2, tzinfo=UTC)
    assert merge_checks.head_committed_at(row, "0" * 40) is None
    assert merge_checks.head_committed_at({**row, "headAt": None}, HEAD) is None


async def test_an_answer_with_one_refused_field_is_still_read(tmp_path, monkeypatch):
    """#3199: an App installation is refused ``branchProtectionRule``; gh exits 1 but GitHub
    answered the rest, and dropping it left every App-read pull request with no row."""
    from agentdeck.git_context import GitContextResolver
    from agentdeck.github_cache import GitHubMetadataCache

    answer = _answer()
    answer["data"]["repository"]["pullRequest"]["baseRef"]["branchProtectionRule"] = None
    answer["errors"] = [{"type": "FORBIDDEN", "path": ["repository", "pullRequest", "baseRef",
                                                       "branchProtectionRule"]}]
    replies = [(1, json.dumps(answer)), (1, "")]
    resolver = GitContextResolver(metadata_cache=GitHubMetadataCache(tmp_path / "c.sqlite3"))
    resolver._gh = "gh"

    async def fake_run(*args, owner=None):
        return replies.pop(0)

    monkeypatch.setattr(resolver, "_run", fake_run)
    row = await resolver._merge_checks_for("Acme/App", 7)
    assert row is not None and row["head"] == HEAD
    assert merge_checks.head_committed_at(row, HEAD) == datetime(2026, 9, 26, 19, 2, tzinfo=UTC)
    # A failure with no answer at all is still no row.
    assert await resolver._merge_checks_for("Acme/App", 8) is None


def test_a_row_cached_before_it_carried_the_merge_answer_is_never_found():
    assert merge_checks.cache_key("Acme/App", 7) == "pr-merge-checks:v2:acme/app:7"


def test_a_merge_candidate_is_watched_for_its_merge_answer(tmp_path):
    from dataclasses import replace

    from agentdeck.web.work_view import merge_watch_pulls

    plain = _work(tmp_path, row=_row(), checks=None)

    assert merge_watch_pulls([plain]) == ()
    # A Work the deck runs itself reads Ready for merge from its own release, with no
    # Poller reviewer behind its outputs (#3199): its pull requests are candidates too.
    from agentdeck.work_derived import READY, DerivedStatus

    ready = replace(plain, derived=DerivedStatus(READY, "Ready for merge"))
    assert merge_watch_pulls([ready]) == ((f"{ORG}/agentdeck", 7),)


# --- a conflict found after Ready for merge (#4217) -------------------------------------------


def _release(awaiting: dict[str, dict]):
    """The release as the Work page reads it: each Work's ``awaiting_merge`` and its looks."""
    looked: list[str] = []

    async def evaluate(work_id):
        looked.append(work_id)
        return ""

    store = SimpleNamespace(get=lambda work_id: {"awaiting_merge": awaiting.get(work_id, {})})
    return SimpleNamespace(store=store, evaluate=evaluate), looked


@pytest.mark.asyncio
async def test_a_ready_work_that_starts_conflicting_is_not_ready_and_its_release_looks(tmp_path):
    """#4217: robot#30 conflicted after something else landed on its base; the Work page kept
    it Ready for merge and nothing told its agent."""
    from agentdeck.web.work_view import WorkOverview, WorkOverviewService, _ready_for_merge

    clean = _work(tmp_path, row=_row(), checks=_checked(mergeable="MERGEABLE"))
    dirty = _work(
        tmp_path, row=_row(), checks=_checked(mergeable="CONFLICTING", merge_state="DIRTY")
    )
    older = _work(  # a conflict read about an older head says nothing about this one
        tmp_path, row=_row(), checks=_checked(head="f" * 40, mergeable="CONFLICTING")
    )
    release, looked = _release({WORK.work_id: {f"{ORG}/agentdeck#7": HEAD}})
    app = SimpleNamespace(state=SimpleNamespace(work_release=release))

    assert _ready_for_merge(app, WORK.work_id, clean.outputs) is clean.outputs[0]
    assert _ready_for_merge(app, WORK.work_id, older.outputs) is older.outputs[0]
    assert _ready_for_merge(app, WORK.work_id, dirty.outputs) is None

    service = WorkOverviewService(app)
    service.overview = WorkOverview(cards=(clean,))
    assert await service.release_conflicts() == 0
    service.overview = WorkOverview(cards=(dirty,))
    works = release.evaluate

    async def restarting(work_id):
        raise OSError("the merge service is restarting")

    release.evaluate = restarting
    assert await service.release_conflicts() == 0, "a failed ask is asked again"
    release.evaluate = works
    assert await service.release_conflicts() == 1
    assert await service.release_conflicts() == 0, "once per Work and heads"
    assert looked == [WORK.work_id]
    # Not held Ready by the release: nothing to pass on.
    release.store.get = lambda work_id: {}
    service = WorkOverviewService(app)
    service.overview = WorkOverview(cards=(dirty,))
    assert await service.release_conflicts() == 0


@pytest.mark.asyncio
async def test_merge_answers_are_read_oldest_first_past_the_limit(tmp_path, monkeypatch):
    """#4217: with more candidates than ``limit``, the same first few took every place, fresh
    or not, and a pull request past them never had its conflict read."""
    from agentdeck import issue_context
    from agentdeck.github_cache import GitHubMetadata, GitHubMetadataCache

    cache = GitHubMetadataCache(tmp_path / "c.sqlite3")
    refs = [(f"{ORG}/store", n) for n in (1, 2, 3)]

    async def fetch():
        return GitHubMetadata({"head": HEAD}, terminal=False)

    for ref in refs[:2]:
        await cache.resolve(merge_checks.cache_key(*ref), fetch)
    asked: list = []

    class Resolver:
        def __init__(self, **kwargs):
            pass

        async def refresh_merge_checks(self, wanted, *, limit):
            asked.extend(list(wanted)[:limit])
            return 0

    monkeypatch.setattr(issue_context, "GitContextResolver", Resolver)
    context = issue_context.IssueResolver.__new__(issue_context.IssueResolver)
    context._metadata = cache
    await context.refresh_merge_checks(refs, limit=1)
    assert asked == [refs[2]], "the one never read goes first"
