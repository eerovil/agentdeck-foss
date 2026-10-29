"""Live prototyping's page and the agent's API for it (#3569)."""

from __future__ import annotations

import subprocess

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck import live_prototype
from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, InjectConfig
from agentdeck.models import Capability, Session, SessionStatus
from agentdeck.providers.instructions import AGENTDECK_SESSION_INSTRUCTIONS

pytestmark = pytest.mark.anyio

KEY = "claude_code:main:abc"
API = f"/api/sessions/{KEY}/live-prototype"


def _app(tmp_path):
    app = create_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
            inject=InjectConfig(enabled=True),
            accounts=[
                AccountConfig(
                    provider="claude_code", label="main", config_dir=str(tmp_path / "main")
                )
            ],
        )
    )
    app.state.app_state.update_session(
        Session(
            key=KEY,
            account_key="claude_code:main",
            session_id="abc",
            status=SessionStatus.LIVE,
            title="Filters prototype",
            cwd=tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_agent_registers_link_records_steps_and_page_shows_them(tmp_path):
    app = _app(tmp_path)
    # Kept per chat, not per leg: a chat that continues keeps its prototype.
    chat = app.state.app_state.chat_identity_key(KEY)
    async with _client(app) as client:
        guide = await client.get("/api/live-prototype/guide")
        assert "/live-prototype/steps" in guide.text
        assert "Build no backend" in guide.text
        assert "Never make the person log in" in guide.text
        assert "Do not take screenshots" in guide.text
        # Every chat's guide sends any "live prototype" ask here (#3760).
        trigger = " ".join(AGENTDECK_SESSION_INSTRUCTIONS.split())
        assert '"Live prototype" in any wording' in trigger
        assert "always means the deck's live prototype page" in trigger
        # A diagram ask is sent to the guide's "Diagrams" part, which uses this page (#3822).
        assert "draw it as a file in the repository it describes" in trigger
        assert "Diagrams (#3822)" in guide.text
        assert "?bare=1" in guide.text

        # A step before any link has nowhere to show.
        early = await client.post(f"{API}/steps", json={"summary": "x"})
        assert early.status_code == 409

        bad = await client.put(API, json={"url": "javascript:alert(1)"})
        assert bad.status_code == 422

        with app.state.app_state.bus.subscribe(live_prototype.topic(chat)) as sub:
            linked = await client.put(
                API,
                json={
                    "url": "https://host.ts.net:8443/",
                    "versions": [
                        {"name": "A", "url": "https://host.ts.net:8443/?v=a"},
                        {"name": "B", "url": "https://host.ts.net:8443/?v=b"},
                    ],
                },
            )
            assert linked.json()["page_url"] == f"/live/{KEY}"
            # The old name still answers for agents on the guide before #3811.
            assert linked.json()["meeting_url"] == f"/live/{KEY}"
            first = await client.post(
                f"{API}/steps", json={"summary": "Filters  on top", "commit": "abc123"}
            )
            second = await client.post(f"{API}/steps", json={"summary": "Bigger cards"})
            assert sub.changed_topics() == {live_prototype.topic(chat)}

        assert [s["number"] for s in second.json()["steps"]] == [1, 2]
        assert first.json()["steps"][0]["summary"] == "Filters on top"
        # A new link keeps the history; versions left out keep theirs.
        relinked = await client.put(API, json={"url": "https://host.ts.net:9000/"})
        assert len(relinked.json()["steps"]) == 2
        assert [v["name"] for v in relinked.json()["versions"]] == ["A", "B"]

        # The person's pick is the prototype's state: the spec request names it, a reload shows it.
        picked = await client.put(f"{API}/picked", json={"version": "B"})
        assert picked.json()["spec_request"] == (
            "Write the spec from what we picked: version B (https://host.ts.net:8443/?v=b)."
        )
        unknown = await client.put(f"{API}/picked", json={"version": "C"})
        assert unknown.status_code == 422

        page = await client.get(f"/live/{KEY}")
        assert '"picked": "B"' in page.text
        assert page.status_code == 200
        assert "live_prototype.js" in page.text
        # Enter sends; on a phone the panel's button counts the changes (#3831).
        assert 'class="send-on-enter"' in page.text
        assert 'aria-label="Send suggestion"' in page.text
        assert 'id="live-side-count"' in page.text
        assert "https://host.ts.net:9000/" in page.text

        # Versions replaced without B drop the pick back to the main link.
        dropped = await client.put(API, json={"url": "https://host.ts.net:9000/", "versions": []})
        assert dropped.json()["picked"] is None
        assert "version the main link (https://host.ts.net:9000/)" in dropped.json()["spec_request"]

        # The record outlives the web process.
        assert live_prototype.read(chat)["revision"] == dropped.json()["revision"]

        # A diagram has a slot of its own (#3827): it keeps the prototype's link, and a
        # bad one stores neither.
        assert (await client.put(API, json={})).status_code == 422
        refused = await client.put(
            API, json={"url": "https://host.ts.net:1/", "diagram": "file:///etc/x.md"}
        )
        assert refused.status_code == 422
        assert live_prototype.read(chat)["url"] == "https://host.ts.net:9000/"
        drawn = await client.put(
            API, json={"diagram": "https://deck.ts.net/w/docs/diagrams/flow.md?bare=1"}
        )
        assert drawn.json()["url"] == "https://host.ts.net:9000/"
        assert drawn.json()["diagram"] == "https://deck.ts.net/w/docs/diagrams/flow.md?bare=1"
        # The file's own repository is kept, for the Work's link once the worktree is gone.
        assert live_prototype.read(chat)["diagram_repository"] == ""
        folder = tmp_path / "shop" / "docs" / "diagrams"
        folder.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(tmp_path / "shop")], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path / "shop"), "remote", "add", "origin",
             "git@github.com:Acme/Shop.git"], check=True,
        )
        await client.put(API, json={"diagram": f"https://deck.ts.net{folder}/flow.md?bare=1"})
        assert live_prototype.read(chat)["diagram_repository"] == "acme/shop"
        assert '{"diagram":' in guide.text and "Inside a Work" in guide.text

    # A chat that only draws records its changes too.
    live_prototype.set_diagram("drawing", "https://deck.ts.net/w/docs/diagrams/a.md?bare=1")
    assert live_prototype.add_step("drawing", "Split the queue")["steps"][0]["number"] == 1
    assert live_prototype.diagram_file("https://d/w%20t/docs/diagrams/a.md?bare=1") == (
        "/w t/docs/diagrams/a.md"
    )
    for other in ("https://d/w/docs/a.md", "https://d/w/docs/diagrams/a.py", None,
                  "https://d/w/docs/diagrams/../../etc/x.md"):
        assert live_prototype.diagram_file(other) == ""


