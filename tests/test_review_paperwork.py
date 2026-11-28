"""The script check of a pull request's paperwork before its review (#4261)."""

from __future__ import annotations

import json

import pytest

from agentdeck import review_paperwork
from agentdeck.review_paperwork import Paperwork, gaps, names_issue

WALK = "Use-case walkthrough: 3 steps, no gaps\n1. ..."
DIAGRAM = "# Flow\n```mermaid\n```\n<!-- agentdeck-diagram -->"


@pytest.mark.parametrize(
    ("body", "named"),
    [
        ("Refs #4261", True),
        ("refs: #4261", True),
        ("Closes #4261", True),
        ("Fixes eerovil/agentdeck#4261", True),
        ("Resolves https://github.com/eerovil/agentdeck/issues/4261", True),
        ("Refs #42610", False),
        ("Refs #426", False),
        ("Refs other/repo#4261", False),
        ("See #4261", False),
        ("", False),
        ("Prefs #4261", False),
    ],
)
def test_the_body_names_the_works_issue(body, named):
    assert names_issue(body, "eerovil/agentdeck", "EEROVIL/agentdeck", 4261) is named


def test_an_issue_in_another_repository_needs_its_full_name():
    assert not names_issue("Refs #5", "eerovil/agentdeck", "eerovil/other", 5)
    assert names_issue("Refs eerovil/other#5", "eerovil/agentdeck", "eerovil/other", 5)
    paper = Paperwork("", (), (WALK,))
    assert "`Refs eerovil/other#5`" in gaps(paper, "eerovil/agentdeck", "eerovil/other", 5)[0]


def test_each_gap_is_named_and_complete_paperwork_has_none():
    files = ("src/agentdeck/x.py", "docs/diagrams/flow.md")
    drawn = files[1:]
    missing = gaps(Paperwork("Some body", files, ("hello",), drawn), "o/r", "o/r", 7)
    assert len(missing) == 3
    assert "`Refs #7`" in missing[0]
    assert "`Use-case walkthrough:`" in missing[1]
    assert "`docs/diagrams/flow.md`" in missing[2] and "agentdeck-diagram" in missing[2]
    assert gaps(Paperwork("Refs #7", files, (WALK, DIAGRAM), drawn), "o/r", "o/r", 7) == []
    # #4264 review: the mark mentioned in prose is no diagram comment; trailing space is fine.
    mention = "Post the diagram ending `<!-- agentdeck-diagram -->` please"
    assert len(gaps(Paperwork("Refs #7", files, (WALK, mention), drawn), "o/r", "o/r", 7)) == 1
    trailing = Paperwork("Refs #7", files, (WALK, DIAGRAM + "\n\n"), drawn)
    assert gaps(trailing, "o/r", "o/r", 7) == []
    # No drawing changed, only box notes: no diagram comment needed (#4598).
    assert gaps(Paperwork("Refs #7", files, (WALK,)), "o/r", "o/r", 7) == []


@pytest.mark.parametrize(
    ("comments", "pull", "helpers", "gap"),
    [
        ((WALK,), "o/r", False, False),
        ((WALK,), "o/r", True, True),
        ((WALK, "Helper checks: #9\n1. fixed in abc"), "o/r", True, False),
        (
            (WALK, "**Helper checks: o/r#9 did not run (no network to the Claude API)**"),
            "o/r",
            True,
            False,
        ),
        ((WALK, "Helper checks: https://github.com/o/r/pull/9 ran"), "o/r", True, False),
        ((WALK, "Please post the `Helper checks: #9` line"), "o/r", True, True),
        # #4437 review: a receipt is per pull request. Another pull request's, or none named,
        # does not do for this one: a Work's second pull request runs its own helpers.
        ((WALK, "Helper checks:\n1. fixed in abc"), "o/r", True, True),
        # Only the pull request the line opens with counts, never one named later (#4437
        # second review).
        ((WALK, "Helper checks: #8 (for #9 see later)"), "o/r", True, True),
        ((WALK, "Helper checks: two runs on #9"), "o/r", True, True),
        ((WALK, "Helper checks: #8\nHelper checks: #9 did not run (x)"), "o/r", True, False),
        ((WALK, "Helper checks: #9, after the fix"), "o/r", True, False),
        ((WALK, "Helper checks: #9/x"), "o/r", True, True),
        ((WALK, "Helper checks: o/r#9#2"), "o/r", True, True),
        ((WALK, "Helper checks: #8\n1. fixed, see #9"), "o/r", True, True),
        ((WALK, "Helper checks: #90"), "o/r", True, True),
        ((WALK, "Helper checks: x/o/r#9"), "o/r", True, True),
        # The pull request in another repository than the issue: only its full name counts.
        ((WALK, "Helper checks: #9"), "o/code", True, True),
        ((WALK, "Helper checks: O/Code#9 did not run (x)"), "o/code", True, False),
    ],
)
def test_the_helper_checks_comment_is_asked_for_before_the_first_review(
    comments, pull, helpers, gap
):
    """#4437: ``helpers`` (no review asked yet) asks for a ``Helper checks:`` line naming this
    pull request; a quote of it in prose, or a line naming another, is not one."""
    body = "Refs o/r#7" if pull != "o/r" else "Refs #7"
    missing = gaps(Paperwork(body, (), comments), pull, "o/r", 7, helpers=helpers, number=9)
    assert bool(missing) is gap
    if gap:
        named = "#9" if pull == "o/r" else "o/code#9"
        assert f"`Helper checks: {named}`" in missing[0] and "pre-review-helpers" in missing[0]


