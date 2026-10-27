from agentdeck.db import Db
from agentdeck.models import IssueRef, PullRequestRef
from agentdeck.state import AppState


def test_registered_work_links_survive_a_reopen(tmp_path):
    path = tmp_path / "control.db"
    session_key = "codex:test:thread-1"
    issue = IssueRef("eerovil/agentdeck", 1749)
    pull = PullRequestRef("eerovil/agentdeck", 1759)
    control = Db(path)
    try:
        control.ensure_session_chats((session_key,))
        state = AppState(db=control, budget_db=control)
        assert state.set_issues(session_key, (issue,)) == (issue,)
        assert state.set_pull_requests(session_key, (pull,)) == (pull,)
    finally:
        control.close()

    reopened = Db(path)
    try:
        state = AppState(
            db=reopened,
            budget_db=reopened,
        )

        assert state.issues_for(session_key) == (issue,)
        assert state.pull_requests_for(session_key) == (pull,)
        assert state.set_issues(session_key, ()) == ()
        assert state.set_pull_requests(session_key, ()) == ()
    finally:
        reopened.close()

    reopened = Db(path)
    try:
        state = AppState(
            db=reopened,
            budget_db=reopened,
        )

        assert state.issues_for(session_key) == ()
        assert state.pull_requests_for(session_key) == ()
    finally:
        reopened.close()
