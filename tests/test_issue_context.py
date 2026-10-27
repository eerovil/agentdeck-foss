"""The deck's issue read model and the attention/issue/card join (#821)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentdeck import poller_ops
from agentdeck.git_context import GitContext, PullRequestContext
from agentdeck.issue_context import (
    IssueResolver,
    issue_number_from_branch,
    issue_reference_from_url,
    join_pending,
)
from agentdeck.models import IssueRef, Session, SessionStatus


@pytest.fixture(autouse=True)
def _isolated_github_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(tmp_path / "github.sqlite3"))


def _install(root: Path, instance: str, *, manifest: str, state: dict | None = None) -> None:
    base = root / "instances" / instance
    base.mkdir(parents=True, exist_ok=True)
    (base / "poller.toml").write_text(manifest)
    if state is not None:
        (base / "state.json").write_text(
            json.dumps({"schema_version": 1, "data": state})
        )


_MANIFEST = """
contract = 1

[board]
org = "eerovil"
project_number = 3
ownership_label = "agent"

[[repos]]
id = "agentdeck"
holds_issues = true
holds_code = true
verification = "agentdeck_job"

[[repos]]
id = "notes"
holds_code = true
verification = "agentdeck_job"
"""


def _poller_root(tmp_path: Path, *, state: dict | None = None) -> Path:
    root = tmp_path / "poller"
    _install(root, "agentdeck", manifest=_MANIFEST, state=state or {"issues": {}})
    return root


def _resolver(root: Path, *, gh: str | None = "gh") -> IssueResolver:
    resolver = IssueResolver(poller_root=root)
    resolver._gh = gh
    return resolver


def _issue_row(number: int, *, title: str = "Something", labels=()) -> dict:
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/eerovil/agentdeck/issues/{number}",
        "state": "OPEN",
        "labels": [{"name": name} for name in labels],
        "assignees": [],
        "updatedAt": "2026-08-08T03:00:00Z",
    }


def _session(
    *,
    key: str = "claude_code:main:chat-1",
    issue_url: str | None = None,
    question: str | None = None,
    issues: tuple[IssueRef, ...] = (),
) -> Session:
    return Session(
        key=key,
        account_key="claude_code:main",
        session_id=key.rsplit(":", 1)[-1],
        status=SessionStatus.LIVE,
        cwd=Path("/work/agentdeck"),
        title="Working",
        issue_url=issue_url,
        issues=issues,
        question=question,
    )


# --- scope and board ----------------------------------------------------


def test_scope_is_the_repositories_the_manifest_marks_as_holding_issues(tmp_path):
    board = poller_ops.read_board(_poller_root(tmp_path))
    # `notes` holds code only, so it is not part of the issue query.
    assert board.issue_repositories == ("eerovil/agentdeck",)
    assert board.ownership_label("eerovil/agentdeck") == "agent"
    assert board.complete is True


def test_board_status_comes_from_the_engines_own_durable_state(tmp_path):
    root = _poller_root(
        tmp_path,
        state={
            "issues": {
                "agentdeck#821": {
                    "status": "In progress",
                    "engine_labeled": True,
                    "started": True,
                    "worktrees": ["/work/agentdeck/.worktrees/issue-821"],
                }
            }
        },
    )
    board = poller_ops.read_board(root)
    card = board.card("eerovil/agentdeck", 821)
    assert card is not None
    assert (card.status, card.ownership) == ("In progress", True)
    # The engine names a card `repo#number`; the deck holds the owner too, so a
    # same-named repository under another owner cannot collide with it.
    assert card.identity == "eerovil/agentdeck#821"


def test_board_status_roles_follow_the_manifests_display_names(tmp_path):
    root = tmp_path / "poller"
    manifest = _MANIFEST.replace(
        'ownership_label = "agent"',
        'ownership_label = "agent"\n\n[board.statuses]\nin_review = "Awaiting approval"',
    )
    _install(
        root,
        "agentdeck",
        manifest=manifest,
        state={
            "issues": {
                "agentdeck#906": {"status": "Awaiting approval"},
                "agentdeck#907": {"status": "In review"},
            }
        },
    )

    board = poller_ops.read_board(root)

    assert board.card("eerovil/agentdeck", 906).in_review is True
    assert board.card("eerovil/agentdeck", 907).in_review is False


def test_each_scope_keeps_its_own_cards_when_instances_watch_the_same_repository(tmp_path):
    root = tmp_path / "poller"
    _install(
        root,
        "first",
        manifest=_MANIFEST,
        state={"issues": {"agentdeck#985": {"status": "Todo"}}},
    )
    _install(
        root,
        "second",
        manifest=_MANIFEST,
        state={"issues": {"agentdeck#985": {"status": "In progress"}}},
    )

    board = poller_ops.read_board(root)

    scopes = {scope.instance: scope for scope in board.scopes}
    assert scopes["first"].cards[0].status == "Todo"
    assert scopes["second"].cards[0].status == "In progress"
    assert all(scope.complete for scope in scopes.values())


async def test_instance_board_paginates_the_project_inventory(tmp_path):
    resolver = _resolver(_poller_root(tmp_path, state={"issues": {}}))
    calls = []

    def response(number, *, has_next, cursor):
        return {
            "data": {
                "repositoryOwner": {
                    "projectV2": {
                        "items": {
                            "nodes": [
                                {
                                    "content": {
                                        "number": number,
                                        "title": f"Issue {number}",
                                        "url": f"https://github.com/eerovil/agentdeck/issues/{number}",
                                        "state": "OPEN",
                                        "repository": {
                                            "nameWithOwner": "eerovil/agentdeck"
                                        },
                                    },
                                    "fieldValueByName": {"name": "Todo"},
                                }
                            ],
                            "pageInfo": {
                                "hasNextPage": has_next,
                                "endCursor": cursor,
                            },
                        }
                    }
                }
            }
        }

    async def fake_run(*args, **_kwargs):
        calls.append(args)
        page = response(985, has_next=True, cursor="next") if len(calls) == 1 else response(
            984, has_next=False, cursor=None
        )
        return (0, json.dumps(page))

    resolver._run = fake_run

    board = (await resolver.resolve_instance_boards())["agentdeck"]

    assert [issue.number for issue in board.issues] == [985, 984]
    assert board.complete is True
    assert all("cursor=" not in arg for arg in calls[0])
    assert "cursor=next" in calls[1]


async def test_the_project_board_query_is_balanced(tmp_path):
    """GitHub refuses a query with a stray brace, and a faked ``gh`` never parses it (#2384)."""
    resolver = _resolver(_poller_root(tmp_path, state={"issues": {}}))
    queries = []

    async def fake_run(*args, **_kwargs):
        queries.extend(arg.removeprefix("query=") for arg in args if arg.startswith("query="))
        return (1, "")

    resolver._run = fake_run

    await resolver.resolve_instance_boards()

    assert queries
    # Only open issues are shown, so only open items are asked for (#2384).
    assert all('query:"is:open"' in query for query in queries)
    for query in queries:
        depth = 0
        for character in query:
            depth += {"{": 1, "}": -1}.get(character, 0)
            assert depth >= 0, query
        assert depth == 0, query


async def test_project_graphql_errors_keep_returned_rows_visibly_incomplete(tmp_path):
    resolver = _resolver(_poller_root(tmp_path, state={"issues": {}}))

    async def fake_run(*_args, **_kwargs):
        return (
            0,
            json.dumps(
                {
                    "errors": [{"message": "project field was masked"}],
                    "data": {
                        "repositoryOwner": {
                            "projectV2": {
                                "items": {
                                    "nodes": [],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    },
                }
            ),
        )

    resolver._run = fake_run

    board = (await resolver.resolve_instance_boards())["agentdeck"]

    assert board.complete is False
    assert board.unknown == ("eerovil/agentdeck",)


def test_an_unreadable_instance_makes_the_board_incomplete_not_empty(tmp_path):
    root = _poller_root(tmp_path)
    _install(root, "broken", manifest="this is not toml = [", state={"issues": {}})
    board = poller_ops.read_board(root)
    assert board.complete is False


def test_an_unreadable_state_file_makes_the_board_incomplete(tmp_path):
    root = _poller_root(tmp_path)
    (root / "instances" / "agentdeck" / "state.json").write_text("{not json")
    board = poller_ops.read_board(root)
    assert board.complete is False
    assert board.scopes[0].complete is False


def test_an_invalid_project_number_keeps_known_cards_visible_but_incomplete(tmp_path):
    root = _poller_root(tmp_path, state={"issues": {"agentdeck#985": {"status": "Todo"}}})
    manifest = root / "instances" / "agentdeck" / "poller.toml"
    manifest.write_text(_MANIFEST.replace("project_number = 3", "project_number = 0"))

    resolver = _resolver(root, gh=None)
    board = resolver.cached_instance_boards()["agentdeck"]
    assert board.complete is False
    assert [(issue.number, issue.status) for issue in board.issues] == [(985, "Todo")]


def test_a_project_that_names_no_ownership_label_still_reports_the_engine_default(tmp_path):
    root = tmp_path / "poller"
    _install(
        root,
        "bazaar",
        manifest=(
            'contract = 1\n[board]\norg = "acmecorp"\nproject_number = 1\n'
            '[[repos]]\nid = "bazaar"\nholds_issues = true\n'
        ),
        state={"issues": {}},
    )
    board = poller_ops.read_board(root)
    assert board.ownership_label("acmecorp/bazaar") == "claude"


# --- the read model -----------------------------------------------------


async def test_resolves_open_issues_for_every_declared_repository(tmp_path):
    resolver = _resolver(_poller_root(tmp_path))
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, **_kwargs):
        calls.append(args)
        return (0, json.dumps([_issue_row(821, labels=("agent",)), _issue_row(814)]))

    resolver._run = fake_run
    backlog = await resolver.resolve()

    assert backlog.complete is True
    assert backlog.unknown == ()
    assert [issue.number for issue in backlog.issues] == [821, 814]
    issue = backlog.issue("eerovil/agentdeck", 821)
    assert issue is not None and issue.has_label("agent") and issue.is_open
    assert ("--repo", "eerovil/agentdeck") == calls[0][3:5]
    # Only the declared repository is queried — its issues and its open pull
    # requests, one list call each (#1012).
    assert {(call[1], call[4]) for call in calls} == {
        ("issue", "eerovil/agentdeck"),
        ("pr", "eerovil/agentdeck"),
    }


async def test_cached_backlog_reuses_shared_issue_titles_without_calling_github(tmp_path):
    resolver = _resolver(_poller_root(tmp_path))

    async def seed_run(*_args):
        return (0, json.dumps([_issue_row(821, title="Cached title")]))

    resolver._run = seed_run
    await resolver.resolve()

    async def forbidden_run(*_args):
        raise AssertionError("the cached read must not call GitHub")

    resolver._run = forbidden_run
    backlog = resolver.cached()

    assert backlog.complete is True
    assert backlog.issue("eerovil/agentdeck", 821).title == "Cached title"


async def test_cached_backlog_reads_open_pull_requests_without_calling_github(tmp_path):
    """The page render peeks; only the background refresh spends quota (#1012)."""
    resolver = _resolver(_poller_root(tmp_path))

    async def seed_run(*args):
        if args[1] == "pr":
            return (
                0,
                json.dumps(
                    [
                        {
                            "number": 990,
                            "title": "the fix",
                            "url": "https://github.com/eerovil/agentdeck/pull/990",
                            "headRefName": "agent/issue-821",
                            "baseRefName": "master",
                            "isDraft": False,
                        }
                    ]
                ),
            )
        return (0, json.dumps([_issue_row(821)]))

    resolver._run = seed_run
    await resolver.resolve()

    async def forbidden_run(*_args):
        raise AssertionError("the cached read must not call GitHub")

    resolver._run = forbidden_run
    backlog = resolver.cached()

    assert backlog.pulls_complete is True
    joined = backlog.pulls_by_issue()
    assert [pull["number"] for pull in joined["eerovil/agentdeck#821"]] == [990]


def test_cached_backlog_reports_an_unread_pull_request_list_as_unknown(tmp_path):
    backlog = _resolver(_poller_root(tmp_path)).cached()

    assert backlog.pulls_complete is False
    assert backlog.pulls[0].reason == "GitHub pull requests are not cached"


def test_cached_backlog_reports_a_cache_miss_as_unknown(tmp_path):
    backlog = _resolver(_poller_root(tmp_path)).cached()

    assert backlog.complete is False
    assert backlog.unknown == ("eerovil/agentdeck",)
    assert backlog.repositories[0].reason == "GitHub issues are not cached"


async def test_a_failed_refresh_keeps_the_operator_cache_visibly_incomplete(tmp_path):
    resolver = _resolver(_poller_root(tmp_path))

    async def succeeds(*_args):
        return (0, json.dumps([_issue_row(821)]))

    resolver._run = succeeds
    await resolver.resolve()

    async def fails(*_args):
        return (1, "")

    resolver._run = fails
    refreshed = await resolver.resolve(force=True)
    cached = resolver.cached()

    assert refreshed.complete is False
    assert cached.complete is False
    assert cached.repositories[0].reason == "GitHub issue cache is stale"


async def test_a_throttled_read_is_unknown_and_never_an_empty_backlog(tmp_path):
    resolver = _resolver(_poller_root(tmp_path))

    async def fake_run(*args, **_kwargs):
        return (1, "")  # rate limited, timed out, whatever: we could not look

    resolver._run = fake_run
    backlog = await resolver.resolve()

    assert backlog.complete is False
    assert backlog.unknown == ("eerovil/agentdeck",)
    assert backlog.repositories[0].issues == ()
    assert backlog.repositories[0].reason


async def test_a_host_without_gh_reports_unknown_rather_than_nothing_pending(tmp_path):
    resolver = _resolver(_poller_root(tmp_path), gh=None)
    backlog = await resolver.resolve()
    assert backlog.complete is False
    assert backlog.unknown == ("eerovil/agentdeck",)


async def test_a_failed_refresh_keeps_the_last_trusted_issues_but_stays_incomplete(tmp_path):
    root = _poller_root(tmp_path)
    resolver = _resolver(root)
    issue_responses = [(0, json.dumps([_issue_row(821)])), (1, "")]

    async def fake_run(*args, **_kwargs):
        # The pull request list is answered separately; this test is about what
        # a failed *issue* read does to the backlog.
        if args[1] == "pr":
            return (0, "[]")
        return issue_responses.pop(0)

    resolver._run = fake_run
    first = await resolver.resolve()
    assert first.complete is True

    second = await resolver.resolve(force=True)
    # The stale list is still worth showing as historical evidence — but the
    # repository reads as unknown, so nobody can call it an empty backlog.
    assert [issue.number for issue in second.issues] == [821]
    assert second.complete is False


async def test_a_repeated_read_is_served_from_the_shared_cache(tmp_path):
    resolver = _resolver(_poller_root(tmp_path))
    calls = 0

    async def fake_run(*args, **_kwargs):
        nonlocal calls
        if args[1] == "issue":
            calls += 1
        return (0, json.dumps([_issue_row(821)]) if args[1] == "issue" else "[]")

    resolver._run = fake_run
    await resolver.resolve()
    await resolver.resolve()
    assert calls == 1


async def test_an_unrecognised_issue_row_is_skipped_not_fatal(tmp_path):
    resolver = _resolver(_poller_root(tmp_path))

    async def fake_run(*args, **_kwargs):
        return (0, json.dumps([{"number": "not a number"}, _issue_row(821)]))

    resolver._run = fake_run
    backlog = await resolver.resolve()
    assert [issue.number for issue in backlog.issues] == [821]
    assert backlog.complete is True


async def test_an_incomplete_board_makes_the_whole_backlog_incomplete(tmp_path):
    root = _poller_root(tmp_path)
    (root / "instances" / "agentdeck" / "state.json").write_text("{not json")
    resolver = _resolver(root)

    async def fake_run(*args, **_kwargs):
        return (0, json.dumps([_issue_row(821)]))

    resolver._run = fake_run
    backlog = await resolver.resolve()
    assert backlog.repositories[0].complete is True
    assert backlog.complete is False


# --- identities ---------------------------------------------------------


def test_the_dispatch_branch_name_carries_the_issue_number():
    assert issue_number_from_branch("agent/issue-821") == 821
    assert issue_number_from_branch("agent/issue-821-retry") == 821
    assert issue_number_from_branch("feature/deckhand") is None
    assert issue_number_from_branch(None) is None


def test_only_an_issue_link_names_an_issue():
    assert issue_reference_from_url(
        "https://github.com/eerovil/agentdeck/issues/821"
    ) == ("eerovil/agentdeck", 821)
    # A pull request number is a different sequence: joining on it would attach
    # a chat to whatever issue happens to share the number.
    assert issue_reference_from_url("https://github.com/eerovil/agentdeck/pull/821") is None


# --- the join -----------------------------------------------------------


async def _backlog(tmp_path, *, rows=None, state=None):
    resolver = _resolver(_poller_root(tmp_path, state=state))

    async def fake_run(*args, **_kwargs):
        return (0, json.dumps(rows if rows is not None else [_issue_row(821)]))

    resolver._run = fake_run
    return await resolver.resolve()


async def test_a_chat_its_issue_and_its_card_join_into_one_item(tmp_path):
    backlog = await _backlog(
        tmp_path,
        state={"issues": {"agentdeck#821": {"status": "In progress", "engine_labeled": True}}},
    )
    session = _session(
        issue_url="https://github.com/eerovil/agentdeck/issues/821",
        question="Which shape should I use?",
    )
    view = join_pending(backlog, [session])

    assert [item.identity for item in view.items] == ["eerovil/agentdeck#821"]
    item = view.items[0]
    assert item.issue is not None and item.card is not None
    assert item.card.status == "In progress"
    assert [ref.key for ref in item.sessions] == [session.key]
    assert item.sessions[0].matched_on == "issue_url"
    assert item.waiting is True
    assert item.owned_by_poller is False  # the issue carries no `agent` label


async def test_a_chat_joins_through_an_explicit_issue_relationship(tmp_path):
    backlog = await _backlog(tmp_path)
    session = _session(issues=(IssueRef("eerovil/agentdeck", 821),))

    view = join_pending(backlog, [session])

    assert [item.identity for item in view.items] == ["eerovil/agentdeck#821"]
    assert [ref.matched_on for ref in view.items[0].sessions] == ["issue"]


async def test_a_chat_joins_through_its_dispatch_branch(tmp_path):
    backlog = await _backlog(tmp_path)
    session = _session()
    contexts = {
        session.key: GitContext("eerovil/agentdeck", "agent/issue-821", False, (), True)
    }
    view = join_pending(backlog, [session], contexts)
    assert [ref.matched_on for ref in view.items[0].sessions] == ["branch"]


async def test_a_chat_joins_through_a_registered_pull_requests_head_branch(tmp_path):
    backlog = await _backlog(tmp_path)
    session = _session()
    pull = PullRequestContext(
        repository="eerovil/agentdeck",
        number=830,
        title="feat: the issue read model",
        url="https://github.com/eerovil/agentdeck/pull/830",
        status="open",
        head_branch="agent/issue-821",
    )
    contexts = {
        session.key: GitContext("eerovil/agentdeck", None, False, (pull,), True)
    }
    view = join_pending(backlog, [session], contexts)
    item = view.items[0]
    assert [ref.matched_on for ref in item.sessions] == ["pull_request"]
    assert [row["number"] for row in item.pull_requests] == [830]


async def test_a_pull_request_in_another_repository_joins_the_issue_it_names(tmp_path):
    """Issues in one repository, code in another — the Trailhead shape (#1020)."""
    backlog = await _backlog(tmp_path)
    session = _session(issue_url="https://github.com/eerovil/agentdeck/issues/821")
    pull = PullRequestContext(
        repository="eerovil/other",
        number=830,
        title="feat: the fix",
        url="https://github.com/eerovil/other/pull/830",
        status="open",
        head_branch="agent/issue-821",
    )
    contexts = {session.key: GitContext("eerovil/other", None, False, (pull,), True)}

    view = join_pending(backlog, [session], contexts)
    item = next(item for item in view.items if item.number == 821)
    assert [row["number"] for row in item.pull_requests] == [830]


async def test_a_pull_request_elsewhere_needs_the_chat_to_name_the_issue(tmp_path):
    """A branch alone is not enough across repositories: issue 821 in two repos
    is two different issues, and the chat never claimed this one."""
    backlog = await _backlog(tmp_path)
    session = _session()  # no registered issue — matched by the branch only
    pull = PullRequestContext(
        repository="eerovil/other",
        number=830,
        title="feat: the fix",
        url="https://github.com/eerovil/other/pull/830",
        status="open",
        head_branch="agent/issue-821",
    )
    contexts = {session.key: GitContext("eerovil/other", None, False, (pull,), True)}

    view = join_pending(backlog, [session], contexts)
    item = next(item for item in view.items if item.identity == "eerovil/agentdeck#821")
    assert item.pull_requests == ()


async def test_work_that_is_not_a_chat_yet_is_still_a_pending_item(tmp_path):
    backlog = await _backlog(tmp_path, rows=[_issue_row(821), _issue_row(814)])
    view = join_pending(backlog, [])
    assert [item.number for item in view.items] == [821, 814]
    assert all(item.sessions == () for item in view.items)


async def test_a_chat_whose_issue_could_not_be_read_still_appears(tmp_path):
    resolver = _resolver(_poller_root(tmp_path))

    async def fake_run(*args, **_kwargs):
        return (1, "")

    resolver._run = fake_run
    backlog = await resolver.resolve()
    session = _session(issue_url="https://github.com/eerovil/agentdeck/issues/821")
    view = join_pending(backlog, [session])

    assert [item.identity for item in view.items] == ["eerovil/agentdeck#821"]
    assert view.items[0].issue is None
    # The item exists but the answer is explicitly partial, so a reader cannot
    # present this list as the whole backlog.
    assert view.complete is False
    assert view.unknown == ("eerovil/agentdeck",)


async def test_the_ownership_label_decides_what_the_poller_holds(tmp_path):
    backlog = await _backlog(tmp_path, rows=[_issue_row(821, labels=("agent",))])
    view = join_pending(backlog, [])
    assert view.items[0].ownership_label == "agent"
    assert view.items[0].owned_by_poller is True


async def test_any_ownership_alias_decides_what_the_poller_holds(tmp_path):
    root = _poller_root(tmp_path)
    manifest = root / "instances" / "agentdeck" / "poller.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'ownership_label = "agent"', 'ownership_label = ["agent", "claude"]'
        )
    )
    resolver = _resolver(root)

    async def fake_run(*args, **_kwargs):
        return (0, json.dumps([_issue_row(821, labels=("claude",))]))

    resolver._run = fake_run
    item = join_pending(await resolver.resolve(), []).items[0]

    assert item.ownership_label == "agent"
    assert item.ownership_labels == ("agent", "claude")
    assert item.owned_by_poller is True


async def test_a_card_outliving_its_issue_is_owned_by_the_poller_but_not_by_label(tmp_path):
    """#866: `owned_by_label` is the half that a stale card cannot answer yes.

    The card fallback exists so an unreadable issue does not lose the engine's
    own record. But a card stays on the board after its issue is merged, so a
    caller that must not act on a leftover asks the stricter question.
    """
    backlog = await _backlog(
        tmp_path,
        rows=[],  # the issue is closed, so the open-issue read does not carry it
        state={"issues": {"agentdeck#821": {"status": "In progress", "engine_labeled": True}}},
    )
    item = join_pending(backlog, []).items[0]

    assert item.issue is None
    assert item.owned_by_poller is True
    assert item.owned_by_label is False


async def test_owned_by_label_needs_the_live_open_issue_to_carry_it(tmp_path):
    backlog = await _backlog(tmp_path, rows=[_issue_row(821, labels=("agent",))])
    assert join_pending(backlog, []).items[0].owned_by_label is True


async def test_owned_by_label_is_false_when_the_issue_carries_another_label(tmp_path):
    backlog = await _backlog(tmp_path, rows=[_issue_row(821, labels=("bug",))])
    assert join_pending(backlog, []).items[0].owned_by_label is False


async def test_a_repository_outside_the_declared_scope_is_not_joined(tmp_path):
    backlog = await _backlog(tmp_path)
    session = _session(issue_url="https://github.com/someone/elsewhere/issues/7")
    view = join_pending(backlog, [session])
    assert [item.identity for item in view.items] == ["eerovil/agentdeck#821"]


async def test_a_chat_aware_attention_report_makes_the_current_leg_waiting(tmp_path):
    from datetime import UTC, datetime

    from agentdeck.models import AttentionReport

    backlog = await _backlog(tmp_path)
    session = _session(issue_url="https://github.com/eerovil/agentdeck/issues/821")
    old_leg_report = AttentionReport("waiting", datetime(2026, 8, 8, tzinfo=UTC))

    def attention_report_for(key: str):
        assert key == session.key
        # The resolver owns the chat lookup. The report itself may still live on
        # the leg this chat left before ``adopt_chat_leg`` could re-anchor it.
        return old_leg_report

    view = join_pending(
        backlog,
        [session],
        attention_report_for=attention_report_for,
    )
    assert view.items[0].waiting is True
    assert view.items[0].sessions[0].attention == "waiting"


async def test_the_local_api_serves_the_read_model_and_the_join(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from agentdeck.app import create_app
    from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, PollerConfig

    root = _poller_root(
        tmp_path,
        state={"issues": {"agentdeck#821": {"status": "In progress", "engine_labeled": True}}},
    )
    app = create_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
            accounts=[
                AccountConfig(provider="claude_code", label="main", config_dir=str(tmp_path))
            ],
            poller=PollerConfig(root=str(root)),
        )
    )
    app.state.issues._gh = "gh"

    async def fake_run(*args, **_kwargs):
        return (0, json.dumps([_issue_row(821, labels=("agent",))]))

    app.state.issues._run = fake_run
    session = _session(issue_url="https://github.com/eerovil/agentdeck/issues/821")
    app.state.app_state.update_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        issues = (await client.get("/api/issues")).json()["issues"]
        pending = (await client.get("/api/pending")).json()["pending"]

    assert issues["complete"] is True
    assert issues["repositories"][0]["repository"] == "eerovil/agentdeck"
    assert [item["number"] for item in issues["repositories"][0]["issues"]] == [821]

    assert pending["complete"] is True
    assert pending["unknown"] == []
    assert len(pending["items"]) == 1
    item = pending["items"][0]
    assert item["identity"] == "eerovil/agentdeck#821"
    assert item["card"]["status"] == "In progress"
    assert item["owned_by_poller"] is True
    assert [ref["key"] for ref in item["sessions"]] == [session.key]


