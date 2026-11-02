"""An open issue no agent works on is a Work in Needs you, with Start agent (#3558).

One without its repository's ownership label, opened in the last 14 days, read from the
backlog's cached issue lists. Start agent adds the label, so the Poller or the deck starts on it
as on any labelled issue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_work_actions import _Gh
from test_work_ui import WORK, _add, _app

from agentdeck import gh_cli
from agentdeck.issue_context import IssueBacklog, IssueContext, RepositoryIssues, RepositoryPulls
from agentdeck.poller_ops import BoardScope, BoardView
from agentdeck.work_actions import pausable
from agentdeck.work_attention import START, UNSTARTED
from agentdeck.work_overview import other_deck_works, unstarted_issues, work_href

NOW = datetime.now(UTC)


def _issue(number, *, labels=(), age=timedelta(days=1), state="open", assignees=()):
    return IssueContext(
        "eerovil/agentdeck", number, f"Issue {number}",
        f"https://github.com/eerovil/agentdeck/issues/{number}", state, labels=tuple(labels),
        assignees=tuple(assignees),
        created_at=(NOW - age).isoformat() if age is not None else None,
    )


def _backlog(*issues, age_s=60.0, labels=("agent",), pulls=()):
    scope = BoardScope("agentdeck", org="eerovil", ownership_labels=labels,
                       issue_repositories=("eerovil/agentdeck",))
    entry = RepositoryIssues("eerovil/agentdeck", issues, present=True, age_s=age_s)
    return IssueBacklog(
        repositories=(entry,), board=BoardView(scopes=(scope,)),
        pulls=(RepositoryPulls("eerovil/agentdeck", tuple(pulls)),),
    )


def _pull(number, branch):
    return {"repository": "eerovil/agentdeck", "number": number, "title": f"PR {number}",
            "url": f"https://github.com/eerovil/agentdeck/pull/{number}", "status": "open",
            "draft": True, "head_branch": branch, "base_branch": "master"}


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
    # #3731: an issue another deck works is that deck's to start: one assigned to its person,
    # and an unassigned one where that deck takes those.
    shared = _backlog(_issue(1, assignees=["Coworker"]), _issue(2), _issue(3, assignees=["me"]))
    takes = lambda repository, assignee: assignee in {"", "me"}  # noqa: E731
    assert sorted(ref.number for ref in unstarted_issues(shared, now=NOW, mine=takes)) == [2, 3]
    leaves = lambda repository, assignee: assignee == "me"  # noqa: E731
    assert sorted(ref.number for ref in unstarted_issues(shared, now=NOW, mine=leaves)) == [3]


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
    # A quick tidy-up, listed below Working now (#3818).
    assert [group.label for group in front.groups_in("cleanup")] == ["Not started"]
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
    # Only an issue assigned to this deck's person is listed there, so it is one.
    backlog = _backlog(_issue(WORK.number, assignees=["me"]))
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

    # #3834: the coworker's deck's Work is listed folded, read only, until taken over here.
    gh.calls.clear()
    theirs = _backlog(
        _issue(WORK.number, labels=["agent"], assignees=["Coworker"]),
        pulls=[_pull(90, f"agent/issue-{WORK.number}")],
    )
    app.state.issues.cached = lambda: theirs
    client = TestClient(app)
    await app.state.work_overview.refresh()
    overview = await app.state.work_overview.current()
    assert [item.work_id for item in overview.other_decks] == [WORK.work_id]
    assert overview.front.needs_you == ()
    front = client.get("/", params={"chats": "0"}).text
    assert 'data-front-collapse="other-decks" data-front-shut' in front
    # #3855: drawn like your own row, its status from GitHub alone; no row read yet, so the
    # open list's draft says Building, and the backfill is asked to read it.
    assert 'data-other-deck-status="building"' in front
    assert "Building</span> on @coworker&#39;s deck" in front
    assert ("eerovil/agentdeck", 90, "") in overview.unsized and overview.merge_watch == ()
    page = client.get(work_href(WORK.work_id)).text
    assert "data-other-deck" in page and "Take over" in page and 'id="work-result"' in page
    assert 'id="work-message"' not in page and 'value="start"' not in page

    # Its row read and the reviewer's pass at its head seen in the webhook log: Reviewed,
    # sized like your own, and still never merge-watched.
    from agentdeck.github_cache import GitHubMetadata, GitHubMetadataCache
    from agentdeck.issue_marks import cache_key

    cache_path = tmp_path / "marks.sqlite3"
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(cache_path))
    GitHubMetadataCache(cache_path).offer(
        cache_key(f"eerovil/agentdeck#{WORK.number}"),
        GitHubMetadata({"heads": {"abc1234": {"id": 7, "url": "https://x/c7", "kind": "passed"}}},
                       terminal=True),
    )
    head = "abc1234" + "0" * 33
    row = {"state": "OPEN", "headRefOid": head, "isDraft": False, "baseRefName": "master",
           "changedFiles": 1, "files": [{"path": "src/a.py", "additions": 5, "deletions": 1,
                                         "changeType": "MODIFIED"}]}
    app.state.issues.cached_pulls = lambda wanted: {ref: row for ref in wanted}
    await app.state.work_overview.refresh()
    overview = await app.state.work_overview.current()
    assert overview.other_decks[0].status.key == "reviewed" and overview.merge_watch == ()
    assert not [ref for ref in overview.unsized if ref[1] == 90]
    front = client.get("/", params={"chats": "0"}).text
    assert 'data-other-deck-status="reviewed"' in front and "+5/&minus;1 · 1 file" in front
    page = client.get(work_href(WORK.work_id)).text
    assert "Reviewed on @coworker" in page and 'href="https://x/c7"' in page
    assert "data-wd-output-size" in page

    # Two pull requests: each lists its own verdict, and findings on one are not hidden by a
    # pass on the other (#3855 review).
    GitHubMetadataCache(cache_path).offer(
        cache_key(f"eerovil/agentdeck#{WORK.number}"),
        GitHubMetadata({"heads": {
            "abc1234": {"id": 9, "url": "https://x/c9", "kind": "passed"},
            "def5678": {"id": 8, "url": "https://x/c8", "kind": "findings"},
        }}, terminal=True),
    )
    two = _backlog(
        _issue(WORK.number, labels=["agent"], assignees=["Coworker"]),
        pulls=[_pull(90, f"agent/issue-{WORK.number}"), _pull(91, f"agent/issue-{WORK.number}")],
    )
    app.state.issues.cached = lambda: two
    other = dict(row, headRefOid="def5678" + "0" * 33)
    app.state.issues.cached_pulls = lambda wanted: {
        ref: (row if ref[1] == 90 else other) for ref in wanted
    }
    page = client.get(work_href(WORK.work_id)).text
    assert 'data-other-deck-status="findings"' in page
    lines = {
        number: page.split(f'data-wd-output="https://github.com/eerovil/agentdeck/pull/{number}"',
                           1)[1].split("</li>", 1)[0]
        for number in (90, 91)
    }
    assert 'href="https://x/c9"' in lines[90] and 'data-wd-output-verdict="passed"' in lines[90]
    assert 'href="https://x/c8"' in lines[91] and 'data-wd-output-verdict="findings"' in lines[91]
    app.state.issues.cached = lambda: theirs

    taken = client.post(work_href(WORK.work_id) + "/take-over",
                        data={"client_action_id": "press-0002"})

    assert taken.status_code == 200 and "Took over" in taken.text
    assert [argv for argv, _stdin, _repo in gh.calls] == [
        ["/usr/bin/gh", "issue", "edit", "2147", "--repo", "eerovil/agentdeck",
         "--remove-assignee", "Coworker", "--add-assignee", "me"],
        ["/usr/bin/gh", "issue", "comment", "2147", "--repo", "eerovil/agentdeck",
         "--body", "@me took this over from @Coworker's deck."],
    ]
    # A replay answers with what the press did; once the issue is this deck's, a new press
    # is refused. Neither writes.
    replay = client.post(work_href(WORK.work_id) + "/take-over",
                         data={"client_action_id": "press-0002"})
    # A second tap with a new id while the cached lists still say the coworker's: refused.
    again = client.post(work_href(WORK.work_id) + "/take-over",
                        data={"client_action_id": "press-0004"})
    assert again.status_code == 409 and "not sent again" in again.text
    app.state.issues.cached = lambda: backlog
    late = client.post(work_href(WORK.work_id) + "/take-over",
                       data={"client_action_id": "press-0003"})
    assert replay.status_code == 200 and late.status_code == 409 and len(gh.calls) == 2
    # One this deck ran before the issue went to the coworker is shown read only too.
    _add(app, "claude_code:test:earlier", WORK)
    app.state.issues.cached = lambda: theirs
    assert "data-other-deck" in client.get(work_href(WORK.work_id)).text


async def test_start_here_takes_an_unstarted_issue_and_says_why_a_comment_failed(monkeypatch):
    """#3834: the one edit for each kind, and a failed comment is said, not undone."""
    from agentdeck.work_claims import take_over

    gh = _Gh((0, b"", b""))
    monkeypatch.setattr(gh_cli, "run_once", gh)
    outcome, text = await take_over("gh", "o/r", 5, assignees=(), person="me",
                                    start_label="agent", started=False)
    assert outcome == "done" and "Started o/r#5 here" in text
    assert gh.calls[0][0] == ["gh", "issue", "edit", "5", "--repo", "o/r",
                              "--add-assignee", "me", "--add-label", "agent"]
    gh = _Gh((0, b"", b""), (1, b"", b"boom"))
    monkeypatch.setattr(gh_cli, "run_once", gh)
    outcome, text = await take_over("gh", "o/r", 5, assignees=("Them", "ME", "other"),
                                    person="me", start_label="agent", started=True)
    assert outcome == "done" and "@Them's deck stops its agent" in text and "boom" in text
    # Every other assignee goes, or one left first would keep it on another deck.
    assert gh.calls[0][0][-4:] == ["--remove-assignee", "Them,other", "--add-assignee", "me"]
    gh = _Gh((1, b"", b"denied"))
    monkeypatch.setattr(gh_cli, "run_once", gh)
    outcome, _ = await take_over("gh", "o/r", 5, assignees=("them",), person="me",
                                 start_label="agent", started=True)
    assert outcome == "uncertain" and len(gh.calls) == 1


