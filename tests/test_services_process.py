"""The Work router, the merge release and the backstop out of the web process (#4016)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from agentdeck import services_link, session_copy, work_services
from agentdeck.db import Db
from agentdeck.models import IssueRef, PullRequestRef, Session, SessionStatus
from agentdeck.state import AppState
from agentdeck.webhook_tailer import WebhookTailer
from agentdeck.work_router import (
    AgainRefused,
    owner_of_issue,
    owner_of_pull,
    pulls_by_work,
    work_busy,
    work_pulls,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
SEVEN = IssueRef("eerovil/agentdeck", 7)
EIGHT = IssueRef("eerovil/agentdeck", 8)


# --- the web starts none of them (#3999's first guard) ----------------------------------


@pytest.mark.parametrize("in_web", [False, True])
def test_the_web_starts_the_work_services_only_with_the_switch_back(tmp_path, monkeypatch, in_web):
    """Switched off, the web builds and starts none of the three: it keeps stand-ins that send
    every act to the services socket. With ``[services] in_web`` it runs them itself (#4017)."""
    from fastapi.testclient import TestClient
    from test_web import _app_with_state

    from agentdeck.work_backstop import MissedStartBackstop
    from agentdeck.work_release import WorkReleaseService
    from agentdeck.work_router import WorkRouterService

    started = []
    for cls in (WorkRouterService, WorkReleaseService, MissedStartBackstop):
        monkeypatch.setattr(cls, "start", lambda self, *a, **k: started.append(type(self)))
    monkeypatch.setattr(services_link, "IN_PROCESS", False)
    built = []
    real_build = work_services.build
    monkeypatch.setattr(
        work_services, "build", lambda *a, **k: built.append(1) or real_build(*a, **k)
    )
    app = _app_with_state(tmp_path, services_in_web=in_web)
    with TestClient(app):
        pass

    assert isinstance(app.state.work_router, services_link.RouterLink)
    assert isinstance(app.state.work_release, services_link.ReleaseLink)
    if in_web:
        assert built
        assert set(started) == {WorkRouterService, WorkReleaseService, MissedStartBackstop}
        assert app.state.session_copy is None
    else:
        assert built == [] and started == [] and app.state.work_services is None
        assert isinstance(app.state.session_copy, session_copy.SessionCopyFeed)


# --- the session copy (#4001) ---------------------------------------------------------------


def _session(key: str, *, minutes: int, **fields) -> Session:
    account, _, session_id = key.rpartition(":")
    return Session(
        key=key,
        account_key=account,
        session_id=session_id,
        status=SessionStatus.IDLE,
        last_activity=NOW - timedelta(minutes=minutes),
        **fields,
    )


@pytest.fixture
def web(tmp_path):
    db = Db(tmp_path / "control.db")
    state = AppState(db=db, budget_db=db)
    keys = ("claude_code:main:a", "claude_code:main:b", "codex:main:c")
    db.ensure_session_chats(keys)
    state.sessions = {
        "claude_code:main:a": _session("claude_code:main:a", minutes=5, cwd=tmp_path),
        "claude_code:main:b": _session(
            "claude_code:main:b",
            minutes=1,
            thinking=True,
            activity="Working",
            issue_url="https://github.com/eerovil/agentdeck/issues/8",
        ),
        "codex:main:c": _session("codex:main:c", minutes=9, stalled=True),
    }
    state.set_issues("claude_code:main:a", (SEVEN,))
    state.set_issues("codex:main:c", (SEVEN, EIGHT))
    state.set_pull_requests("claude_code:main:a", (PullRequestRef("eerovil/agentdeck", 9),))
    state.set_pull_requests("claude_code:main:b", (PullRequestRef("EeroVil/AgentDeck", 10),))
    state.set_pull_requests(
        "codex:main:c",
        (PullRequestRef("eerovil/agentdeck", 11, "github_issue:eerovil/agentdeck#7"),),
    )
    try:
        yield state
    finally:
        db.close()


def _copied(web) -> AppState:
    feed = session_copy.SessionCopyFeed(web)
    copy = AppState(db=web.db, budget_db=web.db)
    changed, registrations = session_copy.apply(copy, feed.answer(None, None))
    assert changed == frozenset(web.sessions) and registrations
    return copy


def test_the_copy_answers_the_moved_code_as_the_live_chats_do(web):
    copy = _copied(web)

    assert pulls_by_work(copy) == pulls_by_work(web)
    assert pulls_by_work(web), "the fixture places pull requests"
    for number in (7, 8):
        work_id = f"github_issue:eerovil/agentdeck#{number}"
        assert work_pulls(copy, work_id) == work_pulls(web, work_id)
        assert work_busy(copy, work_id) == work_busy(web, work_id)
        assert owner_of_issue(copy, "eerovil/agentdeck", number) == owner_of_issue(
            web, "eerovil/agentdeck", number
        )
    for number in (9, 10, 11, 12):
        assert owner_of_pull(copy, "eerovil/agentdeck", number) == owner_of_pull(
            web, "eerovil/agentdeck", number
        )
    assert work_busy(web, "github_issue:eerovil/agentdeck#8"), "a thinking member holds it"


def test_a_field_the_copy_does_not_carry_is_an_error_not_a_default(web):
    copy = _copied(web).sessions["claude_code:main:a"]
    assert copy.cwd == web.sessions["claude_code:main:a"].cwd
    with pytest.raises(session_copy.CopyFieldError):
        getattr(copy, "title", None)  # a Session field: never quietly None
    assert getattr(copy, "provider_id", "") == "", "not a Session field: as on the real one"


def test_the_feed_sends_only_what_moved_and_starts_over_on_another_epoch(web):
    feed = session_copy.SessionCopyFeed(web)
    first = feed.answer(None, None)
    assert first["reset"] and len(first["sessions"]) == 3
    since = first["revision"]
    assert not feed.compare(), "nothing moved"

    web.sessions["claude_code:main:a"].thinking = True
    del web.sessions["codex:main:c"]
    assert feed.compare()
    moved = feed.answer(since, feed.epoch)
    assert [entry["key"] for entry in moved["sessions"]] == ["claude_code:main:a"]
    assert moved["removed"] == ["codex:main:c"] and not moved["registrations"]

    web.set_pull_requests("claude_code:main:a", (PullRequestRef("eerovil/agentdeck", 12),))
    assert feed.compare()
    assert feed.answer(moved["revision"], feed.epoch)["registrations"]
    assert feed.answer(moved["revision"], "another web worker")["reset"]


async def test_a_held_read_answers_once_a_chat_moves(web):
    feed = session_copy.SessionCopyFeed(web)
    first = feed.answer(None, None)
    held = asyncio.create_task(feed.wait(first["revision"], feed.epoch, 5.0))
    await asyncio.sleep(0)
    assert not held.done()
    web.sessions["codex:main:c"].stalled = False
    feed.compare()
    answer = await asyncio.wait_for(held, 1.0)
    assert [entry["key"] for entry in answer["sessions"]] == ["codex:main:c"]


# --- an act crosses the socket (#4003) -------------------------------------------------------


class _Services:
    def __init__(self) -> None:
        self.calls = []

    async def call(self, target, method, args, kwargs):
        self.calls.append((target, method, args, kwargs))
        if method == "ask_again":
            raise AgainRefused("a review of abc is already asked for")
        return {"outcome": "asked"}


async def test_an_act_reaches_the_services_and_its_refusal_comes_back_by_name(tmp_path):
    from agentdeck.services_process import create_services_app

    app = create_services_app(work_services_config(tmp_path), watchdog=False)
    app.state.services = _Services()
    transport = services_link.SocketTransport(
        lambda: tmp_path / "unused.sock", transport=httpx.ASGITransport(app=app)
    )
    router = services_link.RouterLink(work_services_config(tmp_path), transport, _NoState())

    assert await router.pre_review_done("eerovil/agentdeck", 9) == {"outcome": "asked"}
    with pytest.raises(AgainRefused, match="already asked"):
        await router.ask_again("eerovil/agentdeck", 9, "the owner kept it")
    assert app.state.services.calls[1] == (
        "router",
        "ask_again",
        ["eerovil/agentdeck", 9, "the owner kept it"],
        {},
    )


async def test_an_act_with_the_service_down_says_it_is_restarting(tmp_path, monkeypatch):
    monkeypatch.setattr(services_link, "CONNECT_WAIT_S", 0.0)
    release = services_link.ReleaseLink(
        work_services_config(tmp_path),
        services_link.SocketTransport(lambda: tmp_path / "gone.sock"),
        _NoState(),
    )
    with pytest.raises(services_link.ServicesUnavailable, match="restarting"):
        await release.request_merge("github_issue:eerovil/agentdeck#7")


async def test_only_the_listed_calls_run_and_a_router_call_reads_the_registrations_first():
    from types import SimpleNamespace

    seen = []
    router = SimpleNamespace(note_registered=lambda *ref: seen.append(("registered", *ref)))
    services = work_services.WorkServices(
        router=router, release=None, backstop=None, hooks=None, state=None
    )
    services.before_router_call = lambda: seen.append("reloaded")
    with pytest.raises(LookupError):
        await services.call("release", "merge", [], {})
    with pytest.raises(LookupError):
        await services.call("router", "_start", [], {})
    # A pull request registered a moment ago is known to the call it caused (#4016).
    await services.call("router", "note_registered", ["eerovil/agentdeck", 9], {})
    assert seen == ["reloaded", ("registered", "eerovil/agentdeck", 9)]


class _NoState:
    def review_events(self, pull):
        return []


def work_services_config(tmp_path: Path):
    from agentdeck.config import AppConfig, HistoryConfig, PollerConfig

    return AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        poller=PollerConfig(root=str(tmp_path / "poller")),
    )


