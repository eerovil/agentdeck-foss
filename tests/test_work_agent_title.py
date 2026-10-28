"""The name a worker gives its Work when it reports (#2474): stored on the deck, never on GitHub.

A Work's title was its issue's, which is whatever the requester typed first — one was a pasted
chat link, and nothing could ever replace it but a person's own ✎ name.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck.work_overview import work_href, work_title

WORK_ID = "github_issue:eerovil/agentdeck#2147"
KEY = "claude_code:test:sid1"
URL = f"/api/sessions/{KEY}/work-title"


# --- the store -----------------------------------------------------------------------------------


def test_a_worker_name_survives_a_reopen_and_never_touches_the_operators(tmp_path):
    from agentdeck.db import Db

    db = Db(tmp_path / "control.sqlite3")
    assert db.work_agent_titles() == {}
    assert db.set_work_title(WORK_ID, "Mine")
    assert db.set_work_agent_title(WORK_ID, "Worker's name", KEY)

    reopened = Db(tmp_path / "control.sqlite3")
    assert reopened.work_agent_titles() == {WORK_ID: "Worker's name"}
    assert reopened.work_titles() == {WORK_ID: "Mine"}
    session = reopened._conn.execute(
        "SELECT agent_title_session_key FROM work_metadata WHERE work_id = ?", (WORK_ID,)
    ).fetchone()[0]
    assert session == KEY

    assert reopened.set_work_agent_title(WORK_ID, "", KEY)
    assert reopened.work_agent_titles() == {}
    assert reopened.work_titles() == {WORK_ID: "Mine"}


def test_the_operator_beats_the_worker_who_beats_the_issue_and_the_state_stays():
    from test_work_ui import WORK

    record = {"title": "https://deck/chats/abc", "state": "OPEN", "is_pr": False}
    assert work_title(WORK, record=record) == ("https://deck/chats/abc", "open")
    assert work_title(WORK, record=record, agent="Trip video") == ("Trip video", "open")
    assert work_title(WORK, record=record, custom="Mine", agent="Trip video") == ("Mine", "open")
    assert work_title(WORK, record=None, agent="Trip video") == ("Trip video", "")
    # The Work's own chat's name sits between the worker's and GitHub's (#3597).
    assert work_title(WORK, record=record, chat="Spec: cut it") == ("Cut it", "open")
    assert work_title(WORK, record=record, chat="zoined: spec") == ("zoined: spec", "open")
    assert work_title(WORK, record=record, chat="Spec:") == ("Spec:", "open")
    assert work_title(WORK, record=record, agent="Trip video", chat="Cut") == ("Trip video", "open")


def test_only_a_works_own_agent_chat_names_it(tmp_path):
    from agentdeck.db import Db
    from agentdeck.work_actions import NEW_CHAT
    from agentdeck.work_start import start_action_id

    db = Db(tmp_path / "control.sqlite3")
    usual, early = WORK_ID, "github_issue:eerovil/agentdeck#2148"

    def name(key, title, source="agent"):
        db.record_generated_title(key, title, "", source=source)

    # A helper chat that joined before the start is not the Work's agent.
    db.bind_session_work("k:before", early, "side_chat")
    name("k:before", "helper")
    # The usual start: the chat runs under the Work's start token.
    db.reserve_work_action(start_action_id(usual), usual, NEW_CHAT)
    db.record_session_token(start_action_id(usual), "k:own")
    db.bind_session_work("k:own", usual, "side_chat")
    name("k:own", "usual agent")
    # A start that took a chat begun before its issue (#3590): its token is another one, so
    # it is the first side chat bound after the start; a helper bound later is not.
    db.reserve_work_action(start_action_id(early), early, NEW_CHAT)
    db.record_session_token("3f1c0ffee", "k:early")
    db.bind_session_work("k:early", early, "side_chat")
    db.bind_session_work("k:helper", early, "side_chat")
    name("k:early", "early agent")
    name("k:helper", "helper")
    assert db.work_start_chat_titles() == {usual: "usual agent", early: "early agent"}

    # A Deckhand guess, or a name typed on the chat, does not name the Work.
    name("k:early", "guess", source="deckhand")
    name("k:own", "typed", source="user")
    assert db.work_start_chat_titles() == {}

    # Continued on another account, the chat's name lives on its new leg and still counts.
    db.append_chat_leg("k:own", "k:next")
    name("k:next", "renamed")
    assert db.work_start_chat_titles() == {usual: "renamed"}


# --- the worker's write --------------------------------------------------------------------------


def _history_deck(tmp_path):
    from test_web import _app_with_state

    app = _app_with_state(tmp_path, with_transcript=True)
    app.state.app_state.bind_session_run(KEY, WORK_ID, "run-1")
    return app


@pytest.mark.asyncio
async def test_the_worker_names_its_own_work_and_a_repeat_changes_nothing(tmp_path):
    app = _history_deck(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        empty = (await client.get(URL)).json()
        assert empty == {"work_id": WORK_ID, "title": None, "agent_title": None, "source": None}

        first = await client.put(URL, json={"title": "  Work title   stuck on a link "})
        again = await client.put(URL, json={"title": "Work title stuck on a link"})
        read = (await client.get(URL)).json()

    named = {
        "work_id": WORK_ID,
        "title": "Work title stuck on a link",
        "agent_title": "Work title stuck on a link",
        "source": "agent",
    }
    assert first.status_code == 200 and first.json() == named
    assert again.json() == named and read == named
    assert app.state.control_db.work_agent_titles() == {WORK_ID: "Work title stuck on a link"}
    count = app.state.control_db._conn.execute("SELECT COUNT(*) FROM work_metadata").fetchone()
    assert count[0] == 1


@pytest.mark.asyncio
async def test_the_operators_name_still_shows_and_the_worker_cannot_clear_it(tmp_path):
    app = _history_deck(tmp_path)
    app.state.control_db.set_work_title(WORK_ID, "Mine")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        put = (await client.put(URL, json={"title": "Worker's"})).json()
        deleted = (await client.delete(URL)).json()

    assert put == {"work_id": WORK_ID, "title": "Mine", "agent_title": "Worker's", "source": "user"}
    assert deleted == {"work_id": WORK_ID, "title": "Mine", "agent_title": None, "source": "user"}
    assert app.state.control_db.work_titles() == {WORK_ID: "Mine"}
    assert app.state.control_db.work_agent_titles() == {}


@pytest.mark.asyncio
async def test_an_empty_name_is_refused(tmp_path):
    app = _history_deck(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.put(URL, json={"title": "  \n "})
    assert response.status_code == 422 and "must not be empty" in response.json()["detail"]
    assert app.state.control_db.work_agent_titles() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
async def test_a_chat_in_no_run_cannot_name_a_work(tmp_path, method):
    from test_web import _app_with_state

    app = _app_with_state(tmp_path, with_transcript=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        kwargs = {"json": {"title": "x"}} if method == "PUT" else {}
        response = await client.request(method, URL, **kwargs)
    assert response.status_code == 409 and "not part of any Work" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_rescue_chat_that_registered_the_issue_names_its_work(tmp_path):
    """#2552: the same rule as the resolution — a registered issue is the chat's own act."""
    from test_web import _app_with_state

    app = _app_with_state(tmp_path, with_transcript=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        registered = await client.post(
            f"/api/sessions/{KEY}/issues", json={"issue": WORK_ID.split(":", 1)[1]}
        )
        assert registered.status_code == 200, registered.text
        response = await client.put(URL, json={"title": "Rescued"})
    assert response.status_code == 200 and response.json()["work_id"] == WORK_ID
    assert app.state.control_db.work_agent_titles() == {WORK_ID: "Rescued"}


@pytest.mark.asyncio
async def test_a_chat_in_two_works_names_one_in_the_title_body(tmp_path):
    from test_web import _app_with_state

    other = "github_issue:eerovil/agentdeck#99"
    app = _app_with_state(tmp_path, with_transcript=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        for work in (WORK_ID, other):
            registered = await client.post(
                f"/api/sessions/{KEY}/issues", json={"issue": work.split(":", 1)[1]}
            )
            assert registered.status_code == 200, registered.text
        unnamed = await client.put(URL, json={"title": "Which?"})
        named = await client.put(URL, json={"title": "This one", "work_id": other})
        stranger = await client.put(
            URL, json={"title": "No", "work_id": "github_issue:eerovil/agentdeck#12345"}
        )
    assert unnamed.status_code == 409
    assert named.status_code == 200 and named.json()["work_id"] == other
    assert stranger.status_code == 409
    assert app.state.control_db.work_agent_titles() == {other: "This one"}


# --- on the deck ---------------------------------------------------------------------------------


def _front_titles(overview) -> set[str]:
    front = overview.front
    return {row.card.title for row in (*front.needs_you, *front.active, *front.finished)}


@pytest.mark.asyncio
async def test_the_worker_name_shows_on_the_work_page_and_the_front_page(tmp_path):
    from fastapi.testclient import TestClient
    from test_work_ui import WORK, _add, _app

    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", WORK, thinking=True)
    app.state.issues.cached_issues = lambda refs: {
        ("eerovil/agentdeck", 2147): {
            "title": "https://deck/chats/abc",
            "state": "open",
            "is_pr": False,
        }
    }
    client = TestClient(app)
    assert (
        '<h1 class="wd-title">https://deck/chats/abc<button' in client.get(work_href(WORK_ID)).text
    )

    # The chat the deck started for the Work named itself (#3597): that names the Work, less
    # its "Spec:" label. A Deckhand guess on that chat, or any name on another chat, would not.
    from agentdeck.work_actions import NEW_CHAT
    from agentdeck.work_start import start_action_id

    db = app.state.control_db
    db.reserve_work_action(start_action_id(WORK_ID), WORK_ID, NEW_CHAT)
    db.record_session_token(start_action_id(WORK_ID), KEY)
    db.record_generated_title(KEY, "Spec: Trip video cut", "", source="agent")
    await app.state.work_overview.refresh()
    assert "Trip video cut" in _front_titles(app.state.work_overview.overview)

    app.state.control_db.set_work_agent_title(WORK_ID, "Trip video follow-up", KEY)
    await app.state.work_overview.refresh()

    page = client.get(work_href(WORK_ID)).text
    assert '<h1 class="wd-title">Trip video follow-up<button' in page
    assert 'value=""' in page  # the ✎ box still holds only the operator's own name
    assert "Trip video follow-up" in _front_titles(app.state.work_overview.overview)

    app.state.control_db.set_work_title(WORK_ID, "Mine")
    await app.state.work_overview.refresh()
    assert '<h1 class="wd-title">Mine<button' in client.get(work_href(WORK_ID)).text
    assert "Mine" in _front_titles(app.state.work_overview.overview)
