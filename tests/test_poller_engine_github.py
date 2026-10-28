"""The GitHub adapter (#180), parsed against payloads recorded from real `gh` output.

Every fixture in tests/fixtures/github/ was captured from live `gh` against this repository, so
these tests check the shapes GitHub actually returns rather than shapes I imagined. The parts
that cannot be recorded (a Project board this login cannot read) follow the reference engine's
production query and are exercised through its documented response shape.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentdeck.poller_engine import github as github_module
from agentdeck.poller_engine import queue, rate_limits
from agentdeck.poller_engine.config import Reachability, Role
from agentdeck.poller_engine.github import (
    BLOCKED_BY_PAGES,
    BOT_MARKER,
    CHATGPT_REVIEW_MARKER,
    TERMINAL_MARKER,
    Gh,
    GitHubAdapter,
)
from agentdeck.poller_engine.ownership import LabelEvent, LabelRemoval
from agentdeck.poller_engine.ports import (
    ArmOutcome,
    CardQuery,
    CommentBody,
    Effect,
    EffectKind,
    LinkedPRs,
    WorktreeCleanup,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github"


def recorded(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_without_a_runtime_the_job_rows_are_read_from_the_queue_file(tmp_path):
    jobs = tmp_path / "jobs.json"
    jobs.write_text('[{"id": "job-1"}, "junk"]')

    assert adapter(jobs_path=jobs).verification_jobs() == [{"id": "job-1"}]
    jobs.write_text("{not json")
    assert adapter(jobs_path=jobs).verification_jobs() is None


class FakeGh(Gh):
    """A `gh` that answers from a script instead of the network, recording what was asked."""

    def __init__(self, answers=None, default=(0, "null", "")):
        super().__init__()
        self.answers = list(answers or [])
        self.default = default
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args):
        self.calls.append(args)
        for match, answer in self.answers:
            if all(part in " ".join(args) for part in match):
                return answer
        return self.default

    def asked(self, *parts: str) -> bool:
        return any(all(part in " ".join(call) for part in parts) for call in self.calls)

    def current_rate_limits(self, *, max_age: float | None = None):
        self.rate_limit_max_age = max_age
        value = self.json("api", "rate_limit")
        resources = value.get("resources") if isinstance(value, dict) else None
        readings = {}
        for name in ("core", "graphql"):
            resource = resources.get(name) if isinstance(resources, dict) else None
            remaining = resource.get("remaining") if isinstance(resource, dict) else None
            if isinstance(remaining, int) and not isinstance(remaining, bool):
                readings[name] = rate_limits.Reading(
                    resource=name,
                    remaining=remaining,
                    source=rate_limits.ESTIMATE_SOURCE,
                )
        return readings


def adapter(api=None, host=None, **kwargs) -> GitHubAdapter:
    defaults = {
        "org": "eerovil",
        "issue_repos": ("agentdeck",),
        "code_repos": ("agentdeck",),
        "project_number": 2,
        "status_names": {Role.IN_PROGRESS: "In Progress"},
    }
    defaults.update(kwargs)
    return GitHubAdapter(api=api or FakeGh(), host=host or FakeGh(), **defaults)


def ok(payload) -> tuple[int, str, str]:
    return (0, json.dumps(payload), "")


def labelled_batch(*items, has_next=False, cursor="next"):
    def event_payload(event):
        if not isinstance(event, dict):
            return event
        return {"createdAt": "2026-08-09T08:00:00Z", **event}

    return {
        "data": {
            # Every issue repository rides one aliased request, so a one-repository
            # sweep answers under ``r0``.
            "r0": {
                "issues": {
                    "nodes": [
                        {
                            "number": number,
                            "timelineItems": {
                                "nodes": [event_payload(event) for event in events],
                                "pageInfo": {"hasPreviousPage": False},
                            },
                        }
                        for number, events in items
                    ],
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                }
            },
            "rateLimit": {"remaining": 4999, "cost": 1},
        }
    }


def labelled_ids(found):
    return {
        key: [event.id if isinstance(event, LabelEvent) else event for event in events]
        for key, events in found.items()
    }


def card_batch(meta=None, *, comments=(), status=None, remaining=4999):
    if meta is None:
        card = None
    else:
        card = {
            **meta,
            "labels": {"nodes": list(meta.get("labels") or [])},
            "comments": {
                "nodes": [
                    {"body": item} if isinstance(item, str) else dict(item)
                    for item in comments
                ]
            },
            "projectItems": {"nodes": [] if status is None else [
                {
                    "project": {"number": 2},
                    "fieldValueByName": {"name": status},
                }
            ]},
        }
    return {"data": {"rateLimit": {"remaining": remaining}, "r0": {"card": card}}}


def test_gh_runner_records_real_headers_without_forwarding_api_trace(
    tmp_path, monkeypatch
):
    cache = tmp_path / "github.sqlite3"
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(cache))
    trace = """ordinary warning
* Request at now
> Authorization: token secret
< HTTP/2.0 200 OK
< X-RateLimit-Limit: 5000
< X-RateLimit-Remaining: 41
< X-RateLimit-Resource: core
* Request took 1ms
"""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, '{"id":20142828}', trace
        ),
    )
    gh = Gh(token="one-token")

    code, _out, err = gh.run("api", "user")

    assert code == 0
    assert err == "ordinary warning\n"
    credential = rate_limits.credential_identity(gh._environment())
    reading = rate_limits.RateLimitStore(cache).read(credential)["core"]
    assert reading.remaining == 41
    assert reading.source == rate_limits.HEADER_SOURCE


def test_gh_runner_remembers_what_github_answered_a_failed_call(tmp_path, monkeypatch):
    """The status behind "unexpected end of JSON input", and whether it was a hiccup (#2714)."""
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(tmp_path / "github.sqlite3"))
    answers = iter(
        [
            (1, "* Request at now\n> POST /graphql HTTP/1.1\n< HTTP/2.0 502 Bad Gateway\n"
                "< X-Accepted-Github-Permissions: issues=write\n* Request took 1ms\n"
                "unexpected end of JSON input\n"),
            (0, ""),
        ]
    )

    def completed(argv, **_kwargs):
        code, trace = next(answers)
        return subprocess.CompletedProcess(argv, code, "{}", trace)

    monkeypatch.setattr(subprocess, "run", completed)
    gh = Gh(token="one-token", authority="app:o")

    code, _out, err = gh.run("issue", "comment", "5", "-R", "o/r", "--body", "x")

    assert code == 1
    assert "lacks a permission" not in err
    assert "POST /graphql -> HTTP 502, empty body" in err
    assert gh.last_failure == "github answered: POST /graphql -> HTTP 502, empty body"
    assert gh.last_failure_transient is True
    assert gh.run("api", "user")[0] == 0
    assert (gh.last_failure, gh.last_failure_transient) == ("", False)


def test_real_quota_path_shares_users_and_keeps_headers_authoritative(
    tmp_path, monkeypatch
):
    cache = tmp_path / "github.sqlite3"
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(cache))
    calls = []

    def completed(argv, **_kwargs):
        calls.append(argv[1:])
        remaining = 41 if argv[1:] == ["api", "user"] else 40
        trace = (
            "* Request at now\n"
            "< HTTP/2.0 200 OK\n"
            "< X-RateLimit-Limit: 5000\n"
            f"< X-RateLimit-Remaining: {remaining}\n"
            "< X-RateLimit-Resource: core\n"
            "* Request took 1ms\n"
        )
        stdout = (
            '{"id":20142828}'
            if argv[1:] == ["api", "user"]
            else '{"resources":{"core":{"remaining":4999},'
            '"graphql":{"remaining":700}}}'
        )
        return subprocess.CompletedProcess(argv, 0, stdout, trace)

    monkeypatch.setattr(subprocess, "run", completed)

    first = Gh(token="token-a").current_rate_limits()
    second = Gh(token="token-b").current_rate_limits()

    assert first["core"].remaining == 40
    assert first["core"].source == rate_limits.HEADER_SOURCE
    assert first["graphql"].source == rate_limits.ESTIMATE_SOURCE
    assert second["graphql"].remaining == 700
    assert calls.count(["api", "rate_limit"]) == 1

    # An App installation token cannot read `GET /user`; its probe is its own repository
    # list, which answers with the same core headers (#3711).
    calls.clear()
    app = Gh(token="token-c", authority="app:o").current_rate_limits()
    assert ["api", "user"] not in calls
    assert ["api", "installation/repositories?per_page=1"] in calls
    assert app["core"].remaining == 40


# --- authorization input ---

def test_a_real_permission_payload_yields_its_level():
    api = FakeGh([(("collaborators",), ok(recorded("permission")))])
    assert adapter(api=api).permission("agentdeck", "eerovil") == "admin"


def test_an_unreadable_permission_is_unknown_not_a_denial():
    api = FakeGh(default=(1, "", "HTTP 403"))
    # The engine turns None into "uncertain" and retries; an adapter must never answer "no" here.
    assert adapter(api=api).permission("agentdeck", "eerovil") is None


def test_an_anonymous_login_is_not_looked_up():
    api = FakeGh()
    assert adapter(api=api).permission("agentdeck", "") is None
    assert api.calls == []


# --- intake ---

def test_a_real_comment_page_becomes_a_scan_result():
    api = FakeGh([(("issues/comments",), ok(recorded("comments")))])
    result = adapter(api=api).scan_comments(since="2026-07-25T07:00:00Z")
    assert result.complete is True
    assert result.scanned_to is not None
    first = result.comments[0]
    # The engine needs the id, the body, the author, the association and the card it belongs to.
    assert first["id"] == 5077455835
    assert first["author_association"] == "OWNER"
    assert first["user"]["login"] == "eerovil"
    assert first["issue_url"].endswith("/issues/115")
    assert first["repo"] == "agentdeck"


def test_a_repository_that_cannot_be_listed_makes_the_scan_incomplete():
    api = FakeGh(default=(1, "", "HTTP 502"))
    result = adapter(api=api, issue_repos=("agentdeck", "other")).scan_comments(since="x")
    # Incomplete, not empty: the engine holds its cursor so nothing is skipped.
    assert result.complete is False
    assert result.comments == []


def test_one_broken_repository_does_not_discard_the_others():
    api = FakeGh([(("repos/eerovil/agentdeck/issues/comments",), ok(recorded("comments")))],
                 default=(1, "", "boom"))
    result = adapter(api=api, issue_repos=("agentdeck", "broken")).scan_comments(since="x")
    assert result.complete is False
    assert len(result.comments) == 3


def _pull_request_comment(cid=9, *, repo="agentdeck", number=12, body="/merge review"):
    return {
        "id": cid,
        "body": body,
        "user": {"login": "eerovil", "type": "User"},
        "author_association": "OWNER",
        "issue_url": f"https://api.github.com/repos/eerovil/{repo}/issues/{number}",
        "html_url": f"https://github.com/eerovil/{repo}/pull/{number}#issuecomment-{cid}",
        "created_at": "2026-08-12T08:00:00Z",
    }


def _closing_issues(*refs, has_next=False):
    return {
        "data": {
            "rateLimit": {"remaining": 4998, "resetAt": "2026-08-12T09:00:00Z", "cost": 1},
            "repository": {
                "pullRequest": {
                    "closingIssuesReferences": {
                        "nodes": [
                            {
                                "number": number,
                                "repository": {
                                    "name": repo,
                                    "owner": {"login": owner},
                                },
                            }
                            for owner, repo, number in refs
                        ],
                        "pageInfo": {"hasNextPage": has_next},
                    }
                }
            },
        }
    }


def test_a_pull_request_command_is_routed_to_its_closing_issue():
    api = FakeGh(
        [
            (("issues/comments",), ok([_pull_request_comment()])),
            (("closingIssuesReferences",), ok(_closing_issues(("eerovil", "agentdeck", 967)))),
        ]
    )

    result = adapter(api=api).scan_comments(since="2026-08-12T07:00:00Z")

    assert result.complete is True
    assert result.comments == [
        {
            **_pull_request_comment(),
            "repo": "agentdeck",
            "issue_url": "https://api.github.com/repos/eerovil/agentdeck/issues/967",
            "pull_request_repo": "agentdeck",
            "pull_request_number": 12,
        }
    ]
    query = next(arg for call in api.calls for arg in call if "closingIssuesReferences" in arg)
    assert query.count("{") == query.count("}"), "the live GraphQL query must be balanced"


def test_pull_request_commands_are_scanned_from_a_separate_code_repository():
    api = FakeGh(
        [
            (("repos/eerovil/tickets/issues/comments",), ok([])),
            (("repos/eerovil/code/issues/comments",), ok([
                _pull_request_comment(repo="code")
            ])),
            (("closingIssuesReferences",), ok(_closing_issues(("eerovil", "tickets", 5)))),
        ]
    )

    result = adapter(
        api=api, issue_repos=("tickets",), code_repos=("code",)
    ).scan_comments(since="2026-08-12T07:00:00Z")

    assert result.complete is True
    assert result.comments[0]["repo"] == "tickets"
    assert result.comments[0]["author_association"] is None
    assert result.comments[0]["issue_url"].endswith("/tickets/issues/5")
    assert len([call for call in api.calls if "issues/comments" in " ".join(call)]) == 2


def test_an_unreadable_pull_request_link_holds_the_scan_cursor():
    api = FakeGh(
        [(("issues/comments",), ok([_pull_request_comment()]))],
        default=(1, "", "HTTP 502"),
    )

    result = adapter(api=api).scan_comments(since="2026-08-12T07:00:00Z")

    assert result.complete is False
    assert result.comments == []


def test_one_pull_request_command_never_targets_several_closing_issues():
    api = FakeGh(
        [
            (("issues/comments",), ok([_pull_request_comment()])),
            (("closingIssuesReferences",), ok(_closing_issues(
                ("eerovil", "agentdeck", 967),
                ("eerovil", "agentdeck", 968),
            ))),
        ]
    )

    result = adapter(api=api).scan_comments(since="2026-08-12T07:00:00Z")

    assert result.complete is True
    assert result.comments == [
        {
            **_pull_request_comment(),
            "repo": "agentdeck",
            "pull_request_repo": "agentdeck",
            "pull_request_number": 12,
        }
    ]


def test_an_unlinked_command_in_a_code_only_repo_does_not_invent_an_issue_card():
    api = FakeGh(
        [
            (("repos/eerovil/tickets/issues/comments",), ok([])),
            (("repos/eerovil/code/issues/comments",), ok([
                _pull_request_comment(repo="code")
            ])),
            (("closingIssuesReferences",), ok(_closing_issues())),
        ]
    )

    result = adapter(
        api=api, issue_repos=("tickets",), code_repos=("code",)
    ).scan_comments(since="2026-08-12T07:00:00Z")

    assert result.complete is True
    assert result.comments == []


def test_an_ordinary_pull_request_comment_costs_no_link_lookup():
    ordinary = _pull_request_comment(body="Reviewer notes only")
    api = FakeGh([(("issues/comments",), ok([ordinary]))])

    result = adapter(api=api).scan_comments(since="2026-08-12T07:00:00Z")

    assert result.complete is True
    assert result.comments == [{**ordinary, "repo": "agentdeck"}]
    assert not api.asked("closingIssuesReferences")


def test_a_pull_request_link_is_read_once_for_several_recent_comments():
    comments = [_pull_request_comment(9), _pull_request_comment(10, body="/claude continue")]
    api = FakeGh(
        [
            (("issues/comments",), ok(comments)),
            (("closingIssuesReferences",), ok(_closing_issues(("eerovil", "agentdeck", 967)))),
        ]
    )

    result = adapter(api=api).scan_comments(since="2026-08-12T07:00:00Z")

    assert len(result.comments) == 2
    assert all(comment["issue_url"].endswith("/issues/967") for comment in result.comments)
    assert len([call for call in api.calls if "closingIssuesReferences" in " ".join(call)]) == 1


# --- the live view of a card ---

def test_card_snapshots_are_split_into_bounded_graphql_batches():
    meta = {"state": "OPEN", "labels": [], "updatedAt": None}
    api = FakeGh(default=ok(card_batch(meta)))
    cards = {
        f"agentdeck#{number}": CardQuery("agentdeck", number)
        for number in range(1, 22)
    }

    result = adapter(api=api).snapshot_cards(cards)

    calls = [call for call in api.calls if "graphql" in call]
    assert len(calls) == 2
    assert "r19:repository" in " ".join(calls[0])
    assert "r20:repository" not in " ".join(calls[0])
    assert set(result.cards) == {"agentdeck#1", "agentdeck#21"}


def test_one_snapshot_batch_returns_every_requested_card_without_followup_reads():
    meta = {"state": "OPEN", "title": "First", "labels": [], "updatedAt": None}
    payload = card_batch(meta, status="In Progress")
    second = dict(payload["data"]["r0"]["card"])
    second["title"] = "Second"
    payload["data"]["r1"] = {"card": second}
    api = FakeGh([(("graphql",), ok(payload))])

    result = adapter(api=api).snapshot_cards(
        {
            "agentdeck#1": CardQuery("agentdeck", 1),
            "agentdeck#2": CardQuery("agentdeck", 2),
        }
    )

    assert set(result.cards) == {"agentdeck#1", "agentdeck#2"}
    assert result.cards["agentdeck#1"].title == "First"
    assert result.cards["agentdeck#2"].title == "Second"
    assert len([call for call in api.calls if "graphql" in call]) == 1
    assert not api.asked("issue view")
    assert not api.asked("project item-list")


def test_a_missing_graphql_node_is_unknown_not_closed():
    api = FakeGh([(("graphql",), ok(card_batch(None)))])
    result = adapter(api=api).snapshot_cards({"agentdeck#9": CardQuery("agentdeck", 9)})
    assert result.cards == {}
    assert result.deferred is False


def test_one_failed_snapshot_batch_does_not_discard_a_later_successful_batch():
    meta = {"state": "OPEN", "labels": [], "updatedAt": None}
    api = FakeGh(
        [(('number0=21',), ok(card_batch(meta)))],
        default=(1, "", "temporary GraphQL failure"),
    )
    cards = {
        f"agentdeck#{number}": CardQuery("agentdeck", number)
        for number in range(1, 22)
    }

    result = adapter(api=api).snapshot_cards(cards)

    assert set(result.cards) == {"agentdeck#21"}
    assert len([call for call in api.calls if "graphql" in call]) == 2


def test_low_graphql_budget_defers_snapshots_without_spending_graphql_points():
    api = FakeGh(
        [(('rate_limit',), ok({"resources": {"graphql": {"remaining": 200}}}))],
        default=ok(card_batch({"state": "OPEN", "labels": [], "updatedAt": None})),
    )

    result = adapter(api=api, graphql_reserve=200).snapshot_cards(
        {"agentdeck#1": CardQuery("agentdeck", 1)}
    )

    assert result.deferred is True
    assert not any("graphql" in call for call in api.calls)


def test_one_rate_limit_probe_guards_both_live_query_budgets():
    api = FakeGh(
        [
            (
                ("rate_limit",),
                ok(
                    {
                        "resources": {
                            "core": {"remaining": 499},
                            "graphql": {"remaining": 5000},
                        }
                    }
                ),
            )
        ]
    )
    guarded = adapter(api=api, core_reserve=500, graphql_reserve=200)

    assert guarded.live_queries_halt() == (
        "core-budget: untrusted /rate_limit cold-start estimate"
    )
    assert guarded.live_queries_halt() == (
        "core-budget: untrusted /rate_limit cold-start estimate"
    )
    assert [call for call in api.calls if "rate_limit" in call] == [("api", "rate_limit")]


def test_healthy_rate_limit_buckets_permit_live_queries():
    api = FakeGh(
        [
            (
                ("rate_limit",),
                ok(
                    {
                        "resources": {
                            "core": {"remaining": 5000},
                            "graphql": {"remaining": 5000},
                        }
                    }
                ),
            )
        ]
    )

    assert adapter(api=api).live_queries_halt() is None


def test_core_budget_at_the_configured_floor_permits_live_queries():
    api = FakeGh(
        [
            (
                ("rate_limit",),
                ok(
                    {
                        "resources": {
                            "core": {"remaining": 500},
                            "graphql": {"remaining": 5000},
                        }
                    }
                ),
            )
        ]
    )

    assert adapter(api=api, core_reserve=500).live_queries_halt() is None


def test_low_graphql_budget_has_its_own_halt_reason():
    api = FakeGh(
        [
            (
                ("rate_limit",),
                ok(
                    {
                        "resources": {
                            "core": {"remaining": 5000},
                            "graphql": {"remaining": 200},
                        }
                    }
                ),
            )
        ]
    )

    assert adapter(api=api).live_queries_halt() == (
        "graphql-budget: untrusted /rate_limit cold-start estimate"
    )


def test_failed_rate_limit_probe_never_blocks_the_live_wake():
    api = FakeGh(default=(1, "", "temporary GitHub failure"))

    assert adapter(api=api).live_queries_halt() is None


def test_a_real_issue_view_becomes_a_card_snapshot():
    api = FakeGh([(("graphql",), ok(card_batch(recorded("issue_view"))))])
    snapshot = adapter(api=api).snapshot("agentdeck", 105)
    assert snapshot is not None
    assert snapshot.open is True
    assert snapshot.owned is False           # this card carries wayfinder:map, not claude
    assert snapshot.quiet_seconds > 0
    assert snapshot.terminal_marker is None


def test_a_snapshot_carries_the_title_and_body_the_placeholder_repair_reads():
    """Recorded from #338 itself: the template's title with the ask typed after it, no body."""
    api = FakeGh([(("graphql",), ok(card_batch(recorded("issue_view_placeholder"))))])
    snapshot = adapter(api=api).snapshot("agentdeck", 338)
    assert snapshot is not None
    assert snapshot.title.startswith("Requested changes Update github issue title")
    assert snapshot.body == ""


def test_a_snapshot_carries_the_human_comments_the_authoritative_bundle_fences():
    """#1473: a requester who asks for something in prose rather than a `/claude` command has
    still asked for it, so the bundle has to be able to see and fence that comment. The
    engine's own comments are not instructions and never enter it."""
    api = FakeGh([(("graphql",), ok(card_batch(
        {"state": "OPEN", "title": "T", "body": "B", "updatedAt": "2026-09-04T10:00:00Z"},
        comments=[
            {
                "id": "IC_1",
                "createdAt": "2026-09-04T10:01:00Z",
                "author": {"login": "eero"},
                "body": "actually, keep the old header too",
            },
            {
                "id": "IC_2",
                "createdAt": "2026-09-04T10:02:00Z",
                "author": {"login": "eero"},
                "body": "Plan: do the thing\n\n<!-- claude-bot -->",
            },
        ],
    )))])

    snapshot = adapter(api=api).snapshot("agentdeck", 1473)

    assert snapshot is not None
    assert snapshot.comments == (
        {
            "id": "IC_1",
            "created_at": "2026-09-04T10:01:00Z",
            "author": "eero",
            "body": "actually, keep the old header too",
        },
    )
    assert snapshot.comments_omitted == 0