def test_other_decks_works_are_the_issues_another_deck_has():
    """#3834: what unstarted_issues leaves to another deck, listed read only."""
    leaves = lambda repository, assignee: assignee == "me"  # noqa: E731
    backlog = _backlog(
        _issue(1, labels=["agent"], assignees=["Coworker"], age=timedelta(days=40)),
        _issue(2, assignees=["coworker"]),  # not started, within the window
        _issue(3, assignees=["coworker"], age=timedelta(days=15)),  # not started, too old
        _issue(4, labels=["agent"], assignees=["me"]),  # this deck's
        _issue(5, labels=["agent"]),  # unassigned, left to the other deck
        _issue(6, labels=["agent"], assignees=["coworker"], state="closed"),
        _issue(7, labels=["scheduled"], assignees=["coworker"]),
        pulls=[_pull(90, "agent/issue-1"), _pull(91, "agent/issue-4")],
    )
    found = {item.ref.number: item for item in other_deck_works(backlog, now=NOW, mine=leaves)}
    assert sorted(found) == [1, 2, 5]
    assert found[1].started and found[1].assignee == "coworker" and found[1].pull["number"] == 90
    # Take over sends every assignee as GitHub spells it: gh removes only an exact match (#3852).
    assert found[1].assignees == ("Coworker",)
    assert not found[2].started and found[2].pull is None and found[2].start_label == "agent"
    assert found[5].assignee == ""
    # Where this deck takes unassigned issues, the unassigned one is its own.
    takes = lambda repository, assignee: assignee in {"", "me"}  # noqa: E731
    assert sorted(i.ref.number for i in other_deck_works(backlog, now=NOW, mine=takes)) == [1, 2]
    # No deck person: every issue is this deck's. A stale list names none.
    assert other_deck_works(backlog, now=NOW, mine=lambda r, a: True) == ()
    assert other_deck_works(backlog, now=NOW, mine=None) == ()
    assert other_deck_works(_backlog(_issue(1, labels=["agent"], assignees=["x"]), age_s=7200.0),
                            now=NOW, mine=leaves) == ()