# --- one reader per consumer name (#4002) ------------------------------------------------------


async def test_a_second_reader_of_a_consumer_waits_for_the_first(tmp_path, monkeypatch):
    monkeypatch.setattr("agentdeck.webhook_tailer.LOCK_POLL_S", 0.01)
    first = WebhookTailer(tmp_path / "streams", consumer="work-router")
    second = WebhookTailer(tmp_path / "streams", consumer="work-router")
    stop = asyncio.Event()
    async with first.held() as holding:
        assert holding
        waiting = asyncio.create_task(_hold(second, stop))
        await asyncio.sleep(0.05)
        assert not waiting.done(), "the first still reads"
    assert await asyncio.wait_for(waiting, 1.0) is True
    stop.set()
    async with second.held(stop) as holding:
        assert holding, "let go again once the block ended"


async def _hold(tailer, stop) -> bool:
    async with tailer.held(stop) as holding:
        return holding


# --- a deck-wide walk once per event (#3999's second guard) --------------------------------


def _hooks(tmp_path) -> work_services.Hooks:
    from agentdeck.wake_on import WakeOnRegistry

    async def nothing(*_args, **_kwargs):
        return None

    return work_services.Hooks(
        start_work=nothing,
        review_chat=nothing,
        reviewer=dict,
        interrupt=nothing,
        load_work=nothing,
        refresh_overview=nothing,
        schedule=lambda message: message,
        exists=lambda message_id: True,
        board_row=lambda number: None,
        runtime_http=lambda: None,
        scanned=nothing,
        wake_registry=WakeOnRegistry(tmp_path / "wake-on.json"),
    )