@pytest.mark.parametrize(
    ("changed", "body", "gap"),
    [
        (999, "Refs #7", False),
        (1000, "Refs #7", True),
        (4655, "Refs #7\n\n## Reading guide: why so big\n1. `a.py`", False),
        (1000, "Refs #7\n**Reading guide:** one new file", False),
        (1000, "Refs #7\nreading guide: lower case", False),
        (1000, "Refs #7\nSee the Reading guide: below", True),
        (1000, "Refs #7\n> Reading guide: quoted", True),
        (1000, "Refs #7\n```\nReading guide: in code\n```", True),
        (1000, "Refs #7\n<!-- Reading guide: hidden -->", True),
        (1000, "Refs #7\n<!--\nReading guide: hidden\n-->", True),
        (1000, "Refs #7\nThe deck skips <!-- comments.\n\nReading guide: hidden", True),
        # A ``<!--`` shown inside a code block opens no comment (#4786 helpers).
        (1000, "Refs #7\n```html\n<!--\n```\n\nReading guide: why", False),
        (1000, "Refs #7\n<!-- a\nb -->\nReading guide: after a comment", False),
        # A delimiter in an inline code span is shown text and opens no comment (#4786 review).
        (1000, "Refs #7\nSkips `<!--` in prose.\n\n## Reading guide: why", False),
        (1000, "Refs #7\n``a `<!--` b`` here\nReading guide: why", False),
        (1000, "Refs #7\n<!-- `-->` is in a span -->\nReading guide: why", False),
        (1000, "Refs #7\nx <!-- `code` y\nReading guide: hidden", True),
    ],
)
def test_a_big_pull_request_needs_a_reading_guide(changed, body, gap):
    missing = gaps(Paperwork(body, (), (WALK,), changed=changed), "o/r", "o/r", 7)
    assert bool(missing) is gap
    if gap:
        assert "`Reading guide:`" in missing[0] and f"{changed:,} lines" in missing[0]


@pytest.mark.parametrize(
    ("comment", "found"),
    [
        ("Use-case walkthrough: not applicable — tests only", True),
        ("Screenshots\n\n**Use-case walkthrough: 4 steps, no gaps**", True),
        ("> Use-case walkthrough: 2 steps", True),
        ("The issue lacks the `Use-case walkthrough:` line", False),
        ("Please add a Use-case walkthrough: soon", False),
    ],
)
def test_the_walkthrough_is_a_line_of_its_own(comment, found):
    missing = gaps(Paperwork("Refs #7", (), (comment,)), "o/r", "o/r", 7)
    assert (missing == []) is found