def test_a_conversation_beyond_the_bundle_bound_is_counted_not_quietly_shortened():
    """A conversation this engine cannot carry exactly is one it must not paraphrase, so the
    count travels with the snapshot and makes the bundle unprovable downstream."""
    total = github_module.MAX_REQUESTER_COMMENTS + 3
    api = FakeGh([(("graphql",), ok(card_batch(
        {"state": "OPEN", "title": "T", "body": "B", "updatedAt": "2026-09-04T10:00:00Z"},
        comments=[
            {"id": f"IC_{index}", "createdAt": "2026-09-04T10:00:00Z",
             "author": {"login": "eero"}, "body": f"comment {index}"}
            for index in range(total)
        ],
    )))])

    snapshot = adapter(api=api).snapshot("agentdeck", 1473)

    assert snapshot is not None
    assert len(snapshot.comments) == github_module.MAX_REQUESTER_COMMENTS
    assert snapshot.comments_omitted == 3
    # Newest kept: the instruction most likely to still be unresolved.
    assert snapshot.comments[-1]["body"] == f"comment {total - 1}"


@pytest.mark.parametrize("total, kept, omitted", [(30, 30, 0), (31, 30, 1)])
def test_the_bundle_carries_thirty_requester_comments(total, kept, omitted):
    """#2416: deck-relayed chat messages count as requester comments, so the bound is 30."""
    api = FakeGh([(("graphql",), ok(card_batch(
        {"state": "OPEN", "title": "T", "body": "B", "updatedAt": "2026-09-04T10:00:00Z"},
        comments=[
            {"id": f"IC_{index}", "createdAt": "2026-09-04T10:00:00Z",
             "author": {"login": "eero"}, "body": f"comment {index}"}
            for index in range(total)
        ],
    )))])

    snapshot = adapter(api=api).snapshot("agentdeck", 1473)

    assert snapshot is not None
    assert (len(snapshot.comments), snapshot.comments_omitted) == (kept, omitted)


def test_the_snapshot_query_reads_comment_identity_in_the_same_call():
    """The identity rides along with the bodies the query already asks for, so fencing an
    ordinary comment costs no second request."""
    query, _ = adapter()._snapshot_query([("agentdeck#1", CardQuery("agentdeck", 1))])

    assert "comments(last:100){nodes{id createdAt body author{login}}}" in query


def test_an_unreadable_card_is_unknown_rather_than_closed():
    api = FakeGh(default=(1, "", "HTTP 404"))
    assert adapter(api=api).snapshot("agentdeck", 999) is None


# --- dependencies: what is still holding a card (#640) ---

def page_info(cursor=None):
    """``cursor=None`` is the whole list; a string (even empty) says there is more."""
    return {"hasNextPage": cursor is not None, "endCursor": cursor}


def blocked_by(*nodes, cursor=None, **meta):
    """A card payload carrying GitHub's own dependency list."""
    return card_batch({"state": "OPEN", "labels": [], "updatedAt": None, **meta,
                       "blockedBy": {"nodes": list(nodes), "pageInfo": page_info(cursor)}})


def blocked_by_page(*nodes, cursor=None):
    """A follow-up dependency page, answered on its own repository/issue query."""
    return {"data": {"rateLimit": {"remaining": 4999}, "repository": {"card": {
        "blockedBy": {"nodes": list(nodes), "pageInfo": page_info(cursor)}}}}}


def dependency(number, state="OPEN", repo="agentdeck", owner="eerovil"):
    return {"number": number, "state": state,
            "repository": {"name": repo, "owner": {"login": owner}}}


def test_native_dependencies_are_read_inside_the_card_query():
    api = FakeGh([(("graphql",), ok(blocked_by(dependency(635))))])

    snapshot = adapter(api=api).snapshot("agentdeck", 640)

    assert snapshot is not None and snapshot.blockers == ("agentdeck#635",)
    # No query of its own: the dependency rides in the snapshot every wake already takes.
    assert len([call for call in api.calls if "graphql" in call]) == 1
    assert api.asked("blockedBy")


def test_a_closed_dependency_no_longer_blocks_although_github_still_lists_it():
    api = FakeGh([(("graphql",), ok(blocked_by(
        dependency(635, state="CLOSED"), dependency(636),
    )))])

    assert adapter(api=api).snapshot("agentdeck", 640).blockers == ("agentdeck#636",)


def test_a_dependency_in_another_owner_keeps_its_owner():
    api = FakeGh([(("graphql",), ok(blocked_by(
        dependency(4, repo="thing", owner="elsewhere"),
    )))])

    assert adapter(api=api).snapshot("agentdeck", 640).blockers == ("elsewhere/thing#4",)


def test_a_github_without_issue_dependencies_degrades_once_and_keeps_every_card():
    """The field is free because it rides in the card query — which is also the risk: a GitHub
    that retired it would fail the whole query and leave every card unreadable. So a failure
    naming the field is answered by asking again without it, once, and never asking again."""
    meta = {"state": "OPEN", "title": "Live", "labels": [], "updatedAt": None}
    api = FakeGh(
        [(("blockedBy",), (1, "", "GraphQL: Field 'blockedBy' doesn't exist on type 'Issue'"))],
        default=ok(card_batch(meta)),
    )
    port = adapter(api=api)

    first = port.snapshot_cards({"agentdeck#1": CardQuery("agentdeck", 1)})
    assert first.cards["agentdeck#1"].title == "Live"
    assert first.cards["agentdeck#1"].blockers == ()

    second = port.snapshot_cards({"agentdeck#2": CardQuery("agentdeck", 2)})
    assert second.cards["agentdeck#2"].title == "Live"
    # Three calls in total: the refused one, its replacement, and a second wake that never asks.
    graphql = [" ".join(call) for call in api.calls if "graphql" in call]
    assert len(graphql) == 3
    assert [("blockedBy" in call) for call in graphql] == [True, False, False]


def test_an_ordinary_query_failure_is_not_retried_as_a_missing_field():
    api = FakeGh(default=(1, "", "HTTP 502"))

    result = adapter(api=api).snapshot_cards({"agentdeck#1": CardQuery("agentdeck", 1)})

    assert result.cards == {}
    assert len([call for call in api.calls if "graphql" in call]) == 1


def test_an_open_dependency_past_the_first_page_still_holds_the_card():
    """The whole point of #684: fifty *closed* dependencies fit a page and prove nothing."""
    first = blocked_by(dependency(635, state="CLOSED"), cursor="Y3Vyc29yOjUw")
    api = FakeGh(
        [(("after=Y3Vyc29yOjUw",), ok(blocked_by_page(dependency(700))))],
        default=ok(first),
    )

    snapshot = adapter(api=api).snapshot("agentdeck", 640)

    assert snapshot.blockers == ("agentdeck#700",)
    assert api.asked("blockedBy(first:50,after:$after)")


def test_dependency_pages_are_followed_until_the_list_ends():
    api = FakeGh(
        [
            (("after=p1",), ok(blocked_by_page(dependency(701), cursor="p2"))),
            (("after=p2",), ok(blocked_by_page(dependency(702, state="CLOSED")))),
        ],
        default=ok(blocked_by(dependency(700), cursor="p1")),
    )

    snapshot = adapter(api=api).snapshot("agentdeck", 640)

    assert snapshot.blockers == ("agentdeck#700", "agentdeck#701")
    # The card query plus one query per further page, and no page after the list ended.
    assert len([call for call in api.calls if "graphql" in call]) == 3


def test_a_dependency_page_that_cannot_be_read_holds_the_card():
    api = FakeGh(
        [(("after=p1",), (1, "", "HTTP 502"))],
        default=ok(blocked_by(dependency(635, state="CLOSED"), cursor="p1")),
    )

    # Unknown is not "nothing is blocking": the card waits on a dependency nobody can name.
    assert adapter(api=api).snapshot("agentdeck", 640).blockers == ("unread dependencies",)


def test_a_truncated_page_that_names_no_cursor_holds_the_card_without_asking():
    api = FakeGh(default=ok(blocked_by(dependency(635, state="CLOSED"), cursor="")))

    assert adapter(api=api).snapshot("agentdeck", 640).blockers == ("unread dependencies",)
    # Nothing to continue from, so nothing is asked — the hold is the whole answer.
    assert len([call for call in api.calls if "graphql" in call]) == 1


def test_a_card_that_never_stops_paginating_is_held_at_the_page_cap():
    api = FakeGh(default=ok(blocked_by(dependency(635, state="CLOSED"), cursor="p")))
    # Every follow-up page also claims more, which is not a card any project has; it is the
    # shape a bounded reader has to survive without either looping or starting the card.
    api.answers = [(("after=p",), ok(blocked_by_page(dependency(700), cursor="p")))]

    snapshot = adapter(api=api).snapshot("agentdeck", 640)

    assert snapshot.blockers == ("agentdeck#700", "unread dependencies")
    assert len([call for call in api.calls if "graphql" in call]) == 1 + BLOCKED_BY_PAGES


def test_a_complete_first_page_costs_no_pagination():
    api = FakeGh(default=ok(blocked_by(dependency(635))))

    assert adapter(api=api).snapshot("agentdeck", 640).blockers == ("agentdeck#635",)
    assert len([call for call in api.calls if "graphql" in call]) == 1


def test_a_written_declaration_is_resolved_against_the_blockers_it_names():
    cards = card_batch({"state": "OPEN", "labels": [], "updatedAt": None,
                        "body": "Blocked by: #635, #636"})
    states = {"data": {"rateLimit": {"remaining": 4999},
                       "b0": {"card": {"state": "CLOSED"}},
                       "b1": {"card": {"state": "OPEN"}}}}
    api = FakeGh([(("b1:repository",), ok(states))], default=ok(cards))

    snapshot = adapter(api=api).snapshot("agentdeck", 640)

    # Only what is still open holds the card; the closed one is out of the way.
    assert snapshot.blockers == ("agentdeck#636",)


def test_a_written_declaration_nobody_can_resolve_keeps_blocking():
    cards = card_batch({"state": "OPEN", "labels": [], "updatedAt": None,
                        "body": "Blocked by: #9999"})
    api = FakeGh([(("b0:repository",), (1, "", "GraphQL: Could not resolve to an Issue"))],
                 default=ok(cards))

    # Not proven closed is not proven fine: a typo waits visibly instead of starting work.
    assert adapter(api=api).snapshot("agentdeck", 640).blockers == ("agentdeck#9999",)


def test_one_unresolvable_reference_does_not_hold_the_cards_around_it():
    cards = card_batch({"state": "OPEN", "labels": [], "updatedAt": None,
                        "body": "Blocked by: #9999, #635"})
    # GitHub answers an error *and* partial data; discarding it would hold #635 too.
    # The refs are resolved in sorted order, so `b0` is #635 and `b1` is the one that is not real.
    partial = {"errors": [{"message": "Could not resolve to an Issue"}],
               "data": {"b0": {"card": {"state": "CLOSED"}}, "b1": {"card": None}}}
    api = FakeGh([(("b1:repository",), (1, json.dumps(partial), "GraphQL: could not resolve"))],
                 default=ok(cards))

    assert adapter(api=api).snapshot("agentdeck", 640).blockers == ("agentdeck#9999",)


def test_a_card_with_no_dependency_costs_no_extra_query():
    api = FakeGh(default=ok(card_batch(
        {"state": "OPEN", "labels": [], "updatedAt": None, "body": "Just the work."}
    )))

    snapshot = adapter(api=api).snapshot("agentdeck", 640)
    assert snapshot.blockers == () and snapshot.dependencies == ()
    assert len([call for call in api.calls if "graphql" in call]) == 1


# --- the whole declared list, closed entries included (#1588) ---
#
# `blockers` alone cannot tell a card whose dependency has landed from a card nobody ever
# declared a dependency for: both report an empty tuple. GitHub's answer holds the difference
# already, so keeping it costs no query and is what lets a blocked card resume.

def test_a_closed_dependency_is_still_a_dependency_the_card_declares():
    api = FakeGh([(("graphql",), ok(blocked_by(
        dependency(635, state="CLOSED"), dependency(636),
    )))])

    snapshot = adapter(api=api).snapshot("agentdeck", 640)

    assert snapshot.blockers == ("agentdeck#636",)
    assert snapshot.dependencies == ("agentdeck#635", "agentdeck#636")


def test_a_card_whose_every_dependency_landed_still_says_it_had_them():
    api = FakeGh([(("graphql",), ok(blocked_by(
        dependency(635, state="CLOSED"), dependency(636, state="MERGED"),
    )))])

    snapshot = adapter(api=api).snapshot("agentdeck", 640)

    assert snapshot.blockers == ()
    assert snapshot.dependencies == ("agentdeck#635", "agentdeck#636")


def test_dependencies_past_the_first_page_join_the_declared_list_too():
    api = FakeGh(
        [(("after=p1",), ok(blocked_by_page(dependency(701, state="CLOSED"))))],
        default=ok(blocked_by(dependency(700, state="CLOSED"), cursor="p1")),
    )

    snapshot = adapter(api=api).snapshot("agentdeck", 640)

    assert snapshot.blockers == ()
    assert snapshot.dependencies == ("agentdeck#700", "agentdeck#701")


def test_a_page_nobody_could_read_leaves_the_unnameable_hold_in_both_lists():
    api = FakeGh(
        [(("after=p1",), (1, "", "HTTP 502"))],
        default=ok(blocked_by(dependency(635, state="CLOSED"), cursor="p1")),
    )

    snapshot = adapter(api=api).snapshot("agentdeck", 640)

    # Still holding, so nothing may resume off this answer — and the declared list says why.
    assert snapshot.blockers == ("unread dependencies",)
    assert snapshot.dependencies == ("agentdeck#635", "unread dependencies")


def test_a_written_declaration_that_closed_is_still_declared():
    cards = card_batch({"state": "OPEN", "labels": [], "updatedAt": None,
                        "body": "Blocked by: #635, #636"})
    states = {"data": {"rateLimit": {"remaining": 4999},
                       "b0": {"card": {"state": "CLOSED"}},
                       "b1": {"card": {"state": "MERGED"}}}}
    api = FakeGh([(("b1:repository",), ok(states))], default=ok(cards))

    snapshot = adapter(api=api).snapshot("agentdeck", 640)

    assert snapshot.blockers == ()
    assert snapshot.dependencies == ("agentdeck#635", "agentdeck#636")


def test_a_terminal_marker_counts_only_for_the_current_lease():
    body = "Done.\n<!-- claude-terminal generation=gen1 status=In_review -->"
    payload = card_batch({"state": "OPEN", "labels": [], "updatedAt": None}, comments=[body])
    api = FakeGh([(("graphql",), ok(payload))])
    assert adapter(api=api).snapshot("a", 1, generation="gen1").terminal_marker == "In review"
    # A marker from a superseded worker is not this card's news.
    assert adapter(api=api).snapshot("a", 1, generation="gen2").terminal_marker is None
    assert adapter(api=api).snapshot("a", 1).terminal_marker is None


def test_a_review_verdict_rides_the_same_readable_snapshot_as_the_card():
    body = "Looks good\n<!-- claude-merge-review generation=gen1 verdict=pass -->"
    payload = card_batch({"state": "OPEN", "labels": [], "updatedAt": None}, comments=[body])
    api = FakeGh([(("graphql",), ok(payload))])

    assert adapter(api=api).snapshot("a", 1, generation="gen1").review_verdict == "pass"
    assert adapter(api=api).snapshot("a", 1, generation="gen2").review_verdict is None


def test_a_terminal_marker_is_reported_as_a_role_whatever_the_worker_capitalized():
    """Workers are told to post `status=in_review`, which parses to "in review" — and the board's
    option ids are keyed by role ("In review"), so the recovered status write looked up a key
    that was not there and failed on every wake without logging anything (#279).

    The marker is a status the outside world spelled, exactly like a board status, so it is
    normalized in the same place and by the same rule.
    """
    def marker_of(status: str) -> str | None:
        api = FakeGh(
            [(("graphql",), ok(card_batch(
                {"state": "OPEN", "labels": [], "updatedAt": None},
                comments=[f"done\n<!-- claude-terminal generation=g status={status} -->"],
            )))]
        )
        return adapter(api=api).snapshot("a", 1, generation="g").terminal_marker

    assert marker_of("in_review") == Role.IN_REVIEW.value
    assert marker_of("needs_more_info") == Role.NEEDS_INFO.value
    assert marker_of("blocked") == Role.BLOCKED.value
    # The board's own spelling still wins over the role name for a mapped column.
    assert marker_of("in_progress") == Role.IN_PROGRESS.value
    # #1410's ending: Done, with nothing to merge. Every other token happens to spell its column
    # once the underscores become spaces, which is why normalizing markers as column names looked
    # complete; this one names no column anywhere, so it survived as the raw word "resolved",
    # reached a status write no binding had an option for, and was retried on every wake forever
    # — including the promotion canary's, which is how it turned into a red deploy (#1939).
    assert marker_of("resolved") == Role.DONE.value
    assert marker_of("human_review_pending") == Role.HUMAN_REVIEW_PENDING.value
    # A map's rest, which recovery has to read back as Todo rather than as the Done its
    # ``resolved`` report literally said.
    assert marker_of("todo") == Role.TODO.value


def test_every_ending_a_finish_can_report_reads_back_as_the_role_it_wrote():
    """The two halves of one contract, pinned together (#1939).

    ``worker_control.finish`` writes the ending as the worker's own token and moves the board to
    the role that token means; this adapter reads the token back when the board write is the part
    that was lost. They live in different files and were written against different vocabularies,
    so they could disagree — and for ``resolved`` they did, silently, because the disagreement is
    only visible on the recovery path, where it became a status write no board could accept.
    """
    from agentdeck.poller_engine.worker_control import _FINISH_STATUSES

    for token, canonical in _FINISH_STATUSES.items():
        api = FakeGh(
            [(("graphql",), ok(card_batch(
                {"state": "OPEN", "labels": [], "updatedAt": None},
                comments=[f"done\n<!-- claude-terminal generation=g status={token} -->"],
            )))]
        )
        read_back = adapter(api=api).snapshot("a", 1, generation="g").terminal_marker
        assert read_back == canonical, f"finish {token} wrote {canonical}, read back {read_back}"


def test_a_marker_naming_no_ending_at_all_is_not_a_status():
    """The failure mode #1939 was, one layer up: an unrecognised word must not become a write.

    `_role_of` deliberately hands back a column spelling it has not been taught, because a board
    may name its columns anything. A marker is the opposite kind of thing — a closed vocabulary
    the engine itself writes — so a word outside it names no status, and answering it would put
    that word into a status write that no binding can satisfy and every wake would retry.
    """
    api = FakeGh(
        [(("graphql",), ok(card_batch(
            {"state": "OPEN", "labels": [], "updatedAt": None},
            comments=["done\n<!-- claude-terminal generation=g status=teapot -->"],
        )))]
    )
    assert adapter(api=api).snapshot("a", 1, generation="g").terminal_marker is None


def test_a_stop_ack_comment_is_not_proof_the_worker_stopped():
    """#2159: a model saying it stopped proves nothing about its process. Only the Runtime's
    answer, recorded by the host, releases a stop."""
    body = "<!-- claude-stop-ack generation=old -->"
    api = FakeGh([(("graphql",), ok(card_batch(
        {"state": "OPEN", "labels": [], "updatedAt": None}, comments=[body]
    )))])
    assert adapter(api=api).snapshot("a", 1, stop_generation="old").execution_stopped is False


def test_quiet_seconds_is_the_issues_own_silence_and_nothing_more():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 8, 16, 12, tzinfo=UTC)
    api = FakeGh([(("graphql",), ok(card_batch(
        {
            "state": "OPEN",
            "labels": [],
            "updatedAt": (now - timedelta(hours=3)).isoformat(),
        },
        comments=[],
    )))])

    snapshot = adapter(api=api, now=now).snapshot(
        "a",
        1,
        stop_generation="old",
        stop_requested_at=(now - timedelta(seconds=30)).isoformat(),
    )

    # Reported only; it releases nothing (#2159), so the stop's own age no longer fences it.
    assert snapshot.quiet_seconds == 3 * 3600


class FakeRuntime:
    """The Runtime's execution routes, answered from a table instead of a socket (#2200)."""

    def __init__(self, monkeypatch, outcomes=None, *, stop=None):
        self.outcomes = dict(outcomes or {})
        self.stop_answer = stop
        self.gets: list[str] = []
        self.stops: list[dict] = []
        monkeypatch.setattr(github_module, "_runtime_get", self._get)
        monkeypatch.setattr(github_module, "runtime_post", self._post)

    def _get(self, socket_path, path, **_kwargs):
        from urllib.parse import parse_qs, urlsplit

        assert socket_path == "/run/rt.sock"
        self.gets.append(path)
        parts = urlsplit(path)
        assert parts.path == "/executions/outcome"
        query = {key: value[0] for key, value in parse_qs(parts.query).items()}
        return self.outcomes.get((query["work_id"], query["run_id"]))

    def _post(self, socket_path, path, body, **_kwargs):
        assert (socket_path, path) == ("/run/rt.sock", "/executions/stop")
        self.stops.append(body)
        return (200, self.stop_answer) if self.stop_answer is not None else (0, None)


_WORK_A1 = "github_issue:eerovil/a#1"


def _outcome(run_id="gen-1", state="ended", reason="result", **extra):
    return {
        "work_id": _WORK_A1,
        "run_id": run_id,
        "execution_state": state,
        "reason_code": reason,
        "provider": "claude_code",
        "account": "main",
        **extra,
    }


def _card_snapshot(**query):
    api = FakeGh([(("graphql",), ok(card_batch(
        {"state": "OPEN", "labels": [], "updatedAt": None}, comments=[]
    )))])
    return adapter(api=api, runtime_socket="/run/rt.sock").snapshot("a", 1, **query)


def test_a_stop_is_released_by_the_runtimes_outcome_for_exactly_the_stopped_run(monkeypatch):
    """#2159, #2200: the Runtime's own outcome is the proof — a stop was asked of that Run and
    nothing in it can still write. No receipt, and no other Run of the card, stands in for it."""
    stopped = _outcome("captured", "stopped", "poller_stop", stop_requested_at=1.0)
    FakeRuntime(monkeypatch, {(_WORK_A1, "captured"): stopped})
    assert _card_snapshot(stop_generation="captured").execution_stopped is True

    for state, reason in (("ended", "result"), ("died", "process_gone"), ("not_started", "")):
        # Over by itself, and the Runtime has recorded the stop's ask: nothing can resume it.
        FakeRuntime(monkeypatch, {
            (_WORK_A1, "captured"): _outcome("captured", state, reason, stop_requested_at=1.0)
        })
        assert _card_snapshot(stop_generation="captured").execution_stopped is True, state


