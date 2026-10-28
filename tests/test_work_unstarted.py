"""An open issue no agent works on is a Work in Needs you, with Start agent (#3558).

One without its repository's ownership label, opened in the last 14 days, read from the
backlog's cached issue lists. Start agent adds the label, so the Poller or the deck starts on it
as on any labelled issue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_work_actions import _Gh
from test_work_ui import WORK, _app

from agentdeck import gh_cli
from agentdeck.issue_context import IssueBacklog, IssueContext, RepositoryIssues
from agentdeck.poller_ops import BoardScope, BoardView
from agentdeck.work_actions import pausable
from agentdeck.work_attention import START, UNSTARTED
from agentdeck.work_overview import unstarted_issues, work_href

NOW = datetime.now(UTC)


def _issue(number, *, labels=(), age=timedelta(days=1), state="open", assignees=()):
    return IssueContext(
        "eerovil/agentdeck", number, f"Issue {number}",
        f"https://github.com/eerovil/agentdeck/issues/{number}", state, labels=tuple(labels),
        assignees=tuple(assignees),
        created_at=(NOW - age).isoformat() if age is not None else None,
    )


def _backlog(*issues, age_s=60.0, labels=("agent",)):
    scope = BoardScope("agentdeck", org="eerovil", ownership_labels=labels,
                       issue_repositories=("eerovil/agentdeck",))
    entry = RepositoryIssues("eerovil/agentdeck", issues, present=True, age_s=age_s)
    return IssueBacklog(repositories=(entry,), board=BoardView(scopes=(scope,)))


def test_only_recent_open_issues_without_the_label_are_unstarted():
    backlog = _backlog(
        _issue(1),
        _issue(2, labels=["Agent"]),  # the label, in any case
        _issue(3, labels=["bug"]),
        _issue(4, age=timedelta(days=15)),  # older than the window
        _issue(5, age=None),  # a list cached before it carried createdAt
        _issue(6, labels=["scheduled"]),  # a schedule definition, open on purpose
    )
    assert sorted(ref.number for ref in unstarted_issues(backlog, now=NOW)) == [1, 3]
    # A list read too long ago to say what is still open names none.
    assert unstarted_issues(_backlog(_issue(1), age_s=7200.0), now=NOW) == {}
    # A repository with no ownership label has nothing to start one with.
    assert unstarted_issues(_backlog(_issue(1), labels=()), now=NOW) == {}
    assert unstarted_issues(None, now=NOW) == {}
    # #3731: an issue assigned to another deck's person is that deck's to start.
    shared = _backlog(_issue(1, assignees=["Coworker"]), _issue(2), _issue(3, assignees=["me"]))
    mine = lambda repository, assignee: assignee in {"", "me"}  # noqa: E731
    assert sorted(ref.number for ref in unstarted_issues(shared, now=NOW, mine=mine)) == [2, 3]


@pytest.mark.asyncio
async def test_start_agent_adds_the_ownership_label(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from agentdeck.web import routes_works

    app = _app(tmp_path)
    backlog = _backlog(_issue(WORK.number))
    app.state.issues.read_board = lambda: backlog.board
    app.state.issues.cached = lambda: backlog
    gh = _Gh((0, b"", b""))
    monkeypatch.setattr(gh_cli, "run_once", gh)
    monkeypatch.setattr(routes_works.shutil, "which", lambda name: "/usr/bin/gh")
    client = TestClient(app)

    front = (await app.state.work_overview.current()).front
    (row,) = front.needs_you
    assert row.card.work_id == WORK.work_id and row.card.start_label == "agent"
    assert row.attention.category == UNSTARTED and START in row.attention.actions
    assert row.status.headline == "Not started" and not pausable(row.card)
    assert [group.label for group in front.groups] == ["Not started"] and front.stopped == ()
    page = client.get("/", params={"chats": "0"}).text
    assert 'value="start"' in page and "Not started" in page
    assert 'value="start"' in client.get(work_href(WORK.work_id)).text

    pressed = client.post(
        work_href(WORK.work_id) + "/action",
        data={"action": "start", "client_action_id": "press-0001"},
    )

    assert pressed.status_code == 200 and "Started" in pressed.text
    ((argv, _stdin, repo),) = gh.calls
    assert argv == ["/usr/bin/gh", "issue", "edit", "2147", "--repo", "eerovil/agentdeck",
                    "--add-label", "agent"]
    assert repo == "eerovil/agentdeck"

    # Once the label is on, the issue is no longer a Work of its own, and Start is refused.
    labelled = _backlog(_issue(WORK.number, labels=["agent"]))
    app.state.issues.cached = lambda: labelled
    again = client.post(
        work_href(WORK.work_id) + "/action",
        data={"action": "start", "client_action_id": "press-0002"},
    )
    assert again.status_code in {404, 409} and len(gh.calls) == 1

    # Close issue, for one a person handled themselves (#3562), closes it and finishes it.
    app.state.issues.cached = lambda: backlog
    assert 'value="close"' in client.get(work_href(WORK.work_id)).text
    closed = client.post(
        work_href(WORK.work_id) + "/action",
        data={"action": "close", "client_action_id": "press-0003"},
    )
    assert closed.status_code == 200 and "Closed" in closed.text
    assert gh.calls[-1][0] == ["/usr/bin/gh", "issue", "close", "2147", "--repo",
                               "eerovil/agentdeck", "--reason", "completed"]


async def test_start_agent_assigns_this_decks_person_where_another_deck_takes_unassigned(
    tmp_path, monkeypatch
):
    """#3731: assigned first, so the label's delivery already shows whose issue it is."""
    from fastapi.testclient import TestClient

    from agentdeck.web import routes_works

    app = _app(tmp_path)
    backlog = _backlog(_issue(WORK.number))
    app.state.issues.read_board = lambda: backlog.board
    app.state.issues.cached = lambda: backlog
    app.state.deck_claims = lambda repository, assignee: assignee == "me"
    gh = _Gh((0, b"", b""))
    monkeypatch.setattr(gh_cli, "run_once", gh)
    monkeypatch.setattr(routes_works.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr("agentdeck.poller_engine.github_authority.deck_person", lambda: "me")

    pressed = TestClient(app).post(
        work_href(WORK.work_id) + "/action",
        data={"action": "start", "client_action_id": "press-0001"},
    )

    assert pressed.status_code == 200 and "assigned to me" in pressed.text
    assert [argv[-2:] for argv, _stdin, _repo in gh.calls] == [
        ["--add-assignee", "me"], ["--add-label", "agent"]
    ]
