"""Control-plane state that must survive a web restart, read back after a reopen."""

from datetime import UTC, datetime

from agentdeck.context_delivery import ContextDelivery
from agentdeck.db import Db
from agentdeck.models import IssueRef, PullRequestRef
from agentdeck.state import AppState


def test_attention_report_survives_a_reopen(tmp_path):
    path = tmp_path / "control.db"
    target = "codex:test:target"
    detail = "Context delivery remains fenced; provider outcome is uncertain."
    control = Db(path)
    try:
        routed = control
        routed.record_session_attention_report(target, "waiting", detail)

        assert routed.load_session_attention_reports()[target].detail == detail
    finally:
        control.close()

    reopened = Db(path)
    try:
        routed = reopened
        restarted = AppState(db=routed, budget_db=reopened)
        report = restarted.attention_reports[target]

        assert report.state == "waiting"
        assert report.detail == detail

        routed.delete_session_attention_report(target)
        assert routed.load_session_attention_reports() == {}
    finally:
        reopened.close()


def test_registered_github_refs_survive_a_reopen(tmp_path):
    path = tmp_path / "control.db"
    target = "codex:test:target"
    control = Db(path)
    try:
        state = AppState(db=control, budget_db=control)
        state.set_issues(target, (IssueRef("eerovil/agentdeck", 1749),))
        state.set_pull_requests(target, (PullRequestRef("eerovil/agentdeck", 1750),))
    finally:
        control.close()

    reopened = Db(path)
    try:
        restarted = AppState(db=reopened, budget_db=reopened)

        assert restarted.issues_for(target) == (IssueRef("eerovil/agentdeck", 1749),)
        assert restarted.pull_requests_for(target) == (PullRequestRef("eerovil/agentdeck", 1750),)
    finally:
        reopened.close()


def test_rollover_reasons_read_the_store(tmp_path):
    control = Db(tmp_path / "control.db")
    source = "codex:test:source"
    target = "codex:test:target"
    try:
        chat = control.ensure_session_chats((source,))[0]
        row = ContextDelivery(
            action_id="action-1",
            attempt=1,
            provider_id="codex",
            account_key="codex:test",
            worker_key="worker-1",
            delivery_id="delivery-1",
            request_sha256="a" * 64,
            chat_id=chat.chat_id,
            source_session_key=source,
            source_session_id="source",
            mode="enforce",
            proposed_action="rollover",
            executed_action="rollover",
            reason="hard threshold",
            target_session_key=target,
        )
        assert control.reserve_context_delivery(row) is not None

        state = AppState(db=control, budget_db=control)

        assert state.chat_rollover_reasons(source) == {target: "hard threshold"}
    finally:
        control.close()


def test_deckhand_checkpoint_survives_a_reopen(tmp_path):
    path = tmp_path / "control.db"
    looked_at = datetime(2026, 9, 9, 5, 0, tzinfo=UTC)
    checkpoint = {
        "version": 11,
        "working": ["codex:test:first"],
        "last_look": looked_at.isoformat(),
        "unsent": [{"session_key": "codex:test:second", "headline": "finished"}],
        "contexts": {},
    }
    control = Db(path)
    try:
        control.record_assistant_checkpoint(checkpoint)
    finally:
        control.close()

    reopened = Db(path)
    try:
        assert reopened.load_assistant_checkpoint() == checkpoint
    finally:
        reopened.close()
