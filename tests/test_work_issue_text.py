"""The issue, and a wayfinder ticket's map, pasted into a Work's first message (#4254)."""

from __future__ import annotations

import json

import pytest

from agentdeck import work_issue_text
from agentdeck.work_issue_text import issue_text
from agentdeck.work_signals import start_comment


def _runner(pages: dict[str, object], calls: list[str]):
    async def run(argv, repository):
        path = argv[-1]
        calls.append(path)
        if path not in pages:
            return 1, b"", b"HTTP 404"
        return 0, json.dumps(pages[path]).encode(), b""

    return run


TICKET = (
    "## Question\n\nWhich way?\n\n"
    "Map: [reduce tokens](https://github.com/o/r/issues/1)\n\nThis is a wayfinder ticket."
)


@pytest.mark.asyncio
async def test_the_issue_its_comments_and_its_map_are_pasted_without_the_start_comment():
    calls: list[str] = []
    pages = {
        "repos/o/r/issues/7": {"title": "Pick a way", "body": TICKET, "comments": 2},
        "repos/o/r/issues/7/comments?per_page=30": [
            {"body": start_comment(action_id="work-start-x"), "user": {"login": "deck"}},
            {"body": "Also the other way.", "user": {"login": "eero"}},
        ],
        "repos/o/r/issues/1": {
            "title": "reduce tokens",
            "body": "Destination: less",
            "comments": 0,
        },
    }
    text = await issue_text("o/r", 7, run=_runner(pages, calls))

    assert text.startswith("The issue as it reads now is below")
    assert "Issue o/r#7: Pick a way" in text and "Which way?" in text
    assert "--- comment by eero ---\nAlso the other way." in text
    assert "AgentDeck started an agent" not in text
    assert "Its map o/r#1: reduce tokens\n\nDestination: less" in text
    # A map with no comments costs no comment read.
    assert "repos/o/r/issues/1/comments?per_page=30" not in calls


@pytest.mark.asyncio
async def test_an_unread_issue_pastes_nothing_and_an_unread_map_or_comments_say_so():
    assert await issue_text("o/r", 7, run=_runner({}, [])) == ""
    pages = {"repos/o/r/issues/7": {"title": "T", "body": TICKET, "comments": 3}}
    text = await issue_text("o/r", 7, run=_runner(pages, []))
    assert "(Its 3 comments could not be read: read them.)" in text
    assert "(Its map o/r#1 could not be read: read it.)" in text


@pytest.mark.asyncio
async def test_a_huge_issue_is_cut_and_says_so(monkeypatch):
    monkeypatch.setattr(work_issue_text, "MAX_CHARS", 50)
    pages = {"repos/o/r/issues/7": {"title": "T", "body": "x" * 500, "comments": 0}}
    text = await issue_text("o/r", 7, run=_runner(pages, []))
    assert "[cut here: read the rest on GitHub]" in text
    assert "x" * 60 not in text