def test_a_stop_the_runtime_has_not_recorded_or_not_finished_keeps_waiting(monkeypatch):
    for outcome in (
        # Ended by itself, but nobody has asked the Runtime to stop it yet: a job notice could
        # still resume its worker.
        _outcome("captured", "ended", "result"),
        _outcome("captured", "running", "turn_active", stop_requested_at=1.0),
        _outcome("captured", "abandoned", "no_activity", stop_requested_at=1.0),
        _outcome("captured", "uncertain", "runtime_restarted", stop_requested_at=1.0),
        None,
    ):
        FakeRuntime(monkeypatch, {(_WORK_A1, "captured"): outcome})
        assert _card_snapshot(stop_generation="captured").execution_stopped is False, outcome


def test_another_runs_stop_cannot_release_this_one(monkeypatch):
    """#935: an older Run's proven stop says nothing about the Run this stop waits on."""
    old = _outcome("old", "stopped", "poller_stop", stop_requested_at=1.0)
    runtime = FakeRuntime(monkeypatch, {(_WORK_A1, "old"): old})

    assert _card_snapshot(stop_generation="captured").execution_stopped is False
    assert runtime.gets == [
        "/executions/outcome?work_id=github_issue%3Aeerovil%2Fa%231&run_id=captured"
    ]


def test_no_stop_generation_or_no_runtime_asks_nothing(monkeypatch):
    runtime = FakeRuntime(monkeypatch)
    assert _card_snapshot(stop_generation="").execution_stopped is False
    api = FakeGh([(("graphql",), ok(card_batch(
        {"state": "OPEN", "labels": [], "updatedAt": None}, comments=[]
    )))])
    unconnected = adapter(api=api).snapshot("a", 1, generation="gen-1", stop_generation="x")
    assert (unconnected.execution_stopped, unconnected.run_ended) == (False, False)
    assert runtime.gets == []


def test_run_ended_is_the_runtimes_ending_for_exactly_this_generation(monkeypatch):
    """#2159: a card whose board moved without a report is released once the Runtime reports
    its Run over — for this generation, and only an ending, not ``gone`` or unknown."""
    FakeRuntime(monkeypatch, {
        (_WORK_A1, "gen-1"): _outcome("gen-1", "died", "process_gone"),
        (_WORK_A1, "gen-2"): _outcome("gen-2", "not_started", "no_execution"),
        (_WORK_A1, "gen-3"): _outcome("gen-3", "uncertain", "runtime_restarted"),
    })

    assert _card_snapshot(generation="gen-1").run_ended is True
    assert _card_snapshot(generation="gen-2").run_ended is False
    assert _card_snapshot(generation="gen-3").run_ended is False
    assert _card_snapshot(generation="gen-4").run_ended is False


def test_turn_ended_reads_the_runtime_and_carries_the_refusal_it_stamped(monkeypatch):
    """#1519, #2200: the account blocked when the route was frozen is refusal evidence."""
    FakeRuntime(monkeypatch, {
        (_WORK_A1, "gen-1"): _outcome(
            "gen-1", "died", "process_gone", refused_at_delivery="seven_day at 100%"
        ),
    })

    assert adapter(runtime_socket="/run/rt.sock").turn_ended("a", 1, "gen-1") == {
        "observed": "died",
        "subtype": "",
        "refused": "seven_day at 100%",
        "account": "claude_code:main",
    }


def test_a_wedged_run_is_an_ending_only_once_the_runtime_has_stopped_it(monkeypatch):
    """#1400, #2159: `abandoned` counts only after the Runtime ends the process — otherwise
    the next Run would share the workspace with something that may wake up."""
    wedged = _outcome("gen-1", "abandoned", "no_activity")
    runtime = FakeRuntime(
        monkeypatch, {(_WORK_A1, "gen-1"): wedged}, stop={"result": "still_stopping"}
    )
    port = adapter(runtime_socket="/run/rt.sock")

    assert port.turn_ended("a", 1, "gen-1") is None
    assert runtime.stops == [{"work_id": _WORK_A1, "run_id": "gen-1", "reason": "abandoned"}]

    runtime.stop_answer = {
        "result": "stopped",
        "execution": _outcome("gen-1", "stopped", "abandoned", stop_requested_at=1.0),
    }
    assert port.turn_ended("a", 1, "gen-1")["observed"] == "abandoned"

    runtime.stop_answer = None  # unanswered is not stopped
    assert port.turn_ended("a", 1, "gen-1") is None


def test_the_boards_own_spelling_maps_onto_the_role_the_manifest_named():
    assert adapter()._role_of("in progress") == "In progress"
    assert adapter()._role_of("In Progress") == "In progress"
    assert adapter()._role_of("Todo") == "Todo"


# --- ownership provenance ---

def test_real_label_events_yield_ids_and_the_actor():
    events = [
        {"id": 1, "event": "labeled", "label": {"name": "claude"},
         "actor": {"login": "eero"}},
        {"id": 2, "event": "labeled", "label": {"name": "wayfinder:map"},
         "actor": {"login": "bot"}},
        {"id": 3, "event": "unlabeled", "label": {"name": "claude"},
         "actor": {"login": "eero"}},
        {"id": 4, "event": "labeled", "label": {"name": "claude"},
         "actor": {"login": "someone-else"}},
    ]
    graphql_events = [
        {key: value for key, value in event.items() if key != "event"}
        for event in events
        if event["event"] == "labeled"
    ]
    api = FakeGh([(("graphql",), ok(labelled_batch((105, graphql_events))))])
    subject = adapter(api=api)
    assert labelled_ids(subject.labelled_cards()) == {"agentdeck#105": ["1", "4"]}
    # Who added it last is what the engine needs to authorize the act (#110).
    assert subject.ownership_actors({"agentdeck#105": "4"}) == {
        "agentdeck#105": "someone-else"
    }
    assert len([call for call in api.calls if "graphql" in call]) == 1


def test_an_app_labeler_keeps_its_bot_suffix_from_the_graphql_sweep():
    """GraphQL drops the ``[bot]`` REST carries; the sweep puts it back (#2384).

    Without it the engine asked GitHub for the app's collaborator permission, got a 404, and
    recorded "authorization uncertain" for the app's own label on every wake.
    """
    events = [
        {"id": "app", "label": {"name": "claude"},
         "actor": {"__typename": "Bot", "login": "agentdeck-eerovil"}},
        {"id": "person", "label": {"name": "claude"},
         "actor": {"__typename": "User", "login": "eerovil"}},
        {"id": "suffixed", "label": {"name": "claude"},
         "actor": {"__typename": "Bot", "login": "already[bot]"}},
        {"id": "ghost", "label": {"name": "claude"}, "actor": None},
    ]
    api = FakeGh([(("graphql",), ok(labelled_batch((9, events))))])
    subject = adapter(api=api)
    subject.labelled_cards()

    assert any("actor{__typename login}" in " ".join(call) for call in api.calls)
    actors = {
        event_id: subject.ownership_actors({"agentdeck#9": event_id})["agentdeck#9"]
        for event_id in ("app", "person", "suffixed", "ghost")
    }
    assert actors == {
        "app": "agentdeck-eerovil[bot]",
        "person": "eerovil",
        "suffixed": "already[bot]",
        "ghost": "",
    }
    assert queue.is_bot_login(actors["app"])
    assert not queue.is_bot_login(actors["person"])


def test_the_labelled_sweep_returns_current_card_keys():
    api = FakeGh([(("graphql",), ok(labelled_batch((7, []), (8, []))))])

    assert labelled_ids(adapter(api=api).labelled_cards()) == {
        "agentdeck#7": [],
        "agentdeck#8": [],
    }
    assert any("timelineItems" in " ".join(call) for call in api.calls)


def test_ownership_aliases_are_swept_as_separate_or_queries_and_unioned():
    first = labelled_batch((7, [{"id": "agent", "label": {"name": "agent"}}]))
    first["data"]["r1"] = labelled_batch(
        (8, [{"id": "claude", "label": {"name": "claude"}}])
    )["data"]["r0"]
    api = FakeGh([(("graphql",), ok(first))])
    subject = adapter(
        api=api, ownership_label="agent", ownership_labels=("agent", "claude")
    )

    assert labelled_ids(subject.labelled_cards()) == {
        "agentdeck#7": ["agent"],
        "agentdeck#8": ["claude"],
    }
    query = " ".join(next(call for call in api.calls if "graphql" in call))
    assert "label0=agent" in query and "label1=claude" in query
    assert "labels:[$label0]" in query and "labels:[$label1]" in query


def test_ownership_alias_pagination_keeps_the_repository_and_label_cursor_together():
    first = labelled_batch((7, []), has_next=True, cursor="agent-next")
    first["data"]["r1"] = labelled_batch((8, []))["data"]["r0"]
    second = labelled_batch((9, []))
    api = FakeGh([(("after0=agent-next",), ok(second)), (("graphql",), ok(first))])
    subject = adapter(
        api=api, ownership_label="agent", ownership_labels=("agent", "claude")
    )

    assert set(subject.labelled_cards()) == {"agentdeck#7", "agentdeck#8", "agentdeck#9"}
    graphql_calls = [call for call in api.calls if "graphql" in call]
    followup = " ".join(graphql_calls[1])
    assert "label0=agent" in followup and "after0=agent-next" in followup
    assert "label0=claude" not in followup


def test_ownership_alias_events_keep_chronology_and_the_latest_actor():
    events = [
        {
            "id": "agent-old",
            "createdAt": "2026-08-09T08:00:00Z",
            "label": {"name": "agent"},
            "actor": {"login": "first"},
        },
        {
            "id": "claude-new",
            "createdAt": "2026-08-09T09:00:00Z",
            "label": {"name": "claude"},
            "actor": {"login": "second"},
        },
    ]
    payload = labelled_batch((7, events))
    payload["data"]["r1"] = labelled_batch((7, events))["data"]["r0"]
    subject = adapter(
        api=FakeGh([(("graphql",), ok(payload))]),
        ownership_label="agent",
        ownership_labels=("agent", "claude"),
    )

    assert labelled_ids(subject.labelled_cards()) == {
        "agentdeck#7": ["agent-old", "claude-new"]
    }
    assert subject.ownership_actors({"agentdeck#7": "claude-new"}) == {
        "agentdeck#7": "second"
    }


def test_ownership_actor_is_the_active_agent_event_not_a_later_removed_alias():
    events = [
        {
            "id": "agent-active",
            "createdAt": "2026-08-09T08:00:00Z",
            "label": {"name": "agent"},
            "actor": {"login": "human"},
        },
        {
            "id": "claude-removed",
            "createdAt": "2026-08-09T09:00:00Z",
            "label": {"name": "claude"},
            "actor": {"login": "app[bot]"},
        },
    ]
    payload = labelled_batch((7, events))
    payload["data"]["r1"] = labelled_batch()["data"]["r0"]
    subject = adapter(
        api=FakeGh([(("graphql",), ok(payload))]),
        ownership_label="agent",
        ownership_labels=("agent", "claude"),
    )

    assert labelled_ids(subject.labelled_cards()) == {"agentdeck#7": ["agent-active"]}
    assert subject.ownership_actors({"agentdeck#7": "agent-active"}) == {
        "agentdeck#7": "human"
    }


def test_ownership_actor_is_the_active_alias_not_a_later_removed_canonical_label():
    events = [
        {
            "id": "claude-active",
            "createdAt": "2026-08-09T08:00:00Z",
            "label": {"name": "claude"},
            "actor": {"login": "app[bot]"},
        },
        {
            "id": "agent-removed",
            "createdAt": "2026-08-09T09:00:00Z",
            "label": {"name": "agent"},
            "actor": {"login": "human"},
        },
    ]
    payload = labelled_batch()
    payload["data"]["r1"] = labelled_batch((7, events))["data"]["r0"]
    subject = adapter(
        api=FakeGh([(("graphql",), ok(payload))]),
        ownership_label="agent",
        ownership_labels=("agent", "claude"),
    )

    assert labelled_ids(subject.labelled_cards()) == {"agentdeck#7": ["claude-active"]}
    assert subject.ownership_actors({"agentdeck#7": "claude-active"}) == {
        "agentdeck#7": "app[bot]"
    }


def test_the_recorded_event_payload_parses():
    api = FakeGh([(("graphql",), ok(labelled_batch((105, []))))])
    # The recorded card carries wayfinder:map, so no ownership event — and no crash.
    assert labelled_ids(adapter(api=api).labelled_cards()) == {"agentdeck#105": []}


def test_a_card_whose_events_cannot_be_read_is_absent_not_empty():
    api = FakeGh(default=(1, "", "HTTP 502"))
    assert adapter(api=api).labelled_cards() is None


def test_a_labelled_card_with_malformed_timeline_is_an_incomplete_sweep():
    payload = labelled_batch((105, []))
    payload["data"]["r0"]["issues"]["nodes"][0]["timelineItems"] = []
    api = FakeGh([(("graphql",), ok(payload))])

    assert adapter(api=api).labelled_cards() is None


def test_a_malformed_issue_node_makes_the_labelled_sweep_incomplete():
    payload = labelled_batch((105, []))
    payload["data"]["r0"]["issues"]["nodes"].append(None)
    api = FakeGh([(("graphql",), ok(payload))])

    assert adapter(api=api).labelled_cards() is None


def test_a_malformed_event_node_makes_the_labelled_sweep_incomplete():
    payload = labelled_batch((105, [None]))
    api = FakeGh([(("graphql",), ok(payload))])

    assert adapter(api=api).labelled_cards() is None


def test_an_unparseable_ownership_event_time_makes_the_sweep_incomplete():
    payload = labelled_batch((105, [
        {"id": "LE_owner", "createdAt": "not-a-time", "label": {"name": "claude"}}
    ]))
    api = FakeGh([(("graphql",), ok(payload))])

    assert adapter(api=api).labelled_cards() is None


def test_a_failed_sweep_does_not_publish_partial_actor_history():
    payload = labelled_batch(
        (105, [{"id": "LE_owner", "label": {"name": "claude"},
                "actor": {"login": "owner"}}]),
        (106, [None]),
    )
    api = FakeGh([(("graphql",), ok(payload))])
    subject = adapter(api=api)

    assert subject.labelled_cards() is None
    assert subject._event_histories == {}


def test_an_old_ownership_event_outside_the_window_does_not_poison_other_cards():
    payload = labelled_batch(
        (105, [
            {"id": "LE_other", "label": {"name": "other"},
             "actor": {"login": "owner"}},
        ]),
        (106, [
            {"id": "LE_fresh", "label": {"name": "claude"},
             "actor": {"login": "owner"}},
        ]),
    )
    timeline = payload["data"]["r0"]["issues"]["nodes"][0]["timelineItems"]
    timeline["pageInfo"]["hasPreviousPage"] = True
    api = FakeGh([(("graphql",), ok(payload))])

    assert labelled_ids(adapter(api=api).labelled_cards()) == {
        "agentdeck#105": [],
        "agentdeck#106": ["LE_fresh"],
    }


def test_a_truncated_timeline_is_sufficient_when_the_latest_ownership_event_is_present():
    event = {"id": "LE_owner", "label": {"name": "claude"}, "actor": {"login": "owner"}}
    payload = labelled_batch((105, [event]))
    timeline = payload["data"]["r0"]["issues"]["nodes"][0]["timelineItems"]
    timeline["pageInfo"]["hasPreviousPage"] = True
    api = FakeGh([(("graphql",), ok(payload))])

    assert labelled_ids(adapter(api=api).labelled_cards()) == {
        "agentdeck#105": ["LE_owner"]
    }


def test_same_second_label_events_keep_their_distinct_node_ids():
    events = [
        {"id": "LE_one", "createdAt": "2026-08-09T08:39:21Z",
         "label": {"name": "claude"}, "actor": {"login": "owner"}},
        {"id": "LE_two", "createdAt": "2026-08-09T08:39:21Z",
         "label": {"name": "claude"}, "actor": {"login": "owner"}},
    ]
    api = FakeGh([(("graphql",), ok(labelled_batch((105, events))))])

    assert labelled_ids(adapter(api=api).labelled_cards()) == {
        "agentdeck#105": ["LE_one", "LE_two"]
    }


def test_the_labelled_sweep_paginates_in_bounded_graphql_calls():
    first = labelled_batch((7, []), has_next=True, cursor="next")
    second = labelled_batch((8, []))
    api = FakeGh([
        (("after0=next",), ok(second)),
        (("graphql",), ok(first)),
    ])

    assert labelled_ids(adapter(api=api).labelled_cards()) == {
        "agentdeck#7": [],
        "agentdeck#8": [],
    }
    assert len([call for call in api.calls if "graphql" in call]) == 2


def test_a_missing_pagination_cursor_makes_the_labelled_sweep_incomplete():
    payload = labelled_batch((7, []), has_next=True, cursor="")
    api = FakeGh([(("graphql",), ok(payload))])

    assert adapter(api=api).labelled_cards() is None


def test_a_missing_pagination_flag_makes_the_labelled_sweep_incomplete():
    payload = labelled_batch((7, []))
    del payload["data"]["r0"]["issues"]["pageInfo"]["hasNextPage"]
    api = FakeGh([(("graphql",), ok(payload))])

    assert adapter(api=api).labelled_cards() is None


def test_the_labelled_sweep_stops_before_crossing_the_graphql_reserve():
    payload = labelled_batch((7, []), has_next=True, cursor="next")
    payload["data"]["rateLimit"]["remaining"] = 100
    api = FakeGh([(("graphql",), ok(payload))])
    subject = adapter(api=api, issue_repos=("agentdeck", "other"))
    subject._rate_limits_checked = True
    subject._graphql_remaining = 500

    # The first round reads both repositories in one request and lands under the reserve,
    # so the page it still owes is never asked for and the sweep answers "unknown".
    assert subject.labelled_cards() is None
    assert len(api.calls) == 1


def test_every_issue_repository_is_swept_in_one_request():
    payload = labelled_batch((7, []))
    payload["data"]["r1"] = {
        "issues": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": ""}}
    }
    api = FakeGh([(("graphql",), ok(payload))])
    subject = adapter(api=api, issue_repos=("agentdeck", "other"))

    assert labelled_ids(subject.labelled_cards()) == {"agentdeck#7": []}
    assert len([call for call in api.calls if "graphql" in call]) == 1
    query = " ".join(next(call for call in api.calls if "graphql" in call))
    assert "repo0=agentdeck" in query
    assert "repo1=other" in query


def test_one_unreadable_repository_fails_the_whole_sweep():
    payload = labelled_batch((7, []))
    payload["data"]["r1"] = None
    api = FakeGh([(("graphql",), ok(payload))])
    subject = adapter(api=api, issue_repos=("agentdeck", "other"))

    assert subject.labelled_cards() is None
    assert subject._event_histories == {}


# --- linked pull requests ---

def _pr(number, *, state="OPEN", branch="", body="", sha=None, head="h", draft=False, **extra):
    return {
        "number": number,
        "state": state,
        "headRefName": branch,
        "body": body,
        "headRefOid": head,
        "isDraft": draft,
        "mergeCommit": {"oid": sha} if sha else None,
        **extra,
    }


def test_a_pr_card_is_its_own_release_target():
    host = FakeGh([(("pr", "view"), ok(recorded("pr_view")))])
    linked = adapter(host=host).linked_prs("agentdeck", 221)
    # The recorded PR is merged, so it is the merged target and there is nothing open.
    assert linked.merged == ["agentdeck#221"]
    assert linked.open == {}
    assert not host.asked("pr", "list")     # no issue-link discovery after the PR-card path


def test_githubs_own_closing_link_qualifies_a_pr():
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "no pull requests found")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": [{"number": 7}]}}}}}
            )),
            (("pr", "list"), ok([_pr(7, body="unrelated text")])),
        ]
    )
    linked = adapter(host=host).linked_prs("agentdeck", 176)
    assert linked.open == {"agentdeck#7": "h"}


def test_an_open_linked_pr_carries_its_prospective_release_tree():
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "no pull requests found")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": [{"number": 7}]}}}}}
            )),
            (("pr", "list"), ok([_pr(7, body="unrelated text")])),
            (("potentialMergeCommit",), ok(
                {"data": {"rateLimit": {"remaining": 4998},
                 "repository": {"pullRequest": {
                    "potentialMergeCommit": {"tree": {"oid": "tree-1"}}}}}}
            )),
        ]
    )
    subject = adapter(host=host)
    linked = subject.linked_prs("agentdeck", 176)
    assert linked.open == {"agentdeck#7": "h"}
    assert subject.release_trees(["agentdeck#7"]) == {"agentdeck#7": "tree-1"}
    assert subject._graphql_remaining == 4998


# One `gh pr diff` of a two-line change, and the same change after the branch was rebased onto
# a master that had edited the same file: lines were added above the hunk, the context either
# side of it was reworded, and the blob OIDs moved. Not one line the pull request contributes
# is different (#2122).
_DIFF_BEFORE_REBASE = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,7 +10,8 @@ def handler(request):
     ctx = build(request)
     if not ctx:
         return None
-    return render(ctx)
+    log(ctx)
+    return render(ctx, strict=True)


 def build(request):
"""

_DIFF_AFTER_REBASE = """diff --git a/src/app.py b/src/app.py
index 3333333..4444444 100644
--- a/src/app.py
+++ b/src/app.py
@@ -31,7 +31,8 @@ def handler(request):
     ctx = build(request, trace=True)
     if not ctx:
         return None
-    return render(ctx)
+    log(ctx)
+    return render(ctx, strict=True)


 def build(request, trace=False):