def test_another_decks_status_comes_from_github_alone():
    """#3855: the first rule that holds, and a verdict counts only at the current head."""
    from dataclasses import replace

    from agentdeck.issue_marks import IssueMarks, Mark
    from agentdeck.work_facts import OutputFact, WorkFacts
    from agentdeck.work_overview import other_deck_status

    (item,) = other_deck_works(
        _backlog(_issue(1, labels=["agent", "Spec-Only"], assignees=["them"])),
        now=NOW, mine=lambda r, a: a == "me",
    )
    head = "abc1234" + "0" * 33

    def pr(*, draft=False, at=head):
        output = OutputFact("https://github.com/o/r/pull/9", "o/r", 9, "branch", "open", at,
                            draft=draft)
        return replace(item, facts=WorkFacts(outputs=(output,)))

    def key(work, **kwargs):
        return other_deck_status(work, spec_label="spec-only", **kwargs).key

    passed = Mark(5, head="abc1234", kind="passed")
    found = Mark(6, head="abc1234", url="https://x/6", kind="findings")
    assert key(replace(item, started=False)) == "unstarted"
    assert key(pr(), marks=IssueMarks(verdicts=(passed,))) == "reviewed"
    assert key(pr(), marks=IssueMarks(verdicts=(found, passed))) == "findings"
    assert key(pr(), marks=IssueMarks(verdicts=(passed, replace(found, id=4)))) == "reviewed"
    # A verdict for an older head is no verdict; neither is no mark at all.
    assert key(pr(at="f" * 40), marks=IssueMarks(verdicts=(passed,))) == "in_review"
    assert key(pr()) == "in_review"
    assert key(pr(draft=True), marks=IssueMarks(verdicts=(passed,))) == "building"
    # Two ready pull requests are judged each on its own head: a newer pass on one never
    # covers findings on the other, and Reviewed needs a pass on both.
    other = OutputFact("https://github.com/o/r/pull/8", "o/r", 8, "branch", "open", "d" * 40)
    both = replace(pr(), facts=WorkFacts(outputs=(*pr().outputs, other)))
    on_other = Mark(9, head="ddddddd", kind="findings")
    assert key(both, marks=IssueMarks(verdicts=(on_other, replace(passed, id=10)))) == "findings"
    assert key(both, marks=IssueMarks(verdicts=(passed,))) == "in_review"
    assert key(both, marks=IssueMarks(verdicts=(passed, replace(on_other, kind="passed")))) == (
        "reviewed")
    # The spec waits only while it is the issue's newest comment and the label is on.
    spec = IssueMarks(spec=Mark(8, url="https://x/8"))
    status = other_deck_status(item, marks=spec, newest_comment=8, spec_label="spec-only")
    assert status.key == "spec" and status.link == "https://x/8"
    assert key(item, marks=spec, newest_comment=9) == "working"
    assert other_deck_status(item, marks=spec, newest_comment=8, spec_label="").key == "working"
    # The open list's pull request, its row not read yet.
    listed = replace(item, pull={"url": "u", "draft": False})
    assert key(listed) == "in_review"