async def test_two_chats_on_the_same_issue_are_one_item(tmp_path):
    backlog = await _backlog(tmp_path)
    first = _session(
        key="claude_code:main:chat-1",
        issue_url="https://github.com/eerovil/agentdeck/issues/821",
    )
    second = _session(
        key="codex:main:chat-2",
        issue_url="https://github.com/eerovil/agentdeck/issues/821",
    )
    view = join_pending(backlog, [first, second])
    assert len(view.items) == 1
    assert len(view.items[0].sessions) == 2


async def test_a_trees_parents_are_read_on_the_open_ttl_even_when_closed(tmp_path):
    """#3622 live check: GitHub's sub-issue count lags a link by seconds, and a closed parent's
    day-long TTL would keep the answer read too early. Read again once ``OPEN_TTL_S`` old, a
    never-read one at once, and a fresh one not at all."""
    import time

    from agentdeck.github_cache import GitHubMetadata

    resolver = _resolver(_poller_root(tmp_path))
    cache = resolver._metadata
    read: list = []

    async def refresh(refs, *, limit):
        read.append((list(refs), limit))
        return len(refs)

    resolver.refresh_issue_records = refresh
    closed = {"title": "Map", "state": "closed", "subIssues": {"total": 0, "completed": 0}}
    cache.offer("issue:v1:eerovil/agentdeck#1", GitHubMetadata(closed, terminal=True))
    cache.offer("issue:v1:eerovil/agentdeck#2", GitHubMetadata(closed, terminal=True))
    cache.expire("issue:v1:eerovil/agentdeck#2")  # a link event: read it again
    old = time.time() - cache.OPEN_TTL_S - 1
    cache.offer("issue:v1:eerovil/agentdeck#3", GitHubMetadata(closed, terminal=True))
    with cache._connect() as connection:
        connection.execute(
            "UPDATE github_metadata_v1 SET fetched_at = ? WHERE key = ?",
            (old, "issue:v1:eerovil/agentdeck#3"),
        )

    count = await resolver.refresh_tree_records(
        [("eerovil/agentdeck", n) for n in (1, 2, 3, 4)], limit=10
    )

    assert count == 3
    assert read == [([("eerovil/agentdeck", n) for n in (2, 3, 4)], 10)]
    assert await resolver.refresh_tree_records([("eerovil/agentdeck", 1)], limit=10) == 0


async def test_reading_a_parents_sub_issues_relinks_a_child_that_lost_its_parent(tmp_path):
    """#3654: no GitHub issue read or delivery carries ``parent_issue_url``, so a child row
    whose link was erased folds again once its parent's list is read."""
    import json

    from agentdeck import work_tree
    from agentdeck.github_cache import GitHubMetadata

    resolver = _resolver(_poller_root(tmp_path))
    cache = resolver._metadata
    listed = [
        {"repository": "eerovil/agentdeck", "number": n, "title": "T", "state": "open"}
        for n in (5, 6)
    ]

    async def run(*args, owner=None):
        return 0, json.dumps(listed)

    resolver._run = run
    wiped = {"title": "T", "state": "open", "parent": None}
    cache.offer("issue:v1:eerovil/agentdeck#5", GitHubMetadata(wiped, terminal=False))

    assert len(await resolver.sub_issues("eerovil/agentdeck", 9)) == 2

    held = cache.peek("issue:v1:eerovil/agentdeck#5").value
    assert work_tree.parent_ref(held).work_id == "github_issue:eerovil/agentdeck#9"
    assert cache.peek("issue:v1:eerovil/agentdeck#6").value is None  # never read: left alone
