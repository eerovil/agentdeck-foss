"""The topbar job-queue pill (#1301).

The host-wide heavy-command queue (#1287) was invisible unless you ran
``agentdeck job list`` in a terminal, so "my suite has not started yet" and "my
suite is running" looked identical from the deck. These cover the read model,
the change-only publish, the live push, and the mobile geometry.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from conftest import async_playwright
from httpx import ASGITransport, AsyncClient
from playwright.async_api import expect

from agentdeck import job_view
from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig
from agentdeck.job_view import (
    EMPTY,
    PANEL_ROWS,
    build_view,
    format_elapsed,
    read_job_queue,
)
from agentdeck.models import Capability, Session, SessionStatus

NOW = 1_800_000_000.0


def _stop_the_clock(monkeypatch) -> float:
    """Freeze the clock the view reads ages from, and return the frozen time.

    An age is read at render time, so on a loaded runner a second can pass
    between building a view and reading it, and an exact age assertion
    flakes by one (#2908).
    """
    now = time.time()
    monkeypatch.setattr(job_view, "time", SimpleNamespace(time=lambda: now))
    return now


def _wire(job_id, state, *, since=NOW, session_key=None, label="", command=None):
    """One row shaped like the runtime's ``GET /jobs`` payload."""
    return {
        "id": job_id,
        "state": state,
        "command": command if command is not None else ["pytest", "-q"],
        "cwd": "/tmp/wt",
        "log_path": "/tmp/wt/log",
        "submitted_at": since,
        "started_at": since if state == "running" else None,
        "session_key": session_key,
        "label": label,
    }


def _app(tmp_path):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        accounts=[
            AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))
        ],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.LIVE,
            title="Refactor the poller",
            capabilities=frozenset({Capability.TRANSCRIPT}),
            # Attention is activity since the chat was last opened (#2599).
            last_activity=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# --- the read model -------------------------------------------------------


def test_only_unfinished_jobs_reach_the_pill():
    """A finished row lives in the runtime's state for a day so `job status` can
    answer about it. The pill answers "what is the host busy with" instead."""
    view = build_view(
        [
            _wire("a", "running"),
            _wire("b", "queued"),
            _wire("c", "done"),
            _wire("d", "failed"),
            _wire("e", "cancelled"),
            _wire("f", "interrupted"),
        ]
    )

    assert [row.id for row in view.running] == ["a"]
    assert [row.id for row in view.queued] == ["b"]
    assert view.running_count == 1
    assert view.queued_count == 1
    assert view.active
    # The runtime's place in line wins over the submission time (#3116); a runtime from
    # before it sends none, and those rows keep submission order after the placed ones.
    later = {**_wire("g", "queued", since=NOW + 5), "place": 0}
    old = _wire("h", "queued", since=NOW - 5)
    assert [row.id for row in build_view([_wire("b", "queued"), later, old]).queued] == [
        "g", "h", "b"
    ]


def test_an_idle_queue_is_inactive_and_a_broken_row_is_skipped_not_fatal():
    """The runtime outlives web deploys, so its payload can be older or newer
    than this code; an unreadable row must not take the topbar down with it."""
    assert not build_view([]).active
    assert not build_view([{"state": "running"}]).active  # no id
    assert not build_view([{"id": "x", "state": "running"}]).active  # no timestamps
    assert not build_view(["nonsense", None]).active
    assert build_view([_wire("a", "running"), {"id": "b"}]).running_count == 1


def test_running_is_ordered_oldest_first_and_queued_in_start_order():
    view = build_view(
        [
            _wire("young", "running", since=NOW + 50),
            _wire("old", "running", since=NOW),
            _wire("later", "queued", since=NOW + 90),
            _wire("next", "queued", since=NOW + 10),
        ]
    )

    assert [row.id for row in view.running] == ["old", "young"]
    assert [row.id for row in view.queued] == ["next", "later"]


def test_a_row_shows_its_label_or_its_command_and_a_long_one_is_cut():
    labelled = build_view([_wire("a", "running", label="full suite")]).running[0]
    bare = build_view([_wire("b", "running", command=["pytest", "-q", "-x"])]).running[0]
    long = build_view([_wire("c", "running", label="x" * 400)]).running[0]

    assert labelled.display == "full suite"
    assert bare.display == "pytest -q -x"
    assert len(long.display) == 120
    assert long.display.endswith("…")