"""


def _digest(diff: str) -> str | None:
    return adapter(host=FakeGh([(("pr", "diff"), (0, diff, ""))])).review_diffs(
        ["agentdeck#7"]
    )["agentdeck#7"]


def test_a_review_diff_is_hashed_without_storing_its_contents():
    diff = "diff --git a/a.py b/a.py\n-old\n+new\n"
    digest = _digest(diff)
    assert digest and diff not in digest
    assert digest == _digest(diff)


def test_a_rebase_that_changes_no_contributed_line_keeps_the_review_digest():
    # The raw bytes moved, which is exactly what used to cost the card a second review.
    assert _DIFF_BEFORE_REBASE != _DIFF_AFTER_REBASE
    assert _digest(_DIFF_BEFORE_REBASE) == _digest(_DIFF_AFTER_REBASE)


def test_a_push_that_changes_a_contributed_line_moves_the_review_digest():
    changed = _DIFF_AFTER_REBASE.replace("render(ctx, strict=True)", "render(ctx, strict=False)")
    assert _digest(changed) != _digest(_DIFF_BEFORE_REBASE)


def test_a_rebase_that_moves_a_deletion_to_another_file_moves_the_review_digest():
    # The paths are in the digest, so the same contributed lines under a different file are
    # not the same contribution.
    moved = _DIFF_AFTER_REBASE.replace("src/app.py", "src/other.py")
    assert _digest(moved) != _digest(_DIFF_BEFORE_REBASE)


def test_an_added_line_is_not_split_on_anything_but_a_newline():
    # `str.splitlines` breaks on a form feed too, and the tail — " bar" — then begins with a
    # space and reads as a context line, so contributed text would drop out of the digest.
    plain = "diff --git a/a.py b/a.py\n+foo\n"
    with_feed = "diff --git a/a.py b/a.py\n+foo\x0c bar\n"
    assert _digest(plain) != _digest(with_feed)


_BINARY = (
    "diff --git a/logo.png b/logo.png\n"
    "index 1111111..2222222 100644\n"
    "Binary files a/logo.png and b/logo.png differ\n"
)


def test_two_different_binary_replacements_are_not_the_same_contribution():
    # Git says nothing about binary content except the blob OIDs, so dropping `index` wholesale
    # would make every replacement of the same image normalize identically.
    other = _BINARY.replace("..2222222", "..3333333")
    assert _digest(_BINARY) != _digest(other)


def test_a_rebased_binary_keeps_its_digest_when_only_the_pre_image_moved():
    # The base replaced the file too; this pull request's own post-image is unchanged.
    rebased = _BINARY.replace("index 1111111..", "index 9999999..")
    assert _digest(rebased) == _digest(_BINARY)


def test_a_binary_section_without_a_readable_blob_is_unknown_not_equal():
    # Unknown defers the release; a content-independent digest would have shipped it.
    no_index = (
        "diff --git a/logo.png b/logo.png\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )
    assert _digest(no_index) is None
    assert _digest(_BINARY.replace("1111111..2222222", "1111111..zzz")) is None


def test_a_rename_keeps_its_digest_when_only_the_similarity_percentage_moved():
    # `similarity index` is computed from the pre- and post-images, so a base edit to a renamed
    # file moves the number while the rename and the contributed lines stay exactly put.
    before = (
        "diff --git a/old.py b/new.py\nsimilarity index 88%\nrename from old.py\n"
        "rename to new.py\nindex 1111..2222 100644\n--- a/old.py\n+++ b/new.py\n"
        "@@ -3,4 +3,5 @@ def f():\n ctx\n-a\n+b\n ctx\n"
    )
    after = (
        "diff --git a/old.py b/new.py\nsimilarity index 61%\nrename from old.py\n"
        "rename to new.py\nindex 9999..2222 100644\n--- a/old.py\n+++ b/new.py\n"
        "@@ -40,4 +41,5 @@ def f():\n other\n-a\n+b\n other\n"
    )
    assert _digest(before) == _digest(after)


def test_a_rename_to_a_different_path_is_a_different_contribution():
    before = (
        "diff --git a/old.py b/new.py\nsimilarity index 88%\nrename from old.py\n"
        "rename to new.py\nindex 1111..2222 100644\n"
    )
    assert _digest(before) != _digest(before.replace("new.py", "other.py"))


def test_a_binary_patch_body_is_not_hashed_once_the_post_image_is_known():
    # The encoded body is a delta against the pre-image, so a rebase moves it even though the
    # post-image — the identity this code deliberately chose for binary — is unchanged.
    literal = (
        "diff --git a/a.bin b/a.bin\nindex 1111..2222 100644\nGIT binary patch\n"
        "literal 12\nzcmZQzU|?WmWMXDv0Ba*z\n\n"
    )
    delta = (
        "diff --git a/a.bin b/a.bin\nindex 8888..2222 100644\nGIT binary patch\n"
        "delta 7\nocmZQzXXXXXyyyy0Bb9z\ndelta 3\nxABC\n"
    )
    assert _digest(literal) == _digest(delta)
    assert _digest(literal) != _digest(delta.replace("..2222", "..3333"))


def test_the_file_after_a_binary_patch_is_still_read():
    """The skip ends at the next `diff --git`, not at the end of the diff."""
    both = (
        "diff --git a/a.bin b/a.bin\nindex 1111..2222 100644\nGIT binary patch\n"
        "literal 12\nzcmZQzU|?WmWMXDv0Ba*z\n\n"
        "diff --git a/b.py b/b.py\nindex 4444..5555 100644\n@@ -1 +1 @@\n-x\n+y\n"
    )
    changed = both.replace("+y", "+z")
    assert _digest(both) != _digest(changed)


def test_a_binary_file_never_borrows_the_blob_of_the_file_before_it():
    # Two sections in one diff, the second binary with no `index` of its own. Carrying the
    # first file's OID forward would digest the image as that file's content.
    two = (
        "diff --git a/a.py b/a.py\nindex 1111..2222 100644\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/b.png b/b.png\nBinary files a/b.png and b/b.png differ\n"
    )
    assert _digest(two) is None


def test_a_deleted_binary_is_a_statement_about_content_not_a_missing_one():
    deleted = (
        "diff --git a/logo.png b/logo.png\ndeleted file mode 100644\n"
        "index 2222222..0000000\nBinary files a/logo.png and /dev/null differ\n"
    )
    assert _digest(deleted) is not None
    assert _digest(deleted) != _digest(_BINARY)


def test_two_hunks_are_not_the_same_contribution_as_one():
    one = (
        "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,3 +1,3 @@\n ctx\n-foo\n+bar\n ctx\n"
    )
    two = (
        "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n ctx\n-foo\n ctx\n@@ -40,2 +40,2 @@\n ctx\n+bar\n ctx\n"
    )
    assert _digest(one) != _digest(two)


def test_an_unreadable_review_diff_is_unknown():
    host = FakeGh([(("pr", "diff"), (1, "", "diff unavailable"))])
    assert adapter(host=host).review_diffs(["agentdeck#7"]) == {"agentdeck#7": None}


def test_release_tree_reads_stop_at_the_graphql_reserve():
    host = FakeGh([(("potentialMergeCommit",), ok(
        {"data": {"rateLimit": {"remaining": 200},
         "repository": {"pullRequest": {
             "potentialMergeCommit": {"tree": {"oid": "tree-1"}}}}}}
    ))])
    subject = adapter(host=host, graphql_reserve=200)

    assert subject.release_trees(["agentdeck#7", "agentdeck#8"]) == {
        "agentdeck#7": "tree-1",
        "agentdeck#8": None,
    }
    assert len([call for call in host.calls if "potentialMergeCommit" in " ".join(call)]) == 1


def test_a_deliberate_fully_qualified_reference_qualifies_a_cross_repo_pr():
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "not a pull request")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": []}}}}}
            )),
            (("pr", "list", "other"), ok([_pr(3, body="fixes agentdeck#176", branch="hotfix/x")])),
            (("pr", "list", "agentdeck"), ok([])),
        ]
    )
    linked = adapter(host=host, code_repos=("agentdeck", "other")).linked_prs("agentdeck", 176)
    # The only signal that can reach a cross-repo PR on a non-convention branch.
    assert linked.open == {"other#3": "h"}


def test_a_fully_qualified_reference_cannot_match_a_longer_issue_number():
    listing = [
        _pr(3, body="fixes agentdeck#1760", branch="hotfix/hash-prefix"),
        _pr(
            4,
            body="fixes https://github.com/eerovil/agentdeck/issues/1760",
            branch="hotfix/url-prefix",
        ),
        _pr(5, body="fixes agentdeck#176.", branch="hotfix/hash-exact"),
        _pr(
            6,
            body="fixes https://github.com/eerovil/agentdeck/issues/176?from=pr",
            branch="hotfix/url-exact",
        ),
    ]
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "not a pull request")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": []}}}}}
            )),
            (("pr", "list", "other"), ok(listing)),
            (("pr", "list", "agentdeck"), ok([])),
        ]
    )
    linked = adapter(host=host, code_repos=("agentdeck", "other")).linked_prs("agentdeck", 176)
    assert linked.open == {"other#5": "h", "other#6": "h"}


def test_a_bare_reference_is_trusted_only_with_the_branch_convention():
    listing = [
        _pr(11, body="mentions #176 in passing", branch="feature/unrelated"),
        _pr(12, body="part of #176", branch="claude/issue-176-fix"),
        _pr(13, body="refs #176", branch="agent/issue-176"),
        _pr(14, body="refs #176", branch="agent/issue-176-fix"),
        _pr(15, body="refs #176", branch="agent/issue-1760"),
    ]
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "no pull requests found")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": []}}}}}
            )),
            (("pr", "list"), ok(listing)),
        ]
    )
    linked = adapter(host=host).linked_prs("agentdeck", 176)
    assert list(linked.open) == ["agentdeck#12", "agentdeck#13", "agentdeck#14"]


def test_a_pr_that_closes_another_card_is_not_this_cards_release_target():
    # The real pair behind #2095: abacus#1912 closed issues#407 and merely mentioned issues#405
    # on the way past, and `/merge` on issues#405 merged it.
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "not a pull request")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": []}}}}}
            )),
            (("pr", "list", "abacus"), ok([_pr(
                1912,
                body=(
                    "Closes Northwind/issues#407.\n\n"
                    "Built on the product-type tree from issues#405 / store#3314."
                ),
                branch="agent/issue-407-abacus",
            )])),
            (("pr", "list", "store"), ok([_pr(
                3314,
                body="Closes Northwind/issues#405.",
                branch="agent/issue-405-issues",
            )])),
        ]
    )
    linked = adapter(
        host=host,
        org="Northwind",
        issue_repos=("issues", "store", "abacus"),
        code_repos=("store", "abacus"),
    ).linked_prs("issues", 405)
    assert linked.open == {"store#3314": "h"}


def test_a_declaration_in_another_case_is_the_same_card():
    # GitHub reads `Issues#407` and `issues#407` as one repository, so a hand-written pull
    # request with no worker branch must not hide its card behind a capital letter.
    def linked_for(body):
        host = FakeGh(
            [
                (("pr", "view"), (1, "", "not a pull request")),
                (("closedByPullRequestsReferences",), ok(
                    {"data": {"repository": {"issue": {
                        "closedByPullRequestsReferences": {"nodes": []}}}}}
                )),
                (
                    ("pr", "list", "abacus"),
                    ok([_pr(1912, body=body, branch="rewrite-product-types")]),
                ),
                (("pr", "list", "store"), ok([])),
            ]
        )
        return adapter(
            host=host,
            org="Northwind",
            issue_repos=("issues",),
            code_repos=("store", "abacus"),
        ).linked_prs("issues", 405)

    assert linked_for(
        "Closes Northwind/Issues#407. Built on issues#405."
    ).open == {}
    assert linked_for(
        "Closes https://github.com/northwind/ISSUES/issues/407. Built on issues#405."
    ).open == {}


def test_a_branch_number_cannot_vouch_for_the_same_number_in_another_repository():
    # Two tracked issue repositories both hold a #405. The pull request closes abacus#405 and
    # merely mentions issues#405, and its branch carries the number with no repository at all.
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "not a pull request")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": []}}}}}
            )),
            (("pr", "list", "abacus"), ok([_pr(
                1912,
                body=(
                    "Closes Northwind/abacus#405.\n\n"
                    "Built on the product-type tree from issues#405."
                ),
                branch="agent/issue-405-abacus",
            )])),
            (("pr", "list", "store"), ok([])),
        ]
    )
    subject = adapter(
        host=host,
        org="Northwind",
        issue_repos=("issues", "abacus"),
        code_repos=("store", "abacus"),
    )
    assert subject.linked_prs("issues", 405).open == {}


def test_a_pr_that_closes_two_cards_still_releases_for_both():
    def linked_for(number):
        host = FakeGh(
            [
                (("pr", "view"), (1, "", "not a pull request")),
                (("closedByPullRequestsReferences",), ok(
                    {"data": {"repository": {"issue": {
                        "closedByPullRequestsReferences": {"nodes": []}}}}}
                )),
                (("pr", "list", "other"), ok([_pr(
                    3,
                    body="Closes agentdeck#176. Fixes agentdeck#177.",
                    branch="agent/issue-176",
                )])),
                (("pr", "list", "agentdeck"), ok([])),
            ]
        )
        subject = adapter(host=host, code_repos=("agentdeck", "other"))
        return subject.linked_prs("agentdeck", number)

    assert linked_for(176).open == {"other#3": "h"}
    # The branch names only 176, but the body names 177 too, so 177 keeps its release target.
    assert linked_for(177).open == {"other#3": "h"}


def test_a_worker_branch_for_another_card_refuses_a_passing_mention():
    listing = [
        _pr(3, body="follow-up to agentdeck#176", branch="agent/issue-177-agentdeck"),
        _pr(4, body="follow-up to agentdeck#176", branch="claude/issue-177-tidy"),
        _pr(5, body="follow-up to agentdeck#176", branch="hotfix/no-convention"),
    ]
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "not a pull request")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": []}}}}}
            )),
            (("pr", "list", "other"), ok(listing)),
            (("pr", "list", "agentdeck"), ok([])),
        ]
    )
    linked = adapter(host=host, code_repos=("agentdeck", "other")).linked_prs("agentdeck", 176)
    # A branch that states no card at all still leaves the qualified reference to speak.
    assert linked.open == {"other#5": "h"}


def test_a_bare_closing_keyword_across_repositories_does_not_disown_its_own_card():
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "not a pull request")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": []}}}}}
            )),
            (("pr", "list", "other"), ok([_pr(
                3, body="Closes #176 (agentdeck#176).", branch="agent/issue-176"
            )])),
            (("pr", "list", "agentdeck"), ok([])),
        ]
    )
    # `Closes #176` in `other` is GitHub-speak for `other#176`, but the worker meant the card.
    linked = adapter(
        host=host, issue_repos=("agentdeck", "other"), code_repos=("agentdeck", "other")
    ).linked_prs("agentdeck", 176)
    assert linked.open == {"other#3": "h"}


def test_a_bare_closing_keyword_for_another_number_still_names_another_card():
    # #2100: the #2095 shape with a bare closing keyword. `other` holds issues as well as code,
    # so `Closes #177` there is GitHub-speak for the real card `other#177`, and the branch says
    # nothing. The passing mention of `agentdeck#176` must not qualify it for card 176.
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "not a pull request")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": []}}}}}
            )),
            (("pr", "list", "other"), ok([_pr(
                3,
                body="Closes #177.\n\nBuilt on the tree from agentdeck#176.",
                branch="hotfix/no-convention",
            )])),
            (("pr", "list", "agentdeck"), ok([])),
        ]
    )
    linked = adapter(
        host=host, issue_repos=("agentdeck", "other"), code_repos=("agentdeck", "other")
    ).linked_prs("agentdeck", 176)
    assert linked.open == {}


def test_githubs_own_closing_link_outranks_a_foreign_declaration():
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "no pull requests found")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": [{"number": 7}]}}}}}
            )),
            (("pr", "list"), ok([_pr(7, body="Closes #177", branch="agent/issue-177")])),
        ]
    )
    # GitHub has resolved the tie itself; a stale body line does not get to overturn it.
    assert adapter(host=host).linked_prs("agentdeck", 176).open == {"agentdeck#7": "h"}


def test_a_closing_keyword_for_a_repo_this_instance_does_not_track_states_nothing():
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "not a pull request")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": []}}}}}
            )),
            (("pr", "list", "other"), ok([_pr(
                3, body="Fixes vendor/upstream#99, part of agentdeck#176", branch="hotfix/x"
            )])),
            (("pr", "list", "agentdeck"), ok([])),
        ]
    )
    linked = adapter(host=host, code_repos=("agentdeck", "other")).linked_prs("agentdeck", 176)
    # An upstream bug is not a card on this board, so it cannot rule this card out.
    assert linked.open == {"other#3": "h"}


def test_a_listing_that_fails_makes_the_whole_answer_unknown():
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "no pull requests found")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": []}}}}}
            )),
        ],
        default=(1, "", "HTTP 502"),
    )
    # A partial view could look like "nothing to release", which would finish a release early.
    assert adapter(host=host).linked_prs("agentdeck", 176) is None


def test_a_failed_closing_reference_lookup_is_unknown():
    host = FakeGh([(("pr", "view"), (1, "", "no pull requests found"))], default=(1, "", "boom"))
    assert adapter(host=host).linked_prs("agentdeck", 176) is None


# --- pull requests the newest-100 listing can no longer reach (#1467) ---

def _links(*, closing=(), cross=(), has_next=False, cursor=None):
    """The issue-link GraphQL reply: what closes this issue, and what cross-references it."""
    return ok({"data": {"repository": {"issue": {
        "closedByPullRequestsReferences": {"nodes": [{"number": n} for n in closing]},
        "timelineItems": {"nodes": [
            *(
                {"source": {
                    "number": n, "repository": {"name": name, "owner": {"login": owner}}
                }}
                for owner, name, n in cross
            ),
            {"source": {}},        # a cross-reference from an issue carries no PR fields
        ], "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}},
    }}}})


def _cross_page(*, cross=(), has_next=False, cursor=None):
    return ok({"data": {"repository": {"issue": {"timelineItems": {
        "nodes": [
            {"source": {
                "number": n, "repository": {"name": name, "owner": {"login": owner}}
            }}
            for owner, name, n in cross
        ],
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }}}}})


def _windowed(*, cross=(), candidate=None, closing=()):
    """A busy repository whose newest 100 pull requests no longer include the card's own."""
    answers = [
        (("pr", "view", "eerovil/other"), candidate),
        (("pr", "view"), (1, "", "no pull requests found")),
        (("closedByPullRequestsReferences",), _links(closing=closing, cross=cross)),
        (("pr", "list", "other"), ok([_pr(9002, body="something else entirely")])),
        (("pr", "list", "agentdeck"), ok([])),
    ]
    return FakeGh([answer for answer in answers if answer[1] is not None])


def test_a_pr_older_than_the_listing_window_is_still_found():
    """The #1467 shape: three weeks of a busy repository pushed the card's own PR off the end."""
    host = _windowed(
        cross=(("eerovil", "other", 1704),),
        candidate=ok(_pr(1704, body="Closes agentdeck#176", branch="agent/issue-176-posti")),
    )
    subject = adapter(host=host, code_repos=("agentdeck", "other"))

    assert subject.linked_prs("agentdeck", 176).open == {"other#1704": "h"}


def test_a_pr_beyond_the_first_cross_reference_page_is_still_found():
    """More than 100 mentions must not recreate the listing-window false negative."""
    first_page = tuple(("eerovil", "other", n) for n in range(9000, 9100))
    host = FakeGh([
        (("pr", "view", "eerovil/other"), ok(
            _pr(1704, body="Closes agentdeck#176", branch="agent/issue-176-posti")
        )),
        (("pr", "view"), (1, "", "no pull requests found")),
        (("after:$after",), _cross_page(cross=(("eerovil", "other", 1704),))),
        (("closedByPullRequestsReferences",), _links(
            cross=first_page, has_next=True, cursor="page-2"
        )),
        (("pr", "list", "other"), ok([
            _pr(n, body="something else entirely") for _owner, _repo, n in first_page
        ])),
        (("pr", "list", "agentdeck"), ok([])),
    ])
    subject = adapter(host=host, code_repos=("agentdeck", "other"))

    assert subject.linked_prs("agentdeck", 176).open == {"other#1704": "h"}
    assert host.asked("after=page-2")
    assert len([call for call in host.calls if "graphql" in call]) == 2


def test_an_unreadable_cross_reference_continuation_makes_the_answer_unknown():
    host = FakeGh([
        (("pr", "view"), (1, "", "no pull requests found")),
        (("after:$after",), ok({"data": {"rateLimit": None}})),
        (("closedByPullRequestsReferences",), _links(has_next=True, cursor="page-2")),
    ])

    assert adapter(host=host).linked_prs("agentdeck", 176) is None


def test_a_cross_referenced_pr_still_has_to_qualify():
    """A cross-reference is a candidate, not a nomination: a passing mention stays out."""
    host = _windowed(
        cross=(("eerovil", "other", 1704),),
        candidate=ok(_pr(1704, body="unrelated work, see #176", branch="hotfix/x")),
    )
    subject = adapter(host=host, code_repos=("agentdeck", "other"))

    assert subject.linked_prs("agentdeck", 176).open == {}


def test_a_cross_referenced_draft_is_withheld_like_any_other():
    host = _windowed(
        cross=(("eerovil", "other", 1704),),
        candidate=ok(_pr(1704, body="Closes agentdeck#176", draft=True)),
    )
    subject = adapter(host=host, code_repos=("agentdeck", "other"))

    linked = subject.linked_prs("agentdeck", 176)
    assert (linked.open, linked.withheld) == ({}, ["other#1704"])


def test_a_cross_reference_the_project_does_not_hold_code_in_is_ignored():
    host = _windowed(cross=(("eerovil", "elsewhere", 12), ("someone-else", "other", 13)))
    subject = adapter(host=host, code_repos=("agentdeck", "other"))

    assert subject.linked_prs("agentdeck", 176).open == {}
    assert not host.asked("pr", "view", "eerovil/other")


def test_a_pr_the_listing_already_holds_is_not_read_again():
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "no pull requests found")),
            (("closedByPullRequestsReferences",), _links(cross=(("eerovil", "agentdeck", 7),))),
            (("pr", "list"), ok([_pr(7, body="Closes agentdeck#176")])),
        ]
    )
    subject = adapter(host=host)

    assert subject.linked_prs("agentdeck", 176).open == {"agentdeck#7": "h"}
    # Only the card's own "is this a PR?" probe; the listing already answered for #7.
    assert len([call for call in host.calls if call[:2] == ("pr", "view")]) == 1


def test_a_cross_referenced_pr_github_no_longer_has_is_skipped():
    host = _windowed(
        cross=(("eerovil", "other", 1704),),
        candidate=(1, "", "GraphQL: Could not resolve to a PullRequest with the number of 1704."),
    )
    subject = adapter(host=host, code_repos=("agentdeck", "other"))

    assert subject.linked_prs("agentdeck", 176).open == {}


def test_an_unreadable_cross_referenced_pr_makes_the_whole_answer_unknown():
    host = _windowed(
        cross=(("eerovil", "other", 1704),), candidate=(1, "", "HTTP 502"),
    )
    subject = adapter(host=host, code_repos=("agentdeck", "other"))

    assert subject.linked_prs("agentdeck", 176) is None


def test_a_reply_carrying_no_cross_references_still_answers_from_the_listing():
    """Nothing used this part of the reply before, so its absence must not stall a release."""
    host = FakeGh(
        [
            (("pr", "view"), (1, "", "no pull requests found")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {
                    "closedByPullRequestsReferences": {"nodes": []}}}}}
            )),
            (("pr", "list"), ok([_pr(7, body="Closes agentdeck#176")])),
        ]
    )

    assert adapter(host=host).linked_prs("agentdeck", 176).open == {"agentdeck#7": "h"}