async def test_the_chat_page_links_its_live_prototype_once_there_is_one(tmp_path):
    app = _app(tmp_path)
    async with _client(app) as client:
        before = await client.get(f"/sessions/{KEY}")
        assert f'href="/live/{KEY}"' not in before.text
        live_prototype.set_link(
            app.state.app_state.chat_identity_key(KEY), "https://host.ts.net:8443/"
        )
        after = await client.get(f"/sessions/{KEY}")
        assert f'href="/live/{KEY}"' in after.text
        assert "live-diagram-link" not in after.text
        # A diagram gets its own chip, opening the page on it (#3827).
        chat = app.state.app_state.chat_identity_key(KEY)
        drawn = tmp_path / "w" / "docs" / "diagrams" / "a.md"
        drawn.parent.mkdir(parents=True)
        drawn.write_text("# A\n")
        live_prototype.set_diagram(chat, f"https://d{drawn}?bare=1", "acme/shop")
        both = await client.get(f"/sessions/{KEY}")
        assert f'href="/live/{KEY}"' in both.text
        assert f'href="/live/{KEY}?view=diagram"' in both.text
        record = live_prototype.read(chat)
        record.pop("url")
        live_prototype._write(chat, record)
        drawn_only = await client.get(f"/sessions/{KEY}")
        assert f'href="/live/{KEY}"' not in drawn_only.text
        assert f'href="/live/{KEY}?view=diagram"' in drawn_only.text
        # Once the worktree is gone the chip follows the Work page's fallback, to the
        # repository the diagram was drawn in.
        drawn.unlink()
        cleaned = await client.get(f"/sessions/{KEY}")
        assert (
            'href="https://github.com/acme/shop/blob/HEAD/docs/diagrams/a.md"'
            in cleaned.text
        )