def test_the_panel_caps_its_rows_and_says_how_many_it_left_out():
    """`jobs.MAX_QUEUED` allows fifty; a phone-height wall of identical
    `pytest -q` lines answers nothing the counts did not already answer."""
    view = build_view(
        [_wire(f"r{i}", "running", since=NOW + i) for i in range(PANEL_ROWS + 2)]
        + [_wire(f"q{i}", "queued", since=NOW + i) for i in range(PANEL_ROWS + 3)]
    )

    assert len(view.panel_rows) == PANEL_ROWS * 2
    assert view.hidden_count == 5
    assert build_view([_wire("a", "running")]).hidden_count == 0


def test_elapsed_is_read_at_render_time_and_never_makes_two_polls_differ(monkeypatch):
    """Elapsed seconds are not a stored field: if they were, the fragment would
    be re-pushed to every open tab once a second for a queue nothing changed."""
    now = _stop_the_clock(monkeypatch)
    first = build_view([_wire("a", "running", since=now - 125)])
    second = build_view([_wire("a", "running", since=first.running[0].since)])

    assert first == second
    assert first.running[0].elapsed_s == 125
    assert first.running[0].elapsed_label == "2m 5s"


def test_elapsed_labels_match_what_the_browser_keeps_rendering():
    assert format_elapsed(0) == "0s"
    assert format_elapsed(-3) == "0s"
    assert format_elapsed(59) == "59s"
    assert format_elapsed(60) == "1m 0s"
    assert format_elapsed(3599) == "59m 59s"
    assert format_elapsed(3600) == "1h 0m"
    assert format_elapsed(3600 * 2 + 61) == "2h 1m"


def test_the_summary_names_both_states_for_the_pill_tooltip():
    assert build_view([_wire("a", "running")]).summary == "Job queue: 1 running"
    assert build_view([_wire("a", "running"), _wire("b", "queued")]).summary == (
        "Job queue: 1 running, 1 waiting for a slot"
    )
    # Slots free but memory short (#3614): the waiting job says what it waits for.
    short = build_view([_wire("b", "queued")], memory_waiting=1)
    assert short.summary == "Job queue: 0 running, 1 waiting for this host to free memory"
    # Only the local ones wait for memory; a pod job behind a full lane waits for a slot.
    mixed = build_view([_wire("b", "queued"), _wire("c", "queued")], memory_waiting=1)
    assert mixed.summary == (
        "Job queue: 0 running, 2 waiting, 1 of them for this host to free memory"
    )


def test_a_slot_held_outside_the_queue_becomes_a_row():
    """The "0 running, 4 waiting" case, which used to have no explanation."""
    view = build_view(
        [_wire("q", "queued")],
        [
            {
                "slot": 0,
                "kind": "process",
                "pid": 4242,
                "command": "pytest -q",
                "cwd": "/home/me/homr",
                "since": NOW,
            }
        ],
    )
    assert len(view.holders) == 1
    assert view.holders[0].display == "pytest -q"
    assert view.holders[0].detail == "/home/me/homr"
    assert view.summary == (
        "Job queue: 0 running, 1 waiting for a slot, 1 slot held outside the queue"
    )


def test_the_queues_own_jobs_are_not_repeated_as_slot_holders():
    """A running job is already a row; naming its slot again would double it."""
    view = build_view(
        [_wire("a", "running")],
        [{"slot": 0, "kind": "job", "job": "a"}, {"slot": 1, "kind": "free"}],
    )
    assert view.holders == ()
    assert view.summary == "Job queue: 1 running"


def test_a_lease_is_named_by_its_label_rather_than_by_the_runtime_process():
    view = build_view(
        [_wire("q", "queued")],
        [{"slot": 0, "kind": "lease", "label": "web test", "since": NOW}],
    )
    assert view.holders[0].kind == "lease"
    assert view.holders[0].display == "web test"
    assert view.holders[0].detail == ""


def test_an_unreadable_slot_row_is_skipped_rather_than_rendered_blank():
    view = build_view(
        [_wire("q", "queued")],
        [
            "nonsense",
            {"kind": "process"},  # no timestamp to age from
            {"kind": "invented-by-a-newer-runtime", "since": NOW},
            {"kind": "process", "pid": 7, "since": NOW},  # no command line
        ],
    )
    assert [holder.display for holder in view.holders] == ["pid 7"]


def test_a_runtime_from_before_the_slot_section_renders_as_it_used_to():
    assert build_view([_wire("a", "running")]).holders == ()


def test_slot_holders_survive_chat_resolution():
    """`resolved` rebuilds the view, and dropping the holders there would make
    the panel's explanation vanish the moment a job had a chat to name."""
    view = build_view(
        [_wire("a", "queued", session_key="claude_code:test:sid1")],
        [{"slot": 0, "kind": "process", "pid": 9, "command": "pytest", "since": NOW}],
    )
    named = view.resolved(lambda key: ("a title", "/chats/x"))
    assert len(named.holders) == 1