async def test_reads_github_and_an_unread_check_finds_nothing(monkeypatch):
    replies = {
        "pr": {"body": "Refs #7", "files": [{"path": "docs/diagrams/a.md"}, "junk"]}
        | {"baseRefOid": "b" * 40, "headRefOid": "h" * 40},
        "issue": {"comments": [{"body": WALK}, {"body": None}]},
        "api": {
            "merge_base_commit": {"sha": "m" * 40},
            "data": {"repository": {"old": None, "new": {"text": "# New"}}},
        },
    }
    seen: list = []

    async def run(argv, *, timeout, **kw):
        seen.append(argv)
        reply = replies[argv[1]]
        return (0, json.dumps(reply).encode()) if reply is not None else (1, b"")

    monkeypatch.setattr(review_paperwork.gh_cli, "run", run)
    missing = await review_paperwork.check("o/r", 9, "o/r#7")
    assert len(missing) == 1 and "`docs/diagrams/a.md`" in missing[0]
    assert len(await review_paperwork.check("o/r", 9, "o/r#7", helpers=True)) == 2
    replies["issue"]["comments"].append({"body": "Helper checks: #9\n1. fixed"})
    assert len(await review_paperwork.check("o/r", 9, "o/r#7", helpers=True)) == 1
    assert len(await review_paperwork.check("o/r", 10, "o/r#7", helpers=True)) == 2
    assert seen[0][:5] == ["gh", "pr", "view", "9", "--repo"]
    assert seen[1][:4] == ["gh", "issue", "view", "7"]
    assert seen[2][2] == f"repos/o/r/compare/{'b' * 40}...{'h' * 40}?per_page=1"
    assert f"old={'m' * 40}:docs/diagrams/a.md" in seen[3]
    assert f"new={'h' * 40}:docs/diagrams/a.md" in seen[3]
    # A big pull request's body owes a reading guide (#4786); a bool or junk size is unread.
    replies["pr"] |= {"additions": 900, "deletions": 100}
    big = await review_paperwork.check("o/r", 9, "o/r#7")
    assert len(big) == 2 and "changes 1,000 lines" in big[1]
    assert seen[0][-1] == "body,files,baseRefOid,headRefOid,additions,deletions"
    replies["pr"] |= {"additions": True, "deletions": "999"}
    assert len(await review_paperwork.check("o/r", 9, "o/r#7")) == 1
    replies["issue"] = None
    assert await review_paperwork.check("o/r", 9, "o/r#7") == []
    assert await review_paperwork.check("o/r", 9, "not a work") == []


DRAWN = """# Flow

Intro.

```mermaid
flowchart TD
  a[A] --> b[B]
```

## Colours

- **blue**: a step

## Boxes

- **A**: does it
"""


def noted(note: str) -> str:
    return DRAWN.replace("## Colours", f"{note}\n\n## Colours")


def inside(line: str) -> str:
    """``DRAWN`` in a four-backtick block, with ``line`` inside it above the arrow."""
    return DRAWN.replace("```", "````").replace("  a[A]", f"{line}\n  a[A]")