async def test_use_cases_are_written_one_at_a_time_without_reloading_the_prototype(tmp_path):
    app = _app(tmp_path)
    chat = app.state.app_state.chat_identity_key(KEY)
    async with _client(app) as client:
        await client.put(
            API,
            json={
                "url": "https://host.ts.net:8443/",
                "versions": [{"name": "A", "url": "https://host.ts.net:8443/?v=a"}],
            },
        )
        revision = live_prototype.read(chat)["revision"]
        with app.state.app_state.bus.subscribe(live_prototype.topic(chat)) as sub:
            agent = await client.post(
                f"{API}/use-cases",
                json={
                    "title": "Buyer  filters",
                    "steps": ["Open list", "Pick size"],
                    "version": "A",
                },
            )
            person = await client.post(f"{API}/use-cases", json={"title": "Admin", "steps": []})
            assert sub.changed_topics() == {live_prototype.topic(chat)}
        assert [u["id"] for u in person.json()["use_cases"]] == ["u1", "u2"]
        assert agent.json()["use_cases"][0]["title"] == "Buyer filters"
        # The page reloads the prototype only when the revision moves.
        assert person.json()["revision"] == revision

        edited = await client.put(
            f"{API}/use-cases/u1", json={"title": "Buyer", "steps": ["Open"], "url": None}
        )
        assert edited.json()["use_cases"][0] == {
            "id": "u1",
            "title": "Buyer",
            "steps": ["Open"],
            "version": None,
            "url": None,
        }
        assert (
            await client.post(f"{API}/use-cases", json={"title": "x", "version": "Z"})
        ).status_code == 422
        assert (await client.put(f"{API}/use-cases/u9", json={"title": "x"})).status_code == 404
        gone = await client.delete(f"{API}/use-cases/u2")
        assert [u["id"] for u in gone.json()["use_cases"]] == ["u1"]
        # A new id never reuses a deleted one.
        again = await client.post(f"{API}/use-cases", json={"title": "Again"})
        assert again.json()["use_cases"][-1]["id"] == "u3"

        page = await client.get(f"/live/{KEY}")
        assert "data-speech-button" in page.text
        assert "composer_speech.js" in page.text


async def test_live_prototype_stream_says_what_the_agent_is_doing(tmp_path):
    from types import SimpleNamespace

    from agentdeck.web.routes_live_prototype import live_prototype_events

    app = _app(tmp_path)
    state = app.state.app_state
    session = state.sessions[KEY]
    session.thinking, session.activity = True, "Using tools"

    class _Request(SimpleNamespace):
        cycles = 2

        async def is_disconnected(self):
            self.cycles -= 1
            return self.cycles < 0

    request = _Request(app=app, query_params={})
    response = await live_prototype_events(request, KEY)
    frames = response.body_iterator
    assert await anext(frames) == "data: connected\n\n"
    assert await anext(frames) == 'event: activity\ndata: "Using tools"\n\n'
    session.thinking, session.activity = False, None
    state.sessions_changed([KEY])
    assert await anext(frames) == "event: activity\ndata: null\n\n"