class _Issues:
    def cached(self):
        return None

    def cached_issues(self, refs):
        return {}

    def cached_pulls(self, refs):
        return {}


async def test_one_commit_status_walks_the_deck_once_however_many_works(web, tmp_path):
    """The release's walks over every registered pull request froze the web for up to 88 s
    (#3993, #4016): one commit status, and the start's look at every reviewed Work, each walk
    the registrations once, through the real router and release over the real chats."""
    from test_work_router import SHA, deliver

    from agentdeck import work_counter
    from agentdeck.config import GitHubWebhookConfig

    config = work_services_config(tmp_path)
    config.github_webhook = GitHubWebhookConfig(state_dir=str(tmp_path / "hooks" / "streams"))
    services = work_services.build(
        config, state=web, db=web.db, issues=_Issues(), hooks=_hooks(tmp_path)
    )
    services.release.ports.switched = lambda repository: True
    services.router._switched = lambda repository: True
    for number in (7, 8, 12, 13):
        services.release.store.put(f"github_issue:eerovil/agentdeck#{number}", {"seen": 1})
    deliver(config.github_webhook.path, event="status", delivery="d-1", sha=SHA, state="success")
    batch = WebhookTailer(config.github_webhook.path, consumer="count").read()

    with work_counter.counting() as spent:
        await services.router.apply(batch)
    assert spent[work_counter.PULL_WALK] == 1

    with work_counter.counting() as spent:
        await services.release.look_at_reviewed()
    assert spent[work_counter.PULL_WALK] == 1