# --- pull requests whose author held them back, and targets a person named (#1445) ---

def _discovery(*pulls, closing=()):
    """A `gh` script for the issue-link discovery path: no PR card, then a listing."""
    return FakeGh(
        [
            (("pr", "view"), (1, "", "no pull requests found")),
            (("closedByPullRequestsReferences",), ok(
                {"data": {"repository": {"issue": {"closedByPullRequestsReferences": {
                    "nodes": [{"number": n} for n in closing]}}}}}
            )),
            (("pr", "list"), ok(list(pulls))),
        ]
    )


def test_a_draft_pr_is_withheld_from_the_release_set():
    """The #211 shape: a draft follow-up that names the card in its body."""
    host = _discovery(_pr(9, body="follow-up to eerovil/agentdeck#176", draft=True))
    linked = adapter(host=host).linked_prs("agentdeck", 176)
    assert linked.open == {}
    assert linked.withheld == ["agentdeck#9"]


def test_a_pr_that_opts_out_is_withheld_even_when_github_links_it():
    """The opt-out beats GitHub's own closing link: it is the author saying not this one."""
    body = "closes it\n<!-- agentdeck:release=no -->"
    host = _discovery(_pr(7, body=body), closing=(7,))
    linked = adapter(host=host).linked_prs("agentdeck", 176)
    assert linked.open == {}
    assert linked.withheld == ["agentdeck#7"]


def test_a_step_pr_is_linked_released_and_named_as_a_step():
    """A step of the card is still its release target, on its step branch (#2730)."""
    step = "Part of #176\nPR intent: step — needs it live\n<!-- agentdeck:release=step -->"
    host = _discovery(
        _pr(7, state="MERGED", body=step, branch="agent/issue-176", sha="m1"),
        _pr(9, body=step, branch="agent/issue-176-step2"),
        _pr(11, body="Closes #176", branch="agent/issue-176-step3"),
    )
    linked = adapter(host=host).linked_prs("agentdeck", 176)
    assert linked.open == {"agentdeck#9": "h", "agentdeck#11": "h"}
    assert linked.merged == ["agentdeck#7"]
    assert linked.withheld == []
    assert linked.step == ["agentdeck#7", "agentdeck#9"]


def test_a_withheld_pr_is_never_a_step_even_with_the_marker():
    body = "eerovil/agentdeck#176\n<!-- agentdeck:release=step -->"
    host = _discovery(_pr(9, body=body, draft=True))
    linked = adapter(host=host).linked_prs("agentdeck", 176)
    assert linked.withheld == ["agentdeck#9"]
    assert linked.step == []


def test_a_named_step_target_is_classified_as_a_step():
    body = "Part of eerovil/agentdeck#176\n<!-- agentdeck:release=step -->"
    host = FakeGh([(("pr", "view"), ok(_pr(3024, body=body)))])
    linked = adapter(host=host).resolve_prs(["agentdeck#3024"])
    assert linked.open == {"agentdeck#3024": "h"}
    assert linked.step == ["agentdeck#3024"]


def test_an_ordinary_linked_pr_survives_beside_a_withheld_one():
    host = _discovery(
        _pr(7, body="eerovil/agentdeck#176"),
        _pr(9, body="eerovil/agentdeck#176", draft=True),
    )
    linked = adapter(host=host).linked_prs("agentdeck", 176)
    assert linked.open == {"agentdeck#7": "h"}
    assert linked.withheld == ["agentdeck#9"]


def test_a_draft_pr_card_is_not_its_own_release_target():
    host = FakeGh([(("pr", "view"), ok(_pr(221, isDraft=True)))])
    linked = adapter(host=host).linked_prs("agentdeck", 221)
    assert linked.open == {}
    assert linked.withheld == ["agentdeck#221"]


def test_a_named_target_is_classified_like_a_discovered_one():
    host = FakeGh([(("pr", "view"), ok(_pr(3024)))])
    linked = adapter(host=host).resolve_prs(["agentdeck#3024"])
    assert linked.open == {"agentdeck#3024": "h"}


def test_a_named_target_that_does_not_exist_is_absent_not_unknown():
    """A typo must reach the caller as a refusal, not as a wake that retries forever."""
    host = FakeGh(default=(
        1, "", "GraphQL: Could not resolve to a PullRequest with the number of 9999."
    ))
    linked = adapter(host=host).resolve_prs(["agentdeck#9999"])
    assert linked == LinkedPRs()


def test_a_named_target_that_cannot_be_read_makes_the_whole_answer_unknown():
    host = FakeGh(default=(1, "", "connection reset"))
    assert adapter(host=host).resolve_prs(["agentdeck#3024"]) is None


def test_a_named_target_that_is_a_draft_is_reported_as_withheld():
    host = FakeGh([(("pr", "view"), ok(_pr(9, isDraft=True)))])
    linked = adapter(host=host).resolve_prs(["agentdeck#9"])
    assert linked.withheld == ["agentdeck#9"]
    assert linked.open == {}



# --- pull-request state ---

def test_a_real_pr_view_becomes_a_pr_state():
    host = FakeGh(
        [
            (("pr", "view"), ok(recorded("pr_view"))),
            (("pr", "checks"), (0, "", "")),
        ]
    )
    states = adapter(host=host).pr_states(["agentdeck#221"])
    state = states["agentdeck#221"]
    assert state.state == "MERGED"
    assert state.head == "db89a890636f08c4d71880f196de45dd3c6fec3d"
    assert state.merge_commit == "fa27ae3205b86de12216ed46335a123f6f725c55"
    assert state.check_state == "green"


def test_check_states_follow_ghs_exit_codes():
    subject = adapter(host=FakeGh(default=(0, "", "")))
    assert subject.check_state("a", 1) == ("green", [])
    subject = adapter(host=FakeGh(default=(8, "", "")))
    assert subject.check_state("a", 1) == ("pending", [])
    subject = adapter(host=FakeGh(default=(1, "", "no checks reported")))
    assert subject.check_state("a", 1) == ("green", [])   # nothing gates the merge
    failing = adapter(host=FakeGh(default=(1, "Tests\tfail\t1m\turl\nLint\tpass\t2s\turl", "")))
    assert failing.check_state("a", 1) == ("failing", ["Tests"])


def test_an_unreadable_pr_makes_the_probe_unknown():
    assert adapter(host=FakeGh(default=(1, "", "HTTP 404"))).pr_states(["agentdeck#1"]) is None


# --- arming auto-merge ---

def test_arming_classifies_what_github_answered():
    def arm(answer):
        return adapter(host=FakeGh(default=answer)).arm_auto_merge("agentdeck#7")

    assert arm((0, "", "")) is ArmOutcome.ARMED
    assert arm((1, "", "Pull request is in clean status")) is ArmOutcome.ARMED
    assert arm((1, "", "pull request is already merged")) is ArmOutcome.ALREADY_MERGED
    assert arm((1, "", "auto-merge is not enabled for this repository")) is (
        ArmOutcome.UNAVAILABLE
    )
    assert arm((1, "", "something else went wrong")) is ArmOutcome.FAILED


def test_cancelling_auto_merge_is_idempotent():
    def cancel(answer):
        return adapter(host=FakeGh(default=answer)).cancel_auto_merge("agentdeck#7")

    assert cancel((0, "", "")) is True
    assert cancel((1, "", "pull request is already merged")) is True
    assert cancel((1, "", "Pull request is not in auto-merge queue")) is True
    assert cancel((1, "", "something else went wrong")) is False

    host = FakeGh(
        [
            (("pr merge", "--disable-auto"), (1, "", "unknown wording")),
            (("pr view", "autoMergeRequest"), ok({"state": "OPEN", "autoMergeRequest": None})),
        ]
    )
    assert adapter(host=host).cancel_auto_merge("agentdeck#7") is True


# --- effects ---

def test_effects_use_the_credential_each_act_is_trusted_with():
    api, host = FakeGh(), FakeGh()
    subject = adapter(api=api, host=host)

    assert subject.apply(Effect(EffectKind.ADD_OWNERSHIP, "agentdeck", 5)) is True
    assert api.asked("issue", "edit", "--add-label", "claude")

    # Closing, reopening and merging into a protected branch need the host login.
    assert subject.apply(Effect(EffectKind.CLOSE_ISSUE, "agentdeck", 5)) is True
    assert host.asked("issue", "close")
    assert subject.apply(Effect(EffectKind.REOPEN_ISSUE, "agentdeck", 5)) is True
    assert host.asked("issue", "reopen")
    assert subject.apply(Effect(EffectKind.UPDATE_BRANCH, "agentdeck", 7)) is True
    assert host.asked("pr", "update-branch")


def test_adding_ownership_adds_only_the_canonical_label():
    api = FakeGh()
    subject = adapter(
        api=api, ownership_label="agent", ownership_labels=("agent", "claude")
    )

    assert subject.apply(Effect(EffectKind.ADD_OWNERSHIP, "agentdeck", 5))

    add = api.calls[-1]
    assert add.count("--add-label") == 1 and "agent" in add and "claude" not in add


def test_a_comment_carries_the_bot_marker_so_it_can_never_self_trigger():
    api = FakeGh()
    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Queued."))
    body = next(call for call in api.calls if "comment" in call)
    assert "<!-- claude-bot -->" in " ".join(body)


def _posted_body(api) -> str:
    call = next(call for call in api.calls if "comment" in call)
    return call[call.index("--body") + 1]


def test_every_comment_carries_the_projects_control_guide():
    """#373: the guide is appended once, here, so 'every bot comment' can actually be true."""
    guide = "---\n🤖 **Ohjaa Claudea**\n- **Pysäytä työ:** `/claude stop`"
    api = FakeGh()
    adapter(api=api, comment_guide=guide).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, "Work is waiting.")
    )
    body = _posted_body(api)
    assert body.startswith("Work is waiting.")
    assert guide in body
    navigation = "[Issue #5](https://github.com/eerovil/agentdeck/issues/5)"
    assert navigation in body
    assert body.index(guide) < body.index(navigation)
    # The marker stays last, so endswith remains the cheap "we wrote this" test.
    assert body.endswith(BOT_MARKER)
    # The engine holds no prose of its own: nothing it did not render appears.
    assert "/shot" not in body and "/review" not in body


def test_a_project_with_no_guide_posts_exactly_what_it_did_before():
    """Parity-ledger name is stable; #957 adds only the shared navigation row."""
    api = FakeGh()
    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Queued."))
    issue = "[Issue #5](https://github.com/eerovil/agentdeck/issues/5)"
    assert _posted_body(api) == f"Queued.\n\n{issue}\n\n{BOT_MARKER}"


def test_comment_navigation_adds_known_session_and_pull_request_before_the_issue():
    api = FakeGh()
    comment = CommentBody(
        "Ready.",
        session_url="https://deck.example/sessions/codex:test:sid",
        pull_request_url="https://github.com/example/agentdeck/pull/959",
    )
    adapter(api=api).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, comment)
    )

    navigation = (
        "[AgentDeck](https://deck.example/sessions/codex:test:sid) · "
        "[PR #959](https://github.com/example/agentdeck/pull/959) · "
        "[Issue #5](https://github.com/eerovil/agentdeck/issues/5)"
    )
    assert navigation in _posted_body(api)


def test_comment_navigation_resolves_an_engine_discovered_pull_request_ref():
    api = FakeGh()
    comment = CommentBody(
        "Ready.",
        pull_request_url="https://github.com/eerovil/agentdeck/pull/12",
        pull_request_ref="agentdeck#959",
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, comment))

    assert "[PR #959](https://github.com/eerovil/agentdeck/pull/959)" in _posted_body(api)
    assert "/pull/12" not in _posted_body(api)


def test_malformed_comment_links_are_omitted_without_losing_the_issue():
    api = FakeGh()
    comment = CommentBody(
        "Ready.",
        session_url="https://deck.example/sessions/private\\",
        pull_request_url="https://example.com/example/agentdeck/pull/959",
    )
    adapter(api=api).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, comment)
    )
    body = _posted_body(api)
    assert "AgentDeck" not in body and "PR #959" not in body
    assert "[Issue #5](https://github.com/eerovil/agentdeck/issues/5)" in body


def test_a_reaction_needs_both_a_comment_and_a_reaction():
    api = FakeGh()
    subject = adapter(api=api)
    assert subject.apply(
        Effect(EffectKind.REACT, "agentdeck", 5, {"comment": "9", "reaction": "eyes"})
    ) is True
    assert api.asked("reactions", "content=eyes")
    assert subject.apply(Effect(EffectKind.REACT, "agentdeck", 5, {"comment": "9"})) is False


def test_a_retitle_edits_the_issue_and_an_empty_one_never_leaves():
    """#338: the repair speaks as the engine, and refuses to write a title of nothing."""
    api = FakeGh()
    subject = adapter(api=api)
    assert subject.apply(Effect(EffectKind.SET_TITLE, "agentdeck", 338, "Fix the export")) is True
    assert api.asked("issue", "edit", "--title", "Fix the export")

    before = len(api.calls)
    assert subject.apply(Effect(EffectKind.SET_TITLE, "agentdeck", 338, "  ")) is False
    assert len(api.calls) == before  # refused here, not sent for GitHub to reject


def test_a_failed_effect_answers_false_rather_than_raising():
    api = FakeGh(default=(1, "", "HTTP 403"))
    assert adapter(api=api).apply(Effect(EffectKind.ADD_OWNERSHIP, "agentdeck", 5)) is False


def test_deploy_observation_is_left_to_an_extension():
    assert adapter().deploy_states({"agentdeck": "abc"}) == {}


# --- activation discovery, against a payload recorded from a real board ---

def test_a_real_boards_payload_yields_its_ids_and_columns():
    """The shape here was recorded from a live board (ids redacted), not invented."""
    api = FakeGh([(("projectV2",), ok(recorded("board_discovery")))])
    board = adapter(api=api).board("example", 2)
    assert board is not None
    assert board.project_id == "PVT_kwDOEXAMPLE"
    assert board.status_field_id == "PVTSSF_lADOEXAMPLE"
    # Every role the engine moves a card through is present, and so is a column it does not know
    # about — which it must simply ignore rather than object to.
    for role in ("Todo", "In progress", "In review", "Done", "Needs More Info", "Blocked"):
        assert role in board.status_options, role
    assert "Bot-issues" in board.status_options


def test_a_user_owned_board_is_found_under_user_when_organization_is_empty():
    payload = recorded("board_discovery")
    user_shaped = {"data": {"user": payload["data"]["organization"]}}
    api = FakeGh(
        [
            (("organization(login:$org)",), ok({"data": {"organization": None}})),
            (("user(login:$org)",), ok(user_shaped)),
        ]
    )
    # A personal board answers under `user`, so discovery has to ask both ways.
    assert adapter(api=api).board("eerovil", 1) is not None


def test_a_board_nobody_can_read_is_unknown_rather_than_empty():
    api = FakeGh(default=(1, "", "INSUFFICIENT_SCOPES"))
    assert adapter(api=api).board("example", 2) is None


def test_repo_reachability_distinguishes_absent_from_unverifiable():
    absent = FakeGh(default=(1, "", "gh: Not Found (HTTP 404)"))
    assert adapter(api=absent).repo_check("example", "nope").reach is Reachability.ABSENT
    blocked = FakeGh(default=(1, "", "HTTP 502 bad gateway"))
    # Unverifiable must not read as absent: activation fails closed instead of dropping a repo.
    assert adapter(api=blocked).repo_check("example", "store").reach is Reachability.UNAVAILABLE
    fine = FakeGh(default=(0, '"store"', ""))
    assert adapter(api=fine).repo_check("example", "store").reach is Reachability.REACHABLE


def test_a_rate_limited_repo_read_is_not_reported_as_a_credential_failure():
    """#707: both arrive as HTTP 403, and only one of them is anyone's job to fix."""
    limited = FakeGh(
        default=(1, "", "gh: API rate limit exceeded for user ID 42. (HTTP 403)")
    )
    throttled = adapter(api=limited).repo_check("example", "store")
    assert throttled.reach is Reachability.UNAVAILABLE
    assert "API rate limit exceeded" in throttled.detail

    refused = FakeGh(default=(1, "", "gh: Resource not accessible by integration (HTTP 403)"))
    denied = adapter(api=refused).repo_check("example", "store")
    assert denied.reach is Reachability.DENIED
    assert "Resource not accessible by integration" in denied.detail


def test_a_repo_read_that_never_reached_github_reads_as_unavailable():
    for blob in (
        "gh api timed out",
        'Get "https://api.github.com/repos/example/store": dial tcp: lookup api.github.com: '
        "no such host",
        "error connecting to api.github.com",
    ):
        api = FakeGh(default=(1, "", blob))
        check = adapter(api=api).repo_check("example", "store")
        assert check.reach is Reachability.UNAVAILABLE, blob
        assert check.detail


def test_an_unrecognized_repo_failure_does_not_accuse_the_credentials():
    """The default has to be "unanswered": that is the half the reader cannot act on."""
    api = FakeGh(default=(1, "", "gh: something nobody has seen before"))
    check = adapter(api=api).repo_check("example", "store")
    assert check.reach is Reachability.UNAVAILABLE
    assert check.detail == "something nobody has seen before"


def test_the_adapter_still_carries_every_lookup_name_activation_discovers_with():
    """Activation's Discovery is duck-typed, so a renamed lookup breaks at nobody's compile."""
    from agentdeck.poller_engine.config import Discovery

    assert isinstance(adapter(), Discovery)


def test_a_repo_failure_detail_is_one_short_line():
    api = FakeGh(default=(1, "", "gh: " + "x" * 400 + "\nsecond line\n"))
    detail = adapter(api=api).repo_check("example", "store").detail
    assert len(detail) == 120
    assert "\n" not in detail


# --- the port contract, and the budget question it asks (#183) ---

def test_a_blockers_state_is_read_for_an_issue_and_a_pull_request_alike():
    """#1230: the thing a worker is blocked on is usually a PR, but never has to be, so this
    goes through the issues endpoint — which serves both — rather than `pr view`."""
    api = FakeGh(
        [
            (("repos/eerovil/agentdeck/issues/122",),
             ok({"state": "closed", "pull_request": {"merged_at": "2026-08-27T10:00:00Z"}})),
            (("repos/eerovil/agentdeck/issues/120",), ok({"state": "closed"})),
            (("repos/eerovil/agentdeck/issues/9",), ok({"state": "open"})),
            # A pull request somebody abandoned: closed, never merged, and out of the way.
            (("repos/other/thing/issues/4",),
             ok({"state": "closed", "pull_request": {"merged_at": None}})),
        ]
    )
    # The host login, like every other cross-repository card read (`pr_states`, the marker
    # listing): a blocker can live in a repository the project-scoped token cannot see.
    port = adapter(host=api)

    states = port.ref_states(
        ["agentdeck#122", "agentdeck#120", "agentdeck#9", "other/thing#4"]
    )

    assert states == {
        "agentdeck#122": "MERGED",
        "agentdeck#120": "CLOSED",
        "agentdeck#9": "OPEN",
        "other/thing#4": "CLOSED",
    }
    # A cross-organization ref already carries its owner; prefixing this project's would
    # invent a path that does not exist.
    assert api.asked("repos/other/thing/issues/4")


def test_a_blocker_that_could_not_be_read_is_absent_rather_than_called_open():
    """The engine fails closed on a missing entry; reporting `OPEN` would be a claim the
    adapter has no grounds for, and would read as a deliberate answer in the cycle log."""
    api = FakeGh([(("issues/122",), ok({"state": "open"}))], default=(1, "", "not found"))
    port = adapter(host=api)

    assert port.ref_states(["agentdeck#122", "agentdeck#404"]) == {"agentdeck#122": "OPEN"}


def test_the_adapter_implements_every_method_the_engine_calls():
    """`dispatch_ok` was in EnginePort and only ever on the test fakes, so the first real wake
    died on AttributeError with every unit test green. The protocol is the contract; check the
    adapter against it rather than against the set of calls some test happened to make."""
    from agentdeck.poller_engine.ports import EnginePort

    required = [
        name
        for name, value in vars(EnginePort).items()
        if callable(value) and not name.startswith("_")
    ]
    assert required, "EnginePort has no methods — the check would pass vacuously"
    missing = [name for name in required if not hasattr(GitHubAdapter, name)]
    assert missing == []


def _usage(
    tmp_path, label, *, provider="claude_code", five=0.0, seven=0.0, age_s=0.0
):
    from datetime import UTC, datetime, timedelta

    fetched = datetime.now(UTC) - timedelta(seconds=age_s)
    (tmp_path / f"usage-{label}.json").write_text(
        json.dumps(
            {
                "account": f"{provider}:{label}",
                "fetched_at": fetched.isoformat(),
                "five_hour_pct": five,
                "seven_day_pct": seven,
            }
        )
    )


def test_one_account_with_room_is_enough_to_dispatch(tmp_path):
    """A full primary is exactly what the fallback order is for — refusing the wake then would
    stop work the host can still do."""
    _usage(tmp_path, "main", seven=99.0)
    _usage(tmp_path, "alt", seven=12.0)
    port = adapter(
        worker_accounts=("claude_code:main", "claude_code:alt"), usage_cache=tmp_path
    )
    assert port.dispatch_ok() is True
    assert port._has_room("claude_code:main") is True






def test_model_usage_never_stops_dispatch(tmp_path):
    _usage(tmp_path, "main", seven=100.0)
    _usage(tmp_path, "alt", seven=100.0)
    port = adapter(
        worker_accounts=("claude_code:main", "claude_code:alt"), usage_cache=tmp_path
    )

    assert port.dispatch_ok() is True
    assert port.account_full("claude_code:main") is False


def test_usage_that_cannot_be_read_is_not_a_refusal(tmp_path):
    """A stopped collector must not silently halt every poller on the host, and the deck
    enforces its own ceiling at spawn time anyway."""
    _usage(tmp_path, "stale", seven=99.0, age_s=4000.0)
    (tmp_path / "usage-broken.json").write_text("{not json")
    for label in ("stale", "broken", "absent"):
        port = adapter(worker_accounts=(f"claude_code:{label}",), usage_cache=tmp_path)
        assert port.dispatch_ok() is True, label




# --- worktrees the host reported, and only those (#183) ---

def test_a_receipt_carries_the_worktrees_the_dispatcher_created(tmp_path):
    """The engine never derives a worktree path. It removes one because something told it that
    exact path exists — the difference between cleaning up and deleting a directory whose name
    happened to match a convention."""
    from datetime import UTC, datetime

    from agentdeck.poller_engine.github import read_receipt_worktrees, write_receipt

    path = write_receipt(
        tmp_path, "store", 5, "dispatch-1",
        now=datetime(2026, 7, 26, tzinfo=UTC),
        worktrees=["/home/me/store/.worktrees/issue-5"],
    )
    assert read_receipt_worktrees(path) == ["/home/me/store/.worktrees/issue-5"]


