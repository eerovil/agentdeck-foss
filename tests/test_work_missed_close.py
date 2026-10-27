"""A dropped ``issues`` ``closed`` delivery must not hide a Work's close (#2947).

The deck learns a close only from that delivery, and no Poller re-reads a Work the deck runs
itself. The router re-reads the issue when a registered pull request merges
(``test_work_router``); here, the Work page re-reads a stale open issue once every pull
request it produced has settled, through the cache's own TTL.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentdeck.issue_context import IssueResolver
from agentdeck.kanban import KanbanTitleCache
from agentdeck.web.work_view import WORK_INPUTS_TOPIC, _reread_settled_issue
from agentdeck.work import WorkRef
from agentdeck.work_facts import OutputFact, WorkFacts

REF = WorkRef("eerovil/agentdeck", 2943)
PULL = OutputFact(
    "https://github.com/eerovil/agentdeck/pull/2945", REF.repository, 2945, "registered", "merged"
)
OPEN = {"title": "t", "state": "open", "is_pr": False}


def _app(*, switched=True, read=True):
    asked: list = []
    published: list = []

    async def refresh(refs):
        asked.append(list(refs))
        if read == "boom":
            raise RuntimeError("gh is down")
        return read

    app = SimpleNamespace(
        state=SimpleNamespace(
            work_router=SimpleNamespace(switched=lambda repository: switched),
            issues=SimpleNamespace(refresh_stale_issue_records=refresh),
            app_state=SimpleNamespace(bus=SimpleNamespace(publish=published.append)),
        )
    )
    return app, asked, published


def _facts(**fields):
    return replace(WorkFacts(ref=REF, record=OPEN, outputs=(PULL,)), **fields)


@pytest.mark.asyncio
async def test_a_deck_run_works_open_issue_is_reread_once_its_pulls_settled():
    app, asked, published = _app()
    assert await _reread_settled_issue(app, REF, _facts())
    assert asked == [[(REF.repository, REF.number)]]
    assert published == [WORK_INPUTS_TOPIC]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("switched", "fields"),
    [
        (False, {}),  # its project's Poller still runs it
        (True, {"card": object()}),  # a Poller card holds it, and re-reads it itself
        (True, {"record": {**OPEN, "state": "closed"}}),  # the close is already known
        (True, {"record": None}),  # never resolved: the overview's backfill reads it
        (True, {"record": {**OPEN, "is_pr": True}}),
        (True, {"outputs": ()}),  # nothing produced yet
        (True, {"outputs": (PULL, replace(PULL, number=2946, state="open"))}),
    ],
)
async def test_anything_else_is_not_reread(switched, fields):
    app, asked, published = _app(switched=switched)
    assert not await _reread_settled_issue(app, REF, _facts(**fields))
    assert asked == [] and published == []


@pytest.mark.asyncio
async def test_a_fresh_record_or_a_failed_read_changes_nothing():
    app, asked, published = _app(read=False)
    assert not await _reread_settled_issue(app, REF, _facts())
    app, asked, published = _app(read="boom")
    assert not await _reread_settled_issue(app, REF, _facts())
    assert len(asked) == 1 and published == []


@pytest.mark.asyncio
async def test_the_resolver_reads_only_a_stale_record(tmp_path, monkeypatch):
    """At most one GitHub read per open TTL, however often the page is drawn."""
    fetched: list = []

    async def fetch(self, ref):
        fetched.append(ref.key)
        return ref, {**OPEN, "state": "closed", "closedAt": "2026-10-12T19:31:00Z"}

    monkeypatch.setattr(KanbanTitleCache, "_fetch", fetch)
    resolver = IssueResolver(poller_root=tmp_path / "poller", cache_path=tmp_path / "cache.db")
    monkeypatch.setattr("agentdeck.kanban.shutil.which", lambda name: "/usr/bin/gh")
    refs = [(REF.repository, REF.number)]

    assert await resolver.refresh_stale_issue_records(refs)
    assert await resolver.refresh_stale_issue_records(refs) is False  # fresh now
    assert fetched == ["eerovil/agentdeck#2943"]
    assert resolver.cached_issues(refs)[(REF.repository, REF.number)]["state"] == "closed"

    # A cache that cannot be read says so, rather than answering "no record": the overview
    # would read every closed or paused Work as open and stopped (#3553).
    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(resolver._metadata, "_connect", locked)
    with pytest.raises(sqlite3.OperationalError):
        resolver.cached_issues(refs)
    assert resolver._metadata.peek_values(["issue:v1:x#1"]) == {}  # other readers unchanged
