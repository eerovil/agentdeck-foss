"""The missed-start backstop (#2951): a labelled Work no delivery started is started once."""

from __future__ import annotations

import asyncio
import logging

from agentdeck.issue_context import IssueBacklog, IssueContext, RepositoryIssues
from agentdeck.poller_ops import work_hold_labels, work_unassigned_elsewhere
from agentdeck.work_backstop import MissedStartBackstop

REPO = "eerovil/agentdeck"


NOW = 1_800_000_000.0
FRESH = "2027-01-15T07:00:00Z"  # an hour before NOW


def _issue(
    number: int, *labels: str, state: str = "open", updated: str = FRESH, assignee: str = ""
) -> IssueContext:
    assignees = (assignee,) if assignee else ()
    return IssueContext(
        REPO, number, f"#{number}", "", state, labels, assignees, updated_at=updated
    )


class Harness:
    def __init__(self, issues, records=None):
        self.issues = issues
        self.records = records
        self.owned: set[int] = set()
        self.paused: set[str] = set()
        self.spent: set[str] = set()
        self.reread_calls: list[tuple[str, int]] = []
        self.starts: list[tuple[str, int]] = []
        self.labels: dict[int, list[str]] = {}
        self.backstop = MissedStartBackstop(
            start_labels=lambda: {"EeroVil/AgentDeck": ("Agent",)},
            hold_labels=lambda: {REPO: ("paused",)},
            backlog=lambda: IssueBacklog(
                repositories=(RepositoryIssues("EeroVil/AgentDeck", tuple(self.issues)),)
            ),
            reread=self.reread,
            record=self.record,
            owned=lambda repository, number: number in self.owned,
            paused=lambda work_id: work_id in self.paused,
            spent=lambda work_id: work_id in self.spent,
            start_work=self.start,
            clock=lambda: NOW,
        )

    async def reread(self, repository, number):
        self.reread_calls.append((repository, number))

    def record(self, repository, number):
        if self.records is not None:
            return self.records.get(number)
        issue = next(item for item in self.issues if item.number == number)
        return {"state": issue.state, "is_pr": False, "labels": list(issue.labels)}

    async def start(self, repository, number, labels=()):
        self.starts.append((repository, number))
        self.labels[number] = list(labels)
        return "started"

    def sweep(self):
        return asyncio.run(self.backstop.sweep())


def test_starts_a_labelled_issue_with_no_chat_and_logs_it_as_the_backstop(caplog):
    """#3134: a spec-only issue is started too, with its labels, so its agent writes the spec."""
    harness = Harness([_issue(7, "agent"), _issue(8, "bug"), _issue(9, "agent", "Spec-Only")])
    with caplog.at_level(logging.WARNING, logger="agentdeck.work_backstop"):
        started = harness.sweep()
    assert started == ["github_issue:eerovil/agentdeck#7", "github_issue:eerovil/agentdeck#9"]
    assert harness.starts == [(REPO, 7), (REPO, 9)]
    assert harness.labels[9] == ["agent", "Spec-Only"]
    assert "backstop" in caplog.text and "no webhook delivery" in caplog.text


def test_skips_what_a_person_held_back_or_another_path_owns():
    harness = Harness(
        [
            _issue(2, "agent", "paused"),
            _issue(3, "agent", state="closed"),
            _issue(4, "agent"),
            _issue(6, "agent"),
            _issue(9, "agent"),
        ]
    )
    harness.owned.add(4)
    harness.paused.add("github_issue:eerovil/agentdeck#6")
    harness.spent.add("github_issue:eerovil/agentdeck#9")
    assert harness.sweep() == []
    assert harness.starts == [] and harness.reread_calls == []


def test_a_stale_list_is_confirmed_against_the_fresh_record():
    harness = Harness(
        [_issue(1, "agent"), _issue(2, "agent"), _issue(3, "agent"), _issue(4, "agent")],
        records={
            1: {"state": "closed", "is_pr": False, "labels": ["agent"]},
            2: {"state": "open", "is_pr": False, "labels": ["agent", "paused"]},
            3: {"state": "open", "is_pr": True, "labels": ["agent"]},
            4: None,
        },
    )
    assert harness.sweep() == []
    assert len(harness.reread_calls) == 4
    assert harness.starts == []


def test_a_repository_that_is_not_switched_is_left_alone():
    harness = Harness([_issue(1, "agent")])
    harness.backstop._start_labels = lambda: {}
    assert harness.sweep() == []
    assert harness.starts == []


def test_hold_labels_come_from_the_manifest(tmp_path):
    instance = tmp_path / "instances" / "deck"
    instance.mkdir(parents=True)
    (instance / "poller.toml").write_text(
        'contract = 1\n[board]\norg = "EeroVil"\nproject_number = 1\n'
        'ownership_label = "agent"\nspec_only_label = "Spec-Only"\npaused_label = "paused"\n'
        '[[repos]]\nid = "AgentDeck"\nholds_issues = true\n'
    )
    assert work_hold_labels(tmp_path) == {REPO: frozenset({"paused"})}
    # #3731: an overlay leaving unassigned issues to another deck names the repository.
    assert work_unassigned_elsewhere(tmp_path) == frozenset()
    (instance / "overlay.toml").write_text("[claims]\nunassigned = false\n")
    assert work_unassigned_elsewhere(tmp_path) == {REPO}
    # #3755: a shared manifest naming another person's deck as the main one does the same.
    (instance / "overlay.toml").write_text("")
    manifest = (instance / "poller.toml").read_text()
    (instance / "poller.toml").write_text(manifest + '[decks]\nmain = "someone-else"\n')
    assert work_unassigned_elsewhere(tmp_path) == {REPO}


def test_an_issue_another_deck_works_is_not_started():
    """#3731: the cached assignee skips it with no re-read; a fresh record's decides."""
    harness = Harness(
        [_issue(1, "agent", assignee="coworker"), _issue(2, "agent"), _issue(3, "agent")],
        records={
            2: {"state": "open", "is_pr": False, "labels": ["agent"], "assignee": "coworker"},
            3: {"state": "open", "is_pr": False, "labels": ["agent"], "assignee": None},
        },
    )
    harness.backstop._mine = lambda repository, assignee: assignee != "coworker"
    assert harness.sweep() == ["github_issue:eerovil/agentdeck#3"]
    assert harness.reread_calls == [(REPO, 2), (REPO, 3)]


def test_only_an_issue_updated_in_the_last_day_is_started():
    harness = Harness(
        [
            _issue(1, "agent", updated="2027-01-14T08:30:00Z"),
            _issue(2, "agent", updated="2027-01-14T07:00:00+00:00"),
            _issue(3, "agent", updated=None),
            _issue(4, "agent", updated="not a time"),
            _issue(5, "agent", updated="2027-01-15T07:00:00"),
        ]
    )
    assert harness.sweep() == ["github_issue:eerovil/agentdeck#1"]


def test_the_first_pass_runs_soon_after_start_then_every_interval(monkeypatch):
    """#2971: web deploys minutes apart kept a 15-minute first sleep from ever ending."""
    from agentdeck.work_backstop import FIRST_PASS_S, INTERVAL_S

    backstop = Harness([_issue(7, "agent")]).backstop
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        sleeps.append(delay)
        await real_sleep(0)

    async def sweep():
        if len(sleeps) == 2:
            raise asyncio.CancelledError
        return []

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(backstop, "sweep", sweep)
    try:
        asyncio.run(backstop._loop())
    except asyncio.CancelledError:
        pass
    assert sleeps == [FIRST_PASS_S, INTERVAL_S]
