"""The first webhook consumer: PR and issue write-through (#1642, #1681).

Fixtures go through `WebhookLog` and `WebhookTailer` — the same appender and reader the
live path uses — so what is asserted is the consumer's contract over real stream bytes,
not a hand-built delivery.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agentdeck.git_context import _pull_request
from agentdeck.github_cache import GitHubMetadata, GitHubMetadataCache
from agentdeck.github_webhook_log import Repository, WebhookLog
from agentdeck.kanban import KanbanRef, KanbanTitleCache
from agentdeck.webhook_status_cache import WebhookStatusCacheService
from agentdeck.webhook_tailer import WebhookTailer

AGENTDECK = Repository(owner="eerovil", name="agentdeck")


def pull_body(
    *,
    number: int = 7,
    action: str = "opened",
    state: str = "open",
    merged_at: str | None = None,
    updated_at: str = "2026-09-07T10:00:00Z",
    title: str = "Ship the thing",
    head_sha: str = "a" * 40,
) -> bytes:
    return json.dumps(
        {
            "action": action,
            "pull_request": {
                "number": number,
                "title": title,
                "html_url": f"https://github.com/eerovil/agentdeck/pull/{number}",
                "state": state,
                "draft": False,
                "merged_at": merged_at,
                "updated_at": updated_at,
                "head": {"ref": "agent/branch", "sha": head_sha},
                "base": {"ref": "master"},
            },
            "repository": {
                "full_name": AGENTDECK.slug,
                "name": AGENTDECK.name,
                "owner": {"login": AGENTDECK.owner},
            },
        }
    ).encode()


def issue_body(
    number: int = 3,
    *,
    action: str = "closed",
    state: str = "closed",
    state_reason: str | None = "completed",
    updated_at: str = "2026-09-07T10:00:00Z",
    title: str = "Pills go stale",
) -> bytes:
    return json.dumps(
        {
            "action": action,
            "issue": {
                "number": number,
                "title": title,
                "state": state,
                "state_reason": state_reason,
                "updated_at": updated_at,
            },
            "repository": {
                "full_name": AGENTDECK.slug,
                "name": AGENTDECK.name,
                "owner": {"login": AGENTDECK.owner},
            },
        }
    ).encode()


PR_KEY = "pr-ref:v1:eerovil/agentdeck:7"
ISSUE_LIST_KEY = "issue-list:v1:eerovil/agentdeck"
ISSUE_KEY = "issue:v1:eerovil/agentdeck#3"


@pytest.fixture
def root(tmp_path):
    return tmp_path / "github-webhooks"


@pytest.fixture
def cache(tmp_path) -> GitHubMetadataCache:
    return GitHubMetadataCache(tmp_path / "github-cache.sqlite3")


@pytest.fixture
def service(root, cache) -> WebhookStatusCacheService:
    return WebhookStatusCacheService(root, cache=cache)


def deliver(root, body: bytes, *, event: str, delivery: str) -> None:
    WebhookLog(root).append(
        body=body,
        headers={"X-GitHub-Delivery": delivery, "X-GitHub-Event": event},
    )


def apply_stream(root, service: WebhookStatusCacheService) -> int:
    tailer = WebhookTailer(root, consumer="test-apply")
    batch = tailer.read()
    for item in batch:
        service.apply(item)
    tailer.commit()
    return len(batch)


def stored_terminal(cache: GitHubMetadataCache, key: str) -> bool:
    with sqlite3.connect(cache.path) as connection:
        row = connection.execute(
            "SELECT terminal FROM github_metadata_v1 WHERE key = ?", (key,)
        ).fetchone()
    assert row is not None
    return bool(row[0])


def test_pull_request_writes_through_in_the_readers_shape(root, cache, service):
    deliver(root, pull_body(), event="pull_request", delivery="d-1")

    assert apply_stream(root, service) == 1

    lookup = cache.peek(PR_KEY)
    assert lookup.complete
    pull = _pull_request("eerovil/agentdeck", lookup.value)
    assert pull is not None
    assert pull.number == 7
    assert pull.status == "open"
    assert pull.head_branch == "agent/branch"
    # The head commit rides the same row (#2088): it is what says whether a finished
    # review is about the commit this pull request would merge.
    assert pull.head_sha == "a" * 40
    assert not stored_terminal(cache, PR_KEY)


def test_merged_pull_lands_terminal(root, cache, service):
    deliver(
        root,
        pull_body(action="closed", state="closed", merged_at="2026-09-07T11:00:00Z"),
        event="pull_request",
        delivery="d-1",
    )

    apply_stream(root, service)

    pull = _pull_request("eerovil/agentdeck", cache.peek(PR_KEY).value)
    assert pull is not None and pull.is_merged
    assert stored_terminal(cache, PR_KEY)


def test_older_snapshot_never_regresses_a_newer_row(root, cache, service):
    deliver(
        root,
        pull_body(
            action="closed",
            state="closed",
            merged_at="2026-09-07T11:00:00Z",
            updated_at="2026-09-07T11:00:00Z",
        ),
        event="pull_request",
        delivery="d-newer",
    )
    deliver(
        root,
        pull_body(updated_at="2026-09-07T09:00:00Z"),
        event="pull_request",
        delivery="d-older",
    )

    apply_stream(root, service)

    pull = _pull_request("eerovil/agentdeck", cache.peek(PR_KEY).value)
    assert pull is not None and pull.is_merged


def test_replay_is_idempotent(root, cache, service):
    deliver(root, pull_body(), event="pull_request", delivery="d-1")

    apply_stream(root, service)
    apply_stream(root, service)  # a second consumer pass over the same records

    pull = _pull_request("eerovil/agentdeck", cache.peek(PR_KEY).value)
    assert pull is not None and pull.status == "open"


def test_an_issue_event_it_cannot_place_in_the_list_drops_the_list(root, cache, service):
    # No ``html_url``: the row cannot be written in the list's shape, so the list goes.
    cache.offer(ISSUE_LIST_KEY, GitHubMetadata([{"number": 3}]))
    deliver(root, issue_body(), event="issues", delivery="d-1")

    apply_stream(root, service)

    assert cache.peek(ISSUE_LIST_KEY).value is None


def _listed(number: int, title: str, updated: str = "2026-09-07T09:00:00Z") -> dict:
    """One row as ``gh issue list --json number,title,url,state,labels,updatedAt,assignees``."""
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/eerovil/agentdeck/issues/{number}",
        "state": "OPEN",
        "labels": [{"name": "bug", "color": "d73a4a"}],
        "assignees": [],
        "updatedAt": updated,
    }


def _issue_event(number: int, *, state: str, title: str, action: str = "edited",
                 updated_at: str = "2026-09-07T10:00:00Z") -> bytes:
    body = json.loads(issue_body(number, action=action, state=state, title=title,
                                 updated_at=updated_at,
                                 state_reason="completed" if state == "closed" else None))
    body["issue"]["html_url"] = f"https://github.com/eerovil/agentdeck/issues/{number}"
    body["issue"]["labels"] = [{"name": "claude"}]
    body["issue"]["assignees"] = [{"login": "eerovil"}]
    body["issue"]["created_at"] = "2026-09-01T08:00:00Z"
    return json.dumps(body).encode()


def _list_age(cache: GitHubMetadataCache) -> float:
    with sqlite3.connect(cache.path) as connection:
        return connection.execute(
            "SELECT fetched_at FROM github_metadata_v1 WHERE key = ?", (ISSUE_LIST_KEY,)
        ).fetchone()[0]


def test_an_issue_event_moves_its_one_row_and_keeps_the_rest_of_the_list(root, cache, service):
    """#2226: dropping the whole list on every issue event left titles on a busy repository
    falling back to chat names until the next sweep."""
    cache.offer(ISSUE_LIST_KEY, GitHubMetadata([_listed(3, "Old"), _listed(4, "Other"),
                                                _listed(5, "Leaving")]))
    fetched = _list_age(cache)
    deliver(root, _issue_event(3, state="open", title="Renamed"), event="issues", delivery="d-1")
    deliver(root, _issue_event(5, state="closed", title="Leaving", action="closed"),
            event="issues", delivery="d-2")
    deliver(root, _issue_event(6, state="open", title="Brand new", action="opened"),
            event="issues", delivery="d-3")

    apply_stream(root, service)

    rows = {row["number"]: row for row in cache.peek(ISSUE_LIST_KEY).value}
    assert sorted(rows) == [3, 4, 6]
    assert rows[3]["title"] == "Renamed" and rows[3]["labels"] == [{"name": "claude"}]
    assert rows[4]["title"] == "Other"
    assert rows[6] == {
        "number": 6, "title": "Brand new", "url": "https://github.com/eerovil/agentdeck/issues/6",
        "state": "OPEN", "labels": [{"name": "claude"}], "assignees": [{"login": "eerovil"}],
        "updatedAt": "2026-09-07T10:00:00Z", "createdAt": "2026-09-01T08:00:00Z",
    }
    # The list keeps the age of its last real read: it is re-read exactly when it would be.
    assert _list_age(cache) == fetched


def test_an_older_issue_snapshot_leaves_the_listed_row_alone(root, cache, service):
    cache.offer(ISSUE_LIST_KEY, GitHubMetadata([_listed(3, "Newer", "2026-09-07T11:00:00Z")]))
    deliver(root, _issue_event(3, state="open", title="Older"), event="issues", delivery="d-1")

    apply_stream(root, service)

    assert [row["title"] for row in cache.peek(ISSUE_LIST_KEY).value] == ["Newer"]


def test_a_capped_list_is_dropped_rather_than_shortened(root, cache, service):
    from agentdeck.issue_context import IssueResolver
    from agentdeck.webhook_status_cache import _ISSUE_LIST_CAP

    assert _ISSUE_LIST_CAP == IssueResolver.MAX_ISSUES
    full = [_listed(n, f"Issue {n}") for n in range(1, _ISSUE_LIST_CAP + 1)]
    cache.offer(ISSUE_LIST_KEY, GitHubMetadata(full))
    # A title edit keeps the count, so it is revised in place even on a full list.
    deliver(root, _issue_event(3, state="open", title="Renamed"), event="issues", delivery="d-1")
    apply_stream(root, service)
    assert cache.peek(ISSUE_LIST_KEY).value[2]["title"] == "Renamed"

    # A close would make a cut-off list look whole: absence there proves nothing.
    deliver(root, _issue_event(4, state="closed", title="Issue 4", action="closed"),
            event="issues", delivery="d-2")
    apply_stream(root, service)
    assert cache.peek(ISSUE_LIST_KEY).value is None


def test_a_transferred_issue_drops_the_list(root, cache, service):
    cache.offer(ISSUE_LIST_KEY, GitHubMetadata([_listed(3, "Moving")]))
    deliver(root, _issue_event(3, state="open", title="Moving", action="transferred"),
            event="issues", delivery="d-1")

    apply_stream(root, service)

    assert cache.peek(ISSUE_LIST_KEY).value is None


def test_issue_writes_through_in_the_pill_readers_shape(root, cache, service):
    deliver(root, issue_body(), event="issues", delivery="d-1")

    assert apply_stream(root, service) == 1

    # Read it back the way a pill does, so the key spelling is asserted through the
    # reader rather than restated here.
    titles = KanbanTitleCache(cache.path)
    ref = KanbanRef(repo="agentdeck", number=3, owner="eerovil")
    assert titles.get(ref) == "Pills go stale"
    assert titles.get_status(ref) == ("closed", "done")
    assert cache.peek(ISSUE_KEY).complete
    assert stored_terminal(cache, ISSUE_KEY)


def _commented(action: str, comments, updated_at: str) -> bytes:
    body = json.loads(_issue_event(3, state="open", title="Hide me", action=action,
                                   updated_at=updated_at))
    body["issue"]["comments"] = comments
    body["comment"] = {"id": 1, "body": "anything at all"}
    return json.dumps(body).encode()


def test_a_comment_writes_the_issues_new_comment_count_through(root, cache, service):
    """#2238: a hidden Work comes back on any new comment, so the count must not wait for a
    poll. A deleted comment is still the same issue: its list row stays."""
    cache.offer(ISSUE_LIST_KEY, GitHubMetadata([_listed(3, "Hide me"), _listed(4, "Other")]))
    deliver(root, _commented("created", 2, "2026-09-07T10:00:00Z"), event="issue_comment",
            delivery="d-1")
    apply_stream(root, service)
    assert cache.peek(ISSUE_KEY).value["comments"] == 2

    deliver(root, _commented("deleted", 1, "2026-09-07T11:00:00Z"), event="issue_comment",
            delivery="d-2")
    deliver(root, _commented("created", True, "2026-09-07T12:00:00Z"), event="issue_comment",
            delivery="d-3")
    apply_stream(root, service)

    # A count that is not a count is left out rather than guessed.
    assert "comments" not in cache.peek(ISSUE_KEY).value
    assert [row["number"] for row in cache.peek(ISSUE_LIST_KEY).value] == [3, 4]


def test_issue_closed_not_planned_reads_as_dropped(root, cache, service):
    deliver(root, issue_body(state_reason="not_planned"), event="issues", delivery="d-1")

    apply_stream(root, service)

    ref = KanbanRef(repo="agentdeck", number=3, owner="eerovil")
    assert KanbanTitleCache(cache.path).get_status(ref) == ("closed", "dropped")


def test_open_issue_row_is_not_terminal(root, cache, service):
    deliver(
        root,
        issue_body(action="edited", state="open", state_reason=None),
        event="issues",
        delivery="d-1",
    )

    apply_stream(root, service)

    ref = KanbanRef(repo="agentdeck", number=3, owner="eerovil")
    assert KanbanTitleCache(cache.path).get_status(ref) == ("open", "open")
    assert not stored_terminal(cache, ISSUE_KEY)


def test_older_issue_snapshot_never_regresses_a_newer_row(root, cache, service):
    deliver(root, issue_body(), event="issues", delivery="d-newer")
    deliver(
        root,
        issue_body(
            action="edited",
            state="open",
            state_reason=None,
            updated_at="2026-09-07T09:00:00Z",
            title="Pills go stale (older)",
        ),
        event="issues",
        delivery="d-older",
    )

    apply_stream(root, service)

    ref = KanbanRef(repo="agentdeck", number=3, owner="eerovil")
    titles = KanbanTitleCache(cache.path)
    assert titles.get_status(ref) == ("closed", "done")
    assert titles.get(ref) == "Pills go stale"


def test_issue_replay_is_idempotent(root, cache, service):
    deliver(root, issue_body(), event="issues", delivery="d-1")

    apply_stream(root, service)
    apply_stream(root, service)  # a second consumer pass over the same records

    ref = KanbanRef(repo="agentdeck", number=3, owner="eerovil")
    assert KanbanTitleCache(cache.path).get_status(ref) == ("closed", "done")


def test_unmappable_issue_payload_invalidates_rather_than_guessing(root, cache, service):
    cache.offer(ISSUE_KEY, GitHubMetadata({"title": "held", "state": "open"}))
    body = json.loads(issue_body())
    del body["issue"]["state"]
    deliver(root, json.dumps(body).encode(), event="issues", delivery="d-1")

    apply_stream(root, service)

    assert cache.peek(ISSUE_KEY).value is None
    assert cache.peek(ISSUE_LIST_KEY).value is None


def test_issue_without_a_number_leaves_rows_alone(root, cache, service):
    body = json.loads(issue_body())
    del body["issue"]["number"]
    deliver(root, json.dumps(body).encode(), event="issues", delivery="d-1")

    apply_stream(root, service)

    assert cache.peek(ISSUE_KEY).value is None


def test_unmappable_pull_payload_invalidates_rather_than_guessing(root, cache, service):
    cache.offer(PR_KEY, GitHubMetadata({"number": 7, "title": "held", "url": "u", "state": "open"}))
    body = json.loads(pull_body())
    del body["pull_request"]["state"]
    deliver(root, json.dumps(body).encode(), event="pull_request", delivery="d-1")

    apply_stream(root, service)

    assert cache.peek(PR_KEY).value is None


def _tree_issue(number: int, *, parent: int | None = None, done: int = 0, total: int = 0,
                updated_at: str = "2026-09-07T10:00:00Z") -> dict:
    """An issue object as GitHub sends it now: with ``sub_issues_summary`` (#3622). No live
    delivery carries ``parent_issue_url`` (#3654); a read does, so ``parent`` adds it."""
    api = "https://api.github.com/repos/eerovil/agentdeck"
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "state_reason": None,
        "updated_at": updated_at,
        "url": f"{api}/issues/{number}",
        "repository_url": api,
        "html_url": f"https://github.com/eerovil/agentdeck/issues/{number}",
        "sub_issues_summary": {
            "total": total, "completed": done, "percent_completed": 0,
        },
        **({"parent_issue_url": f"{api}/issues/{parent}"} if parent else {}),
    }


def _tree_body(**payload) -> bytes:
    repository = {
        "full_name": AGENTDECK.slug, "name": AGENTDECK.name, "owner": {"login": AGENTDECK.owner},
    }
    return json.dumps({**payload, "repository": repository}).encode()


def test_an_issue_keeps_its_parent_and_its_sub_issue_counts(root, cache, service):
    """#3622: the Work tree reads both from the issue's own row."""
    body = _tree_body(action="edited", issue=_tree_issue(3, parent=9))
    deliver(root, body, event="issues", delivery="d-1")
    parent = _tree_body(action="edited", issue=_tree_issue(9, done=1, total=4))
    deliver(root, parent, event="issues", delivery="d-2")

    apply_stream(root, service)

    child = cache.peek(ISSUE_KEY).value
    assert child["parent"] == "https://api.github.com/repos/eerovil/agentdeck/issues/9"
    assert child["subIssues"]["total"] == 0
    held = cache.peek("issue:v1:eerovil/agentdeck#9").value
    assert held.get("parent") is None and held["subIssues"]["completed"] == 1


def test_a_sub_issue_link_or_a_child_closing_wakes_the_work_overview_once(root, cache):
    """Review of 65786a2: a link alone must reach the front page within seconds (D2)."""
    woken = []
    service = WebhookStatusCacheService(root, cache=cache, on_tree_change=lambda: woken.append(1))
    deliver(root, _tree_body(action="edited", issue=_tree_issue(9, total=0)),
            event="issues", delivery="d-1")
    apply_stream(root, service)
    assert woken == []  # an ordinary issue edit moves no tree
    added = {"action": "sub_issue_added", "parent_issue": _tree_issue(9, total=1),
             "sub_issue": _tree_issue(3)}
    deliver(root, _tree_body(**added), event="sub_issues", delivery="d-2")
    apply_stream(root, service)
    assert woken == [1]
    closed = {**_tree_issue(3, parent=9), "state": "closed",
              "updated_at": "2026-09-07T11:00:00Z"}
    deliver(root, _tree_body(action="closed", issue=closed), event="issues", delivery="d-3")
    apply_stream(root, service)
    assert woken == [1, 1]


def test_a_sub_issue_link_sets_and_clears_the_parent_whatever_updated_at_says(
    root, cache, service
):
    """#3622: GitHub need not move ``updated_at`` for a link, and the snapshot may not show it
    yet, so the link itself is written; an unlink clears it. The parent's count is read
    again rather than taken from the payload: live, both removal events still carried the
    count from before the unlink (#3622's live check)."""
    stamp = "2026-09-07T10:00:00Z"
    parent_key = "issue:v1:eerovil/agentdeck#9"
    deliver(root, _tree_body(action="edited", issue=_tree_issue(3)),
            event="issues", delivery="d-1")
    added = {
        "action": "sub_issue_added",
        "parent_issue": _tree_issue(9, done=1, total=1, updated_at=stamp),
        "sub_issue": _tree_issue(3, updated_at=stamp),  # the snapshot predates the link
    }
    deliver(root, _tree_body(**added), event="sub_issues", delivery="d-2")

    apply_stream(root, service)

    assert cache.peek(ISSUE_KEY).value["parent"].endswith("/issues/9")
    assert cache.peek(parent_key).value is not None and not cache.is_fresh(parent_key)

    # A comment's payload has no ``parent_issue_url``: the link stays (#3654).
    later = _tree_issue(3, updated_at="2026-09-07T10:05:00Z")
    deliver(root, _tree_body(action="created", issue=later, comment={}),
            event="issue_comment", delivery="d-c")
    apply_stream(root, service)
    assert cache.peek(ISSUE_KEY).value["parent"].endswith("/issues/9")

    # As GitHub sent it live: the parent's count as it was before the unlink.
    removed = {**added, "action": "sub_issue_removed"}
    deliver(root, _tree_body(**removed), event="sub_issues", delivery="d-3")
    apply_stream(root, service)

    assert cache.peek(ISSUE_KEY).value["parent"] is None
    assert not cache.is_fresh(parent_key)  # its next read takes GitHub's own count


def test_a_maps_cached_reads_are_dropped_when_github_says_they_changed(root, cache, service):
    """#3627: a wayfinder map's diagram reads its body, its sub-issue list and each ticket's
    "blocked by" list through the cache; the deliveries that change them mark those rows out
    of date, so the next page read fetches them again and nothing polls."""
    from agentdeck.github_cache import GitHubMetadata

    def held(prefix: str, number: int) -> str:
        key = f"{prefix}eerovil/agentdeck#{number}"
        cache.offer(key, GitHubMetadata({"held": True}, terminal=False))
        return key

    moved = []
    service._on_map_change = lambda: moved.append(True)
    body, sub_issues = held("issue-body:v1:", 9), held("sub-issues:v2:", 9)
    blocked, other = held("blocked-by:v1:", 3), held("blocked-by:v1:", 4)
    comment = _tree_body(action="created", issue=_tree_issue(3, parent=9), comment={})
    deliver(root, comment, event="issue_comment", delivery="d-1")
    apply_stream(root, service)
    assert all(cache.peek(key).value for key in (body, sub_issues, blocked, other))
    assert moved == []

    # As GitHub sends it: no ``parent_issue_url``, so the row the comment wrote names it.
    closed = _tree_body(action="closed", issue=_tree_issue(3))
    deliver(root, closed, event="issues", delivery="d-2")
    edited = _tree_body(action="edited", issue=_tree_issue(9))
    deliver(root, edited, event="issues", delivery="d-3")
    dependency = _tree_body(action="blocked_by_added", blocked_issue=_tree_issue(3),
                            blocking_issue=_tree_issue(4))
    deliver(root, dependency, event="issue_dependencies", delivery="d-4")
    apply_stream(root, service)

    # Marked out of date, their values kept for the page to draw meanwhile (#3739).
    for key in (body, sub_issues, blocked):
        assert cache.peek(key).value == {"held": True} and not cache.peek(key).complete
    assert cache.peek(other).complete
    # The open map pages build again, their arrows and fog with them.
    assert moved

    # Every change to what a box shows, a ticket's type label included, drops the parent's
    # list again, even for a ticket the deck holds no record of its own for (#3627).
    for action in ("labeled", "unlabeled", "assigned"):
        sub_issues = held("sub-issues:v2:", 9)
        moved.clear()
        deliver(root, _tree_body(action=action, issue=_tree_issue(5, parent=9),
                                 label={"name": "wayfinder:prototype"}),
                event="issues", delivery=f"d-{action}")
        apply_stream(root, service)
        assert not cache.peek(sub_issues).complete and moved
    from agentdeck import wayfinder_map

    before = wayfinder_map.generation()
    wayfinder_map.bump_generation()
    assert wayfinder_map.generation() == before + 1  # what the Work page's build key reads


def test_a_sub_issue_closing_marks_even_a_closed_parent_stale(root, cache, service):
    """Second escalated review: the child's payload does not carry its parent's new count, and
    a closed parent's row would otherwise be reused for a day."""
    parent = {**_tree_issue(9, done=0, total=2), "state": "closed"}
    deliver(root, _tree_body(action="closed", issue=parent), event="issues", delivery="d-1")
    apply_stream(root, service)
    key = "issue:v1:eerovil/agentdeck#9"
    assert cache.is_fresh(key) and stored_terminal(cache, key)

    linked = {"action": "sub_issue_added", "parent_issue": parent, "sub_issue": _tree_issue(3)}
    deliver(root, _tree_body(**linked), event="sub_issues", delivery="d-2")
    apply_stream(root, service)
    deliver(root, _tree_body(action="closed", issue=parent), event="issues", delivery="d-3")
    apply_stream(root, service)
    assert cache.is_fresh(key)
    # As GitHub sends it: no ``parent_issue_url``; the link is on the child's row (#3654).
    child = {**_tree_issue(3), "state": "closed", "updated_at": "2026-09-07T11:00:00Z"}
    deliver(root, _tree_body(action="closed", issue=child), event="issues", delivery="d-4")
    apply_stream(root, service)

    assert not cache.is_fresh(key)  # read again on the next refresh
    assert cache.peek(key).value["subIssues"]["total"] == 2  # still drawn meanwhile


def test_other_events_change_nothing(root, cache, service):
    deliver(root, issue_body(), event="label", delivery="d-1")

    apply_stream(root, service)

    assert cache.peek(PR_KEY).value is None
    assert cache.peek(ISSUE_LIST_KEY).value is None
    assert cache.peek(ISSUE_KEY).value is None


# --- Telling the reader to look, on a real change only (#2042) --------------
#
# The cache write is the durable half and has its own tests above. These are about
# the signal beside it: the callback that turns a 60-second wait for Deckhand's next
# sweep into a pill that follows the delivery. The rule is the one every consumer
# obeys — a delivery says *when to look*, never what is true — so the assertions are
# about when the reader is woken, not about what it then decides.


@pytest.fixture
def looks() -> list[int]:
    return []


@pytest.fixture
def signalling(root, cache, looks) -> WebhookStatusCacheService:
    return WebhookStatusCacheService(
        root, cache=cache, on_pull_change=lambda: looks.append(1)
    )


def batch(root, service: WebhookStatusCacheService) -> int:
    """Apply the stream the way the live loop does: whole batch, one commit."""
    tailer = WebhookTailer(root, consumer="test-batch")
    deliveries = tailer.read()
    service.apply_batch(deliveries)
    tailer.commit()
    return len(deliveries)


def test_a_merge_wakes_the_reader(root, signalling, looks):
    deliver(root, pull_body(), event="pull_request", delivery="d-1")
    assert batch(root, signalling) == 1
    assert looks == [1]

    deliver(
        root,
        pull_body(
            action="closed",
            state="closed",
            merged_at="2026-09-07T11:00:00Z",
            updated_at="2026-09-07T11:00:00Z",
        ),
        event="pull_request",
        delivery="d-2",
    )

    assert batch(root, signalling) == 1
    assert looks == [1, 1]


def test_a_comment_bumping_updated_at_wakes_nobody(root, signalling, looks):
    """The common case, and the reason this is not "signal on every write".

    A comment, a label, an assignee — GitHub bumps ``updated_at`` and sends the whole
    pull request again. The row is rewritten because it is newer, but nothing a pill
    draws has moved, so waking the reader would be a sweep with no pixel behind it.
    """
    deliver(root, pull_body(), event="pull_request", delivery="d-1")
    batch(root, signalling)
    looks.clear()

    deliver(
        root,
        pull_body(action="edited", updated_at="2026-09-07T10:30:00Z"),
        event="pull_request",
        delivery="d-2",
    )

    assert batch(root, signalling) == 1
    assert looks == []


def test_a_title_change_wakes_the_reader(root, signalling, looks):
    """The pill's words are the pull request's title, so a retitle is visible too."""
    deliver(root, pull_body(), event="pull_request", delivery="d-1")
    batch(root, signalling)
    looks.clear()

    deliver(
        root,
        pull_body(action="edited", title="Ship it properly", updated_at="2026-09-07T10:30:00Z"),
        event="pull_request",
        delivery="d-2",
    )

    batch(root, signalling)

    assert looks == [1]


def test_a_push_to_the_branch_wakes_the_reader(root, signalling, looks):
    """#2088: a `synchronize` moves the head and nothing else the row carries.

    Without the head commit among the fields a wake is judged on, this write would land and
    be read as changing nothing — leaving a chip claiming the latest commit was reviewed
    until some unrelated event happened to come past.
    """
    deliver(root, pull_body(), event="pull_request", delivery="d-1")
    batch(root, signalling)
    looks.clear()

    deliver(
        root,
        pull_body(
            action="synchronize",
            head_sha="b" * 40,
            updated_at="2026-09-07T10:30:00Z",
        ),
        event="pull_request",
        delivery="d-2",
    )

    batch(root, signalling)

    assert looks == [1]


def test_a_burst_wakes_the_reader_once(root, signalling, looks):
    """A merge lands beside its own review and label events; one sweep covers all."""
    deliver(root, pull_body(), event="pull_request", delivery="d-1")
    deliver(
        root,
        pull_body(number=8, updated_at="2026-09-07T10:01:00Z"),
        event="pull_request",
        delivery="d-2",
    )
    deliver(
        root,
        pull_body(
            action="closed",
            state="closed",
            merged_at="2026-09-07T11:00:00Z",
            updated_at="2026-09-07T11:00:00Z",
        ),
        event="pull_request",
        delivery="d-3",
    )

    assert batch(root, signalling) == 3
    assert looks == [1]


def test_an_older_snapshot_wakes_nobody(root, signalling, looks):
    """It does not win the write, so it cannot have moved anything either."""
    deliver(
        root,
        pull_body(
            action="closed",
            state="closed",
            merged_at="2026-09-07T11:00:00Z",
            updated_at="2026-09-07T11:00:00Z",
        ),
        event="pull_request",
        delivery="d-1",
    )
    batch(root, signalling)
    looks.clear()

    deliver(
        root,
        pull_body(updated_at="2026-09-07T09:00:00Z"),
        event="pull_request",
        delivery="d-2",
    )

    batch(root, signalling)

    assert looks == []


def test_an_issue_event_alone_wakes_nobody(root, signalling, looks):
    """Issue state reaches a row here, but not through the pull-request sweep.

    ``Session.issue_status`` is stamped by a provider scan, not read at render, so
    there is nothing for this callback to freshen and waking it would sweep for
    nothing.
    """
    deliver(root, issue_body(), event="issues", delivery="d-1")

    assert batch(root, signalling) == 1
    assert looks == []


def test_a_failing_subscriber_does_not_lose_the_write(root, cache):
    """The cache is the durable half; a broken reader must not cost it a delivery."""

    def explode() -> None:
        raise RuntimeError("no")

    service = WebhookStatusCacheService(root, cache=cache, on_pull_change=explode)
    deliver(root, pull_body(), event="pull_request", delivery="d-1")

    batch(root, service)

    assert cache.peek(PR_KEY).value is not None


def test_an_issue_row_keeps_githubs_close_time_apart_from_its_last_update():
    """#2219: the front page's Recently finished goes by ``closedAt``; a comment on a closed
    issue moves ``updatedAt`` and must not look like a fresh closing."""
    row = WebhookStatusCacheService._issue_value(
        {
            "title": "Done long ago",
            "state": "closed",
            "state_reason": "completed",
            "closed_at": "2026-09-01T10:00:00Z",
            "updated_at": "2026-09-25T10:00:00Z",
        }
    )
    assert row["closedAt"] == "2026-09-01T10:00:00Z"
    assert row["updatedAt"] == "2026-09-25T10:00:00Z"
    reopened = WebhookStatusCacheService._issue_value(
        {"title": "Back", "state": "open", "closed_at": None, "updated_at": "2026-09-25T10:00:00Z"}
    )
    assert reopened["closedAt"] is None


def test_an_issue_row_carries_its_label_names_when_the_payload_has_them():
    """#2548: a Work is paused by a label, and the deck reads it from this row."""
    base = {"title": "t", "state": "open", "updated_at": "2026-09-25T10:00:00Z"}
    row = WebhookStatusCacheService._issue_value(
        {**base, "labels": [{"name": "agent"}, {"name": "Paused"}, {"id": 3}]}
    )
    assert row["labels"] == ["agent", "Paused"]
    assert "labels" not in WebhookStatusCacheService._issue_value(base)
    # #3731: its first assignee, which decides which of two decks works it.
    value = WebhookStatusCacheService._issue_value
    assert value({**base, "assignee": {"login": "coworker"}})["assignee"] == "coworker"
    assert value({**base, "assignee": None})["assignee"] is None
    assert "assignee" not in value(base)
