"""A Work's own title and resolution (#2328): stored with the Work, shown on its page."""

from __future__ import annotations

import dataclasses
from functools import partial

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck import gh_cli
from agentdeck.work_overview import work_href, work_title
from agentdeck.work_resolution import (
    MAX_PART,
    MAX_TITLE,
    ResolutionError,
    WorkResolution,
    evidence_added,
    normalize_title,
    with_evidence,
)

WORK_ID = "github_issue:eerovil/agentdeck#2147"


def _resolution(**fields) -> WorkResolution:
    parts = {
        "background": "The Work page shows what a Work is about and why it changed.",
        "problem": "The Work page could not say why a change was made.",
        "solution": "A Work-owned record the verify worker writes.",
        "rationale": "Rebuilding it from chats was rejected: chats age out.",
        "correctness": "Focused tests pass; the selected suite is running.",
    }
    parts.update(fields)
    return WorkResolution(WORK_ID, **parts)


# --- the store -----------------------------------------------------------------------------------


def test_a_custom_title_survives_a_reopen_and_clearing_it_restores_the_fallback(tmp_path):
    from agentdeck.db import Db

    db = Db(tmp_path / "control.sqlite3")
    assert db.work_titles() == {}
    assert db.set_work_title(WORK_ID, "My own name")
    assert Db(tmp_path / "control.sqlite3").work_titles() == {WORK_ID: "My own name"}
    assert db.set_work_title(WORK_ID, "")
    assert Db(tmp_path / "control.sqlite3").work_titles() == {}


def test_a_resolution_write_replaces_the_one_record_and_keeps_the_title(tmp_path):
    from agentdeck.db import Db

    db = Db(tmp_path / "control.sqlite3")
    assert db.work_resolution(WORK_ID) is None
    db.set_work_title(WORK_ID, "Named")
    first = db.set_work_resolution(_resolution(run_id="run-1", session_key="claude_code:a:s1"))
    assert first is not None and first.run_id == "run-1" and first.updated_at is not None

    again = db.set_work_resolution(
        _resolution(correctness="The selected suite passed.", run_id="run-2", background="")
    )

    reopened = Db(tmp_path / "control.sqlite3")
    stored = reopened.work_resolution(WORK_ID)
    assert stored == again
    assert stored.correctness == "The selected suite passed." and stored.run_id == "run-2"
    assert stored.session_key == ""  # the whole record is replaced, provenance included
    assert first.background and stored.background == ""  # the background too (#3928)
    assert reopened.work_titles() == {WORK_ID: "Named"}
    rows = reopened._conn.execute("SELECT COUNT(*) FROM work_metadata").fetchone()[0]
    assert rows == 1
    # A title written after a resolution leaves the resolution alone.
    reopened.set_work_title(WORK_ID, "")
    assert reopened.work_resolution(WORK_ID) == stored


def test_the_decks_evidence_is_added_to_the_correctness_part_once_and_only_if_it_fits():
    """#4253: the deck writes its own review and test evidence once a Work waits for Merge."""
    resolution = _resolution()
    sentence = "Pakka tarkisti:  katselmointi hyväksyi\n abc123."

    added = with_evidence(resolution, sentence)

    assert added.correctness == (
        "Focused tests pass; the selected suite is running.\n\n"
        "Pakka tarkisti: katselmointi hyväksyi abc123."
    )
    assert (added.background, added.solution) == (resolution.background, resolution.solution)
    assert with_evidence(added, sentence) is added, "not twice"
    assert with_evidence(resolution, "  ") is resolution
    assert with_evidence(_resolution(correctness=""), sentence).correctness.startswith("Pakka")
    full = _resolution(correctness="x" * (MAX_PART - 5))
    with pytest.raises(ResolutionError):  # never past the part's limit: the agent is asked
        with_evidence(full, sentence)
    assert with_evidence(_resolution(correctness=f"x\n\n{added.correctness}"), sentence)