@pytest.mark.parametrize(
    ("old", "new", "drawn"),
    [
        # #4598: the comment shows neither the box notes nor the introduction.
        (DRAWN, DRAWN.replace("does it", "does it twice"), False),
        (DRAWN, DRAWN.replace("Intro.", "Longer.") + "- **B**: more\n", False),
        (DRAWN, DRAWN.replace("Intro.", "Intro.\n# Notes"), False),
        # #4647: nor a plain note under the block, also one split by a blank line.
        (DRAWN, noted("Past 3 s it checks.\n\nThen more."), False),
        (noted("Old note."), noted("New note."), False),
        # A changed block or colour beside a note, and a second block after one, still ask.
        (DRAWN, noted("Note.").replace("b[B]", "b[C]"), True),
        (DRAWN, noted("Note.").replace("a step", "a question"), True),
        (DRAWN, noted("Note.\n\n```mermaid\nflowchart TD\n  c[C]\n```"), True),
        # What may be a legend under the block counts, and so does the text after it.
        (DRAWN, noted("- **amber**: new"), True),
        (DRAWN, noted("| amber | new |"), True),
        (DRAWN, noted("### Legend"), True),
        (noted("Legend\n------"), noted("Key\n------"), True),
        (noted("Legend\n==="), noted("Key\n==="), True),
        (noted("amber | old\n--- | ---"), noted("amber | new\n--- | ---"), True),
        (DRAWN, noted("> amber: new"), True),
        (DRAWN, noted("    amber: new"), True),
        (DRAWN, noted("1."), True),
        (DRAWN, noted("<b>amber</b>"), True),
        (DRAWN, noted("<br/>"), True),
        (DRAWN, noted("</div>"), True),
        (DRAWN, noted("<!-- amber -->"), True),
        (DRAWN, noted("<span\nclass='x'>amber</span>"), True),
        # Text that only looks like one of them.
        (DRAWN, noted("Checks\n#4645): too."), False),
        (DRAWN, noted("<3 s is short."), False),
        (DRAWN, noted("<https://example.com/a> has more."), False),
        (DRAWN, noted("<first-last@example.com> knows."), False),
        (DRAWN, noted("<a/b@example.com> knows."), False),
        (DRAWN, noted("```x``` is the flag."), False),
        (noted("### Legend\n\namber: old"), noted("### Legend\n\namber: new"), True),
        # A line inside the block that is no closing fence for it does not end it.
        (inside("```"), inside("```").replace("b[B]", "b[C]"), True),
        (inside("    ````"), inside("    ````").replace("b[B]", "b[C]"), True),
        (inside("## Boxes"), inside("## Boxes").replace("b[B]", "b[C]"), True),
        # Unusual shapes count as drawing.
        (DRAWN.replace("```", "~~~"), DRAWN.replace("```", "~~~").replace("b[B]", "b[C]"), True),
        (DRAWN.replace("```", "  ```"), DRAWN.replace("```", "  ```").replace("[B]", "[C]"), True),
        (
            DRAWN.replace("Colours", "Colours (key)"),
            DRAWN.replace("Colours", "Colours (key)").replace("a step", "a question"),
            True,
        ),
        (DRAWN.replace("```\n\n", ""), DRAWN.replace("```\n\n", "").replace("step", "x"), True),
        (DRAWN, DRAWN.replace("b[B]", "b[C]"), True),
        (DRAWN, DRAWN.replace("a step", "a question"), True),
        (DRAWN, DRAWN.replace("# Flow", "# Other"), True),
        (None, DRAWN, True),  # added
        (DRAWN, None, False),  # deleted
        ("unread", DRAWN, False),
    ],
)
async def test_only_a_changed_drawing_asks_for_the_comment(monkeypatch, old, new, drawn):
    async def run(argv, *, timeout, repo=None, **kw):
        if argv[2] != "graphql":
            return 0, json.dumps({"merge_base_commit": {"sha": "m"}}).encode()
        assert repo == "o/r"  # graphql names no repository in its argv
        if old == "unread":
            return 1, b""
        blob = {
            "old": None if old is None else {"text": old},
            "new": None if new is None else {"text": new},
        }
        return 0, json.dumps({"data": {"repository": blob}}).encode()

    monkeypatch.setattr(review_paperwork.gh_cli, "run", run)
    path = "docs/diagrams/a.md"
    found = await review_paperwork.read_drawn("o/r", "b", "h", [path])
    assert found == ((path,) if drawn else ())


async def test_an_added_diagram_asks_unread_and_a_posted_comment_reads_nothing(monkeypatch):
    files = [
        {"path": "docs/diagrams/new.md", "changeType": "ADDED"},
        {"path": "docs/diagrams/old.md", "changeType": "MODIFIED"},
    ]
    pull = {"body": "Refs #7", "files": files, "baseRefOid": "b", "headRefOid": "h"}
    comments = [{"body": WALK}]
    calls: list[str] = []

    async def run(argv, *, timeout, **kw):
        calls.append(argv[1])
        reply = {"pr": pull, "issue": {"comments": comments}}.get(argv[1])
        return (0, json.dumps(reply).encode()) if reply else (1, b"")  # compare fails

    monkeypatch.setattr(review_paperwork.gh_cli, "run", run)
    paper = await review_paperwork.read("o/r", 9, "o/r", 7)
    assert paper.drawn == ("docs/diagrams/new.md",)
    calls.clear()
    comments.append({"body": DIAGRAM})
    assert (await review_paperwork.read("o/r", 9, "o/r", 7)).drawn == ()
    assert calls == ["pr", "issue"]


ROBOT = "Acme/bot"


