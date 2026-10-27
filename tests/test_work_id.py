"""A GitHub issue gives one source-derived work_id and one inventory row (#2140)."""

import sqlite3
import subprocess
import sys

import pytest

from agentdeck.db import Db
from agentdeck.models import IssueRef, Session, SessionStatus
from agentdeck.state import AppState
from agentdeck.work import (
    WorkBinding,
    WorkChatFacts,
    WorkRef,
    build_work_projection,
    work_id_for,
)

ISSUE = IssueRef("eerovil/agentdeck", 2140)
ISSUE_URL = "https://github.com/eerovil/agentdeck/issues/2140"
WORK_ID = "github_issue:eerovil/agentdeck#2140"


def _session(key: str, **fields) -> Session:
    account, _, session_id = key.rpartition(":")
    return Session(
        key=key, account_key=account, session_id=session_id, status=SessionStatus.IDLE, **fields
    )


def _rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT work_id, source_kind, source_key FROM works ORDER BY work_id"
        ).fetchall()


def test_work_id_is_the_qualified_source():
    assert work_id_for(ISSUE) == WORK_ID
    work = WorkRef.from_issue(ISSUE)
    assert (work.source_kind, work.source_key, work.work_id) == (
        "github_issue",
        "eerovil/agentdeck#2140",
        WORK_ID,
    )


@pytest.mark.parametrize(
    "spelling",
    [
        "EeroVil/AgentDeck#2140",
        "eerovil/AGENTDECK#2140",
        "https://github.com/EEROVIL/agentdeck/issues/2140",
        "  eerovil/agentdeck#2140 ",
    ],
)
def test_casing_and_spelling_variants_give_one_id(spelling):
    assert work_id_for(IssueRef.parse(spelling)) == WORK_ID
    # Built directly from a name as typed, too: two starts once got two ids (#3629).
    ref = IssueRef.parse(spelling)
    assert WorkRef(ref.repository, ref.number).work_id == WORK_ID


def test_same_repository_different_issue_differs():
    assert work_id_for(IssueRef("eerovil/agentdeck", 2141)) != WORK_ID
    # A prefix-sharing number is not the same Work either.
    assert work_id_for(IssueRef("eerovil/agentdeck", 214)) != WORK_ID


def test_different_repository_same_number_differs():
    assert work_id_for(IssueRef("eerovil/trailhead", 2140)) != WORK_ID
    assert work_id_for(IssueRef("other/agentdeck", 2140)) != WORK_ID


def test_id_is_the_same_in_a_fresh_process():
    code = (
        "from agentdeck.models import IssueRef\n"
        "from agentdeck.work import work_id_for\n"
        "print(work_id_for(IssueRef.parse('EEROVIL/AgentDeck#2140')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == WORK_ID


def test_repeated_persistence_stays_one_row_across_reopen(tmp_path):
    path = tmp_path / "deck.db"
    db = Db(path)
    db.record_works([ISSUE, IssueRef("EEROVIL/AgentDeck", 2140)])
    db.record_works([ISSUE])
    first = db.load_works()[WORK_ID]
    db.close()

    reopened = Db(path)  # a web/runtime restart
    reopened.record_works([IssueRef.parse(ISSUE_URL)])
    assert reopened.load_works() == {WORK_ID: first}
    reopened.close()
    assert _rows(path) == [(WORK_ID, "github_issue", "eerovil/agentdeck#2140")]


def test_state_records_registered_inferred_and_already_known_work(tmp_path):
    path = tmp_path / "deck.db"
    db = Db(path)
    # An issue registered before the works table existed.
    db.record_session_issue("claude_code:main:old-1", IssueRef("eerovil/agentdeck", 1))
    state = AppState(db=db, budget_db=db)
    state.set_issues("claude_code:main:reg-1", (IssueRef("EeroVil/agentdeck", 2),))
    state.replace_account_sessions(
        "claude_code:main", [_session("claude_code:main:worker-1", issue_url=ISSUE_URL)]
    )
    db.close()

    assert [row[0] for row in _rows(path)] == [
        "github_issue:eerovil/agentdeck#1",
        "github_issue:eerovil/agentdeck#2",
        WORK_ID,
    ]


def test_provider_account_and_chat_changes_never_move_the_id():
    legs = [
        # One chat that moved provider and account mid-way.
        WorkChatFacts("chat-1", issue_urls=(("claude_code:main:worker-1", ISSUE_URL),)),
        WorkChatFacts("chat-1", issue_urls=(("codex:second:worker-2", ISSUE_URL),)),
        # A fresh review chat that registered another spelling of the issue.
        WorkChatFacts(
            "review-chat",
            registered=(("codex:main:review-1", IssueRef("EEROVIL/agentdeck", 2140)),),
        ),
        # A delegated chat whose Work was saved on it when it was created.
        WorkChatFacts(
            "delegate-chat",
            bindings=(WorkBinding("claude_code:main:delegate-1", WORK_ID, "delegated", "chat-1"),),
        ),
    ]
    (work,) = build_work_projection(legs).works
    assert work.work_id == WORK_ID
    assert set(work.chat_ids) == {"chat-1", "review-chat", "delegate-chat"}