# --- the services process reads the web's copy through the internal route (#4013) -------


def test_the_services_call_their_own_decks_web_whatever_agentdeck_url_says(monkeypatch):
    """The user's service manager sets ``AGENTDECK_URL`` to the live deck for every unit: the
    staging twin once called the live web with it (#4016)."""
    from agentdeck.config import AppConfig, ServerConfig
    from agentdeck.services_process import web_base_url

    monkeypatch.setenv("AGENTDECK_URL", "http://127.0.0.1:8756")
    assert web_base_url(AppConfig(server=ServerConfig(port=8757))) == "http://127.0.0.1:8757"
    assert web_base_url(AppConfig(server=ServerConfig(bind="::1", port=1))) == "http://[::1]:1"


async def test_the_services_follow_the_webs_chats_with_the_caller_key_only(tmp_path, monkeypatch):
    from test_web import _app_with_state

    from agentdeck.services_process import (
        SessionFollower,
        WebCalls,
        caller_key_path,
        write_caller_key,
    )

    monkeypatch.setattr(services_link, "IN_PROCESS", False)
    app = _app_with_state(tmp_path)
    web = app.state.app_state
    web.sessions = {"claude_code:test:a": _session("claude_code:test:a", minutes=2, thinking=True)}
    key = write_caller_key(caller_key_path(app.state.config))
    transport = httpx.ASGITransport(app=app)

    stranger = WebCalls("http://deck", "not the key", transport=transport)
    with pytest.raises(httpx.HTTPStatusError):
        await stranger.get("sessions", {}, timeout=5.0)
    calls = WebCalls("http://deck", key, transport=transport)
    copy = AppState(db=web.db, budget_db=web.db)
    follower = SessionFollower(calls, copy, web.db, on_registered=lambda *ref: None)
    follower.apply(await calls.get("sessions", {}, timeout=5.0))

    assert list(copy.sessions) == ["claude_code:test:a"]
    assert copy.sessions["claude_code:test:a"].thinking is True
    assert follower.copied.is_set(), "a web whose collector never started has scanned"
    # A let-go Work's chats the web just loaded are waited for before the news is routed.
    assert await follower.holds(("claude_code:test:a",), 0.01)
    assert not await follower.holds(("claude_code:test:gone",), 0.01)
    await stranger.aclose()
    await calls.aclose()


def test_a_services_change_or_reconnect_is_republished_on_the_webs_bus():
    """The web's follower turns the services' kinds into its own topics; a reconnect names
    every kind, which is the catch-up after the services process restarted (#4003)."""
    from types import SimpleNamespace

    from agentdeck.events import EventBus
    from agentdeck.state import REVIEWS_TOPIC
    from agentdeck.web.work_view import WORK_INPUTS_TOPIC

    bus = EventBus()
    woken, reread = [], []
    follower = services_link.ServicesFollower(
        lambda: Path("/nonexistent"),
        state=SimpleNamespace(bus=bus),
        scheduled_messages=SimpleNamespace(note_stored=lambda: woken.append(1)),
        wake_on=SimpleNamespace(registry=SimpleNamespace(all=lambda: reread.append(1))),
    )
    follower.moved(frozenset({services_link.SCHEDULED}))
    assert woken == [1] and reread == [] and bus.revision(WORK_INPUTS_TOPIC) == (0,)
    follower.moved(services_link.KINDS)
    assert woken == [1, 1] and reread == [1]
    assert bus.revision(WORK_INPUTS_TOPIC, REVIEWS_TOPIC) == (1, 1)