def test_a_receipt_from_an_older_dispatcher_is_still_an_acknowledgement(tmp_path):
    """Receipts used to hold a bare timestamp. One is still a valid acknowledgement; it simply
    reports no paths. Unreadable is read the same way — a receipt nobody can parse must never
    look like a demand to delete something."""
    from agentdeck.poller_engine.github import read_receipt_worktrees, receipt_path

    legacy = receipt_path(tmp_path, "store", 5, "dispatch-1")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("2026-07-26T00:00:00+00:00\n")
    assert read_receipt_worktrees(legacy) == []

    garbage = receipt_path(tmp_path, "store", 6, "dispatch-2")
    garbage.write_text("{not json")
    assert read_receipt_worktrees(garbage) == []


def _receipts(tmp_path):
    from agentdeck.poller_engine.state import EnginePaths

    return EnginePaths.from_base(tmp_path / "state.json")


def _write(paths, repo, number, token):
    from datetime import UTC, datetime

    from agentdeck.poller_engine.github import write_receipt

    return write_receipt(
        paths.dispatch_ack_dir, repo, number, token, now=datetime(2026, 7, 26, tzinfo=UTC)
    )


def test_discarding_one_deliverys_receipt_removes_exactly_that_receipt(tmp_path):
    """`DISCARD_RECEIPTS` had no branch in `apply` at all, so it answered False like an unknown
    effect and every acknowledged dispatch recorded a cycle error (#279) — in the same health
    channel a real problem would have to be reported through."""
    paths = _receipts(tmp_path)
    mine, theirs = _write(paths, "store", 5, "d1"), _write(paths, "store", 5, "d2")

    port = adapter(receipts=paths)
    assert port.apply(
        Effect(EffectKind.DISCARD_RECEIPTS, "store", 5, {"dispatch_id": "d1", "generation": "g1"})
    ) is True
    assert not mine.exists()
    assert theirs.exists()
    # A receipt already gone is discarded, not a failure: the point is that it is not there.
    assert port.apply(
        Effect(EffectKind.DISCARD_RECEIPTS, "store", 5, {"dispatch_id": "d1"})
    ) is True


def test_capacity_deferral_is_read_and_discarded_with_delivery_receipts(tmp_path):
    from datetime import UTC, datetime

    from agentdeck.poller_engine.github import write_receipt

    paths = _receipts(tmp_path)
    deferred = write_receipt(
        paths.dispatch_defer_dir,
        "store",
        5,
        "d1",
        now=datetime(2026, 7, 26, tzinfo=UTC),
        extra={"reason": "at_capacity"},
    )
    port = adapter(receipts=paths)

    assert port.dispatch_deferred("store", 5, {"id": "d1"}) == "at_capacity"
    assert port.dispatch_deferred("store", 5, {"id": "d2"}) is None
    assert port.apply(
        Effect(EffectKind.DISCARD_RECEIPTS, "store", 5, {"dispatch_id": "d1"})
    ) is True
    assert not deferred.exists()


def test_discarding_every_receipt_a_card_holds_spares_the_other_cards(tmp_path):
    """A released card asks for all of its receipts. The file name is a digest and cannot be read
    back, so the card is recorded inside the receipt — and a receipt written before that (which
    names no card) is left alone rather than deleted on a guess."""
    from agentdeck.poller_engine.github import receipt_path

    paths = _receipts(tmp_path)
    ours = [_write(paths, "store", 5, token) for token in ("d1", "d2")]
    other = _write(paths, "store", 6, "d3")
    legacy = receipt_path(paths.dispatch_ack_dir, "store", 5, "old")
    legacy.write_text("2026-07-26T00:00:00+00:00\n")

    assert adapter(receipts=paths).apply(
        Effect(EffectKind.DISCARD_RECEIPTS, "store", 5, None)
    ) is True
    assert not any(path.exists() for path in ours)
    assert other.exists()
    assert legacy.exists()


def test_an_adapter_that_stores_no_receipts_has_nothing_to_discard():
    assert adapter().apply(Effect(EffectKind.DISCARD_RECEIPTS, "store", 5, None)) is True