def test_the_decks_evidence_writes_only_its_part_and_keeps_an_edit_made_meanwhile(tmp_path):
    """#4253 review: the deck's append and a person's PUT interleave. Only ``correctness`` is
    written, by a compare-and-swap, so their edit is kept, and the evidence is added to it."""
    from agentdeck.db import Db

    db = Db(tmp_path / "control.sqlite3")
    db.set_work_resolution(_resolution())
    sentence = "Deck: review passed at abc123."
    reads: list[str] = []

    def append(current: str) -> str:
        reads.append(current)
        if len(reads) == 1:  # a person's whole-summary write lands between read and write
            db.set_work_resolution(
                _resolution(problem="Edited problem.", correctness="Edited correctness.")
            )
        return evidence_added(current, sentence)

    assert db.append_work_correctness(WORK_ID, append) is True
    stored = db.work_resolution(WORK_ID)
    assert stored.problem == "Edited problem."
    assert stored.correctness == f"Edited correctness.\n\n{sentence}"
    assert reads[1] == "Edited correctness.", "read again after the swap missed"
    # Already there: nothing written. No summary: None. Always beaten: False, nothing lost.
    assert db.append_work_correctness(WORK_ID, partial(evidence_added, sentence=sentence))
    assert db.append_work_correctness("github_issue:o/r#404", str.upper) is None

    def always_beaten(current: str) -> str:
        db.set_work_resolution(_resolution(correctness=current + "!"))
        return current + " evidence"

    assert db.append_work_correctness(WORK_ID, always_beaten) is False
    assert "evidence" not in db.work_resolution(WORK_ID).correctness


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("  Two   words \n", "Two words"),
        ("", ""),
        (None, ""),
        ("x" * (MAX_TITLE + 5), "x" * MAX_TITLE),
    ],
)
def test_a_title_is_one_trimmed_bounded_line(typed, stored):
    assert normalize_title(typed) == stored


def test_the_custom_title_wins_but_keeps_the_issue_state():
    from test_work_ui import WORK

    record = {"title": "From GitHub", "state": "OPEN", "is_pr": False}
    assert work_title(WORK, record=record) == ("From GitHub", "open")
    assert work_title(WORK, record=record, custom="Mine") == ("Mine", "open")
    assert work_title(WORK, record=None, custom="") == ("", "")


# --- through the deck ----------------------------------------------------------------------------


class _NoGh:
    """Renaming a Work never touches GitHub, so the issue keeps its title."""

    calls = 0

    async def __call__(self, *args, **kwargs):
        type(self).calls += 1
        raise AssertionError("renaming a Work must not call gh")


def _deck(tmp_path, monkeypatch):
    from test_work_ui import WORK, _add, _app

    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", WORK, thinking=True)
    app.state.issues.cached_issues = lambda refs: {
        (WORK.repository, WORK.number): {"title": "From GitHub", "state": "open", "is_pr": False}
    }
    monkeypatch.setattr(gh_cli, "run_once", _NoGh())
    return app


def _rename(client, **data):
    return client.post(work_href(WORK_ID) + "/title", data=data)


def _front_titles(overview) -> set[str]:
    front = overview.front
    return {row.card.title for row in (*front.needs_you, *front.active, *front.finished)}


