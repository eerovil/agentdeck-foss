from datetime import UTC, datetime
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from agentdeck.app import create_app
from agentdeck.assistant import AssistantInsight, AssistantView
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig
from agentdeck.git_context import GitContext, PullRequestContext
from agentdeck.host_stats import HostStats
from agentdeck.models import (
    Capability,
    IssueRef,
    PullRequestRef,
    Session,
    SessionStatus,
    UsageSnapshot,
)
from agentdeck.providers import PROVIDERS


def _app(tmp_path):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        accounts=[AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    now = datetime(2026, 7, 28, 9, 30, tzinfo=UTC)
    session = Session(
        key="claude_code:test:session-1",
        account_key="claude_code:test",
        session_id="session-1",
        status=SessionStatus.LIVE,
        thinking=True,
        stalled=True,
        cwd=Path("/work/agentdeck"),
        generated_title="AgentDeck · Build whole-deck API",
        question="Which response shape should I use?",
        activity="Running tests",
        worker_type="kanban",
        issue_url="https://github.com/eerovil/agentdeck/issues/436",
        issue_status="open",
        started_at=now,
        last_activity=now,
        last_progress=now,
        capabilities=frozenset({Capability.TRANSCRIPT, Capability.STEER}),
        # These are intentionally absent from the read API.
        initial_prompt="private first prompt",
        last_prompt="private latest prompt",
        last_text="private latest answer",
        pid=1234,
    )
    app.state.app_state.update_session(session)
    app.state.app_state.add_issue(session.key, IssueRef("eerovil/agentdeck", 436))
    app.state.app_state.add_pull_request(session.key, PullRequestRef("eerovil/agentdeck", 999))
    app.state.app_state.set_usage(
        UsageSnapshot(
            account_key="claude_code:test",
            five_hour_pct=42.0,
            five_hour_resets_at=None,
            seven_day_pct=7.0,
            seven_day_resets_at=None,
            fetched_at=now,
        )
    )
    app.state.app_state.set_host_stats(
        HostStats(
            cpu_pct=23.0,
            memory_pct=61.0,
            memory_used_bytes=10,
            memory_total_bytes=20,
            sampled_at=now,
        )
    )
    app.state.assistant.view = AssistantView(
        state="ready",
        insights=(AssistantInsight(session.key, "waiting", "waiting", "Session is stalled"),),
        analyzed_at=now,
    )
    app.state.assistant._contexts.update(
        {
            session.key: GitContext(
                repository="eerovil/agentdeck",
                branch="agent/issue-436",
                dirty=False,
                pull_requests=(
                    PullRequestContext(
                        repository="eerovil/agentdeck",
                        number=999,
                        title="Whole-deck API",
                        url="https://github.com/eerovil/agentdeck/pull/999",
                        status="open",
                        head_branch="agent/issue-436",
                        base_branch="master",
                    ),
                ),
                head_sha="d" * 40,
            )
        }
    )
    return app


async def test_whole_deck_routes_return_current_in_memory_state(tmp_path):
    app = _app(tmp_path)
    chat_id = app.state.app_state.sessions["claude_code:test:session-1"].chat_id
    assert chat_id is not None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        sessions = (await client.get("/api/sessions")).json()
        usage = (await client.get("/api/usage")).json()
        host_stats = (await client.get("/api/host-stats")).json()
        git_context = (await client.get("/api/git-context")).json()

    assert sessions == {
        "sessions": [
            {
                "key": "claude_code:test:session-1",
                "session_url": f"/chats/{chat_id}",
                "account_key": "claude_code:test",
                "status": "live",
                "display_state": "thinking",
                "title": "AgentDeck · Build whole-deck API",
                "cwd": "/work/agentdeck",
                "thinking": True,
                "stalled": True,
                "turn_failed": False,
                "subagent_count": 0,
                "activity": "Running tests",
                "waiting": True,
                "worker_type": "kanban",
                "parent_session_key": None,
                "is_delegated": False,
                "issue_url": "https://github.com/eerovil/agentdeck/issues/436",
                "issue_status": "open",
                "issues": [
                    {
                        "repository": "eerovil/agentdeck",
                        "number": 436,
                        "url": "https://github.com/eerovil/agentdeck/issues/436",
                    }
                ],
                "started_at": "2026-07-28T09:30:00+00:00",
                "last_activity": "2026-07-28T09:30:00+00:00",
                "last_progress": "2026-07-28T09:30:00+00:00",
                "capabilities": ["steer", "transcript"],
                "pull_requests": [
                    {
                        "repository": "eerovil/agentdeck",
                        "number": 999,
                        "url": "https://github.com/eerovil/agentdeck/pull/999",
                    }
                ],
            }
        ]
    }
    assert "private" not in str(sessions)
    assert "1234" not in str(sessions)
    assert usage["stale_after_s"] == 900.0
    assert usage["usage"] == [
        {
            "account_key": "claude_code:test",
            "five_hour_pct": 42.0,
            "five_hour_resets_at": None,
            "seven_day_pct": 7.0,
            "seven_day_resets_at": None,
            "fetched_at": "2026-07-28T09:30:00+00:00",
            "stale": False,
            # The billing-cycle window (#730); None for a provider that reports
            # rolling windows instead.
            "month_pct": None,
            "month_resets_at": None,
        }
    ]
    assert host_stats["host_stats"]["cpu_pct"] == 23.0
    assert host_stats["host_stats"]["sampled_at"] == "2026-07-28T09:30:00+00:00"
    assert git_context["git_context"] == {
        "claude_code:test:session-1": {
            "repository": "eerovil/agentdeck",
            "branch": "agent/issue-436",
            "dirty": False,
            "pull_requests": [
                {
                    "repository": "eerovil/agentdeck",
                    "number": 999,
                    "title": "Whole-deck API",
                    "url": "https://github.com/eerovil/agentdeck/pull/999",
                    "status": "open",
                    "draft": False,
                    "head_branch": "agent/issue-436",
                    "base_branch": "master",
                    # The pull request's own head commit (#2088). None here because this
                    # fixture predates the field, and an unknown commit is spelled out
                    # rather than left off the payload.
                    "head_sha": None,
                }
            ],
            "pulls_complete": True,
            # The commit the session chip is about (#1127).
            "head_sha": "dddddddddddddddddddddddddddddddddddddddd",
        }
    }


async def test_whole_deck_routes_never_scan_providers_or_resolve_git_context(tmp_path, monkeypatch):
    app = _app(tmp_path)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("read-only deck routes must not perform external reads")

    monkeypatch.setattr(PROVIDERS["claude_code"], "scan_sessions", forbidden)
    monkeypatch.setattr(app.state.assistant.context_resolver, "resolve", forbidden)
    monkeypatch.setattr(app.state.assistant.context_resolver, "_run", forbidden)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path in (
            "/api/sessions",
            "/api/usage",
            "/api/host-stats",
            "/api/git-context",
        ):
            response = await client.get(path)
            assert response.status_code == 200


async def test_empty_whole_deck_state_is_still_valid_json(tmp_path):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        accounts=[AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/sessions")).json() == {"sessions": []}
        assert (await client.get("/api/poller/account-loads")).status_code == 404
        assert (await client.get("/api/usage")).json()["usage"] == []
        assert (await client.get("/api/host-stats")).json() == {"host_stats": None}
        assert (await client.get("/api/git-context")).json() == {"git_context": {}}














def _history_app(tmp_path):
    """The same deck with its history database switched on, which is where reviews land."""
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "history.db")),
        accounts=[AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))],
    )
    return create_app(config)