def test_the_real_adapter_answers_every_effect_the_engine_can_declare(tmp_path):
    """`apply` falls through to False for anything it does not recognize, which is right for an
    unknown effect and wrong for one the port declares — and the FakePort the cycle tests use
    implemented `DISCARD_RECEIPTS` while the real adapter never had, so nothing failed (#279).

    This asserts the two implementations of `EnginePort` actually agree on the vocabulary.
    """
    from agentdeck.poller_engine.config import ResolvedBinding

    api = FakeGh(
        [
            (("--json id",), ok({"id": "I_issue"})),
            (
                ("addProjectV2ItemById",),
                ok({"data": {"addProjectV2ItemById": {"item": {"id": "I"}}}}),
            ),
            (
                ("updateProjectV2ItemFieldValue",),
                ok({"data": {"updateProjectV2ItemFieldValue": {}}}),
            ),
        ]
    )
    port = adapter(
        api=api,
        receipts=_receipts(tmp_path),
        binding=ResolvedBinding(
            project_id="P", status_field_id="F", status_option_ids={Role.IN_REVIEW.value: "opt"}
        ),
    )
    values = {
        EffectKind.SET_STATUS: Role.IN_REVIEW.value,
        EffectKind.REACT: {"comment": 1, "reaction": "eyes"},
        EffectKind.COMMENT: "hello",
        EffectKind.REMOVE_WORKTREES: [],
        EffectKind.SET_TITLE: "A title somebody can read",
        EffectKind.SET_BODY: "A body somebody can read",
    }
    for kind in EffectKind:
        result = port.apply(Effect(kind, "store", 5, values.get(kind)))
        if kind is EffectKind.REMOVE_WORKTREES:
            assert result == WorktreeCleanup(), kind
        else:
            assert result is True, kind


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def test_worktree_cleanup_validates_every_path_before_force_removal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "--initial-branch=master", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "tracked").write_text("one")
    _git("add", "tracked", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    valid = repo / ".worktrees" / "issue-5"
    _git("worktree", "add", "-b", "agent/issue-5", str(valid), cwd=repo)
    unsafe = repo / "ordinary-directory"
    unsafe.mkdir()

    result = adapter().remove_worktrees([str(valid), str(unsafe)])

    assert result == WorktreeCleanup((str(unsafe),))
    assert not valid.exists()
    assert unsafe.is_dir(), "an unregistered path must never reach git worktree remove --force"


def test_dirty_counts_read_only_validated_worktrees_and_degrade_to_none(tmp_path):
    """#787: the park comment's evidence read. Same eligibility rule as removal — an
    unregistered path is never inspected — and every failure is None, never an exception."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "--initial-branch=master", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "tracked").write_text("one")
    _git("add", "tracked", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    valid = repo / ".worktrees" / "issue-5"
    _git("worktree", "add", "-b", "agent/issue-5", str(valid), cwd=repo)
    (valid / "left-behind.py").write_text("progress")
    (valid / "tracked").write_text("edited")
    unsafe = repo / "ordinary-directory"
    unsafe.mkdir()

    counts = adapter().worktree_dirty_counts([str(valid), str(unsafe)])

    assert counts == {str(valid): 2, str(unsafe): None}


def _checkout_with_worktree(tmp_path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "--initial-branch=master", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "tracked").write_text("one")
    _git("add", "tracked", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    tree = repo / ".worktrees" / "issue-5"
    _git("worktree", "add", "-b", "agent/issue-5", str(tree), cwd=repo)
    return repo, tree


def test_changed_paths_report_committed_and_local_work_with_their_checkout(tmp_path):
    """#1736: the evidence writer serialization is decided on.

    Committed, modified and untracked work are one answer, because all three are what this
    worktree would carry into a pull request. The checkout travels back with the paths so the
    engine never reads two repositories' `README.md` as the same file, and the same
    eligibility rule as every other worktree read applies — an unregistered path is `None`.
    """
    repo, tree = _checkout_with_worktree(tmp_path)
    # The base the committed half is measured against is whatever the clone says its default
    # branch is, so the fixture has to have one — a `git init` checkout has no remote at all.
    _git("update-ref", "refs/remotes/origin/master", "master", cwd=repo)
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master", cwd=repo)
    (tree / "committed.py").write_text("landed")
    _git("add", "committed.py", cwd=tree)
    _git("commit", "-m", "work", cwd=tree)
    (tree / "tracked").write_text("edited")
    (tree / "scratch.py").write_text("progress")
    unsafe = repo / "ordinary-directory"
    unsafe.mkdir()

    changed = adapter().worktree_changed_paths([str(tree), str(unsafe)])

    assert changed[str(unsafe)] is None
    assert changed[str(tree)].checkout == str(repo)
    assert changed[str(tree)].local == ("scratch.py", "tracked")
    assert [paths for _commit, paths in changed[str(tree)].commits] == [("committed.py",)]
    assert changed[str(tree)].paths == ("committed.py", "scratch.py", "tracked")


def test_committed_work_is_reported_per_commit_so_shared_history_can_be_cancelled(tmp_path):
    """#2065: the branch diff alone cannot say which commits are this card's own work.

    `origin/master` is only as fresh as the clone's last fetch, so the base here deliberately
    lags behind what the branch was cut from — exactly the shape that made two unrelated cards
    look like they were writing the same files. Each commit keeps its own paths, so a
    caller can tell inherited history from this card's own work.
    """
    repo, tree = _checkout_with_worktree(tmp_path)
    _git("update-ref", "refs/remotes/origin/master", "master", cwd=repo)
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master", cwd=repo)
    # Already merged upstream, but not into this clone's `origin/master` yet.
    (tree / "inherited.vue").write_text("somebody else's feature")
    _git("add", "inherited.vue", cwd=tree)
    _git("commit", "-m", "upstream work this branch was cut on top of", cwd=tree)
    (tree / "mine.py").write_text("this card's work")
    _git("add", "mine.py", cwd=tree)
    _git("commit", "-m", "the card's own commit", cwd=tree)

    changed = adapter().worktree_changed_paths([str(tree)])

    assert [paths for _commit, paths in changed[str(tree)].commits] == [
        ("mine.py",), ("inherited.vue",),
    ]
    ids = [commit for commit, _paths in changed[str(tree)].commits]
    assert ids == _git(
        "log", "--format=%H", "origin/master..HEAD", cwd=tree,
    ).stdout.split()


def test_two_cards_on_different_stale_prefixes_each_measure_from_their_own_base(tmp_path):
    """#2065: the boundary is this branch's own creation point, not a comparison.

    The first fix cancelled a commit only when another compared card also carried it, which
    says nothing about upstream work the other card happens *not* to have. Here both cards are
    cut from a master this clone has not fetched, and the later one is cut from a newer prefix
    of it — and the commit only it inherited touches the very file the earlier card is really
    editing. Measured from each branch's recorded base, neither card is charged with it.
    """
    repo, early = _checkout_with_worktree(tmp_path)
    stale = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _git("update-ref", "refs/remotes/origin/master", stale, cwd=repo)
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master", cwd=repo)
    # Master moves on upstream; this clone's `origin/master` stays where it was.
    (repo / "shared.py").write_text("upstream feature")
    _git("add", "shared.py", cwd=repo)
    _git("commit", "-m", "landed upstream after the last fetch", cwd=repo)
    late = repo / ".worktrees" / "issue-6"
    _git("worktree", "add", "-b", "agent/issue-6", str(late), "master", cwd=repo)
    # The earlier card is genuinely editing the file the later card only inherited.
    (early / "shared.py").write_text("the early card's own work")
    _git("add", "shared.py", cwd=early)
    _git("commit", "-m", "the early card's own commit", cwd=early)
    (late / "mine.py").write_text("the late card's own work")
    _git("add", "mine.py", cwd=late)
    _git("commit", "-m", "the late card's own commit", cwd=late)

    changed = adapter().worktree_changed_paths([str(early), str(late)])

    assert [paths for _commit, paths in changed[str(early)].commits] == [("shared.py",)]
    assert [paths for _commit, paths in changed[str(late)].commits] == [("mine.py",)]


def test_upstream_a_worker_merged_in_after_its_base_is_not_that_cards_work(tmp_path):
    """#2065: only one card has the merged-in commit, so nothing else can cancel it.

    The branch base keeps out history older than the branch, but a worker that merges master
    mid-work gains upstream commits *after* it — and if no other compared card happens to
    carry the same SHA there is nothing to cancel it against. Walking the card's own line
    with `--first-parent` never enters what the merge brought in, so the later card is not
    charged with a file the earlier card is genuinely editing.
    """
    repo, early = _checkout_with_worktree(tmp_path)
    _git("update-ref", "refs/remotes/origin/master", "master", cwd=repo)
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master", cwd=repo)
    late = repo / ".worktrees" / "issue-6"
    _git("worktree", "add", "-b", "agent/issue-6", str(late), "master", cwd=repo)
    # Master moves on, and only the later card merges it in.
    (repo / "shared.py").write_text("upstream feature")
    _git("add", "shared.py", cwd=repo)
    _git("commit", "-m", "landed upstream", cwd=repo)
    _git("merge", "--no-ff", "-m", "Merge branch 'master'", "master", cwd=late)
    (late / "mine.py").write_text("the late card's own work")
    _git("add", "mine.py", cwd=late)
    _git("commit", "-m", "the late card's own commit", cwd=late)
    # The earlier card is genuinely editing the file that merge brought in.
    (early / "shared.py").write_text("the early card's own work")
    _git("add", "shared.py", cwd=early)
    _git("commit", "-m", "the early card's own commit", cwd=early)

    changed = adapter().worktree_changed_paths([str(early), str(late)])

    assert [paths for _commit, paths in changed[str(late)].commits] == [("mine.py",), ()]
    assert [paths for _commit, paths in changed[str(early)].commits] == [("shared.py",)]


def test_a_branch_with_no_reflog_falls_back_to_the_clones_default_branch(tmp_path):
    """A reflog this clone never had is ordinary, so the old, wider base still answers."""
    repo, tree = _checkout_with_worktree(tmp_path)
    _git("update-ref", "refs/remotes/origin/master", "master", cwd=repo)
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master", cwd=repo)
    (tree / "committed.py").write_text("landed")
    _git("add", "committed.py", cwd=tree)
    _git("commit", "-m", "work", cwd=tree)
    (repo / ".git" / "logs" / "refs" / "heads" / "agent" / "issue-5").unlink()

    changed = adapter().worktree_changed_paths([str(tree)])

    assert [paths for _commit, paths in changed[str(tree)].commits] == [("committed.py",)]


def test_a_checkout_with_no_default_branch_still_reports_the_work_on_disk(tmp_path):
    """A clone whose `origin/HEAD` is unset is ordinary, not broken.

    It used to cost the committed half of the answer, leaving only the edits on disk. Since
    the base is read from the branch's own reflog it costs nothing at all (#2065) — but the
    point of the case is unchanged: such a checkout still reports its work rather than
    vanishing from the answer.
    """
    _repo, tree = _checkout_with_worktree(tmp_path)
    (tree / "committed.py").write_text("landed")
    _git("add", "committed.py", cwd=tree)
    _git("commit", "-m", "work", cwd=tree)
    (tree / "scratch.py").write_text("progress")

    changed = adapter().worktree_changed_paths([str(tree)])

    assert changed[str(tree)].local == ("scratch.py",)
    assert changed[str(tree)].paths == ("committed.py", "scratch.py")


def test_a_worktree_git_cannot_read_at_all_is_reported_as_unknown(tmp_path):
    """Deciding what an unreadable checkout means is the caller's job; this only says it does
    not know."""
    repo, tree = _checkout_with_worktree(tmp_path)
    (repo / ".git").rename(repo / ".git-moved")

    assert adapter().worktree_changed_paths([str(tree)]) == {str(tree): None}


def test_a_worktree_still_holding_uncommitted_work_is_kept_not_force_removed(tmp_path):
    """#1718: `git worktree remove --force` deleted a live session's unfinished patch.

    Retained is deliberately not `failed`. Reporting it pending would make the card
    unforgettable and retry the same refusal on every wake (#1189); the release is allowed
    to finish, and the directory is handed to the operator sweep instead.
    """
    _repo, tree = _checkout_with_worktree(tmp_path)
    (tree / "tracked").write_text("edited but never committed")
    (tree / "scratch.py").write_text("progress")

    result = adapter().remove_worktrees([str(tree)])

    assert result == WorktreeCleanup((), ((str(tree), "2 uncommitted file(s)"),))
    assert result.landed, "a deliberate keep is not a failed effect"
    assert (tree / "scratch.py").exists(), "the worker's unfinished work is still there"


def test_a_clean_worktree_is_still_released_by_an_ordinary_removal(tmp_path):
    """The guard is about work, not about worktrees: nothing to lose, nothing to keep."""
    _repo, tree = _checkout_with_worktree(tmp_path)

    assert adapter().remove_worktrees([str(tree)]) == WorktreeCleanup()
    assert not tree.exists()


def test_work_written_after_the_precheck_is_still_not_deleted(tmp_path, monkeypatch):
    """The precheck cannot close the window; the removal itself has to refuse.

    A `git status` read and a `git worktree remove --force` are two processes, so a session
    writing between them loses exactly the work the read was meant to protect — the same
    class of loss #1718 is about. The stubbed precheck below *is* that race: it reports the
    clean checkout it saw, while the directory on disk is already dirty. Git's non-forced
    removal declines it as one operation, and that refusal is a keep, not a failure.
    """
    _repo, tree = _checkout_with_worktree(tmp_path)
    (tree / "tracked").write_text("written a moment after the check returned clean")
    (tree / "scratch.py").write_text("progress")
    monkeypatch.setattr(github_module, "_holds_work", _clean_then_real())

    result = adapter().remove_worktrees([str(tree)])

    assert result.failed == (), "a refusal that saved work is not a cleanup failure"
    assert [path for path, _ in result.retained] == [str(tree)]
    assert result.retained[0][1] == "2 uncommitted file(s)"
    assert (tree / "scratch.py").exists(), "the work written mid-removal is still there"
    assert tree.is_dir()


def _clean_then_real():
    """A precheck that reports clean once, then answers honestly — i.e. loses the race."""
    real = github_module._holds_work
    calls = {"n": 0}

    def probe(target, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""
        return real(target, **kwargs)

    return probe


def test_a_clean_worktree_git_refuses_for_its_submodules_is_kept_not_failed(tmp_path):
    """Git declines a worktree that ever had a submodule without --force; a retry never helps."""
    repo, tree = _checkout_with_worktree(tmp_path)
    (repo / ".git" / "worktrees" / "issue-5" / "modules").mkdir()

    result = adapter().remove_worktrees([str(tree)])

    assert result.failed == ()
    assert [path for path, _ in result.retained] == [str(tree)]
    assert "submodules" in result.retained[0][1]
    assert tree.is_dir()


def test_a_worktree_whose_state_cannot_be_read_is_kept_rather_than_deleted(tmp_path,
                                                                           monkeypatch):
    """An unknown is never permission — the same rule the reaper already follows."""
    _repo, tree = _checkout_with_worktree(tmp_path)
    monkeypatch.setattr(github_module, "_dirty_count", lambda target, **kwargs: None)

    result = adapter().remove_worktrees([str(tree)])

    assert result == WorktreeCleanup((), ((str(tree), "its state could not be read"),))
    assert tree.is_dir()


def test_an_already_removed_valid_worktree_path_is_idempotently_clean(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "--initial-branch=master", cwd=repo)
    absent = repo / ".worktrees" / "issue-5"

    assert adapter().remove_worktrees([str(absent)]) == WorktreeCleanup()


def test_a_nested_worktree_whose_whole_checkout_vanished_is_clean_not_pending(tmp_path):
    """#1189: the outer checkout goes first and takes the inner path's repository with it.

    Nothing is left to remove and no repository can be attributed, so reporting this as
    pending made the card unforgettable: every wake retried it, every wake's report carried
    an error, and no activation canary could ever come back clean.
    """
    outer = tmp_path / "trailhead" / ".worktrees" / "issue-207-issues"
    nested = outer / "web" / ".worktrees" / "issue-207"

    assert not nested.exists(), "the whole checkout is gone, not just the worktree"
    assert adapter().remove_worktrees([str(nested)]) == WorktreeCleanup()


def test_something_still_on_the_host_that_no_checkout_claims_stays_pending(tmp_path):
    """The other half of the same rule: absent is clean, present-but-unattributable is not.

    A dangling symlink counts as present. Something is there, nothing owns it, and writing
    it off as released would hide a real leftover behind the fix above.
    """
    orphan = tmp_path / "nowhere" / ".worktrees" / "issue-5"
    orphan.mkdir(parents=True)
    dangling = tmp_path / "elsewhere" / ".worktrees" / "issue-6"
    dangling.parent.mkdir(parents=True)
    dangling.symlink_to(tmp_path / "gone")

    result = adapter().remove_worktrees([str(orphan), str(dangling)])

    assert result == WorktreeCleanup((str(orphan), str(dangling)))
    assert dangling.is_symlink(), "a dangling symlink is a leftover, not a clean path"


def test_a_relative_cleanup_path_is_refused_rather_than_written_off(tmp_path, monkeypatch):
    """Absent *relative to some cwd* says nothing about the host, so it stays pending."""
    monkeypatch.chdir(tmp_path)

    assert adapter().remove_worktrees(["repo/.worktrees/issue-5"]) == WorktreeCleanup(
        ("repo/.worktrees/issue-5",)
    )


def test_the_adapter_reports_only_the_paths_its_own_receipt_named(tmp_path):
    from datetime import UTC, datetime

    from agentdeck.poller_engine.github import write_receipt
    from agentdeck.poller_engine.state import EnginePaths

    paths = EnginePaths.from_base(tmp_path / "state.json")
    write_receipt(
        paths.dispatch_ack_dir, "store", 5, "d1",
        now=datetime(2026, 7, 26, tzinfo=UTC), worktrees=["/w/one"],
    )
    port = adapter(receipts=paths)
    assert port.dispatch_worktrees("store", 5, {"id": "d1"}) == ["/w/one"]
    # A delivery with no receipt reports nothing rather than guessing.
    assert port.dispatch_worktrees("store", 5, {"id": "d2"}) == []


def test_the_adapter_reads_the_chat_its_own_receipt_named_and_only_a_public_one(tmp_path):
    """#2155: the host records which chat took a delivery; the engine only reads it back."""
    from datetime import UTC, datetime

    from agentdeck.poller_engine.github import write_receipt
    from agentdeck.poller_engine.state import EnginePaths

    paths = EnginePaths.from_base(tmp_path / "state.json")
    now = datetime(2026, 9, 24, tzinfo=UTC)
    url = "https://deck.example/sessions/claude_code:main:s1"
    write_receipt(paths.dispatch_ack_dir, "store", 5, "d1", now=now, extra={"session_url": url})
    write_receipt(
        paths.dispatch_ack_dir, "store", 5, "d2", now=now,
        extra={"session_url": "http://127.0.0.1:8756/sessions/claude_code:main:s1"},
    )
    write_receipt(paths.dispatch_ack_dir, "store", 5, "d3", now=now)
    port = adapter(receipts=paths)

    assert port.dispatch_session_url("store", 5, {"id": "d1"}) == url
    # A loopback link opens the reader's own machine, so it is never carried on.
    assert port.dispatch_session_url("store", 5, {"id": "d2"}) == ""
    assert port.dispatch_session_url("store", 5, {"id": "d3"}) == ""
    assert port.dispatch_session_url("store", 5, {"id": "missing"}) == ""


def test_the_scan_cursor_survives_the_url_it_is_put_in():
    """A cursor is an ISO timestamp and carries a `+00:00` offset. In a query string `+` means
    a space, so GitHub received an invalid `since`, ignored the request and answered `[]`.

    Every wake. Since the day this was written. The poller scanned faithfully, reported no scan
    failures, and could never ingest a single command.
    """
    api = FakeGh(default=(0, "[]", ""))
    adapter(api=api).scan_comments(since="2026-07-26T07:37:18.901699+00:00")

    url = next(arg for arg in api.calls[-1] if "issues/comments" in str(arg))
    assert "+00:00" not in url, "a raw + in a query string is a space, not a plus"
    assert "%2B00%3A00" in url


def test_the_cursor_round_trips_back_to_the_value_that_was_sent():
    """The encoding must be reversible, or the engine would be asking for a different moment
    than the one it recorded — a subtler version of the same bug."""
    from urllib.parse import parse_qs, urlsplit

    cursor = "2026-07-26T07:37:18.901699+00:00"
    api = FakeGh(default=(0, "[]", ""))
    adapter(api=api).scan_comments(since=cursor)

    url = next(arg for arg in api.calls[-1] if "issues/comments" in str(arg))
    assert parse_qs(urlsplit(url).query)["since"] == [cursor]


def test_the_board_status_of_a_card_can_actually_be_read():
    meta = {"state": "OPEN", "labels": [], "updatedAt": None}
    api = FakeGh([(("graphql",), ok(card_batch(meta, status="In Progress")))])

    assert adapter(api=api).project_status("agentdeck", 7) == Role.IN_PROGRESS.value


def test_the_status_rides_the_same_graphql_query_as_card_metadata():
    api = FakeGh([(("graphql",), ok(card_batch(None)))])
    adapter(api=api).project_status("agentdeck", 7)

    assert len([call for call in api.calls if "graphql" in call]) == 1
    assert not any("issues/7" in " ".join(call) for call in api.calls)


# --- the ownership label the Manifest named, not the engine's default (#108) ---

def test_the_adapter_reads_ownership_against_the_projects_own_label():
    """A card is owned when it carries *this project's* label, not the engine's default."""
    from agentdeck.poller_engine.github import GitHubAdapter, _is_label_added_event

    adapter = GitHubAdapter(
        org="acme", issue_repos=("app",), code_repos=("app",), project_number=1,
        ownership_label="agent",
    )
    assert adapter.ownership_label == "agent"
    # The label-event predicate is asked which label to look for rather than deciding itself,
    # so one shared engine can serve projects that own their cards with different labels.
    labeled = {"event": "labeled", "id": 1, "label": {"name": "agent"}}
    assert _is_label_added_event(labeled, "agent")
    assert _is_label_added_event(labeled, "AGENT")
    assert not _is_label_added_event(labeled, "claude")


def test_an_engine_with_no_manifest_label_still_owns_with_claude():
    """Defaulted, so a project that never names one behaves exactly as it did before."""
    from agentdeck.poller_engine.github import OWNERSHIP_LABEL, GitHubAdapter

    adapter = GitHubAdapter(
        org="acme", issue_repos=("app",), code_repos=("app",), project_number=1,
    )
    assert adapter.ownership_label == OWNERSHIP_LABEL == "claude"


# --- per-project labels: the engine never learns their names, only the facts ---

def _labelled(*names):
    return card_batch(
        {"state": "OPEN", "title": "t", "body": "", "updatedAt": "2026-07-25T11:00:00Z",
         "labels": [{"name": name} for name in names]},
        status="In review",
    )


def test_a_card_is_owned_by_any_alias_and_reports_the_spec_only_label():
    api = FakeGh([(("graphql",), ok(_labelled("CLAUDE", "SPEC-ONLY", "AUTOMERGE")))])
    snapshot = adapter(
        api=api,
        ownership_label="agent",
        ownership_labels=("agent", "claude"),
        spec_only_label="spec-only",
    ).snapshot("agentdeck", 7)
    assert snapshot.owned is True
    assert snapshot.spec_only is True
    # The retired standing-approval label is not a fact the engine reads any more (#2502).
    assert not hasattr(snapshot, "automerge")


def test_a_card_reports_the_paused_label_only_when_the_manifest_names_one():
    """#2548: label identity is case-insensitive, and no label means no pause."""
    api = FakeGh([(("graphql",), ok(_labelled("agent", "Paused")))])
    snapshot = adapter(api=api, ownership_label="agent", paused_label="paused").snapshot(
        "agentdeck", 7
    )
    assert snapshot.paused is True
    api = FakeGh([(("graphql",), ok(_labelled("agent", "paused")))])
    assert adapter(api=api, ownership_label="agent").snapshot("agentdeck", 7).paused is False


def test_a_terminal_marker_on_the_stopped_generation_is_reported_as_such():
    """The proof a stop is waiting for, previously invisible to both checks (#299).

    A worker finishing as the stop lands posts its ordinary terminal marker under the
    generation it worked with — the one `request_stop` invalidated. Matched against the
    fresh generation it looks like a superseded worker's news.
    """
    body = "Done.\n<!-- claude-terminal generation=old status=in_review -->"
    def snapshot_of(**kwargs):
        api = FakeGh([(("graphql",), ok(card_batch(
            {"state": "OPEN", "labels": [], "updatedAt": None}, comments=[body]
        )))])
        return adapter(api=api).snapshot("a", 1, **kwargs)

    stopped = snapshot_of(generation="new", stop_generation="old")
    assert stopped.stopped_terminal_marker == "In review"
    assert stopped.terminal_marker is None, "it is not news under the current lease"
    assert stopped.execution_stopped is False, "a terminal marker is not the Runtime's proof"

    # No stop in flight: nothing to report, and the current-lease check is unchanged.
    assert snapshot_of(generation="old").stopped_terminal_marker is None
    assert snapshot_of(generation="old").terminal_marker == "In review"


def test_a_terminal_marker_carries_when_its_comment_was_posted():
    """The Run's own end, which a racing `/claude stop` is compared with (#2448)."""
    marker = {
        "body": "Done.\n<!-- claude-terminal generation=old status=in_review -->",
        "createdAt": "2026-07-25T11:58:00Z",
    }
    def snapshot_of(**kwargs):
        api = FakeGh([(("graphql",), ok(card_batch(
            {"state": "OPEN", "labels": [], "updatedAt": None},
            comments=["earlier", marker, {"body": "later", "createdAt": "2026-07-25T11:59:00Z"}],
        )))])
        return adapter(api=api).snapshot("a", 1, **kwargs)

    assert snapshot_of(generation="old").terminal_at == "2026-07-25T11:58:00Z"
    stopped = snapshot_of(generation="new", stop_generation="old")
    assert stopped.stopped_terminal_at == "2026-07-25T11:58:00Z"
    assert stopped.terminal_at == ""
    assert snapshot_of(generation="other").terminal_at == ""


# --- finding an issue this engine already opened (#393) ---

_MARK = "<!-- poller-opened: ext:sentry-autofix:WEB-7 -->"
_SINCE = "2026-07-27T12:00:00+00:00"


def test_a_marked_issue_is_found_by_listing_not_by_search():
    api = FakeGh(answers=[(("issue", "list"), ok([
        {"number": 9, "body": "unrelated", "createdAt": "2026-07-27T12:30:00Z"},
        {"number": 8, "body": f"blah\n{_MARK}", "createdAt": "2026-07-27T12:10:00Z"},
    ]))])
    assert adapter(api=api).find_marked_issue("agentdeck", _MARK, since=_SINCE) == (8, True)
    # A search would be wrong here: GitHub's index lags creation, and this is asked precisely
    # about an issue created moments ago.
    assert not api.asked("--search")


def test_absence_is_only_conclusive_once_the_listing_reaches_past_the_intent():
    """The hole this closes: a bounded page on a busy repository can miss the issue entirely,
    and calling that absence is exactly how the duplicate gets created."""
    older_than_intent = ok([
        {"number": 9, "body": "unrelated", "createdAt": "2026-07-27T12:30:00Z"},
        {"number": 7, "body": "also unrelated", "createdAt": "2026-07-27T11:00:00Z"},
    ])
    found, conclusive = adapter(
        api=FakeGh(answers=[(("issue", "list"), older_than_intent)])
    ).find_marked_issue("agentdeck", _MARK, since=_SINCE)
    assert (found, conclusive) == (None, True), "it saw past the intent, so absence is real"

    # Every issue on the page is newer than the intent, so the window is not covered.
    all_newer = ok([
        {"number": 9, "body": "unrelated", "createdAt": "2026-07-27T12:30:00Z"},
        {"number": 8, "body": "unrelated", "createdAt": "2026-07-27T12:20:00Z"},
    ])
    found, conclusive = adapter(
        api=FakeGh(answers=[(("issue", "list"), all_newer)])
    ).find_marked_issue("agentdeck", _MARK, since=_SINCE)
    assert (found, conclusive) == (None, False), "a page that proves nothing must say so"


def test_an_unreadable_listing_is_never_absence():
    api = FakeGh(default=(1, "", "boom"))
    assert adapter(api=api).find_marked_issue("agentdeck", _MARK, since=_SINCE) == (None, False)


def test_an_empty_repository_is_conclusively_without_the_issue():
    api = FakeGh(answers=[(("issue", "list"), ok([]))])
    assert adapter(api=api).find_marked_issue("agentdeck", _MARK, since=_SINCE) == (None, True)


def test_opening_an_issue_reads_its_number_off_the_url_it_prints():
    api = FakeGh(answers=[
        (("issue", "create"), (0, "https://github.com/eerovil/agentdeck/issues/412\n", "")),
    ])
    subject = adapter(api=api)
    assert subject.open_issue("agentdeck", "boom", "body") == 412
    # Owned in the same write, so the card is never briefly unlabelled.
    assert api.asked("--label", subject.ownership_label)


def test_an_extra_label_goes_on_in_the_same_write_as_ownership():
    api = FakeGh(answers=[
        (("issue", "create"), (0, "https://github.com/eerovil/agentdeck/issues/9\n", "")),
    ])
    subject = adapter(api=api)
    assert subject.open_issue("agentdeck", "t", "b", labels=("spec-only",)) == 9
    (call,) = [call for call in api.calls if call[:2] == ("issue", "create")]
    labels = [call[i + 1] for i, arg in enumerate(call) if arg == "--label"]
    assert labels == [subject.ownership_label, "spec-only"]


def test_a_notice_printed_beside_the_url_is_not_read_as_a_number():
    api = FakeGh(answers=[(("issue", "create"), (
        0, "Creating issue in eerovil/agentdeck\nhttps://github.com/eerovil/agentdeck/issues/7\n",
        ""))])
    assert adapter(api=api).open_issue("agentdeck", "boom", "body") == 7


def test_a_refused_creation_answers_none_rather_than_guessing():
    assert adapter(api=FakeGh(default=(1, "", "denied"))).open_issue("a", "t", "b") is None


# --- window readings for pacing (follow-up to #1137) ---


def _paced_usage(tmp_path, label, *, provider="claude_code", five=0.0, five_in_h=1.0):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    (tmp_path / f"usage-{provider}-{label}.json").write_text(
        json.dumps(
            {
                "account": f"{provider}:{label}",
                "fetched_at": now.isoformat(),
                "five_hour_pct": five,
                "five_hour_resets_at": (now + timedelta(hours=five_in_h)).isoformat(),
                "seven_day_pct": 10.0,
                "seven_day_resets_at": None,
                "month_pct": None,
                "month_resets_at": None,
            }
        )
    )


def test_window_readings_report_only_windows_with_both_a_meter_and_a_boundary(tmp_path):
    """Pacing needs the boundary as well as the number. A window carrying one without the
    other is left out rather than paired with a guessed value."""
    from agentdeck.poller_engine.github import account_window_readings

    _paced_usage(tmp_path, "main", five=45.0, five_in_h=4.25)
    readings = account_window_readings(tmp_path, "claude_code:main")

    assert [(window, pct) for window, pct, _ in readings] == [("five_hour", 45.0)]
    assert readings[0][2].tzinfo is not None


def test_window_readings_are_empty_without_a_usable_snapshot(tmp_path):
    from agentdeck.poller_engine.github import account_window_readings

    assert account_window_readings(tmp_path, "claude_code:nobody") == ()
    assert account_window_readings(None, "claude_code:main") == ()
    # The older cache format has no reset boundaries at all, so it reports no windows here
    # while `account_usage_pct` still reads its percentages.
    _usage(tmp_path, "main", five=80.0)
    assert account_window_readings(tmp_path, "claude_code:main") == ()


# --- budget about to expire unspent (#1965) ---


def _weekly(tmp_path, label, *, provider="claude_code", pct, resets_in_h, month=None):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    payload = {
        "account": f"{provider}:{label}",
        "fetched_at": now.isoformat(),
        "seven_day_pct": pct,
        "seven_day_resets_at": (now + timedelta(hours=resets_in_h)).isoformat(),
    }
    if month is not None:
        month_pct, month_in_h = month
        payload["month_pct"] = month_pct
        payload["month_resets_at"] = (now + timedelta(hours=month_in_h)).isoformat()
    (tmp_path / f"usage-{provider}-{label}.json").write_text(json.dumps(payload))


def test_a_window_about_to_reset_part_spent_reports_what_it_will_waste(tmp_path):
    """The operator's own example: alt resets in 12 hours and sits at 85%."""
    from agentdeck.poller_engine.github import account_expiring_headroom

    _weekly(tmp_path, "alt", pct=85.0, resets_in_h=12.0)

    assert account_expiring_headroom(tmp_path, "claude_code:alt") == 10.0
    assert account_expiring_headroom(tmp_path, "claude_code:alt", target_pct=99.0) == 14.0


def test_a_window_with_days_left_to_run_wastes_nothing_yet(tmp_path):
    """Being behind is only a loss once the remainder cannot plausibly be spent."""
    from agentdeck.poller_engine.github import account_expiring_headroom

    _weekly(tmp_path, "alt", pct=20.0, resets_in_h=120.0)

    assert account_expiring_headroom(tmp_path, "claude_code:alt") == 0.0


def test_an_account_at_its_target_and_an_unknown_one_both_waste_nothing(tmp_path):
    """Ignorance must not promote an account over one with a real reading."""
    from agentdeck.poller_engine.github import account_expiring_headroom

    _weekly(tmp_path, "main", pct=97.0, resets_in_h=3.0)

    assert account_expiring_headroom(tmp_path, "claude_code:main") == 0.0
    assert account_expiring_headroom(tmp_path, "claude_code:nobody") == 0.0
    assert account_expiring_headroom(None, "claude_code:main") == 0.0


def test_a_target_of_zero_opts_the_account_out(tmp_path):
    from agentdeck.poller_engine.github import account_expiring_headroom

    _weekly(tmp_path, "alt", pct=40.0, resets_in_h=2.0)

    assert account_expiring_headroom(tmp_path, "claude_code:alt", target_pct=0.0) == 0.0


def test_the_five_hour_rate_limit_is_not_a_budget_that_expires(tmp_path):
    """A five-hour window refills on its own, so leaving it part spent costs nothing and
    chasing it would flip accounts every few hours for no gain."""
    from agentdeck.poller_engine.github import account_expiring_headroom

    _paced_usage(tmp_path, "alt", five=20.0, five_in_h=0.25)

    assert account_expiring_headroom(tmp_path, "claude_code:alt") == 0.0


def test_the_worst_expiring_window_is_the_one_that_counts(tmp_path):
    """Two budget windows can both be closing; the account wastes the larger of the two."""
    from agentdeck.poller_engine.github import account_expiring_headroom

    _weekly(tmp_path, "alt", pct=90.0, resets_in_h=6.0, month=(60.0, 100.0))

    assert account_expiring_headroom(tmp_path, "claude_code:alt") == 35.0


# --- budget left per hour (#2757) ---


def test_budget_pace_is_points_left_per_hour_to_the_reset(tmp_path):
    from agentdeck.poller_engine.github import account_budget_pace

    _weekly(tmp_path, "alt", pct=11.0, resets_in_h=84.0)

    assert account_budget_pace(tmp_path, "claude_code:alt") == pytest.approx(1.0, rel=1e-3)
    assert account_budget_pace(tmp_path, "claude_code:alt", target_pct=0.0) == 0.0


def test_budget_pace_is_zero_past_the_target_and_none_when_unknown(tmp_path):
    from agentdeck.poller_engine.github import account_budget_pace

    _weekly(tmp_path, "main", pct=97.0, resets_in_h=30.0)

    assert account_budget_pace(tmp_path, "claude_code:main") == 0.0
    assert account_budget_pace(tmp_path, "claude_code:nobody") is None
    assert account_budget_pace(None, "claude_code:main") is None


def test_budget_pace_ignores_the_five_hour_window_and_a_past_reset(tmp_path):
    from agentdeck.poller_engine.github import account_budget_pace

    _paced_usage(tmp_path, "alt", five=20.0, five_in_h=0.25)
    assert account_budget_pace(tmp_path, "claude_code:alt") is None

    _weekly(tmp_path, "main", pct=10.0, resets_in_h=-1.0)
    assert account_budget_pace(tmp_path, "claude_code:main") is None


def test_budget_pace_takes_the_faster_of_two_budget_windows(tmp_path):
    from agentdeck.poller_engine.github import account_budget_pace

    _weekly(tmp_path, "alt", pct=55.0, resets_in_h=100.0, month=(35.0, 20.0))

    assert account_budget_pace(tmp_path, "claude_code:alt") == pytest.approx(3.0, rel=1e-3)


# --- when a refusal clears (#1726) ---


def test_a_refusal_clears_only_once_every_blocked_window_has_reset(tmp_path):
    """The reason names the first blocked window; the boundary is the last one to reset,
    because the account cannot run a turn until all of them have."""
    from datetime import UTC, datetime, timedelta

    from agentdeck.poller_engine.github import account_refusal_detail

    now = datetime.now(UTC)
    soon, later = now + timedelta(hours=2), now + timedelta(days=3)
    (tmp_path / "usage-claude_code-main.json").write_text(
        json.dumps(
            {
                "account": "claude_code:main",
                "fetched_at": now.isoformat(),
                "five_hour_pct": 100.0,
                "five_hour_resets_at": soon.isoformat(),
                "seven_day_pct": 100.0,
                "seven_day_resets_at": later.isoformat(),
            }
        )
    )

    reason, clears_at = account_refusal_detail(tmp_path, "claude_code:main")

    assert reason.startswith("five_hour window at 100%")
    assert clears_at == later


def test_an_account_that_is_not_refusing_reports_no_boundary(tmp_path):
    from agentdeck.poller_engine.github import account_refusal_detail

    _paced_usage(tmp_path, "alt", five=31.0)

    assert account_refusal_detail(tmp_path, "claude_code:alt") == ("", None)


def test_every_graphql_query_the_adapter_builds_is_syntactically_whole():
    """Brace-balanced, and no two field names run together by adjacent string literals.

    Both halves once shipped broken in a removed sweep (#1413): the ticket selection closed one
    of its two open selections, and `"...state body" "repository{name}"` concatenated into a
    field named `bodyrepository`. GitHub answered `Expected NAME, actual: (none)` and the
    engine reported an unreadable sweep on every wake — a whole feature inert, with nothing in
    the tests to notice, because a scripted `gh` never parses what it is asked.
    """
    asked: list[str] = []

    class Recording(FakeGh):
        def run(self, *args):
            for argument in args:
                if argument.startswith("query="):
                    asked.append(argument[len("query="):])
            return super().run(*args)

    subject = adapter(api=Recording())
    subject.labelled_cards()
    subject.snapshot_cards({"agentdeck#5": CardQuery("agentdeck", 5)})

    assert asked, "no GraphQL query was built at all"
    for query in asked:
        depth = 0
        for character in query:
            depth += (character == "{") - (character == "}")
            assert depth >= 0, f"closes a selection it never opened: {query}"
        assert depth == 0, f"leaves {depth} selection(s) unclosed: {query}"
        # A field name is letters; two of them may only meet across a separator, never directly
        # out of one literal into the next.
        assert "bodyrepository" not in query


def test_the_reserve_gate_reuses_a_reading_too_old_to_report_rather_than_buying_one(
    tmp_path, monkeypatch
):
    """#2082: a quiet wake must not spend a core call to find out whether it may spend core calls.

    `live_queries_halt` reaches here on every wake including the webhook-quiet ones, and those
    make no other GitHub call — so at the 30s reporting freshness the store was always empty by
    the time the gate looked, and the gate bought its own answer. Seven instances, ~670 wakes an
    hour. A reading minutes old still answers "is there room?" correctly, and errs towards
    refusing rather than towards overspending, because `remaining` only rises at the reset.

    That last sentence is only true while every spender records what it spends, and on a *full*
    wake one does not: `chatgpt-nudge` reads GitHub over raw `urllib` from a separate stdlib-only
    process and keeps none of the response headers. So the tolerance is offered per wake, and the
    default — a full wake — still buys a fresh reading.
    """
    cache = tmp_path / "github.sqlite3"
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(cache))
    calls = []

    def completed(argv, **_kwargs):
        calls.append(argv[1:])
        trace = (
            "* Request at now\n"
            "< HTTP/2.0 200 OK\n"
            "< X-RateLimit-Limit: 5000\n"
            "< X-RateLimit-Remaining: 4200\n"
            "< X-RateLimit-Resource: core\n"
            "* Request took 1ms\n"
        )
        stdout = (
            '{"id":20142828}'
            if argv[1:] == ["api", "user"]
            else '{"resources":{"core":{"remaining":4200},"graphql":{"remaining":700}}}'
        )
        return subprocess.CompletedProcess(argv, 0, stdout, trace)

    monkeypatch.setattr(subprocess, "run", completed)

    first = Gh(token="token-a").current_rate_limits(max_age=rate_limits.GATE_MAX_AGE_S)
    assert first["core"].remaining == 4200
    probes = calls.count(["api", "user"])

    # Well past the reporting freshness, well inside the gate's tolerance.
    base = time.time()
    monkeypatch.setattr(
        rate_limits,
        "time",
        SimpleNamespace(time=lambda: base + rate_limits.READING_MAX_AGE_S * 4),
    )

    later = Gh(token="token-a").current_rate_limits(max_age=rate_limits.GATE_MAX_AGE_S)

    assert later["core"].remaining == 4200
    assert calls.count(["api", "user"]) == probes, "the gate bought a reading it already had"

    # A full wake asks at the reporting freshness, because the nudge extension may have spent
    # core quota since without recording a header anywhere.
    assert Gh(token="token-a").current_rate_limits()["core"].remaining == 4200
    assert calls.count(["api", "user"]) > probes, "a full wake trusted an unaccountable reading"


def test_only_a_webhook_quiet_wake_is_offered_the_stale_reserve_reading(tmp_path, monkeypatch):
    """#2082 review: the stale-reading proof does not cover the nudge extension's raw calls.

    `chatgpt_nudge_core._get` spends the same core budget through `urllib` and throws the
    `x-ratelimit-*` response headers away, so between two full wakes inside `GATE_MAX_AGE_S` the
    real bucket can fall below the reserve while the gate still reads the older, higher number.
    A webhook-quiet wake is the one wake where that cannot happen — the extension's observe hook
    returns before asking GitHub anything — so the tolerance is handed out per wake, not globally.
    """
    cache = tmp_path / "github.sqlite3"
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(cache))
    api = FakeGh([(("rate_limit",), ok('{"resources":{"core":{"remaining":4200},'
                                       '"graphql":{"remaining":700}}}'))])

    assert adapter(api=api).live_queries_halt(stale_reading_ok=True) is None
    assert api.rate_limit_max_age == rate_limits.GATE_MAX_AGE_S

    assert adapter(api=api).live_queries_halt() is None
    assert api.rate_limit_max_age == rate_limits.READING_MAX_AGE_S


