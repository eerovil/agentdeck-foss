"""A dropped ``issues`` ``closed`` delivery must not hide a Work's close (#2947).

The deck learns a close only from that delivery, and no Poller re-reads a Work the deck runs
itself. The router re-reads the issue when a registered pull request merges
(``test_work_router``); here, the Work page re-reads a stale open issue once every pull
request it produced has settled, through the cache's own TTL.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentdeck import wayfinder_map
from agentdeck.issue_context import IssueResolver
from agentdeck.kanban import KanbanTitleCache
from agentdeck.web import work_view
from agentdeck.web.work_view import WORK_INPUTS_TOPIC, _reread_issue_behind, _reread_settled_issue
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read", "moved"), [(True, True), (True, False), (False, False), ("boom", False)]
)
async def test_a_page_rereads_behind_itself_once_and_redraws_only_on_a_change(read, moved):
    """#4639: an open page never waits for GitHub, and an unchanged or failed read builds
    nothing again (which would read again, without end)."""
    app, asked, published = _app(read=read)
    records = [OPEN, {**OPEN, "state": "closed"} if moved else dict(OPEN)]
    app.state.issues.cached_issues = lambda refs: {refs[0]: records[min(len(asked), 1)]}
    generation = wayfinder_map.generation()
    _reread_issue_behind(app, REF, _facts())
    _reread_issue_behind(app, REF, _facts())
    assert asked == []  # nothing awaited by the page
    await app.state.work_issue_rereads[REF.work_id]
    await asyncio.sleep(0)
    assert len(asked) == 1 and app.state.work_issue_rereads == {}
    assert published == ([WORK_INPUTS_TOPIC] if moved else [])
    assert (wayfinder_map.generation() != generation) is moved


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record", "wait", "behind"), [(OPEN, False, True), (OPEN, True, False), (None, False, False)]
)
async def test_only_a_page_with_a_held_record_reads_behind_itself(
    tmp_path, monkeypatch, record, wait, behind
):
    """#4639: an action press, and a Work with no record yet, still wait for GitHub."""
    from test_work_resolution import _deck
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    calls: list[str] = []
    facts = work_view.gather_facts
    monkeypatch.setattr(
        work_view,
        "gather_facts",
        lambda *args: {k: replace(v, record=record) for k, v in facts(*args).items()},
    )
    monkeypatch.setattr(work_view, "_reread_issue_behind", lambda *_: calls.append("behind"))

    async def waited(*_args):
        calls.append("waited")
        return False

    monkeypatch.setattr(work_view, "_reread_issue", waited)
    await work_view.load_work_detail(app, WORK, workspace=False, wait_for_issue=wait)
    assert calls == ["behind" if behind else "waited"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("labels", "wait", "text"),
    [([], False, ""), ([], True, "Body"), (["wayfinder:map"], False, "Body")],
)
async def test_a_page_does_not_wait_for_a_text_never_read(
    tmp_path, monkeypatch, labels, wait, text
):
    """#4639: an action press and a map, whose diagram is drawn from the body, still wait."""
    from test_work_resolution import _deck
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    facts = work_view.gather_facts
    monkeypatch.setattr(
        work_view,
        "gather_facts",
        lambda *args: {
            k: replace(v, record={**OPEN, "labels": labels}) for k, v in facts(*args).items()
        },
    )

    async def body(repository, number):
        return "Body"

    monkeypatch.setattr(IssueResolver, "issue_body", lambda self, *args: body(*args))
    monkeypatch.setattr(IssueResolver, "held_issue_body", lambda self, *_: (None, False))
    monkeypatch.setattr(work_view, "_reread_issue_behind", lambda *_: None)
    detail = await work_view.load_work_detail(app, WORK, workspace=False, wait_for_issue=wait)
    assert detail is not None and detail.issue_text == text
    assert hasattr(app.state, "work_tree_refreshes") is (text == "")
    # The session shares one loop: a read left running would end inside a later test.
    await asyncio.gather(*getattr(app.state, "work_tree_refreshes", {}).values())