@pytest.mark.parametrize(
    ("files", "body", "gap"),
    [
        (("bot/run.py",), "Refs #7", True),
        (("bot/run.py", "tests/test_run.py"), "Refs #7", False),
        (("bot/run.py", "bot/test_run.py"), "Refs #7", False),
        (("bot/run.py",), "Refs #7\n\nNo test: a log line only", False),
        (("bot/run.py",), "Refs #7\n- **No test:** config only", False),
        (("bot/run.py",), "Refs #7\nNo test:", True),  # no reason given
        (("bot/run.py",), "Refs #7, see the `No test: <why>` rule", True),
        # #4327 review: the placeholder, a quote or a code example is no reason.
        (("bot/run.py",), "Refs #7\nNo test: <why>", True),
        (("bot/run.py",), "Refs #7\nNo test: `<why>`", True),
        (("bot/run.py",), "Refs #7\n> No test: a log line only", True),
        (("bot/run.py",), "Refs #7\n```\nNo test: a log line only\n```", True),
        (("bot/run.py",), "Refs #7\n~~~md\nNo test: x\n~~~\nNo test: config only", False),
        (("bot/run.py",), "Refs #7\n    No test: indented code", True),
        (("bot/run.py",), "Refs #7\nno test: the <b> tag only", False),
        # A hidden HTML comment is not shown, so it gives no reason either.
        (("bot/run.py",), "Refs #7\n<!--\nNo test: arbitrary reason\n-->", True),
        (("bot/run.py",), "Refs #7\n<!-- No test: x -->", True),
        (("bot/run.py",), "Refs #7\n<!--\nNo test: never closed", True),
        (("bot/run.py",), "Refs #7\n<!-- note -->\nNo test: config only", False),
        (("tests/conftest.py", "tests/helpers.py", "bot/run_test.py"), "Refs #7", False),
        (("README.md", "bin/test.sh"), "Refs #7", False),
        ((), "Refs #7", False),
    ],
)
def test_robot_python_code_needs_a_test_or_a_reason(files, body, gap):
    missing = gaps(Paperwork(body, files, (WALK,)), ROBOT, ROBOT, 7, tests=True)
    assert bool(missing) is gap
    if gap:
        assert "`bot/run.py`" in missing[0] and "`No test: <why>`" in missing[0]
    # Only a repository whose tests are checked asks (#4326).
    assert gaps(Paperwork(body, files, (WALK,)), ROBOT, ROBOT, 7) == []


W = "github_issue:o/robot#7"
EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _job(id_, state, head, *, work=W, result="success", at=1.0, purpose="verification"):
    snap = {
        "root": "/w",
        "head": head,
        "staged_sha256": EMPTY,
        "tracked_diff_sha256": EMPTY,
        "untracked_sha256": EMPTY,
        "untracked_count": 0,
    }
    return {
        "id": id_,
        "work_id": work,
        "purpose": purpose,
        "state": state,
        "result_class": result if state != "queued" else "",
        "submitted_at": at,
        "tested_workspace": snap,
        "verified_workspace": snap,
        "tested_workspaces": {"r": snap},
        "verified_workspaces": {"r": snap},
    }


HEAD = "a" * 40
OTHER = "b" * 40


def test_the_head_needs_a_verification_job_queued_running_or_green():
    gap = review_paperwork.verification_gap
    assert "no verification job" in gap([], W, HEAD)
    assert gap([_job("j1", "queued", "")], W, HEAD) == ""
    assert gap([_job("j1", "running", "")], W.upper(), HEAD.upper()) == ""
    assert gap([_job("j1", "running", HEAD)], W, HEAD.upper()) == ""
    assert "no verification job" in gap([_job("j1", "running", OTHER)], W, HEAD)
    # Another Work's job, or a plain job, is not this Work's run.
    assert gap([_job("j1", "running", HEAD, work="github_issue:o/robot#8")], W, HEAD)
    assert gap([_job("j1", "running", HEAD, purpose="")], W, HEAD)
    # Green only at another head proves nothing here.
    assert "no verification job" in gap([_job("j1", "done", OTHER)], W, HEAD)
    red = _job("j2", "failed", HEAD, result="failure", at=2.0)
    found = gap([_job("j1", "done", OTHER), red], W, HEAD)
    assert found.startswith("verification job j2 failed at aaaaaaaaaaaa")
    # A newer green run at the head clears the red one.
    assert gap([red, _job("j3", "done", HEAD, at=3.0)], W, HEAD) == ""
    # The newest job that speaks for the head decides, in submission order (#4327 review):
    # a newer red run is not cleared by an older green, queued or running one ...
    for older in (
        _job("j0", "done", HEAD, at=0.5),
        _job("j0", "queued", "", at=0.5),
        _job("j0", "running", HEAD, at=0.5),
    ):
        assert gap([older, red], W, HEAD).startswith("verification job j2 failed"), older
    # ... while a newer queued or running rerun supersedes an older red one.
    assert gap([red, _job("j4", "queued", "", at=4.0)], W, HEAD) == ""
    assert gap([red, _job("j4", "running", HEAD, at=4.0)], W, HEAD) == ""
    # A run going on at another head, or newer jobs elsewhere, leave the red one standing.
    assert gap([red, _job("j4", "running", OTHER, at=4.0)], W, HEAD).startswith(
        "verification job j2"
    )
    # A job that proves nothing (cancelled at the head) passes over to the one before it.
    cancelled = _job("j5", "cancelled", HEAD, at=5.0, result="")
    assert gap([red, cancelled], W, HEAD).startswith("verification job j2")
    assert gap([_job("j3", "done", HEAD, at=3.0), cancelled], W, HEAD) == ""


