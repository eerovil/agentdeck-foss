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
    missing = gaps(Paperwork("Some body", files, ("hello",)), "o/r", "o/r", 7)
    assert len(missing) == 3
    assert "`Refs #7`" in missing[0]
    assert "`Use-case walkthrough:`" in missing[1]
    assert "`docs/diagrams/flow.md`" in missing[2] and "agentdeck-diagram" in missing[2]
    assert gaps(Paperwork("Refs #7", files, (WALK, DIAGRAM)), "o/r", "o/r", 7) == []
    # #4264 review: the mark mentioned in prose is no diagram comment; trailing space is fine.
    mention = "Post the diagram ending `<!-- agentdeck-diagram -->` please"
    assert len(gaps(Paperwork("Refs #7", files, (WALK, mention)), "o/r", "o/r", 7)) == 1
    assert gaps(Paperwork("Refs #7", files, (WALK, DIAGRAM + "\n\n")), "o/r", "o/r", 7) == []
    # No diagram changed: no diagram comment needed (#4254 draws one only for a new feature).
    assert gaps(Paperwork("Refs #7", files[:1], (WALK,)), "o/r", "o/r", 7) == []


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
        "pr": {"body": "Refs #7", "files": [{"path": "docs/diagrams/a.md"}, "junk"]},
        "issue": {"comments": [{"body": WALK}, {"body": None}]},
    }
    seen: list = []

    async def run(argv, *, timeout, **kw):
        seen.append(argv)
        reply = replies[argv[1]]
        return (0, json.dumps(reply).encode()) if reply is not None else (1, b"")

    monkeypatch.setattr(review_paperwork.gh_cli, "run", run)
    missing = await review_paperwork.check("o/r", 9, "o/r#7")
    assert len(missing) == 1 and "`docs/diagrams/a.md`" in missing[0]
    assert seen[0][:5] == ["gh", "pr", "view", "9", "--repo"]
    assert seen[1][:4] == ["gh", "issue", "view", "7"]
    replies["issue"] = None
    assert await review_paperwork.check("o/r", 9, "o/r#7") == []
    assert await review_paperwork.check("o/r", 9, "not a work") == []