_REVIEW = {"repo": "org/store", "sha": "a" * 40, "verdict": "pass", "pull_request": "org/store#7"}


async def test_a_review_verdict_is_stored_and_read_back(tmp_path):
    """#1122: the record a review leaves about one commit."""
    app = _history_app(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        posted = await client.post("/api/review-verdicts", json=_REVIEW)
        listed = await client.get("/api/poller/review-verdicts")

    assert posted.json()["recorded"] is True and posted.json()["stored"] is True
    rows = listed.json()["review_verdicts"]
    assert len(rows) == 1
    assert rows[0]["repo"] == "org/store" and rows[0]["sha"] == "a" * 40
    assert rows[0]["verdict"] == "pass" and rows[0]["result"] == "none"


async def test_the_same_commit_delivered_twice_stays_one_review(tmp_path):
    """A verdict is about a commit, so a second one for it replaces the first."""
    app = _history_app(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/review-verdicts", json=_REVIEW)
        await client.post("/api/review-verdicts", json={**_REVIEW, "verdict": "changes-needed"})
        listed = await client.get("/api/poller/review-verdicts")

    rows = listed.json()["review_verdicts"]
    assert len(rows) == 1 and rows[0]["verdict"] == "changes-needed"


async def test_a_review_verdict_without_a_commit_is_refused(tmp_path):
    app = _history_app(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/review-verdicts", json={**_REVIEW, "sha": ""})
        # The poller's own hand-off route went with its cards (#3732).
        retired = await client.post("/api/poller/review-verdicts", json=_REVIEW)

    assert response.status_code == 422
    assert retired.status_code == 405