def _thread(*comments, remaining: int = 4000) -> tuple[int, str, str]:
    """One GraphQL answer shaped like the issue-comment page the retirement pass reads."""
    return ok(
        {
            "data": {
                "rateLimit": {
                    "remaining": remaining,
                    "resetAt": "2026-09-22T09:00:00Z",
                    "cost": 1,
                },
                "repository": {"issue": {"comments": {"nodes": list(comments)}}},
            }
        }
    )


def _bot(node_id: str, text: str = "Report.", minimized: bool = False) -> dict:
    return {"id": node_id, "isMinimized": minimized, "body": f"{text}\n\n{BOT_MARKER}"}


def _minimized_ids(api: FakeGh) -> list[str]:
    """Every comment id this run actually asked GitHub to hide."""
    mutations = [
        " ".join(call) for call in api.calls if "minimizeComment" in " ".join(call)
    ]
    return [
        part.split('"')[1]
        for call in mutations
        for part in call.split("subjectId:")[1:]
    ]


def test_a_new_poller_comment_hides_the_cards_older_poller_comments():
    """#2116: the thread grew by re-posting and nothing was ever marked superseded.

    agentdeck#2108 was 77 KB, 64 KB of it four near-identical review packets, one per
    re-dispatch. Only the newest stays open; the rest collapse to a grey row.
    """
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}, "r1": {}}})),
            (("graphql",), _thread(_bot("C_one"), _bot("C_two"), _bot("C_new"))),
        ]
    )

    assert adapter(api=api).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, "Report.")
    ) is True

    # The two older ones, and never the newest — that is the comment a reader opens the card
    # to find.
    assert _minimized_ids(api) == ["C_one", "C_two"]


def test_only_the_pollers_own_comments_are_ever_hidden():
    """A person's instruction and an external reviewer's verdict stay visible however old.

    The discriminator is the marker the engine appends to its own comments and to nothing
    else, so this holds without the adapter knowing which accounts are agents.
    """
    human = {"id": "C_human", "isMinimized": False, "body": "/claude please redo the guard"}
    reviewer = {"id": "C_review", "isMinimized": False, "body": "Looks good to me."}
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (("graphql",), _thread(_bot("C_old"), human, reviewer, _bot("C_new"))),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    assert _minimized_ids(api) == ["C_old"]


def _review(node_id: str, marker: bool = True, minimized: bool = False) -> dict:
    body = "/claude\nReview findings for PR #9 at abc123:\n\n1. a.py:1 — wrong → fix it"
    if marker:
        body += "\n\n" + CHATGPT_REVIEW_MARKER
    return {"id": node_id, "isMinimized": minimized, "body": body}


def test_older_chatgpt_reviews_are_hidden_and_the_newest_stays_open():
    """#2167: every review round re-posts a findings list, and only the newest still applies.

    Reviews posted before the reviewer carried a marker are recognised by their heading, and
    the newest review stays open beside the newest poller report even when it is older than
    that report, because the worker may not have answered it yet.
    """
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}, "r1": {}, "r2": {}, "r3": {}}})),
            (
                ("graphql",),
                _thread(
                    _review("R_legacy", marker=False),
                    {"id": "R_oneline", "isMinimized": False,
                     "body": "/claude Review findings for PR #9 at abc123:\n\n1. a.py:1"},
                    _bot("C_old"),
                    _review("R_mid"),
                    _review("R_new"),
                    _bot("C_new"),
                ),
            ),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    assert sorted(_minimized_ids(api)) == ["C_old", "R_legacy", "R_mid", "R_oneline"]


def _report(node_id: str, status: str = "in_review", minimized: bool = False) -> dict:
    marker = f"{TERMINAL_MARKER}gen-{node_id} status={status} -->"
    return {
        "id": node_id,
        "isMinimized": minimized,
        "body": f"What was done.\n\n{marker}\n\n{BOT_MARKER}",
    }


def test_the_newest_worker_report_stays_open_beside_later_notices():
    """#2689: on agentdeck#2687 Releasing, Merged and Done hid the report saying what was done."""
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}, "r1": {}, "r2": {}}})),
            (
                ("graphql",),
                _thread(
                    _report("W_old"),
                    _bot("N_a"),
                    _report("W_new"),
                    _bot("N_b"),
                    _bot("N_c"),
                ),
            ),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Done."))

    assert sorted(_minimized_ids(api)) == ["N_a", "N_b", "W_old"]


def test_a_resumed_marker_is_a_notice_not_a_report():
    """A relayed answer or a reopen note says the work restarted, not what was done."""
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (("graphql",), _thread(_report("W"), _report("R", status="resumed"), _bot("N"))),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Note."))

    assert _minimized_ids(api) == ["R"]


def test_only_the_newest_of_several_worker_reports_stays_open():
    """Older reports still fold, so a thread never stacks old review packets again (#2116)."""
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}, "r1": {}}})),
            (
                ("graphql",),
                _thread(
                    _report("W_one", status="blocked"),
                    _report("W_two", status="needs_more_info"),
                    _report("W_three", minimized=True),
                    {"id": "W_bare", "isMinimized": False,
                     "body": f"{TERMINAL_MARKER}gen-bare -->\n\n{BOT_MARKER}"},
                    _report("W_new"),
                ),
            ),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    # A marker with no status still reads as a report ("acknowledged"), so it folds as one.
    assert sorted(_minimized_ids(api)) == ["W_bare", "W_one", "W_two"]


def test_a_quoted_terminal_marker_without_the_bot_marker_is_never_touched():
    """Only the poller's own comments are reports; a person quoting one stays as written."""
    quote = {
        "id": "H_quote",
        "isMinimized": False,
        "body": f"look at this: {TERMINAL_MARKER}gen status=in_review -->",
    }
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {}})),
            (("graphql",), _thread(quote, _report("W"), {"id": "H_empty", "body": None})),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    assert _minimized_ids(api) == []


def test_a_persons_claude_instruction_is_never_taken_for_a_review():
    """Only the findings heading or the reviewer's marker makes a comment a review."""
    nodes = [
        {"id": "H_one", "isMinimized": False, "body": "/claude please redo the guard"},
        {"id": "H_two", "isMinimized": False, "body": "/claude\nReview the findings for PR #9"},
        {"id": "H_three", "isMinimized": False, "body": "Review findings for PR #9 at abc"},
        {"id": "H_four", "isMinimized": False, "body": "/claude\nreview findings for PR #9"},
        {"id": "H_five", "isMinimized": False, "body": ""},
        {"id": "H_six", "isMinimized": False, "body": None},
        _review("R_only"),
        {"id": "B_quote", "isMinimized": False,
         "body": "/claude\nReview findings for PR #9\n\n" + BOT_MARKER},
    ]
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (("graphql",), _thread(*nodes, _bot("C_new"))),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    # The quoting comment is the poller's own and older than its newest, so it goes as one;
    # the lone review is the newest review and stays.
    assert _minimized_ids(api) == ["B_quote"]


def test_an_already_hidden_comment_is_not_hidden_again():
    """Every later comment re-runs this pass; it must not re-pay for work already done."""
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (
                ("graphql",),
                _thread(
                    _bot("C_done", minimized=True),
                    _bot("C_open"),
                    _bot("C_new"),
                ),
            ),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    assert _minimized_ids(api) == ["C_open"]


def test_a_comment_that_did_not_land_retires_nothing():
    """Nothing was superseded, because nothing replaced it."""
    api = FakeGh(default=(1, "", "no such issue"))

    assert adapter(api=api).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, "Report.")
    ) is False
    assert not api.asked("minimizeComment")


def test_a_failed_retirement_never_unlands_the_comment():
    """The comment is the durable fact; tidying up after it is not allowed to deny it."""
    api = FakeGh(
        answers=[
            (("minimizeComment",), (1, "", "Resource not accessible")),
            (("graphql",), _thread(_bot("C_old"), _bot("C_new"))),
        ]
    )

    assert adapter(api=api).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, "Report.")
    ) is True


def test_an_unreadable_thread_hides_nothing():
    """An answer this pass cannot parse is unknown, never an empty thread to act on."""
    for answer in ((0, "not json", ""), ok({"data": {"repository": None}}), (1, "", "boom")):
        api = FakeGh(answers=[(("graphql",), answer)])

        assert adapter(api=api).apply(
            Effect(EffectKind.COMMENT, "agentdeck", 5, "Report.")
        ) is True
        assert not api.asked("minimizeComment")


def test_a_comment_id_that_is_not_a_node_id_is_left_alone():
    """Ids are pasted into a mutation document, so a surprising one is dropped, not sent.

    GitHub has never returned anything but an opaque node id here. The guard is for the day
    something else arrives, because the cost of being wrong is a document this engine wrote.
    """
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (
                ("graphql",),
                _thread(
                    _bot('C_bad",classifier:OUTDATED}){__typename}}#'),
                    _bot("C_fine"),
                    _bot("C_new"),
                ),
            ),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    assert _minimized_ids(api) == ["C_fine"]


def test_one_pass_hides_at_most_its_own_limit():
    """The cap bounds the document, not the work: the next comment hides the remainder."""
    crowd = [_bot(f"C_{index}") for index in range(github_module.RETIRE_COMMENT_LIMIT + 5)]
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {}})),
            (("graphql",), _thread(*crowd, _bot("C_new"))),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    assert len(_minimized_ids(api)) == github_module.RETIRE_COMMENT_LIMIT


def test_retirement_reports_only_the_comments_github_confirmed():
    """A mutation can apply some aliases and fail others; the count follows the answer."""
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {"minimizedComment": {}}, "r1": None}})),
            (("graphql",), _thread(_bot("C_one"), _bot("C_two"), _bot("C_new"))),
        ]
    )

    assert adapter(api=api).retire_superseded_comments("agentdeck", 5) == 1


def test_a_spent_graphql_budget_retires_nothing():
    """Tidying is the first thing to give way when the shared reserve is low."""
    api = FakeGh(
        answers=[
            (
                ("rate_limit",),
                ok({"resources": {"graphql": {"remaining": 1}, "core": {"remaining": 5000}}}),
            ),
        ]
    )

    assert adapter(api=api).retire_superseded_comments("agentdeck", 5) == 0
    assert not api.asked("minimizeComment")


def test_both_retirement_documents_are_well_formed_graphql():
    """Both of these shipped broken once and no fake caught it (#2116).

    A `FakeGh` answers whatever it is handed, so an unbalanced brace or a field that does not
    exist on the type passes every behavioural test above and fails only against GitHub. Two
    things are checked here because both were really wrong: the read query was one closing
    brace short, and the mutation asked for `rateLimit`, which exists on GitHub's Query type
    and not on Mutation — so the whole document was a schema error that hid nothing.
    """
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (("graphql",), _thread(_bot("C_old"), _bot("C_new"))),
        ]
    )

    adapter(api=api).retire_superseded_comments("agentdeck", 5)

    documents = [
        part[len("query="):]
        for call in api.calls
        for part in call
        if part.startswith("query=")
    ]
    assert len(documents) == 2
    for document in documents:
        assert document.count("{") == document.count("}")
    read, mutation = documents
    assert read.startswith("query(")
    assert mutation.startswith("mutation{")
    assert "rateLimit" in read
    assert "rateLimit" not in mutation


def test_a_cards_first_poller_comment_hides_nothing():
    """The boundary the pass runs at every single time a card opens: one comment, no history."""
    api = FakeGh(answers=[(("graphql",), _thread(_bot("C_only")))])

    assert adapter(api=api).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, "Plan.")
    ) is True
    assert not api.asked("minimizeComment")


def test_a_human_replying_last_does_not_cost_the_poller_its_visible_comment():
    """The newest *poller* comment is what stays open, not the newest comment on the thread.

    A person answering straight after a report is the common case, and hiding the report they
    are answering would be the exact unreadability this was meant to fix.
    """
    human = {"id": "C_reply", "isMinimized": False, "body": "/claude thanks, carry on"}
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (("graphql",), _thread(_bot("C_old"), _bot("C_report"), human)),
        ]
    )

    adapter(api=api).retire_superseded_comments("agentdeck", 5)

    assert _minimized_ids(api) == ["C_old"]


def test_a_retirement_charges_its_own_mutation_against_the_cached_reserve():
    """Two passes near the floor: the second must refuse, not spend through the reserve.

    A mutation cannot report `rateLimit` — the field is on GitHub's Query type only — so the
    cached remaining count refreshes on reads and never on hides. Left unaccounted, a pass that
    starts one point above the reserve leaves the cache claiming a point it already spent, and
    the next pass reads on the strength of it and crosses the floor.
    """
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (
                ("rate_limit",),
                ok({"resources": {"graphql": {"remaining": 102}, "core": {"remaining": 5000}}}),
            ),
            (("graphql",), _thread(_bot("C_old"), _bot("C_new"), remaining=101)),
        ]
    )
    subject = adapter(api=api, graphql_reserve=100)

    assert subject.retire_superseded_comments("agentdeck", 5) == 1
    assert subject._graphql_remaining == 100

    assert subject.retire_superseded_comments("agentdeck", 5) == 0
    assert _minimized_ids(api) == ["C_old"]


def _removals(*nodes) -> dict:
    return {"data": {"repository": {"issue": {"timelineItems": {"nodes": list(nodes)}}}}}


def _unlabeled(at, label, login):
    return {"__typename": "UnlabeledEvent", "createdAt": at, "label": {"name": label},
            "actor": {"login": login} if login else None}


def _comment_node(database_id):
    return {"__typename": "IssueComment", "databaseId": database_id}


def test_ownership_removal_is_the_newest_removal_of_any_accepted_ownership_label():
    """Other labels come and go in the same timeline; only the ownership ones count (#2417)."""
    api = FakeGh([(("UNLABELED_EVENT",), ok(_removals(
        _unlabeled("2026-08-09T08:00:00Z", "Agent", "early"),
        _unlabeled("2026-08-09T09:00:00Z", "claude", "eero"),
        _unlabeled("2026-08-09T10:00:00Z", "bug", "someone"),
    )))])
    subject = adapter(api=api, ownership_label="agent", ownership_labels=("agent", "claude"))

    assert subject.ownership_removal("agentdeck", 7) == LabelRemoval(
        "2026-08-09T09:00:00Z", "eero"
    )
    assert api.asked("graphql", "number=7", "repo=agentdeck", "ISSUE_COMMENT")


def test_ownership_removal_places_the_comments_around_it_in_timeline_order():
    """Timeline order, not stamps: these share a second and still come out ordered."""
    api = FakeGh([(("UNLABELED_EVENT",), ok(_removals(
        _comment_node(11),
        _unlabeled("2026-08-09T09:00:00Z", "claude", "eero"),
        _comment_node(12),
        _unlabeled("2026-08-09T09:00:00Z", "claude", "eero"),
        _comment_node(13),
        _unlabeled("2026-08-09T09:00:00Z", "bug", "eero"),
        _comment_node(14),
    )))])
    removal = adapter(api=api, ownership_label="claude").ownership_removal("agentdeck", 7)

    assert removal.comments_before == frozenset({"11", "12"})
    assert removal.comments_after == frozenset({"13", "14"})


def test_ownership_removal_with_none_on_record_is_an_empty_answer_not_an_unreadable_one():
    api = FakeGh([(("UNLABELED_EVENT",), ok(_removals(
        _unlabeled("2026-08-09T10:00:00Z", "bug", ""), _comment_node(5),
    )))])

    assert adapter(api=api, ownership_label="claude").ownership_removal(
        "agentdeck", 7
    ) == LabelRemoval()


def test_an_unreadable_timeline_is_no_answer_about_the_removal():
    api = FakeGh([(("UNLABELED_EVENT",), ok({"errors": [{"message": "nope"}]}))])

    assert adapter(api=api, ownership_label="claude").ownership_removal("agentdeck", 7) is None


def test_a_closed_card_snapshot_carries_when_it_was_closed():
    """A person's `/claude` reopens only an issue closed before it was written (#2417)."""
    cards = card_batch({"state": "CLOSED", "closedAt": "2026-08-09T09:00:00Z", "labels": [],
                        "updatedAt": None})
    api = FakeGh(default=ok(cards))

    snapshot = adapter(api=api).snapshot("agentdeck", 640)

    assert snapshot.open is False
    assert snapshot.closed_at == "2026-08-09T09:00:00Z"
    assert api.asked("graphql", "closedAt")


def test_the_poller_never_reads_through_the_worker_gh_shim(tmp_path, monkeypatch):
    """Worker-control runs in a worker's shell, shim first on PATH (#2698)."""
    shim = tmp_path / "gh-shim"
    monkeypatch.setenv("AGENTDECK_GH_SHIM_DIR", str(shim))
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}/usr/bin{os.pathsep}{shim}/")

    path = Gh(token="one-token")._environment()["PATH"]

    assert path.split(os.pathsep) == ["/usr/bin"]


@pytest.mark.parametrize(
    "variables",
    [{}, {"XDG_DATA_HOME": "/data"}, {"AGENTDECK_GH_SHIM_DIR": "~/shim"}],
)
def test_the_engine_finds_the_shim_where_the_shim_installs_itself(monkeypatch, variables):
    from agentdeck.gh_shim.install import shim_dir
    from agentdeck.poller_engine.github import _gh_shim_dir

    for name in ("AGENTDECK_GH_SHIM_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    assert _gh_shim_dir() == Path(shim_dir())


def test_only_githubs_own_merge_commit_names_the_head_it_updated():
    """#2793: the parent is read off a two-parent commit GitHub itself committed and signed."""

    def parent(answer):
        gh = FakeGh([(("api", "commits/sha2"), answer)])
        found = adapter(api=gh).sync_merge_parent("agentdeck#7", "sha2")
        assert gh.asked("api", "repos/eerovil/agentdeck/commits/sha2")
        return found

    def commit(parents, committer="web-flow", verified=True):
        body = {"parents": parents, "committer": committer, "verified": verified}
        return (0, json.dumps(body), "")

    assert parent(commit(["AAA", "base"])) == "aaa"
    assert parent(commit(["aaa"])) == ""                       # a plain push
    assert parent(commit(["aaa", "base"], committer="someone")) == ""  # a local merge
    assert parent(commit(["aaa", "base"], verified=False)) == ""
    assert parent((1, "", "boom")) is None


def test_issue_comments_pages_the_whole_history_and_fails_as_unknown():
    """#2845: a worker's queued `reopen` must find its report however many comments followed,
    so it pages every comment rather than the snapshot's newest 100."""
    comments = [{"body": f"c{n}", "created_at": "2026-07-27T10:00:00Z"} for n in range(150)]
    # Review finding: `gh` may print one array per page, back to back, not one merged array.
    for output in (
        json.dumps(comments[:100]) + json.dumps([*comments[100:], "junk"]),
        json.dumps(comments[:100]) + "\n" + json.dumps(comments[100:]) + "\n",
        json.dumps(comments),
    ):
        api = FakeGh([(("issues/5/comments",), (0, output, ""))])
        assert adapter(api=api).issue_comments("agentdeck", 5) == comments
        assert api.asked("--paginate", "repos/eerovil/agentdeck/issues/5/comments?per_page=100")
    for code, output in (
        (1, ""), (0, ""), (0, json.dumps(comments) + "[{"), (0, '{"message": "Not Found"}')
    ):
        broken = FakeGh(default=(code, output, ""))
        assert adapter(api=broken).issue_comments("agentdeck", 5) is None, output[:20]


def test_the_prompt_check_and_the_ending_read_share_one_outcome_read(monkeypatch):
    """#2887 review: one ``/executions/outcome`` request per card per wake, not one per question,
    so an unhealthy Runtime costs a card one timeout, not two."""
    waiting = _outcome("gen-1", "waiting_human", "pending_interaction")
    runtime = FakeRuntime(monkeypatch, {(_WORK_A1, "gen-1"): waiting})
    port = adapter(runtime_socket="/run/rt.sock")

    assert port.waiting_on_person("a", 1, "gen-1") is True
    assert port.turn_ended("a", 1, "gen-1") is None
    assert len(runtime.gets) == 1

    # An unreadable answer is remembered too: it is not asked again on the same wake.
    del runtime.outcomes[(_WORK_A1, "gen-1")]
    other = adapter(runtime_socket="/run/rt.sock")
    other.waiting_on_person("a", 1, "gen-1")
    other.turn_ended("a", 1, "gen-1")
    assert len(runtime.gets) == 2


def test_a_stop_of_an_abandoned_run_reads_its_outcome_afresh(monkeypatch):
    abandoned = _outcome("gen-1", "abandoned", "no_activity")
    stopped = _outcome("gen-1", "stopped", "abandoned", stop_requested_at=1.0)
    runtime = FakeRuntime(
        monkeypatch,
        {(_WORK_A1, "gen-1"): abandoned},
        stop={"result": "stopped", "execution": stopped},
    )
    port = adapter(runtime_socket="/run/rt.sock")

    assert port.waiting_on_person("a", 1, "gen-1") is False
    port.turn_ended("a", 1, "gen-1")

    assert len(runtime.stops) == 1
    assert ("a", 1, "gen-1") not in port._execution_reads