@pytest.mark.asyncio
async def test_renaming_shows_on_the_work_page_and_the_front_page_and_clearing_restores(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    app = _deck(tmp_path, monkeypatch)
    client = TestClient(app)
    page = client.get(work_href(WORK_ID)).text
    assert "From GitHub" in page and "data-work-rename-open" in page and "data-work-rename" in page

    response = _rename(client, title="  My   Work ", action="save")

    assert response.status_code == 200 and 'data-wd-receipt="sent"' in response.text
    assert app.state.control_db.work_titles() == {WORK_ID: "My Work"}
    page = client.get(work_href(WORK_ID)).text
    assert '<h1 class="wd-title">My Work<button' in page and 'value="My Work"' in page
    assert "<title>eerovil/agentdeck#2147 · My Work" in page
    assert "My Work" in _front_titles(app.state.work_overview.overview)  # the press rebuilt it

    assert _rename(client, title="ignored", action="clear").status_code == 200
    assert app.state.control_db.work_titles() == {}
    assert '<h1 class="wd-title">From GitHub<button' in client.get(work_href(WORK_ID)).text
    assert "From GitHub" in _front_titles(app.state.work_overview.overview)
    assert _NoGh.calls == 0


def test_an_empty_name_clears_it_too(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    app = _deck(tmp_path, monkeypatch)
    client = TestClient(app)
    _rename(client, title="Mine")
    assert _rename(client, title="   ").status_code == 200
    assert app.state.control_db.work_titles() == {}


@pytest.mark.parametrize(
    "headers",
    [{"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}],
    ids=["origin", "fetch-site"],
)
def test_a_cross_site_rename_is_refused(tmp_path, monkeypatch, headers):
    from fastapi.testclient import TestClient

    app = _deck(tmp_path, monkeypatch)
    response = TestClient(app).post(
        work_href(WORK_ID) + "/title", data={"title": "x"}, headers=headers
    )
    assert response.status_code == 403 and app.state.control_db.work_titles() == {}


LONG_ISSUE = "## Why\r\n\r\n" + "A long line about the work. " * 12 + "\r\n\r\nThe last line."


def test_the_work_page_shows_the_issues_text_and_folds_a_long_one(tmp_path, monkeypatch):
    """#4101: above the Summary, a long text's first lines show shut, a short one whole, and
    nothing shows for no text. Each is escaped: the browser's markdown pass reads it as text."""
    from fastapi.testclient import TestClient

    app = _deck(tmp_path, monkeypatch)
    client = TestClient(app)
    held = {"body": LONG_ISSUE}
    app.state.issues.held_issue_body = lambda *ref: (held["body"], True)

    page = client.get(work_href(WORK_ID)).text
    assert 'data-work-issue="long"' in page
    assert f'data-keep-open="work-issue:{WORK_ID}"' in page
    assert page.count('class="wd-issue-text') == 2  # the preview, and the whole text
    assert "\r" not in page[page.index("wd-issue-preview") : page.index("The last line.")]
    assert page.index('id="work-sec-result"') < page.index('id="work-sec-issue"')
    assert page.index('id="work-sec-issue"') < page.index('id="work-sec-resolution"')

    held["body"] = "Run <script>alert(1)</script>\n<!-- agentdeck-marker\nid=1 -->\n"
    page = client.get(work_href(WORK_ID)).text
    assert 'data-work-issue="short"' in page and "wd-fold wd-issue" not in page
    assert "Run &lt;script&gt;alert(1)&lt;/script&gt;</div>" in page
    assert "agentdeck-marker" not in page

    for nothing in ("", "  \n", "<!-- only a marker -->", None):
        held["body"] = nothing
        assert "data-work-issue" not in client.get(work_href(WORK_ID)).text, nothing


def test_a_pull_requests_own_text_starts_open_only_while_it_waits():
    """#4137: an open pull request, a draft too, shows its description; a merged or closed one
    folds it behind its title; no description leaves a plain title and no row leaves nothing.
    The key's end moves with the state, so a shut description does not outlive the merge."""
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader
    from test_work_ui import WORK

    import agentdeck.web
    from agentdeck.work_facts import OutputFact, Registration, work_facts

    templates = Path(agentdeck.web.__file__).parent / "templates"
    macros = Environment(loader=FileSystemLoader(templates), autoescape=True).get_template(
        "partials/work_macros.html"
    )

    def about(**row):
        url = "https://github.com/eerovil/agentdeck/pull/9"
        (output,) = work_facts(
            WORK, rows={url: row}, registered=[Registration(url, True, True)]
        ).outputs
        return str(macros.module.output_about(output, WORK_ID))

    text = {"title": "Fix <b>it</b>", "body": "Why\r\n<!-- marker -->\n<script>x</script>"}
    waiting = about(state="OPEN", **text)
    assert 'data-wd-output-about="waiting"' in waiting and "data-open-by-default open>" in waiting
    assert f'"work-pull:{WORK_ID}:https://github.com/eerovil/agentdeck/pull/9:waiting"' in waiting
    assert "<summary>Fix &lt;b&gt;it&lt;/b&gt;</summary>" in waiting
    assert "Why\n\n&lt;script&gt;" in waiting and "marker" not in waiting and "\r" not in waiting
    assert "data-open-by-default" in about(state="OPEN", isDraft=True, **text)
    shut = about(state="MERGED", **text)
    assert 'data-wd-output-about="done"' in shut and " open" not in shut.split(">")[0]
    assert "data-open-by-default" not in shut
    for empty in ("", None, "<!-- only a marker -->"):
        plain = about(state="OPEN", title="Fix", body=empty)
        assert "data-wd-output-title" in plain and "<details" not in plain
    assert "data-wd-output-title" in about(state="MERGED", title="Old, read before #4137")
    bare = OutputFact("https://github.com/eerovil/agentdeck/pull/9", "eerovil/agentdeck", 9, "")
    assert str(macros.module.output_about(bare, WORK_ID)).strip() == ""
    closed = dataclasses.replace(bare, state="closed", title="Dropped", body="Why")
    assert 'data-wd-output-about="done"' in macros.module.output_about(closed, WORK_ID)


@pytest.mark.asyncio
async def test_an_edited_issue_reaches_the_work_page_behind_the_page(tmp_path, monkeypatch):
    """#4101: a webhook marks the held text out of date, so the page draws it and reads it
    again behind itself; the changed answer moves the page's key, and only its section."""
    from test_work_ui import WORK

    from agentdeck import wayfinder_map
    from agentdeck.web.work_view import load_work_detail, render_changed_sections

    app = _deck(tmp_path, monkeypatch)
    held = {"body": ("Old text.", False)}

    async def read(*ref):
        held["body"] = ("New text.", True)
        return "New text."

    app.state.issues.held_issue_body = lambda *ref: held["body"]
    app.state.issues.issue_body = read
    generation = wayfinder_map.generation()

    drawn = await load_work_detail(app, WORK, workspace=False)
    assert drawn.issue_text == "Old text."
    refresh = app.state.work_tree_refreshes.get(WORK_ID)  # gone once it finished
    if refresh is not None:
        await refresh
    assert wayfinder_map.generation() == generation + 1

    moved = await load_work_detail(app, WORK, workspace=False)
    assert moved.issue_text == "New text."
    templates = app.state.templates
    before = render_changed_sections(templates, drawn, previous={})
    after = render_changed_sections(templates, moved, previous=before)
    assert "New text." in after["issue"][1]
    assert after["resolution"] is before["resolution"]


@pytest.mark.parametrize(
    ("text", "short"),
    [
        ("one\ntwo\nthree", True),
        ("one\ntwo\nthree\nfour", False),
        ("x" * 60, True),
        ("x" * 61, False),
        ("x" * 40 + "\nthree", True),
        ("x" * 41 + "\nfour", False),
        ("\N{SMILING FACE WITH SUNGLASSES}" * 30, True),
        ("\N{SMILING FACE WITH SUNGLASSES}" * 31, False),
        ("# A heading", False),
        ("```\ncode\n```", False),
        ("- one\n- two", False),
        ("1. one", False),
        ("> quoted", False),
        ("a | b\n---|---", False),
        ("-5 degrees, #4101 and 3.5", True),
    ],
)
def test_a_short_issue_is_what_the_preview_would_hold(text, short):
    from agentdeck.web.work_view import WorkDetail

    assert WorkDetail(None, None, (), (), issue_text=text).issue_short is short


def test_the_work_page_shows_the_resolution_only_once_one_is_written(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    app = _deck(tmp_path, monkeypatch)
    client = TestClient(app)
    assert "data-work-resolution" not in client.get(work_href(WORK_ID)).text

    app.state.control_db.set_work_resolution(_resolution(rationale="Line one.\nLine two."))

    page = client.get(work_href(WORK_ID)).text
    assert "data-work-resolution" in page
    assert '<h3 class="wd-title-sm">Summary</h3>' in page
    headings = (
        "Background",
        "Problem",
        "Solution",
        "Why this solution",
        "Why we believe it is correct",
    )
    found = [page.index(f'<h4 class="wd-title-xs">{heading}</h4>') for heading in headings]
    assert found == sorted(found)  # the background first (#3928)
    assert '<div class="wd-resolution-text" data-wd-resolution="rationale">' in page
    assert "<p>Line one.\nLine two.</p>" in page
    assert page.index('id="work-sec-result"') < page.index('id="work-sec-resolution"')

    # Written early, a part not known yet has no heading at all (#3928).
    app.state.control_db.set_work_resolution(_resolution(solution="", rationale="", correctness=""))
    page = client.get(work_href(WORK_ID)).text
    assert '<h4 class="wd-title-xs">Background</h4>' in page
    assert '<h4 class="wd-title-xs">Problem</h4>' in page
    assert '<h4 class="wd-title-xs">Solution</h4>' not in page
    assert 'data-wd-resolution="correctness"' not in page


def test_the_work_diagram_opens_the_live_page_then_the_merged_copy(tmp_path, monkeypatch):
    """The Diagram button (#3827): the newest chat's live page while its file is there, then
    the deck's own checkout for agentdeck, else GitHub; and the page shows it in its header."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from agentdeck import live_prototype
    from agentdeck.web import work_view
    from agentdeck.work import WorkRef
    from agentdeck.work_overview import WorkChat

    def chat(key, hour):
        return WorkChat(key, key, key, "/x", False, datetime(2026, 10, 29, hour, tzinfo=UTC), ())

    worktree = tmp_path / "wt" / "docs" / "diagrams"
    worktree.mkdir(parents=True)
    (worktree / "merge-queue.md").write_text("Intro\n# Merge queue\n```mermaid\n```\n")
    deck = SimpleNamespace(
        state=SimpleNamespace(app_state=SimpleNamespace(chat_identity_key=lambda key: key))
    )
    ours, theirs = WorkRef("eerovil/agentdeck", 1), WorkRef("acme/shop", 2)
    chats = [chat("old", 1), chat("new", 2), chat("none", 3)]
    assert work_view.work_diagram(deck, ours, chats) is None
    live_prototype.set_diagram("old", "https://d/elsewhere/docs/diagrams/old.md?bare=1")
    live_prototype.set_diagram("new", f"https://d{worktree}/merge-queue.md?bare=1")

    shown = work_view.work_diagram(deck, ours, chats)
    assert shown == work_view.WorkDiagram("Merge queue", "/live/new?view=diagram")
    (worktree / "merge-queue.md").unlink()
    monkeypatch.setattr(live_prototype, "DECK_CHECKOUT", tmp_path / "deck")
    assert work_view.work_diagram(deck, theirs, chats) == work_view.WorkDiagram(
        "merge-queue",
        "https://github.com/acme/shop/blob/HEAD/docs/diagrams/merge-queue.md",
        external=True,
    )
    merged = tmp_path / "deck" / "docs" / "diagrams"
    merged.mkdir(parents=True)
    (merged / "merge-queue.md").write_text("# Merge queue, merged\n")
    assert work_view.work_diagram(deck, ours, chats) == work_view.WorkDiagram(
        "Merge queue, merged", f"{merged}/merge-queue.md?bare=1"
    )
    # A diagram drawn in another repository than the issue's links to that one, never to a
    # same-named file in the deck's checkout.
    live_prototype.set_diagram("new", f"https://d{worktree}/merge-queue.md?bare=1", "acme/shop")
    assert work_view.work_diagram(deck, ours, chats).href == (
        "https://github.com/acme/shop/blob/HEAD/docs/diagrams/merge-queue.md"
    )

    app = _deck(tmp_path, monkeypatch)
    client = TestClient(app)
    assert "data-wd-diagram" not in client.get(work_href(WORK_ID)).text
    monkeypatch.setattr(
        work_view, "work_diagram", lambda *_: work_view.WorkDiagram("Flow", "/live/k?view=diagram")
    )
    page = client.get(work_href(WORK_ID)).text
    assert '<a class="wd-diagram" data-wd-diagram href="/live/k?view=diagram" title="Flow">' in page


def test_the_work_page_breaks_a_part_into_paragraphs_and_bullets(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    app = _deck(tmp_path, monkeypatch)
    app.state.control_db.set_work_resolution(
        _resolution(
            solution="Store-box shelves hold whole boxes.\n\n"
            "- Scanning a box shelves it\n- <b>Bold</b>",
        )
    )

    page = TestClient(app).get(work_href(WORK_ID)).text

    solution = page[page.index('data-wd-resolution="solution"') :]
    solution = solution[: solution.index("</div>")]
    assert "<p>Store-box shelves hold whole boxes.</p>" in solution
    assert "<ul><li>Scanning a box shelves it</li><li>&lt;b&gt;Bold&lt;/b&gt;</li></ul>" in solution
    assert "<details" not in solution


@pytest.mark.parametrize(
    ("text", "blocks"),
    [
        ("One paragraph.", (("p", "One paragraph."),)),
        ("Line one.\nLine two.", (("p", "Line one.\nLine two."),)),
        ("Gist.\r\n\r\nDetail.", (("p", "Gist."), ("p", "Detail."))),
        ("Gist.\n  \n\n\nDetail.", (("p", "Gist."), ("p", "Detail."))),
        (
            "Gist.\n\n- one\n* two\n  wrapped\nAfter.",
            (("p", "Gist."), ("ul", ("one", "two wrapped")), ("p", "After.")),
        ),
        ("Intro:\n- a\n- b", (("p", "Intro:"), ("ul", ("a", "b")))),
        ("-not a bullet\n*nor this", (("p", "-not a bullet\n*nor this"),)),
        ("Order.save calls x. e.g. y", (("p", "Order.save calls x. e.g. y"),)),
        ("", ()),
        ("- \n- kept", (("p", "-"), ("ul", ("kept",)))),
    ],
)
def test_resolution_blocks_reads_only_paragraphs_and_bullets(text, blocks):
    from agentdeck.work_resolution import resolution_blocks

    assert resolution_blocks(text) == blocks


# --- the verify worker's write -------------------------------------------------------------------


def _history_deck(tmp_path):
    """A deck with one real chat, ``KEY``, whose account the API resolves."""
    from test_web import _app_with_state

    return _app_with_state(tmp_path, with_transcript=True)


KEY = "claude_code:test:sid1"


_BODY = {
    "problem": "P",
    "solution": "S",
    "rationale": "R: the alternative was rejected because of X.",
    "correctness": "C: the selected suite is running.",
}


@pytest.mark.asyncio
async def test_the_worker_writes_its_own_works_resolution_and_a_retry_replaces_it(tmp_path):
    app = _history_deck(tmp_path)
    state = app.state.app_state
    key = KEY
    state.bind_session_run(key, WORK_ID, "run-1")
    url = f"/api/sessions/{key}/work-resolution"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get(url)).json() == {"work_id": WORK_ID, "resolution": None}
        # The first turn's write: only what is known yet (#3928).
        early = await client.put(url, json={"background": "B: what it is.", "problem": "P"})
        assert early.status_code == 200
        assert early.json()["resolution"]["background"] == "B: what it is."
        assert early.json()["resolution"]["solution"] == ""
        first = await client.put(url, json=_BODY)
        assert first.status_code == 200
        body = first.json()["resolution"]
        assert body["run_id"] == "run-1" and body["session_key"] == key
        assert body["rationale"] == _BODY["rationale"]
        assert body["background"] == ""  # a four-part write from an older guide still works

        again = await client.put(url, json={**_BODY, "correctness": "The selected suite passed."})
        assert again.status_code == 200
        read = (await client.get(url)).json()["resolution"]

    assert read["correctness"] == "The selected suite passed."
    stored = app.state.control_db.work_resolution(WORK_ID)
    assert stored is not None and stored.correctness == "The selected suite passed."
    count = app.state.control_db._conn.execute("SELECT COUNT(*) FROM work_metadata").fetchone()
    assert count[0] == 1


@pytest.mark.asyncio
async def test_the_worker_records_its_live_check_apart_from_the_resolution(tmp_path):
    """#3141: the live check is its own record, so a later resolution write keeps it."""
    app = _history_deck(tmp_path)
    app.state.app_state.bind_session_run(KEY, WORK_ID, "run-1")
    url = f"/api/sessions/{KEY}/live-check"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get(url)).json() == {"work_id": WORK_ID, "live_check": None}
        bad = await client.put(url, json={"result": "fine"})
        assert bad.status_code == 422 and "result must be one of" in bad.json()["detail"]
        put = await client.put(url, json={"result": " Verified ", "note": "the  card\nshows"})
        assert put.status_code == 200
        assert put.json()["live_check"]["result"] == "verified"
        await client.put(f"/api/sessions/{KEY}/work-resolution", json=_BODY)
        read = (await client.get(url)).json()["live_check"]
    assert (read["result"], read["note"]) == ("verified", "the card shows")
    assert read["session_key"] == KEY
    check = app.state.control_db.work_live_check(WORK_ID)
    assert check.line == "checked live: the card shows"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        put = await client.put(url, json={"result": "not_live", "note": "VPS connector"})
    assert put.status_code == 200, "#3220: merged but not running yet"
    assert app.state.control_db.work_live_check(WORK_ID).line == "not live yet: VPS connector"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        bare = await client.put(url, json={"result": "needs_person"})
        put = await client.put(url, json={"result": "needs_person", "note": "open the cart"})
    assert bare.status_code == 422 and "needs a note" in bare.json()["detail"], "#4210"
    assert put.status_code == 200
    assert app.state.control_db.work_live_check(WORK_ID).line == "open the cart"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({**_BODY, "problem": "   "}, "problem must not be empty"),
        ({**_BODY, "problem": "x" * 9000}, "problem is longer than"),
        ({**_BODY, "background": "x" * 9000}, "background is longer than"),
    ],
)
async def test_an_empty_or_runaway_part_is_refused(tmp_path, body, detail):
    app = _history_deck(tmp_path)
    state = app.state.app_state
    key = KEY
    state.bind_session_run(key, WORK_ID, "run-1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.put(f"/api/sessions/{key}/work-resolution", json=body)
    assert response.status_code == 422 and detail in response.json()["detail"]
    assert app.state.control_db.work_resolution(WORK_ID) is None


@pytest.mark.asyncio
async def test_a_chat_in_no_run_cannot_write_a_resolution(tmp_path):
    app = _history_deck(tmp_path)
    key = KEY
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.put(f"/api/sessions/{key}/work-resolution", json=_BODY)
    assert response.status_code == 409 and "not part of any Work" in response.json()["detail"]


OTHER_WORK = "github_issue:eerovil/agentdeck#99"


async def _put_as(app, key, *, work_id=None):
    body = {**_BODY, **({"work_id": work_id} if work_id is not None else {})}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.put(f"/api/sessions/{key}/work-resolution", json=body)


async def _register(app, key, issue):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(f"/api/sessions/{key}/issues", json={"issue": issue})
    assert response.status_code == 200, response.text


def _issue_of(work_id):
    return work_id.split(":", 1)[1]


@pytest.mark.asyncio
async def test_a_rescue_chat_that_registered_the_issue_writes_the_resolution(tmp_path):
    """#2552: a person's rescue chat has no Run, but registering the issue is its own act."""
    app = _history_deck(tmp_path)
    await _register(app, KEY, _issue_of(WORK_ID))

    response = await _put_as(app, KEY)

    assert response.status_code == 200, response.text
    assert response.json()["resolution"]["run_id"] is None
    stored = app.state.control_db.work_resolution(WORK_ID)
    assert stored is not None and stored.session_key == KEY


@pytest.mark.asyncio
async def test_a_side_chat_writes_its_works_resolution(tmp_path):
    app = _history_deck(tmp_path)
    app.state.app_state.db.bind_session_work(KEY, WORK_ID, "side_chat")

    assert (await _put_as(app, KEY)).status_code == 200


@pytest.mark.asyncio
async def test_a_chat_in_two_works_must_name_one_of_its_own(tmp_path):
    app = _history_deck(tmp_path)
    await _register(app, KEY, _issue_of(WORK_ID))
    await _register(app, KEY, _issue_of(OTHER_WORK))

    unnamed = await _put_as(app, KEY)
    assert unnamed.status_code == 409 and OTHER_WORK in unnamed.json()["detail"]
    stranger = await _put_as(app, KEY, work_id="github_issue:eerovil/agentdeck#12345")
    assert stranger.status_code == 409
    # Named in the body, in any case spelling of the issue.
    named = await _put_as(app, KEY, work_id=OTHER_WORK.replace("eerovil", "EeroVil"))
    assert named.status_code == 200 and named.json()["work_id"] == OTHER_WORK
    assert app.state.control_db.work_resolution(WORK_ID) is None
    # The read has no body, so it names the Work in the query.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        read = await client.get(
            f"/api/sessions/{KEY}/work-resolution?work_id={OTHER_WORK.replace('#', '%23')}"
        )
    assert read.status_code == 200 and read.json()["resolution"]["problem"] == "P"


@pytest.mark.asyncio
async def test_a_run_bound_chat_cannot_name_another_work(tmp_path):
    app = _history_deck(tmp_path)
    app.state.app_state.bind_session_run(KEY, WORK_ID, "run-1")

    response = await _put_as(app, KEY, work_id=OTHER_WORK)

    assert response.status_code == 409
    assert app.state.control_db.work_resolution(OTHER_WORK) is None


@pytest.mark.asyncio
async def test_a_chat_that_only_mentions_an_issue_cannot_write_its_resolution(tmp_path):
    app = _history_deck(tmp_path)
    state = app.state.app_state
    url = "https://github.com/" + _issue_of(WORK_ID).replace("#", "/issues/")
    state.sessions[KEY] = dataclasses.replace(state.sessions[KEY], issue_url=url)

    # The deck does count the mention as membership; it is just not the chat's own act.
    chat = state.chat_identity_key(KEY)
    assert WORK_ID in {ref.work_id for ref in state.work_projection().works_for_chat(chat)}

    response = await _put_as(app, KEY, work_id=WORK_ID)

    assert response.status_code == 409


# --- in the browser ------------------------------------------------------------------------------


@pytest.mark.browser
@pytest.mark.asyncio
async def test_an_edited_issue_swaps_in_formatted_and_stays_open_on_a_phone(tmp_path, monkeypatch):
    """#4101: a live swap brings the new text in formatted, a section a person opened stays
    open, and nothing is wider than a phone."""
    from conftest import async_playwright
    from test_web import _live_server
    from test_work_actions import _no_overflow

    from agentdeck import wayfinder_map
    from agentdeck.web.work_view import WORKS_TOPIC

    app = _deck(tmp_path, monkeypatch)
    held = {"body": "[a link](https://example.com/a)\n\n" + LONG_ISSUE}
    app.state.issues.held_issue_body = lambda *ref: (held["body"], True)
    await app.state.work_overview.refresh()
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.add_init_script(
            "document.addEventListener('htmx:sseOpen', () => { window.__sseOpen = true; }, true)"
        )
        await page.goto(base + work_href(WORK_ID))
        await page.wait_for_function("() => window.__sseOpen === true")
        issue = page.locator("#work-issue")
        link = issue.locator(".wd-issue-preview a")
        await link.wait_for()
        await link.evaluate("n => n.focus()")
        assert await page.evaluate("document.activeElement.tagName") != "A"
        await issue.locator(".wd-issue-preview").click(force=True)  # the summary takes the tap
        whole = issue.locator(".wd-issue-text:not(.wd-issue-preview)")
        await whole.locator(".md-h").wait_for()
        await _no_overflow(page, 390)

        held["body"] = LONG_ISSUE.replace("## Why", "## Edited on GitHub")
        wayfinder_map.bump_generation()
        app.state.app_state.bus.publish(WORKS_TOPIC)

        await whole.locator(".md-h", has_text="Edited on GitHub").wait_for()
        assert await page.locator("#work-issue").get_attribute("open") is not None
        await _no_overflow(page, 390)

        # A link is a link only to a safe place; the longest text called short takes three
        # lines on the narrowest phone, and one character more folds.
        held["body"] = "[bad](javascript:alert(1)) [good](https://example.com/a)"
        wayfinder_map.bump_generation()
        app.state.app_state.bus.publish(WORKS_TOPIC)
        short = page.locator('[data-work-issue="short"] .wd-issue-text[data-md]')
        await short.locator('a[href="https://example.com/a"]').wait_for()
        assert await short.locator("a").count() == 1
        assert "[bad](javascript:alert(1))" in await short.inner_text()

        await page.set_viewport_size({"width": 320, "height": 640})
        for text, kind in (
            ("W" * 60, "short"),
            ("W" * 61, "long"),
            ("# Title\n```\ncode\n```", "long"),
        ):
            held["body"] = text
            wayfinder_map.bump_generation()
            app.state.app_state.bus.publish(WORKS_TOPIC)
            box = page.locator(f'[data-work-issue="{kind}"] .wd-issue-text[data-md]').first
            await box.wait_for(state="attached")  # the section stays open, so its preview hides
            if kind == "short":
                rows = await box.evaluate(
                    "n => n.getBoundingClientRect().height"
                    " / parseFloat(getComputedStyle(n).lineHeight)"
                )
                assert rows <= 3.1
        await _no_overflow(page, 320)
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_rename_a_work_on_a_phone_and_back(tmp_path, monkeypatch):
    from conftest import async_playwright
    from test_web import _live_server
    from test_work_actions import _no_overflow

    app = _deck(tmp_path, monkeypatch)
    app.state.control_db.set_work_resolution(_resolution())
    app.state.issues.held_issue_body = lambda *ref: (LONG_ISSUE, True)
    await app.state.work_overview.refresh()
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 320, "height": 640}, service_workers="block"
        )
        await page.add_init_script(
            "document.addEventListener('htmx:sseOpen', () => { window.__sseOpen = true; }, true)"
        )
        await page.goto(base + work_href(WORK_ID))
        await page.wait_for_load_state("load")  # htmx has wired the form
        # The new name comes back by the page's stream, so it must be connected first.
        title = page.locator("#work-sec-status h1.wd-title")
        form = page.locator("#work-rename")
        head = await title.bounding_box()
        assert await form.is_hidden()
        await page.locator("#work-sec-resolution [data-work-resolution]").wait_for()
        await _no_overflow(page, 320)

        # The issue's text (#4101): formatted, three lines shut, the rest a tap away.
        issue = page.locator("#work-issue")
        preview = issue.locator(".wd-issue-preview")
        await issue.locator(".wd-issue-preview[data-md] .md-h").wait_for()
        shut = await preview.bounding_box()
        assert 0 < shut["height"] < 80 and not await issue.get_attribute("open")
        await issue.locator("summary").click()
        await issue.locator(".wd-issue-text:not(.wd-issue-preview)").wait_for()
        assert await preview.is_hidden()
        await _no_overflow(page, 320)
        await issue.locator("summary").click()

        await page.locator("[data-work-rename-open]").click()
        box = form.locator('input[name="title"]')
        await box.wait_for()
        await box.fill("Renamed on a phone")
        await form.locator('button[value="save"]').click()
        await page.locator('#work-rename-result [data-wd-receipt="sent"]').wait_for()
        assert await form.is_hidden()
        # The new name rides back with the receipt (#2864: the page has no stream), not a reload.
        await page.wait_for_function(
            "() => document.querySelector('#work-sec-status h1.wd-title')"
            ".textContent.includes('Renamed on a phone')"
        )
        # Measured until it settles: the swap that brings the name in can still be laying out.
        await page.wait_for_function(
            """(height) => {
                const title = document.querySelector('#work-sec-status h1.wd-title');
                return !!title && title.getBoundingClientRect().height === height;
            }""",
            arg=head["height"],
        )
        await _no_overflow(page, 320)

        await page.goto(base + "/")
        row = page.locator(f'[data-work-id="{WORK_ID}"]').first
        await row.wait_for()
        assert "Renamed on a phone" in await row.text_content()

        await page.goto(base + work_href(WORK_ID))
        await page.wait_for_load_state("load")
        await page.locator("[data-work-rename-open]").click()
        await form.locator('button[value="clear"]').click()
        await page.wait_for_function(
            "() => document.querySelector('#work-sec-status h1.wd-title')"
            ".textContent.includes('From GitHub')"
        )
        assert app.state.control_db.work_titles() == {} and _NoGh.calls == 0
        await browser.close()
