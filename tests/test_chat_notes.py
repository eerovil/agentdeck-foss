"""A chat shows the GitHub comments its agent posted, as notes the agent never sees (#3227).

Deliveries go through `WebhookLog` and `WebhookTailer`, the same appender and reader the
live path uses, so the consumer is asserted over real stream bytes.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from agentdeck import chat_notes
from agentdeck.chat_notes import BACKFILL_MARKER, CONSUMER, ChatNotesService
from agentdeck.db import Db
from agentdeck.github_webhook_log import Repository, WebhookLog
from agentdeck.web import render
from agentdeck.web.routes_chat_notes import router
from agentdeck.webhook_tailer import WebhookTailer

AGENTDECK = Repository(owner="eerovil", name="agentdeck")
BOT = "agentdeck-eerovil[bot]"
SHA = "492522ba7ff9b86d904a86f4e63aed1f8d6c79e6"
IMAGE = f"https://github.com/eerovil/agent-evidence/blob/{SHA}/agentdeck/issue-3/shot.png?raw=1"

counter = iter(range(1_000_000))


def comment_body(
    comment_id: int,
    *,
    number: int = 3,
    action: str = "created",
    login: str = BOT,
    body: str = "Done.",
    created: str = "2026-10-17T05:00:00Z",
    updated: str | None = None,
    pull_request: bool = False,
) -> bytes:
    issue: dict = {"number": number}
    if pull_request:
        issue["pull_request"] = {"url": "https://api.github.com/x"}
    return json.dumps(
        {
            "action": action,
            "issue": issue,
            "comment": {
                "id": comment_id,
                "user": {"login": login},
                "body": body,
                "created_at": created,
                "updated_at": updated or created,
                "html_url": f"https://github.com/{AGENTDECK.slug}/issues/{number}"
                f"#issuecomment-{comment_id}",
            },
            "repository": {"full_name": AGENTDECK.slug},
        }
    ).encode()


def deliver(root: Path, body: bytes, received_at: str | None = None) -> None:
    WebhookLog(root).append(
        body=body,
        headers={"X-GitHub-Delivery": f"d-{next(counter)}", "X-GitHub-Event": "issue_comment"},
        received_at=received_at,
    )


@pytest.fixture
def root(tmp_path):
    return tmp_path / "github-webhooks"


@pytest.fixture
def store(tmp_path):
    db = Db(tmp_path / "deck.db", 30)
    yield db
    db.close()


@pytest.fixture
def reads():
    return []


@pytest.fixture
def service(root, store, reads):
    async def read_html(repository, comment_id):
        reads.append((repository, comment_id))
        return f'<p>Rendered {comment_id}</p><img src="{IMAGE}" alt="shot">'

    return ChatNotesService(
        root,
        store=store,
        # Issue 3 and pull request 4 belong to the chat; nothing else does.
        owner_of_issue=lambda repo, n: ("chat-1", f"{repo}#{n}") if n == 3 else None,
        owner_of_pull=lambda repo, n: ("chat-1", f"{repo}#3") if n == 4 else None,
        own_logins=lambda: frozenset({BOT}),
        read_html=read_html,
    )


def apply_stream(root: Path, service: ChatNotesService) -> None:
    tailer = WebhookTailer(root, consumer="test-apply")
    asyncio.run(service.apply(tailer.read()))
    tailer.commit()


def test_own_comments_on_the_chats_items_become_its_notes(root, store, service, reads):
    deliver(root, comment_body(501, body="Two questions"))
    deliver(root, comment_body(502, number=4, pull_request=True, created="2026-10-17T06:00:00Z"))
    deliver(root, comment_body(503, login="eerovil"))  # a person's comment
    deliver(root, comment_body(504, number=9))  # an item no chat owns
    # GitHub stamped this one an hour late (#3966): it shows when it arrived.
    deliver(
        root,
        comment_body(505, created="2026-10-17T07:57:58Z"),
        received_at="2026-10-17T06:58:00.852894+00:00",
    )
    apply_stream(root, service)

    notes = store.chat_notes("chat-1")
    assert [(n.comment_id, n.number, n.is_pull) for n in notes] == [
        (501, 3, False),
        (502, 4, True),
        (505, 3, False),
    ]
    assert [n.created_at for n in notes] == [
        "2026-10-17T05:00:00Z",
        "2026-10-17T06:00:00Z",
        "2026-10-17T06:58:00Z",
    ]
    assert notes[0].body == "Two questions"
    assert reads == [("eerovil/agentdeck", n) for n in (501, 502, 505)]
    # A later edit keeps the corrected time; a record with no arrival keeps GitHub's.
    deliver(
        root,
        comment_body(
            505, action="edited", created="2026-10-17T07:57:58Z", updated="2026-10-17T08:30:00Z"
        ),
        received_at="2026-10-17T08:30:01+00:00",
    )
    apply_stream(root, service)
    assert store.chat_notes("chat-1")[-1].created_at == "2026-10-17T06:58:00Z"
    assert chat_notes._no_later_than_arrival("2026-10-17T07:57:58Z", "") == ("2026-10-17T07:57:58Z")
    assert notes[0].body_html.startswith("<p>Rendered 501</p>")


def test_the_newest_edit_wins_and_a_delete_removes_the_note(root, store, service, reads):
    deliver(root, comment_body(501, body="first"))
    deliver(root, comment_body(501, action="edited", body="fixed", updated="2026-10-17T05:05:00Z"))
    # A late copy of the original arrives after the edit: it changes nothing.
    deliver(root, comment_body(501, body="first"))
    apply_stream(root, service)

    (note,) = store.chat_notes("chat-1")
    assert (note.body, note.updated_at) == ("fixed", "2026-10-17T05:05:00Z")
    assert note.body_html is not None

    deliver(root, comment_body(501, action="deleted"))
    apply_stream(root, service)
    assert store.chat_notes("chat-1") == []


def test_a_first_start_skips_the_comments_already_on_disk(root, store, service):
    deliver(root, comment_body(501))
    assert service.skip_history() is True
    tailer = WebhookTailer(root, consumer=CONSUMER)
    asyncio.run(service.apply(tailer.read()))
    assert store.chat_notes("chat-1") == []
    # Later starts keep their offset.
    assert service.skip_history() is False


def test_the_one_time_backfill_reads_archives_and_the_live_file(root, store, service, reads):
    # An archived generation holds the older comment and its later edit is in the live file.
    deliver(root, comment_body(501, body="first"))
    live = root / "eerovil" / "agentdeck.jsonl"
    with gzip.open(live.with_name("agentdeck.jsonl.1.gz"), "wb") as archive:
        archive.write(live.read_bytes())
    live.write_bytes(b"")
    deliver(root, comment_body(501, action="edited", body="fixed", updated="2026-10-17T05:05:00Z"))
    deliver(root, comment_body(502, created="2026-10-17T06:00:00Z"))
    deliver(root, comment_body(503, login="eerovil"))

    assert asyncio.run(service.backfill()) == 4
    assert [(n.comment_id, n.body) for n in store.chat_notes("chat-1")] == [
        (501, "fixed"),
        (502, "Done."),
    ]
    assert (root.parent / BACKFILL_MARKER).exists()

    # A truncated archive fails the pass, so it is not marked done and the next start retries.
    (root.parent / BACKFILL_MARKER).unlink()
    archive = live.with_name("agentdeck.jsonl.1.gz")
    archive.write_bytes(archive.read_bytes()[:-20])
    with pytest.raises(EOFError):
        asyncio.run(service.backfill())
    assert not (root.parent / BACKFILL_MARKER).exists()


def test_the_one_time_time_repair_moves_notes_stored_before_the_fix(root, store, service, reads):
    # Kept before #3966 with GitHub's hour-late time, and one kept with a right time.
    deliver(root, comment_body(501, created="2026-10-17T07:57:58Z"), "2026-10-17T06:58:00+00:00")
    deliver(root, comment_body(502, created="2026-10-17T06:10:00Z"), "2026-10-17T06:10:02+00:00")
    deliver(
        root,
        comment_body(503, number=9, created="2026-10-17T07:59:00Z"),
        "2026-10-17T06:59:00+00:00",
    )  # no chat owns it
    for comment_id, created in ((501, "2026-10-17T07:57:58Z"), (502, "2026-10-17T06:10:00Z")):
        store.save_chat_note(
            chat_notes.ChatNote(
                "eerovil/agentdeck",
                comment_id,
                "chat-1",
                3,
                False,
                BOT,
                "u",
                "Done.",
                None,
                created,
                created,
            )
        )

    assert service.repair_times() == 1
    notes = store.chat_notes("chat-1")
    assert [(n.comment_id, n.created_at) for n in notes] == [
        (502, "2026-10-17T06:10:00Z"),
        (501, "2026-10-17T06:58:00Z"),
    ]
    assert reads == []
    assert (root.parent / chat_notes.TIME_REPAIR_MARKER).exists()
    assert service.repair_times() == 0


def test_a_halted_or_failed_time_repair_is_not_marked_done(root, store, service):
    for comment_id in (501, 502):
        deliver(
            root,
            comment_body(comment_id, created="2026-10-17T07:57:58Z"),
            "2026-10-17T06:58:00+00:00",
        )
        store.save_chat_note(
            chat_notes.note_from(
                next(
                    d
                    for d in WebhookTailer(root, consumer="peek").read()
                    if f'"id": {comment_id},' in d.raw_body.decode()
                ),
                frozenset({BOT}),
            )[1]._replace(chat_id="chat-1", created_at="2026-10-17T07:57:58Z")
        )
    marker = root.parent / chat_notes.TIME_REPAIR_MARKER
    # The first write holds the repair thread mid-pass while the deck shuts down.
    entered, release = threading.Event(), threading.Event()
    move = store.move_chat_note_earlier

    def held_move(note):
        entered.set()
        release.wait(5)
        return move(note)

    store.move_chat_note_earlier = held_move

    async def shut_down_mid_pass():
        repair = asyncio.create_task(asyncio.to_thread(service.repair_times))
        await asyncio.to_thread(entered.wait, 5)
        stopping = asyncio.create_task(service.stop())
        await asyncio.sleep(0.05)
        assert not stopping.done()  # stop() waits for the thread
        release.set()
        await stopping
        return await repair

    assert asyncio.run(shut_down_mid_pass()) == 1
    assert not marker.exists()

    # The next start runs it again and finishes it.
    store.move_chat_note_earlier = move
    service._halt.clear()  # as start() does
    assert service.repair_times() == 1
    assert [n.created_at for n in store.chat_notes("chat-1")] == ["2026-10-17T06:58:00Z"] * 2
    marker.unlink()

    # A write that fails (the database already closed) aborts the pass unmarked.
    store.close()
    with pytest.raises(sqlite3.Error):
        service.repair_times()
    assert not marker.exists()


def test_the_card_shows_githubs_rendering_with_images_through_the_deck(root, store, service):
    deliver(root, comment_body(501))
    apply_stream(root, service)
    templates = Jinja2Templates(directory=str(Path(render.__file__).parent / "templates"))
    render.register_filters(templates)

    html = render.render_chat_notes(templates, store, "chat-1")

    assert 'data-note-id="501"' in html
    assert "agentdeck#3" in html and "not in context" in html
    assert f'src="/github-image/eerovil/agent-evidence/{SHA}/agentdeck/issue-3/shot.png"' in html
    assert "<p>Rendered 501</p>" in html
    # Before GitHub's rendering lands, the plain text shows, escaped.
    store.save_chat_note(
        store.chat_notes("chat-1")[0]._replace(
            comment_id=600, body="<b>raw</b>", updated_at="2026-10-17T07:00:00Z"
        )
    )
    assert "&lt;b&gt;raw&lt;/b&gt;" in render.render_chat_notes(templates, store, "chat-1")
    assert render.render_chat_notes(templates, store, "chat-2") == ""


def test_the_image_route_serves_only_an_image_a_note_shows(
    root, store, service, tmp_path, monkeypatch
):
    deliver(root, comment_body(501))
    apply_stream(root, service)
    calls = []

    async def fake_run(argv, *, timeout, repo=None, owner=None):
        calls.append(argv)
        return 0, json.dumps({"encoding": "base64", "content": "iVBORy1ieXRlcw=="}).encode()

    monkeypatch.setattr(chat_notes.gh_cli, "run", fake_run)
    app = FastAPI()
    app.include_router(router)
    app.state.control_db = store
    app.state.github_image_cache = tmp_path / "images"
    client = TestClient(app)
    path = f"/github-image/eerovil/agent-evidence/{SHA}/agentdeck/issue-3/shot.png"

    first = client.get(path)
    assert (first.status_code, first.content) == (200, b"\x89PNG-bytes")
    assert first.headers["content-type"] == "image/png"
    assert client.get(path).content == b"\x89PNG-bytes"
    assert len(calls) == 1  # the second answer came from disk
    assert (
        calls[0][-1]
        == f"repos/eerovil/agent-evidence/contents/agentdeck/issue-3/shot.png?ref={SHA}"
    )
    # A file no note shows, and a type the route never serves, are not there.
    assert client.get(path.replace("shot.png", "secret.png")).status_code == 404
    assert client.get(path.replace("shot.png", "shot.svg")).status_code == 404


def test_no_test_reaches_the_live_webhook_streams():
    """#3966: a test deck's default streams folder, and the files beside it, are scratch,
    in a child process too."""
    import subprocess
    import sys

    from agentdeck.config import AppConfig

    live = Path("~/.local/share/agentdeck").expanduser()
    assert not AppConfig().github_webhook.path.is_relative_to(live)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agentdeck.config import AppConfig; print(AppConfig().github_webhook.path)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == str(AppConfig().github_webhook.path)