def _red(id_="j2", at=2.0, base=OTHER):
    job = _job(id_, "failed", HEAD, result="failure", at=at)
    for key in ("tested_workspace", "verified_workspace"):
        job[key] = dict(job[key], base_head=base)
    job.update(
        command=["pytest", "-q"],
        failures=["tests/test_a.py::t"],
        failures_complete=True,
        base_present=["tests/test_a.py::t"],
    )
    return job


def _follow(id_, kind, head, *, state="done", failures=(), passed=(), at=3.0):
    job = _job(id_, state, head, result="failure" if failures else "success", at=at, purpose="")
    job.update(
        follows="j2",
        follow_kind=kind,
        follow_tests=["tests/test_a.py::t"],
        failures=list(failures),
        passed=list(passed),
        failures_complete=True,
    )
    return job


def test_a_failure_its_follow_ups_forgive_is_not_red():
    """The release credits a failure its reruns or merge-base run forgive; so does the check,
    reading the follow-ups that carry no verification purpose (#4327 review)."""
    gap = review_paperwork.verification_gap
    assert gap([_red()], W, HEAD).startswith("verification job j2 failed")
    # Passed on a same-commit rerun: flaky.
    rerun = _follow("f1", "rerun", HEAD, passed=["tests/test_a.py::t"])
    assert gap([_red(), rerun], W, HEAD) == ""
    # Failed on the merge base too: broken before this branch.
    base = _follow("f2", "base", OTHER, failures=["tests/test_a.py::t"])
    assert gap([_red(), base], W, HEAD) == ""
    # A rerun that failed again forgives nothing; one still running may yet.
    again = _follow("f1", "rerun", HEAD, failures=["tests/test_a.py::t"])
    assert gap([_red(), again], W, HEAD).startswith("verification job j2 failed")
    assert gap([_red(), _follow("f3", "rerun", "", state="queued")], W, HEAD) == ""
    # Another Work's follow-up is not this one's.
    other = dict(rerun, work_id="github_issue:o/robot#8")
    assert gap([_red(), other], W, HEAD).startswith("verification job j2 failed")


async def test_the_work_jobs_read_raises_when_unread_and_merges_the_queue():
    """#4327 review: an unread Runtime is an error, not an empty job list, so the check
    holds nothing back; a read merges the evidence with this Work's queue rows."""
    import httpx

    from agentdeck.web.work_view import runtime_work_jobs

    answers: dict = {
        "/job-evidence": {"evidence": [_job("j1", "done", OTHER)]},
        "/jobs": {"jobs": [_job("j1", "running", HEAD), _job("j9", "queued", "", work="x")]},
    }

    def handle(request):
        reply = answers[request.url.path]
        if reply is None:
            return httpx.Response(503)
        assert request.url.params["work_id"] == W
        return httpx.Response(200, json=reply)

    class App:
        class state:  # noqa: N801
            runtime_http = httpx.AsyncClient(
                transport=httpx.MockTransport(handle), base_url="http://rt"
            )

    rows = await runtime_work_jobs(App, W)
    # The queue's copy wins; another Work's unfinished job is read but never counts.
    assert [(row["id"], row["state"]) for row in rows] == [("j1", "running"), ("j9", "queued")]
    assert review_paperwork.verification_gap(rows, W, HEAD) == ""
    assert review_paperwork.verification_gap(rows[1:], W, HEAD)
    answers["/jobs"] = None
    with pytest.raises(httpx.HTTPError):
        await runtime_work_jobs(App, W)
    answers["/jobs"] = {"no": "list"}
    with pytest.raises(ValueError):
        await runtime_work_jobs(App, W)
    App.state.runtime_http = None
    with pytest.raises(RuntimeError):
        await runtime_work_jobs(App, W)