def test_chats_are_resolved_at_render_so_a_new_title_is_not_a_queue_change():
    view = build_view([_wire("a", "running", session_key="claude_code:test:sid1")])
    named = view.resolved(lambda key: (f"title for {key}", f"/chats/{key}"))

    assert view.running[0].chat_title is None
    assert named.running[0].chat_title == "title for claude_code:test:sid1"
    assert named.running[0].chat_path == "/chats/claude_code:test:sid1"
    # The stored view is unchanged, so the next poll still compares equal.
    assert view == build_view(
        [_wire("a", "running", session_key="claude_code:test:sid1")]
    )


def test_a_job_with_no_submitting_chat_resolves_to_nothing():
    view = build_view([_wire("a", "running")]).resolved(
        lambda key: pytest.fail("must not be asked about a job with no session")
    )
    assert view.running[0].session_key is None
    assert view.running[0].chat_title is None


async def test_an_unreachable_runtime_preserves_the_last_known_queue():

    async def refuse(request):
        raise httpx.ConnectError("no such socket")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(refuse), base_url="http://runtime"
    ) as client:
        assert await read_job_queue(client) is None


async def test_a_deck_with_the_queue_turned_off_shows_no_pill():
    """`GET /jobs` 404s when `jobs.enabled` is false on the running runtime."""

    async def not_found(request):
        return httpx.Response(404, json={"detail": "job queue disabled"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(not_found), base_url="http://runtime"
    ) as client:
        assert await read_job_queue(client) == EMPTY


async def test_a_live_runtime_payload_becomes_the_view():
    async def jobs(request):
        assert request.url.path == "/jobs"
        return httpx.Response(
            200,
            json={
                "jobs": [_wire("a", "running"), _wire("b", "queued")],
                "memory_waiting": 1,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(jobs), base_url="http://runtime"
    ) as client:
        view = await read_job_queue(client)

    assert (view.running_count, view.queued_count) == (1, 1)
    assert view.memory_waiting == 1


async def test_a_payload_that_is_not_a_job_list_is_ignored():
    async def junk(request):
        return httpx.Response(200, json={"jobs": "everything is fine"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(junk), base_url="http://runtime"
    ) as client:
        assert await read_job_queue(client) is None


# --- the change-only publish ----------------------------------------------


def test_the_queue_publishes_only_when_it_actually_changed(tmp_path):
    state = _app(tmp_path).state.app_state
    seen: list[str] = []

    with state.bus.subscribe("jobs") as sub:
        state.set_job_queue(build_view([_wire("a", "running")]))
        state.set_job_queue(build_view([_wire("a", "running")]))  # same poll again
        state.set_job_queue(build_view([_wire("a", "running"), _wire("b", "queued")]))
        while (item := sub.get_nowait()) is not None:
            seen.append(item[0])

    assert seen == ["jobs", "jobs"]


def test_a_queue_change_advances_the_session_projection(tmp_path):
    state = _app(tmp_path).state.app_state
    revision = state.sessions_revision

    state.set_job_queue(build_view([_wire("a", "queued")]))
    state.set_job_queue(build_view([_wire("a", "queued")]))

    assert state.sessions_revision == revision + 1


@pytest.mark.parametrize("job_state", ["queued", "running"])
async def test_unfinished_job_keeps_its_submitting_chat_working_in_deckhand(
    tmp_path, job_state
):
    app = _app(tmp_path)
    app.state.assistant.config.enabled = True
    state = app.state.app_state
    session = state.sessions["claude_code:test:sid1"]
    assert not session.thinking

    state.set_job_queue(
        build_view(
            [
                _wire(
                    "a",
                    job_state,
                    since=time.time() - 20,
                    session_key=session.key,
                )
            ]
        )
    )

    presentation = state.session_presentation()
    assert presentation.display(session).thinking
    assert presentation.working_count == 1
    assert not state.sessions[session.key].thinking  # derived state only

    # Deckhand's own working set holds the chat, so it carries no card (#2599).
    assert app.state.assistant._working_owner_keys() == {session.key}
    assert presentation.display(state.sessions[session.key]).thinking

    state.set_job_queue(build_view([_wire("a", "done", session_key=session.key)]))
    assert state.session_presentation().working_count == 0


async def test_a_queued_job_does_not_hide_that_the_chat_is_asking_you_something(
    tmp_path,
):
    # Issue #1878: a chat submitted two suites and then asked a question. The
    # queued jobs made the dashboard call it Working, and because the waiting
    # pill only shows when the chat is *not* thinking, the question it was
    # blocked on got no pill and no attention card either.
    app = _app(tmp_path)
    app.state.assistant.config.enabled = True
    state = app.state.app_state
    session = replace(
        state.sessions["claude_code:test:sid1"],
        question="Close PR 1773 or finish it?",
    )
    state.update_session(session)

    state.set_job_queue(
        build_view(
            [
                _wire(
                    "a",
                    "running",
                    since=time.time() - 20,
                    session_key=session.key,
                )
            ]
        )
    )

    presentation = state.session_presentation()
    assert presentation.display(session).thinking is False
    assert presentation.display(session).display_state == "idle"
    assert presentation.working_count == 0
    # The job itself has not gone anywhere — only the claim on the chat has.
    assert presentation.display(session).last_progress is not None
    statuses = app.state.assistant.deckhand_statuses([presentation.display(session)])
    # Current waiting state alone is not Deckhand attention; only a stop edge
    # can create that status.
    assert session.key not in statuses


def test_job_activity_follows_a_durable_chat_to_its_current_leg(tmp_path):
    app = _app(tmp_path)
    state = app.state.app_state
    source = state.sessions["claude_code:test:sid1"]
    target = Session(
        key="claude_code:test:sid2",
        account_key="claude_code:test",
        session_id="sid2",
        status=SessionStatus.LIVE,
        title="Current provider leg",
        capabilities=frozenset({Capability.TRANSCRIPT}),
    )
    state.update_session(target)
    state.budget_db.append_chat_leg(source.key, target.key)

    state.set_job_queue(
        build_view([_wire("a", "queued", session_key=source.key)])
    )

    presentation = state.session_presentation()
    assert [session.key for session in presentation.top_level] == [target.key]
    assert presentation.display(target).thinking
    assert not state.sessions[target.key].thinking


def test_the_state_resolves_the_submitting_chat_for_the_panel(tmp_path):
    state = _app(tmp_path).state.app_state
    state.set_job_queue(
        build_view([_wire("a", "running", session_key="claude_code:test:sid1")])
    )

    row = state.job_queue().running[0]
    assert row.chat_title == "Refactor the poller"
    # A durable chat id when the deck has one, the leg alias otherwise — either
    # way a path the panel can link to.
    assert row.chat_path.startswith(("/chats/", "/sessions/"))


def test_a_job_from_a_chat_the_deck_never_scanned_still_renders(tmp_path):
    """A job outlives the chat that submitted it, and can come from a shell the
    deck has no session for at all."""
    state = _app(tmp_path).state.app_state
    state.set_job_queue(build_view([_wire("a", "running", session_key="gone:1")]))

    row = state.job_queue().running[0]
    assert row.session_key == "gone:1"
    assert row.chat_path is None


# --- what the page renders ------------------------------------------------


async def test_an_idle_queue_leaves_the_topbar_untouched(tmp_path):
    app = _app(tmp_path)

    async with _client(app) as client:
        page = await client.get("/")

    # The swap slot is always there, so the pill can appear on an open tab.
    assert 'id="job-queue-slot" sse-swap="jobs"' in page.text
    assert 'class="job-queue' not in page.text


async def test_the_pill_shows_both_counts_and_the_panel_names_each_job(tmp_path):
    app = _app(tmp_path)
    app.state.app_state.set_job_queue(
        build_view(
            [
                _wire(
                    "a",
                    "running",
                    since=time.time() - 90,
                    session_key="claude_code:test:sid1",
                    label="full suite",
                ),
                _wire("b", "queued", since=time.time() - 20, command=["ruff", "check"]),
            ]
        )
    )

    async with _client(app) as client:
        page = await client.get("/")

    assert 'class="job-queue job-queue-waiting"' in page.text
    assert 'class="job-queue-count job-queue-count-running">1<' in page.text
    assert 'class="job-queue-count job-queue-count-queued">1<' in page.text
    assert "1 running, 1 waiting for a slot." in page.text
    # Each job is distinguishable, not just counted (the card's acceptance).
    assert "full suite" in page.text
    assert "ruff check" in page.text
    assert "Refactor the poller" in page.text
    # The label is computed at render time from wall clock, so a slow first
    # render (a cold pod importing the app) legitimately shows a second or two
    # more than the 90 the job was given.
    assert re.search(r"1m 3\ds", page.text)


async def test_the_panel_names_who_holds_the_slot_the_queue_is_waiting_for(
    tmp_path, monkeypatch
):
    """The whole point of #1391: waiting jobs and nothing running now say why."""
    app = _app(tmp_path)
    now = _stop_the_clock(monkeypatch)
    app.state.app_state.set_job_queue(
        build_view(
            [_wire("b", "queued", since=now - 20)],
            [
                {
                    "slot": 0,
                    "kind": "process",
                    "pid": 4242,
                    "command": "pytest -q",
                    "cwd": "/home/me/homr/.worktrees/issue-106",
                    "since": now - 90,
                }
            ],
        )
    )

    async with _client(app) as client:
        page = await client.get("/")

    assert "Slot held outside the queue" in page.text
    assert "pytest -q" in page.text
    assert "/home/me/homr/.worktrees/issue-106" in page.text
    # Ages tick from the same attribute the rest of the panel uses, so the
    # holder's clock does not freeze between swaps.
    assert 'class="job-queue-age" data-job-elapsed="90"' in page.text


async def test_the_panel_escapes_a_slot_holder_it_did_not_choose(tmp_path):
    """A holder's command line comes from another user's process, not from us."""
    app = _app(tmp_path)
    app.state.app_state.set_job_queue(
        build_view(
            [_wire("b", "queued")],
            [
                {
                    "slot": 0,
                    "kind": "process",
                    "pid": 1,
                    "command": "<script>alert(1)</script>",
                    "cwd": "<img src=x onerror=alert(2)>",
                    "since": time.time(),
                }
            ],
        )
    )

    async with _client(app) as client:
        page = await client.get("/")

    # No tag of theirs survives as markup; both read back as text.
    assert "<script>alert(1)</script>" not in page.text
    assert "<img src=x" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "&lt;img src=x onerror=alert(2)&gt;" in page.text


async def test_a_slot_the_queue_itself_holds_adds_no_line_to_the_panel(tmp_path):
    app = _app(tmp_path)
    app.state.app_state.set_job_queue(
        build_view([_wire("a", "running")], [{"slot": 0, "kind": "job", "job": "a"}])
    )

    async with _client(app) as client:
        page = await client.get("/")

    assert "Slot held outside the queue" not in page.text


async def test_a_queue_with_nothing_waiting_does_not_render_the_waiting_state(tmp_path):
    app = _app(tmp_path)
    app.state.app_state.set_job_queue(build_view([_wire("a", "running")]))

    async with _client(app) as client:
        page = await client.get("/")

    assert 'class="job-queue"' in page.text
    assert "job-queue-waiting" not in page.text
    assert "job-queue-count-queued" not in page.text


async def test_the_panel_escapes_a_command_it_did_not_choose(tmp_path):
    app = _app(tmp_path)
    app.state.app_state.set_job_queue(
        build_view([_wire("a", "running", label="<script>alert(1)</script>")])
    )

    async with _client(app) as client:
        page = await client.get("/")

    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


# --- no per-test progress (#2864) -------------------------------------------


async def test_a_running_suite_shows_no_per_test_progress(tmp_path):
    """The live progress bar (#2299) was removed: a running row is its state and its clock."""
    app = _app(tmp_path)
    app.state.app_state.set_job_queue(build_view([_wire("a", "running"), _wire("b", "queued")]))
    async with _client(app) as client:
        page = (await client.get("/")).text

    assert "job-queue-progress" not in page
    assert "--job-progress" not in page
    assert page.count('class="job-queue-item job-queue-item-') == 2


async def test_the_poll_asks_the_runtime_for_running_and_queued_rows_only():
    """The pill used to download the whole retained queue every two seconds (#2864)."""
    from agentdeck.job_view import read_job_queue

    asked = []

    def handler(request):
        asked.append(dict(request.url.params))
        # A runtime from before the filter answers with finished rows too.
        return httpx.Response(
            200, json={"jobs": [_wire("a", "running"), {**_wire("c", "running"), "state": "done"}]}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runtime"
    ) as client:
        view = await read_job_queue(client)

    assert asked == [{"active": "1"}]
    assert [row.id for row in view.running] == ["a"]


# --- the live push --------------------------------------------------------


async def _pushed_after(app, mutate, *, primes):
    from agentdeck.web.routes_sse import _stream

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            self.query_params = {}

        async def is_disconnected(self):
            return False

    gen = _stream(FakeRequest(app))
    try:
        for _ in range(primes):
            await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        mutate()
        pushed = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        # The front's primes, and the top bar's Needs you list (#3239).
        while pushed.startswith(("event: works", "event: work-other", "event: needs_you")):
            pushed = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        return pushed
    finally:
        await gen.aclose()


async def test_a_job_entering_the_queue_appears_on_an_open_tab(tmp_path):
    """The card's "updates without a manual page refresh": a submitted job has
    to raise the pill on a dashboard someone is already looking at."""
    app = _app(tmp_path)

    pushed = await _pushed_after(
        app,
        lambda: app.state.app_state.set_job_queue(
            build_view([_wire("a", "running"), _wire("b", "queued", label="ruff")])
        ),
        primes=1,  # usage: no old Deckhand panel (#2256) and no chat list (#2603)
    )

    assert "event: jobs" in pushed
    assert "job-queue-count-queued" in pushed
    assert "ruff" in pushed


async def test_the_queue_draining_takes_the_pill_away_again(tmp_path):
    """"Finished jobs do not remain indefinitely in the active view" — the last
    job finishing pushes an empty fragment, not a frozen one."""
    app = _app(tmp_path)
    app.state.app_state.set_job_queue(build_view([_wire("a", "running")]))

    pushed = await _pushed_after(
        app,
        lambda: app.state.app_state.set_job_queue(build_view([_wire("a", "done")])),
        primes=1,
    )

    assert "event: jobs" in pushed
    assert "job-queue-count" not in pushed


async def test_a_queued_job_replaces_finished_attention_on_an_open_deck(tmp_path):
    """The queue's session invalidation must reach Deckhand, not only the pill.

    Start with a real finished card, then keep a deck stream open while the job
    arrives. The Assistant service consumes the session event and drops the stale
    handoff without a reload; no stream carries the chat list any more (#2603).
    """
    from agentdeck.web.routes_sse import _stream

    app = _app(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    state = app.state.app_state
    session = state.sessions["claude_code:test:sid1"]
    state.set_attention_report(session.key, "finished")
    state.sessions_scanned(session.account_key)
    # A real finished card means a real stop: the chat has to be observed working
    # before it can cross the edge that raises one (#1450).
    state.update_session(replace(session, thinking=True))
    await assistant.refresh()
    state.update_session(replace(session, thinking=False))
    await assistant.refresh()
    assert [insight.session_key for insight in assistant.view.insights] == [session.key]

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            self.query_params = {}

        async def is_disconnected(self):
            return False

    await assistant.start()
    stream = _stream(FakeRequest(app))
    try:
        # The usage prime; no old Deckhand panel (#2256) and no chat list (#2603).
        await asyncio.wait_for(stream.__anext__(), timeout=5.0)
        await asyncio.sleep(0)  # let the Assistant session watcher subscribe

        state.set_job_queue(
            build_view([_wire("a", "queued", session_key=session.key)])
        )

        event = await asyncio.wait_for(stream.__anext__(), timeout=5.0)
        while event.startswith(("event: works", "event: work-other", "event: needs_you")):
            event = await asyncio.wait_for(stream.__anext__(), timeout=5.0)
        assert "event: jobs" in event
        assert "event: assistant\n" not in event
        for _ in range(50):
            if assistant.view.insights == ():
                break
            await asyncio.sleep(0.02)
        assert assistant.view.insights == ()
    finally:
        await stream.aclose()
        await assistant.stop()


async def test_child_heavy_jobs_make_one_parent_deckhand_working_row(tmp_path):
    app = _app(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    state = app.state.app_state
    parent = state.sessions["claude_code:test:sid1"]
    child = replace(
        parent,
        key="claude_code:test:child",
        session_id="child",
        chat_id="child-chat",
        parent_session_key=parent.key,
        title="Child worker",
    )
    sibling = replace(
        child,
        key="claude_code:test:sibling",
        session_id="sibling",
        chat_id="sibling-chat",
        title="Sibling worker",
    )
    state.update_session(child)
    state.update_session(sibling)
    state.set_job_queue(
        build_view(
            [
                _wire("a", "queued", session_key=child.key),
                _wire("b", "queued", session_key=sibling.key),
            ]
        )
    )

    await assistant.refresh()

    # The parent is working, its delegated children are not owners at all: Deckhand's
    # own working set (#2599).
    assert assistant._working_owner_keys() == {parent.key}


async def test_the_session_page_stream_carries_the_pill_too(tmp_path):
    """base.html's topbar is on every page, and a session page runs its own
    stream — so the pill must ride that one as well or it freezes there."""
    from agentdeck.web.routes_sse import _session_stream

    app = _app(tmp_path)

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            # The deck's chat list open, as this test's deck always had it (#1458).
            self.query_params = {"chats": "1"}

        async def is_disconnected(self):
            return False

    gen = _session_stream(FakeRequest(app), "claude_code:test:sid1")
    pushed = ""
    try:
        for _ in range(6):  # drain the primed fragments
            await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        app.state.app_state.set_job_queue(
            build_view([_wire("a", "running", label="full suite")])
        )
        for _ in range(12):
            frame = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            if "event: jobs" in frame:
                pushed = frame
                break
    finally:
        await gen.aclose()

    assert "event: jobs" in pushed
    assert "full suite" in pushed


# --- geometry -------------------------------------------------------------


@asynccontextmanager
async def _live_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await task
        sock.close()


def _busy(app):
    app.state.app_state.set_job_queue(
        build_view(
            [
                _wire(
                    "a",
                    "running",
                    since=time.time() - 315,
                    session_key="claude_code:test:sid1",
                    label="pytest -q (issue-1301)",
                ),
                _wire(
                    "b",
                    "running",
                    since=time.time() - 42,
                    label="pytest -q (issue-1287)",
                ),
                _wire("c", "queued", since=time.time() - 30, label="ruff check src"),
                _wire("d", "queued", since=time.time() - 8, label="pytest -q (master)"),
            ]
        )
    )


@pytest.mark.browser
@pytest.mark.parametrize("width", [320, 1200])
async def test_opening_the_panel_does_not_grow_the_header(tmp_path, width):
    """AGENTS.md's mobile-height invariant: a new topbar control may not make
    the bar taller, and its panel must stay on screen at 320px."""
    app = _app(tmp_path)
    _busy(app)

    async with _live_server(app) as base_url:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": width, "height": 800})
            await page.goto(base_url, wait_until="load")
            collapsed = await page.locator(".topbar").bounding_box()
            await page.locator(".job-queue summary").click()
            panel = page.locator(".job-queue-panel")
            await expect(panel).to_be_visible()
            metrics = await panel.evaluate(
                """panel => ({
                  left: panel.getBoundingClientRect().left,
                  right: panel.getBoundingClientRect().right,
                  viewport: document.documentElement.clientWidth,
                  header: document.querySelector('.topbar').getBoundingClientRect().height,
                  overflow: document.documentElement.scrollWidth
                    - document.documentElement.clientWidth,
                  rows: document.querySelectorAll('.job-queue-item').length,
                })"""
            )
            shots = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
            if shots:
                await page.screenshot(
                    path=str(Path(shots) / f"issue-1301-job-queue-{width}.png")
                )
            await browser.close()

    assert metrics["left"] >= 0
    assert metrics["right"] <= metrics["viewport"]
    assert metrics["header"] == collapsed["height"]
    assert metrics["overflow"] <= 1
    assert metrics["rows"] == 4


@pytest.mark.browser
@pytest.mark.parametrize("path", ["/", "/sessions/claude_code:test:sid1"])
@pytest.mark.parametrize("width", [320, 430, 1200])
async def test_a_busy_queue_does_not_push_the_topbar_off_a_phone(tmp_path, path, width):
    """#1767. Nothing in this row can shrink below its own content, and the pill
    was the one control in it with no narrow-screen collapse of its own — so a
    running job pushed the row past the right edge: 19px over on the chat list
    at 320px, 113px over on a chat, which carries a back arrow and a diff
    toggle as well. At 430px, a common phone, the row collapsed nothing at all
    and was 33px over before a job even started, which is the width the report
    came from.

    The notification bell is revealed by hand because a phone has it and
    headless Chromium leaves it hidden — which is why the check above, measuring
    the same row, stayed green while a phone overflowed.
    """
    app = _app(tmp_path)
    _busy(app)
    phone = width <= 480

    async with _live_server(app) as base_url:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": width, "height": 800})
            await page.goto(base_url + path, wait_until="load")
            # Reveal and measure in one evaluation: push.js re-hides the bell
            # whenever /push/public-key settles, and this app has push disabled,
            # so a reveal in its own round trip can be undone before the row is
            # measured — which would quietly measure the row without the bell,
            # the exact blind spot this check exists to close.
            measured = await page.evaluate(
                """() => {
                  const bell = document.querySelector('.notif-bell');
                  if (bell) bell.hidden = false;
                  const row = document.querySelector('.topbar-row');
                  return {
                    overflow: row.scrollWidth - row.clientWidth,
                    bell: bell ? bell.getBoundingClientRect().width : 0,
                  };
                }"""
            )
            # A phone shows the bell; if it took no width the measurement below
            # is not the row a phone renders.
            assert measured["bell"] > 0, measured
            overflow = measured["overflow"]
            await expect(page.locator(".job-queue-count-running")).to_be_visible()
            icon = page.locator(".job-queue > summary > svg")
            if phone:
                await expect(icon).to_be_hidden()
            else:
                await expect(icon).to_be_visible()
            # The wordmark shrinks to a house mark rather than going: on a chat
            # layered over the list it is the way home, because that page hides
            # its back arrow.
            brand = page.locator("a.brand[href='/']")
            await expect(brand).to_be_visible()
            if phone:
                await expect(page.locator(".brand-mark")).to_be_visible()
                await expect(page.locator(".brand-word")).to_be_hidden()
            else:
                await expect(page.locator(".brand-word")).to_be_visible()
                await expect(page.locator(".brand-mark")).to_be_hidden()
            shots = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
            if shots:
                name = "list" if path == "/" else "chat"
                await page.screenshot(
                    path=str(Path(shots) / f"issue-1767-topbar-{name}-{width}.png")
                )
            await browser.close()

    assert overflow <= 0


@pytest.mark.browser
async def test_a_job_finishing_does_not_snap_the_open_panel_shut(tmp_path):
    """This fragment is replaced every time a job starts or finishes — which is
    exactly when someone has the panel open watching it (AGENTS.md: a swap must
    preserve deliberate control state). The elapsed clocks keep ticking too."""
    app = _app(tmp_path)
    _busy(app)

    async with _live_server(app) as base_url:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 390, "height": 800})
            await page.goto(base_url, wait_until="load")
            await page.locator(".job-queue summary").click()
            await expect(page.locator(".job-queue-panel")).to_be_visible()
            before = await page.locator(".job-queue-age").first.inner_text()

            app.state.app_state.set_job_queue(
                build_view(
                    [
                        _wire(
                            "a",
                            "running",
                            since=time.time() - 315,
                            session_key="claude_code:test:sid1",
                            label="pytest -q (issue-1301)",
                        ),
                        _wire("c", "queued", since=time.time() - 30, label="ruff check src"),
                    ]
                )
            )
            await page.wait_for_function(
                "() => document.querySelectorAll('.job-queue-item').length === 2",
                timeout=10_000,
            )
            still_open = await page.locator(".job-queue-panel").is_visible()
            await page.wait_for_function(
                f"() => document.querySelector('.job-queue-age')"
                f".textContent !== {before!r}",
                timeout=10_000,
            )
            after = await page.locator(".job-queue-age").first.inner_text()
            await browser.close()

    assert still_open
    assert after != before


@pytest.mark.browser
async def test_an_unrelated_swap_landing_on_the_click_leaves_the_panel_open(tmp_path):
    """#1456. `<details>` fires `toggle` a task after the click that opened it,
    and the stream pushes the usage and assistant fragments the moment it
    connects — so a swap with nothing to do with the queue could land in that
    window, re-apply the open state from before the click and shut the panel
    again. It stayed shut, which is why retrying the assertion would not have
    helped.

    Fired from the click itself so the collision is deterministic rather than a
    matter of pod load, as a microtask and as a task so it lands on either side
    of the browser's own toggle task. The event goes to `#limit-bars`, which is
    where the usage push really lands — dispatching it at the body instead
    would be a swap that contains the pill's own slot, which is a different
    case and one that *should* re-apply the stored state."""
    app = _app(tmp_path)
    _busy(app)

    async with _live_server(app) as base_url:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 390, "height": 800})
            await page.goto(base_url, wait_until="load")
            await page.evaluate(
                """() => {
                  const elsewhere = document.getElementById('limit-bars');
                  const fire = () => elsewhere.dispatchEvent(
                    new CustomEvent('htmx:afterSwap', {bubbles: true}));
                  document.querySelector('.job-queue summary')
                    .addEventListener('click', () => {
                      queueMicrotask(fire);
                      setTimeout(fire, 0);
                    });
                }"""
            )
            await page.locator(".job-queue summary").click()
            immediately = await page.locator(".job-queue-panel").is_visible()
            # And it is still open once every queued swap has run.
            await page.wait_for_timeout(200)
            settled = await page.locator(".job-queue-panel").is_visible()
            await browser.close()

    assert immediately
    assert settled


@pytest.mark.browser
async def test_the_pill_does_not_reserve_topbar_width_while_the_queue_is_idle(tmp_path):
    app = _app(tmp_path)

    async with _live_server(app) as base_url:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 320, "height": 800})
            await page.goto(base_url, wait_until="load")
            idle = await page.evaluate(
                """() => ({
                  pill: document.querySelectorAll('.job-queue').length,
                  slotWidth: document.querySelector('#job-queue-slot')
                    ? document.querySelector('#job-queue-slot').getBoundingClientRect().width
                    : -1,
                  header: document.querySelector('.topbar').getBoundingClientRect().height,
                })"""
            )
            app.state.app_state.set_job_queue(build_view([_wire("a", "running")]))
            await page.wait_for_selector(".job-queue", timeout=10_000)
            busy_header = await page.evaluate(
                "() => document.querySelector('.topbar').getBoundingClientRect().height"
            )
            await browser.close()

    assert idle["pill"] == 0
    # `display: contents` — the empty slot is not a flex item taking up a gap.
    assert idle["slotWidth"] == 0
    assert busy_header == idle["header"]
