"""Starting a new Work from the deck (#2415): its issue is filed at once, attachments included.

The route reserves the press's client action id before anything reaches GitHub, uploads any
attachments to the project's evidence repository one after another, then creates the issue
with the canonical ownership label (and the spec-only label when asked) as the repository
owner's non-human authority. A failed upload files nothing; a create that ended without proof
either way is never made again.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from agentdeck import gh_cli, new_work
from agentdeck.models import IssueRef
from agentdeck.poller_ops import BoardScope, BoardView

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

AGENTDECK = BoardScope(
    instance="agentdeck",
    org="eerovil",
    ownership_label="agent",
    ownership_labels=("agent", "claude"),
    spec_only_label="spec-only",
    issue_repositories=("eerovil/agentdeck",),
    artifact_repositories=("eerovil/agent-evidence",),
)
TRAILHEAD = BoardScope(
    instance="trailhead",
    org="Northwind",
    ownership_label="claude",
    ownership_labels=("claude",),
    spec_only_label="spec-only",
    issue_repositories=("Northwind/store", "Northwind/issues"),
    artifact_repositories=("Northwind/issues",),
)
MINECRAFT = BoardScope(
    instance="minecraft",
    org="eerovil",
    ownership_label="claude",
    ownership_labels=("claude",),
    issue_repositories=("eerovil/game-mod",),
)
BOARD = BoardView(scopes=(AGENTDECK, TRAILHEAD, MINECRAFT))


# --- which repositories, and what filing there needs ---------------------------------------------

def test_targets_are_every_issue_repository_with_its_own_projects_labels_and_evidence():
    found = {target.repository: target for target in new_work.targets(BOARD)}

    assert sorted(found) == sorted(
        [
            "eerovil/agentdeck",
            "Northwind/store",
            "Northwind/issues",
            "eerovil/game-mod",
        ]
    )
    deck = found["eerovil/agentdeck"]
    # The first ownership label is the canonical one for creating an issue.
    assert (deck.ownership_label, deck.spec_only_label, deck.evidence_repository) == (
        "agent", "spec-only", "eerovil/agent-evidence"
    )
    assert found["Northwind/store"].evidence_repository == "Northwind/issues"
    assert found["eerovil/game-mod"].evidence_repository == ""
    assert found["eerovil/game-mod"].spec_only_label == ""


def test_a_repository_is_matched_without_regard_to_case_and_keeps_its_own_spelling():
    options = new_work.targets(BOARD)

    assert new_work.target_for(options, " northwind/STORE ").repository == (
        "Northwind/store"
    )
    assert new_work.target_for(options, "eerovil/unknown") is None
    assert new_work.target_for(options, None) is None


def test_a_chat_defaults_to_the_first_registered_issue_a_poller_manages():
    options = new_work.targets(BOARD)
    issues = (
        IssueRef("someone/elsewhere", 3),
        IssueRef("EEROVIL/agentdeck", 2400),
        IssueRef("Northwind/store", 9),
    )

    assert new_work.default_repository(options, issues) == "eerovil/agentdeck"
    assert new_work.default_repository(options, ()) == ""


def test_attachment_names_are_made_unique_within_one_filing():
    assert new_work.unique_names(["image.png", "image.png", "IMAGE.png", "notes", "notes"]) == [
        "image.png", "image-2.png", "IMAGE-3.png", "notes", "notes-2",
    ]


def test_the_body_embeds_images_links_files_and_says_how_to_read_them():
    uploaded = (
        new_work.Uploaded("a b.png", "agentdeck/new-work/x/a b.png", "c0ffee", "b10b", True),
        new_work.Uploaded("log.txt", "agentdeck/new-work/x/log.txt", "c0ffee2", "b10b2", False),
    )

    body = new_work.compose_body(
        "Chart labels overlap\n\nOn the phone.",
        evidence="eerovil/agent-evidence",
        uploaded=uploaded,
        chat_url="https://deck.example/chats/abc",
    )

    assert body.startswith("Chart labels overlap\n\nOn the phone.")
    assert "Started from [this chat](https://deck.example/chats/abc)" in body
    assert (
        "![a b.png](https://github.com/eerovil/agent-evidence/blob/c0ffee/"
        "agentdeck/new-work/x/a%20b.png?raw=1)"
    ) in body
    assert "- [log.txt](https://github.com/eerovil/agent-evidence/blob/c0ffee2/" in body
    assert (
        "`gh api repos/eerovil/agent-evidence/git/blobs/b10b --jq .content "
        "| base64 -d > 'a b.png'`"
    ) in body


def test_a_body_without_a_chat_or_files_is_just_the_text():
    assert new_work.compose_body("  Only this  ") == "Only this\n"


# --- the route ---------------------------------------------------------------------------------

class _Gh:
    """``gh_cli.run_once`` as the filing calls it, answering from a script."""

    def __init__(self, *answers):
        self.calls: list[tuple[list[str], bytes | None, object]] = []
        self.answers = list(answers)
        # Set by a test that holds the create until something else happened first.
        self.gate = None

    async def __call__(self, argv, *, timeout, stdin=None, repo=None, owner=None):
        self.calls.append((list(argv), stdin, repo))
        if self.answers:
            answer = self.answers.pop(0)
        elif argv[1:4] == ["api", "-X", "POST"]:
            if self.gate is not None:
                await self.gate.wait()
            url = "https://github.com/eerovil/agentdeck/issues/77"
            answer = (0, json.dumps({"number": 77, "html_url": url}).encode(), b"")
        else:
            index = len(self.calls)
            answer = (
                0,
                json.dumps(
                    {"commit": {"sha": f"commit{index}"}, "content": {"sha": f"blob{index}"}}
                ).encode(),
                b"",
            )
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _app(tmp_path, monkeypatch, *, gh: _Gh | None = None, board=BOARD):
    from test_web import _app_with_state

    app = _app_with_state(tmp_path)
    monkeypatch.setattr(app.state.issues, "read_board", lambda: board)
    monkeypatch.setattr(new_work.shutil, "which", lambda name: "/usr/bin/gh")
    gh = gh or _Gh()
    monkeypatch.setattr(gh_cli, "run_once", gh)
    return app, gh


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def _file(client, *, message="Chart labels overlap on phone\n\nUnder 400px.",
          repository="eerovil/agentdeck", spec_only=True, action_id="press-0001",
          files=None, query="", **headers):
    data = {"message": message, "repository": repository, "client_action_id": action_id}
    if spec_only:
        data["spec_only"] = "1"
    return client.post(f"/works/new{query}", data=data, files=files, headers=headers)


def _creates(gh):
    return [call for call in gh.calls if call[0][1:4] == ["api", "-X", "POST"]]


def _uploads(gh):
    return [call for call in gh.calls if call[0][1:4] == ["api", "-X", "PUT"]]


def _labels(call):
    return json.loads(call[1])["labels"]


def _router(filed: list | None = None, filing: list | None = None):
    """The Work router as the route uses it: starting on the ownership label."""
    import contextlib

    @contextlib.contextmanager
    def held(repository):
        if filing is not None:
            filing.append(("held", repository))
        yield
        if filing is not None:
            filing.append(("released", repository))

    return SimpleNamespace(
        starts_on=lambda repository, labels: "agent" if "agent" in labels else "",
        filing=held,
        note_filed=lambda *args: filed.append(args) or True if filed is not None else True,
    )


def test_a_filing_creates_the_issue_with_one_request_and_answers_with_the_link(
    tmp_path, monkeypatch
):
    app, gh = _app(tmp_path, monkeypatch)

    response = _file(_client(app))

    assert response.status_code == 200
    assert response.headers["X-AgentDeck-New-Work"] == "filed"
    assert 'data-new-work-receipt="sent"' in response.text
    # No Work router starts it here: the Poller does, and no chat link is offered.
    assert "the Work card appears after the Poller" in response.text
    assert "data-new-work-chat" not in response.text
    assert "https://github.com/eerovil/agentdeck/issues/77" in response.text
    # One REST request, not `gh issue create`'s repository and label lookups (#3590).
    ((argv, stdin, repo),) = gh.calls
    assert argv == [
        "/usr/bin/gh", "api", "-X", "POST", "repos/eerovil/agentdeck/issues", "--input", "-",
    ]
    assert repo == "eerovil/agentdeck"
    assert json.loads(stdin) == {
        "title": "Chart labels overlap on phone",
        "body": "Chart labels overlap on phone\n\nUnder 400px.\n",
        "labels": ["agent", "spec-only"],
    }
    (press,) = app.state.control_db.load_work_actions("new-work:eerovil/agentdeck")
    assert (press.action, press.state) == ("new_work", "sent")
    assert press.result == "https://github.com/eerovil/agentdeck/issues/77"


def test_a_filing_assigns_this_decks_person_where_another_deck_takes_unassigned(
    tmp_path, monkeypatch
):
    """#3731: filed already this deck's, or the deck that takes unassigned issues starts it."""
    app, gh = _app(tmp_path, monkeypatch)
    app.state.deck_claims = lambda repository, assignee: assignee == "me"
    monkeypatch.setattr("agentdeck.poller_engine.github_authority.deck_person", lambda: "me")

    assert _file(_client(app)).status_code == 200

    ((_argv, stdin, _repo),) = _creates(gh)
    assert json.loads(stdin)["assignees"] == ["me"]


async def _post_and_settle(app, **fields):
    """File through the app's own loop, then let the early start settle (#3590)."""
    import asyncio

    import httpx

    from agentdeck.web import routes_works

    data = {
        "message": "Chart labels overlap on phone\n\nUnder 400px.",
        "repository": "eerovil/agentdeck",
        "client_action_id": "press-0001",
        "spec_only": "1",
        **fields,
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/works/new", data=data)
        await asyncio.gather(*list(routes_works._EARLY_STARTS))
        replay = await client.post("/works/new", data=data)
    return response, replay


async def test_the_agent_starts_before_github_answers_and_the_receipt_links_its_chat(
    tmp_path, monkeypatch
):
    """#3590: the start runs while the issue is created, then is bound to the new Work."""
    import asyncio

    from agentdeck import work_start

    app, gh = _app(tmp_path, monkeypatch)
    filing: list = []
    app.state.work_router = _router(filing=filing)
    gh.gate = asyncio.Event()
    early = object()
    calls: list = []

    async def start_before_issue(app_, repository, *, request, press):
        # GitHub has not answered yet: the create waits on this very start.
        calls.append(("start", repository, request, press, len(_creates(gh))))
        gh.gate.set()
        return early

    async def bind_before_issue(app_, started, ref, *, url, labels, on_refused, reserved, filed_at):
        # D19: the router is held only for the create; the Work's own start is reserved.
        held = filing[-1][0] == "held"
        spent = app.state.control_db.work_action(work_start.start_action_id(ref.work_id))
        calls.append(("bind", started, ref.work_id, url, list(labels), held, reserved, bool(spent)))
        return "started"

    monkeypatch.setattr(work_start, "start_before_issue", start_before_issue)
    monkeypatch.setattr(work_start, "bind_before_issue", bind_before_issue)

    response, replay = await asyncio.wait_for(_post_and_settle(app), 5)

    assert response.status_code == 200
    assert "Filed eerovil/agentdeck#77 — its Work is starting" in response.text
    link = 'href="/works/github_issue:eerovil%2Fagentdeck%2377/chat" data-new-work-chat'
    assert link in response.text and ">issue</a>" in response.text
    body = "Chart labels overlap on phone\n\nUnder 400px.\n"
    assert calls == [
        ("start", "eerovil/agentdeck", body, "press-0001", 1),
        (
            "bind",
            early,
            "github_issue:eerovil/agentdeck#77",
            "https://github.com/eerovil/agentdeck/issues/77",
            ["agent", "spec-only"],
            False,
            True,
            True,
        ),
    ]
    # The router's own starts in the repository wait until the Work's start is recorded.
    assert filing == [("held", "eerovil/agentdeck"), ("released", "eerovil/agentdeck")]
    # A replay answers with the same link and starts nothing more.
    assert "Already filed" in replay.text and link in replay.text
    assert len(calls) == 2 and len(_creates(gh)) == 1


async def test_with_no_early_start_the_work_starts_once_the_issue_exists(tmp_path, monkeypatch):
    """No account or spare could start early: the usual start, as #3371 made it."""
    from agentdeck import work_start

    app, _gh = _app(tmp_path, monkeypatch)
    filed: list = []
    app.state.work_router = _router(filed=filed)

    async def start_before_issue(*_args, **_kwargs):
        return None

    monkeypatch.setattr(work_start, "start_before_issue", start_before_issue)

    response, _replay = await _post_and_settle(app)

    assert "its Work is starting" in response.text and "data-new-work-chat" in response.text
    assert filed == [("eerovil/agentdeck", 77, ["agent", "spec-only"])]


async def test_the_work_chat_link_opens_the_chat_once_it_exists_else_the_work_page(
    tmp_path, monkeypatch
):
    """#3590: the receipt's link is drawn before the chat exists; it waits a little for it."""
    import asyncio

    import httpx

    from agentdeck.web import routes_works
    from agentdeck.work_start import start_action_id

    app, _gh = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(routes_works, "WORK_CHAT_POLL_S", 0.01)
    state, db = app.state.app_state, app.state.control_db
    key = "claude_code:test:sid1"
    work_id = "github_issue:eerovil/agentdeck#77"
    page = "/works/github_issue:eerovil%2Fagentdeck%2377"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # No start recorded: straight to the Work page.
        nothing = await client.get(f"{page}/chat")
        assert (nothing.status_code, nothing.headers["location"]) == (303, page)
        # Tapped while the early start is still being bound to the new Work, before its
        # start is recorded: it waits, and its chat opens once it appears.
        start_id = start_action_id(work_id)
        routes_works._EARLY_BINDING.add(work_id)

        async def appear():
            await asyncio.sleep(0.03)
            db.reserve_work_action(start_id, work_id, "new_chat")
            db.claim_start_spare(start_id, "spare-token", "chat-spare")
            routes_works._EARLY_BINDING.discard(work_id)
            await asyncio.sleep(0.03)
            state.session_tokens["spare-token"] = key

        appearing = asyncio.create_task(appear())
        found = await client.get(f"{page}/chat")
        await appearing
        assert found.status_code == 303
        assert found.headers["location"] == state.chat_path_for_session(key)
        assert found.headers["cache-control"] == "no-store"
        # The early start was refused and the usual one runs under the start id: its chat.
        state.session_tokens.pop("spare-token")
        state.session_tokens[start_id] = key
        fallback = await client.get(f"{page}/chat")
        assert fallback.headers["location"] == state.chat_path_for_session(key)
        # A refused start: the Work page, which says why.
        other = "github_issue:eerovil/agentdeck#78"
        db.reserve_work_action(start_action_id(other), other, "new_chat")
        db.settle_work_action(start_action_id(other), "failed", "at_capacity")
        refused = await client.get("/works/github_issue:eerovil%2Fagentdeck%2378/chat")
        assert refused.headers["location"] == "/works/github_issue:eerovil%2Fagentdeck%2378"


def test_an_open_form_asks_for_a_work_spare_only_where_the_deck_starts_works(
    tmp_path, monkeypatch
):
    app, _gh = _app(tmp_path, monkeypatch)
    app.state.work_router = _router()
    wanted: list[str] = []
    app.state.start_spares = SimpleNamespace(want_work=wanted.append)
    client = _client(app)

    for repository in ("eerovil/agentdeck", "eerovil/game-mod", "x/y"):
        response = client.post("/works/new/warm", data={"repository": repository})
        assert response.status_code == 204

    # Only the repository whose ownership label the router starts on; never a stranger.
    assert wanted == ["eerovil/agentdeck"]
    assert client.post(
        "/works/new/warm", data={"repository": "eerovil/agentdeck"},
        headers={"origin": "https://evil.example"},
    ).status_code == 403


@pytest.mark.parametrize(
    ("answer", "maybe", "said"),
    [
        ((1, b"", b"gh: Validation Failed (HTTP 422)\n"), False, "its agent is being stopped"),
        ((1, b"", b"HTTP 502: Bad Gateway\n"), True, "its agent was told to wait"),
    ],
    ids=["refused", "uncertain"],
)
async def test_an_early_start_whose_issue_was_not_filed_is_stopped_or_told_to_wait(
    tmp_path, monkeypatch, answer, maybe, said
):
    from agentdeck import work_start

    app, _gh = _app(tmp_path, monkeypatch, gh=_Gh(answer))
    app.state.work_router = _router()
    early = SimpleNamespace(token="early-token")
    stopped: list = []

    async def start_before_issue(*_args, **_kwargs):
        return early

    async def stop_before_issue(app_, started, why, *, maybe=False):
        stopped.append((started, why, maybe))
        return True

    monkeypatch.setattr(work_start, "start_before_issue", start_before_issue)
    monkeypatch.setattr(work_start, "stop_before_issue", stop_before_issue)

    response, _replay = await _post_and_settle(app)

    assert said in response.text
    # Its chat is linked either way: to give it the number, or to see it stop.
    assert 'href="/sessions/starting/early-token" data-new-work-chat' in response.text
    assert [(started, flag) for started, _why, flag in stopped] == [(early, maybe)]



def test_an_issue_the_shim_filed_starts_only_on_its_labels_read_back_from_github(
    tmp_path, monkeypatch
):
    """#3371 review: the caller names the issue; GitHub, not the caller, says its labels."""
    from datetime import UTC, datetime, timedelta

    app, _gh = _app(tmp_path, monkeypatch)
    filed: list = []
    app.state.work_router = SimpleNamespace(
        switched=lambda repository: repository == "eerovil/agentdeck",
        # As the real router: only the start label starts anything.
        note_filed=lambda *args: "agent" in args[2] and (filed.append(args) or True),
    )
    now = datetime.now(UTC)
    records = {
        78: {"state": "OPEN", "is_pr": False, "labels": ["agent"], "updatedAt": now.isoformat()},
        79: {"state": "OPEN", "is_pr": False, "labels": [], "updatedAt": now.isoformat()},
        80: {
            "state": "OPEN",
            "is_pr": False,
            "labels": ["agent"],
            "updatedAt": (now - timedelta(days=3)).isoformat(),
        },
        81: {"state": "CLOSED", "is_pr": False, "labels": ["agent"], "updatedAt": now.isoformat()},
    }
    read: list = []

    async def refresh(refs, *, limit):
        read.extend(refs)
        return 1

    monkeypatch.setattr(app.state.issues, "refresh_issue_records", refresh)
    monkeypatch.setattr(
        app.state.issues,
        "cached_issues",
        lambda refs: {ref: records[ref[1]] for ref in refs if ref[1] in records},
    )
    client = _client(app)

    def post(issue):
        return client.post("/api/works/filed", json={"issue": issue, "labels": ["agent"]}).json()

    assert post("eerovil/agentdeck#78") == {
        "issue": "https://github.com/eerovil/agentdeck/issues/78",
        "starting": True,
    }
    assert filed == [("eerovil/agentdeck", 78, ["agent"])]
    # Unlabelled on GitHub, old, or closed: nothing starts, whatever the caller claimed.
    for number in (79, 80, 81):
        assert post(f"eerovil/agentdeck#{number}")["starting"] is False
    assert len(filed) == 1
    # A repository the Work router does not run costs no GitHub read.
    assert post("eerovil/other#5")["starting"] is False
    assert ("eerovil/other", 5) not in read


def test_unticking_spec_only_leaves_its_label_off(tmp_path, monkeypatch):
    app, gh = _app(tmp_path, monkeypatch)

    _file(_client(app), spec_only=False)

    (call,) = _creates(gh)
    assert _labels(call) == ["agent"]


def test_a_project_without_a_spec_only_label_ignores_the_box(tmp_path, monkeypatch):
    app, gh = _app(tmp_path, monkeypatch)

    _file(_client(app), repository="eerovil/game-mod")

    (call,) = _creates(gh)
    assert _labels(call) == ["claude"] and call[2] == "eerovil/game-mod"


def test_the_repository_is_matched_without_regard_to_case(tmp_path, monkeypatch):
    app, gh = _app(tmp_path, monkeypatch)

    _file(_client(app), repository="northwind/store")

    ((argv, _stdin, repo),) = _creates(gh)
    assert argv[4] == "repos/Northwind/store/issues"
    assert repo == "Northwind/store"


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"repository": "eerovil/unknown"}, "choose a repository"),
        ({"message": "   "}, "write what the Work is about"),
        ({"message": "###\n- \n"}, "write what the Work is about"),
        # GitHub's issue-body limit, not the deck's (#2677): no message cap by default.
        ({"message": "x" * 60_001}, "longer than 60000"),
        ({"action_id": "bad id!"}, "no usable action ID"),
    ],
    ids=["unknown-repo", "blank", "punctuation-only", "too-long", "bad-action-id"],
)
def test_a_request_that_cannot_be_filed_is_refused_before_anything_is_written(
    tmp_path, monkeypatch, fields, reason
):
    app, gh = _app(tmp_path, monkeypatch)

    response = _file(_client(app), **fields)

    assert response.status_code == 422 and reason in response.text
    assert 'data-new-work-receipt="refused"' in response.text
    assert gh.calls == []


@pytest.mark.parametrize(
    "headers",
    [{"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}],
    ids=["origin", "fetch-site"],
)
def test_a_cross_site_filing_is_refused(tmp_path, monkeypatch, headers):
    app, gh = _app(tmp_path, monkeypatch)

    response = _file(_client(app), **headers)

    assert response.status_code == 403 and gh.calls == []


def test_a_replayed_press_answers_with_what_it_did_and_files_once(tmp_path, monkeypatch):
    app, gh = _app(tmp_path, monkeypatch)
    client = _client(app)

    first = _file(client)
    again = _file(client)

    assert first.status_code == again.status_code == 200
    assert "Already filed" in again.text
    assert "https://github.com/eerovil/agentdeck/issues/77" in again.text
    assert len(_creates(gh)) == 1


def test_an_action_id_reused_for_another_repository_is_refused(tmp_path, monkeypatch):
    app, gh = _app(tmp_path, monkeypatch)
    client = _client(app)
    _file(client)

    response = _file(client, repository="Northwind/store")

    assert response.status_code == 409 and "already used" in response.text
    assert len(_creates(gh)) == 1


def test_a_refused_authority_files_nothing_and_may_be_filed_again(tmp_path, monkeypatch):
    app, gh = _app(
        tmp_path, monkeypatch,
        gh=_Gh(gh_cli.GitHubRefusal("github: no GitHub authority configured for owner x")),
    )
    client = _client(app)

    response = _file(client)

    assert response.status_code == 409 and "no GitHub authority" in response.text
    assert "X-AgentDeck-New-Work" not in response.headers
    (press,) = app.state.control_db.load_work_actions("new-work:eerovil/agentdeck")
    assert press.state == "failed"
    # A new press is a new id, and files.
    assert _file(client, action_id="press-0002").status_code == 200


def test_a_label_github_does_not_know_is_a_definite_failure(tmp_path, monkeypatch):
    app, _gh = _app(
        tmp_path, monkeypatch,
        gh=_Gh((1, b"", b"could not add label: 'spec-only' not found\n")),
    )

    response = _file(_client(app))

    assert response.status_code == 409 and "could not add label" in response.text
    (press,) = app.state.control_db.load_work_actions("new-work:eerovil/agentdeck")
    assert press.state == "failed"


@pytest.mark.parametrize(
    "answer",
    [TimeoutError(), (1, b"", b"HTTP 502: Bad Gateway\n")],
    ids=["timeout", "server-error"],
)
def test_a_filing_with_no_answer_either_way_is_never_made_again(tmp_path, monkeypatch, answer):
    app, gh = _app(tmp_path, monkeypatch, gh=_Gh(answer))
    client = _client(app)

    response = _file(client)

    assert response.status_code == 202 and 'data-new-work-receipt="uncertain"' in response.text
    assert "May have been filed" in response.text
    assert "https://github.com/eerovil/agentdeck/issues" in response.text
    (press,) = app.state.control_db.load_work_actions("new-work:eerovil/agentdeck")
    assert press.state == "uncertain" and press.outcome is None
    replay = _file(client)
    assert replay.status_code == 202 and "may already have been filed" in replay.text
    assert len(_creates(gh)) == 1


def test_a_chat_filing_links_back_to_the_chat_and_leaves_its_issues_alone(
    tmp_path, monkeypatch
):
    app, gh = _app(tmp_path, monkeypatch)
    key = "claude_code:test:sid1"
    monkeypatch.setattr(
        "agentdeck.public_origin.public_origin", lambda **_kw: "https://deck.example"
    )

    response = _file(_client(app), query=f"?from={key}")

    assert response.status_code == 200
    ((_argv, stdin, _repo),) = _creates(gh)
    assert "Started from [this chat](https://deck.example/" in json.loads(stdin)["body"]
    # The chat belongs to another Work: the new issue is not registered on it.
    assert app.state.app_state.issues_for(key) == ()


def test_a_chat_filing_without_a_public_address_leaves_the_link_out(tmp_path, monkeypatch):
    app, gh = _app(tmp_path, monkeypatch)
    monkeypatch.setattr("agentdeck.public_origin.public_origin", lambda **_kw: "")

    _file(_client(app), query="?from=claude_code:main:unknown")

    ((_argv, stdin, _repo),) = _creates(gh)
    assert "Started from" not in json.loads(stdin)["body"]


# --- attachments ----------------------------------------------------------------------------------

def test_attachments_upload_one_after_another_before_the_issue_and_are_pinned(
    tmp_path, monkeypatch
):
    app, gh = _app(tmp_path, monkeypatch)
    files = [
        ("images", ("image.png", PNG, "image/png")),
        ("images", ("image.png", PNG, "image/png")),
        ("images", ("notes?.txt", b"hello", "text/plain")),
    ]

    response = _file(_client(app), files=files)

    assert response.status_code == 200
    kinds = [call[0][3] for call in gh.calls]
    assert kinds == ["PUT", "PUT", "PUT", "POST"]
    paths = [call[0][4] for call in _uploads(gh)]
    assert paths == [
        "repos/eerovil/agent-evidence/contents/agentdeck/new-work/press-0001/image.png",
        "repos/eerovil/agent-evidence/contents/agentdeck/new-work/press-0001/image-2.png",
        "repos/eerovil/agent-evidence/contents/agentdeck/new-work/press-0001/notes%3F.txt",
    ]
    for argv, stdin, repo in _uploads(gh):
        # The file travels as JSON on stdin, never on the command line.
        assert argv[:4] == ["/usr/bin/gh", "api", "-X", "PUT"] and argv[-2:] == ["--input", "-"]
        assert repo == "eerovil/agent-evidence"
        payload = json.loads(stdin)
        assert base64.b64decode(payload["content"]) in (PNG, b"hello")
        assert all(len(part) < 1000 for part in argv)
    ((_argv, body, _repo),) = _creates(gh)
    body = json.loads(body)["body"]
    assert "![image.png](https://github.com/eerovil/agent-evidence/blob/commit1/" in body
    assert "![image-2.png](https://github.com/eerovil/agent-evidence/blob/commit2/" in body
    assert "- [notes?.txt](https://github.com/eerovil/agent-evidence/blob/commit3/" in body
    assert "git/blobs/blob3 --jq .content" in body


def test_trailhead_attachments_go_to_outdoors_own_evidence_repository(tmp_path, monkeypatch):
    app, gh = _app(tmp_path, monkeypatch)

    _file(
        _client(app),
        repository="Northwind/store",
        files=[("images", ("shot.png", PNG, "image/png"))],
    )

    ((argv, _stdin, repo),) = _uploads(gh)
    assert argv[4].startswith("repos/Northwind/issues/contents/store/new-work/")
    assert repo == "Northwind/issues"


def test_files_for_a_project_with_no_evidence_repository_are_refused(tmp_path, monkeypatch):
    app, gh = _app(tmp_path, monkeypatch)

    response = _file(
        _client(app),
        repository="eerovil/game-mod",
        files=[("images", ("shot.png", PNG, "image/png"))],
    )

    assert response.status_code == 422 and "no evidence repository" in response.text
    assert gh.calls == []


def test_a_failed_upload_files_nothing_and_says_which_file(tmp_path, monkeypatch):
    app, gh = _app(tmp_path, monkeypatch, gh=_Gh((1, b"", b"HTTP 422: Invalid request\n")))

    response = _file(_client(app), files=[("images", ("shot.png", PNG, "image/png"))])

    assert response.status_code == 409
    assert "shot.png could not be uploaded" in response.text
    assert _creates(gh) == []
    (press,) = app.state.control_db.load_work_actions("new-work:eerovil/agentdeck")
    assert press.state == "failed"


def test_too_many_files_are_refused_before_any_upload(tmp_path, monkeypatch):
    app, gh = _app(tmp_path, monkeypatch)
    files = [("images", (f"f{index}.txt", b"x", "text/plain")) for index in range(5)]

    response = _file(_client(app), files=files)

    assert response.status_code == 422 and gh.calls == []


# --- the form on the pages ------------------------------------------------------------------------

def test_the_front_page_offers_a_closed_new_work_form(tmp_path, monkeypatch):
    app, _gh = _app(tmp_path, monkeypatch)

    page = _client(app).get("/")

    assert page.status_code == 200
    assert '<details class="new-chat new-work" id="new-work">' in page.text
    assert 'data-agentdeck-action="new_work"' in page.text
    assert 'value="eerovil/agentdeck"' in page.text
    assert 'data-evidence="Northwind/issues"' in page.text
    # It guesses its project from what is typed (#3367); a chat's own form does not.
    assert "data-guess-project" in page.text
    assert "data-guess-project" not in _client(app).get("/sessions/claude_code:test:sid1").text


async def test_haiku_picks_a_new_works_project_only_from_the_offered_ones(tmp_path, monkeypatch):
    from agentdeck import guess_cli

    app, _gh = _app(tmp_path, monkeypatch)
    replies = ["northwind/STORE", "eerovil/somewhere-else"]
    prompts: list[str] = []

    async def runner(account, prompt):
        prompts.append(prompt)
        return replies.pop(0)

    monkeypatch.setattr(guess_cli, "run_claude", runner)
    text = "The checkout page shows the wrong shipping price for Finland"
    client = _client(app)

    def ask(words):
        return client.post("/works/new/guess-project", data={"text": words}).json()

    assert ask(text) == {"repository": "Northwind/store"}
    assert "- eerovil/agentdeck (agentdeck)" in prompts[0] and text in prompts[0]
    # An answer the form does not offer moves nothing.
    assert ask(text) == {"repository": ""}
    # Too little typed, or guessing switched off: no call at all.
    assert ask("fix it") == {"repository": ""}
    app.state.db.record_guessing_enabled(False)
    assert ask(text) == {"repository": ""}
    assert len(prompts) == 2
    assert "data-guess-project" not in client.get("/").text


def test_no_form_without_a_poller_repository(tmp_path, monkeypatch):
    app, _gh = _app(tmp_path, monkeypatch, board=BoardView())

    page = _client(app).get("/")

    assert 'class="new-chat new-work"' not in page.text


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_front_page_form_files_keeps_words_on_failure_and_follows_the_project(
    tmp_path, monkeypatch
):
    """Closed by default, attachments follow the chosen project, and only "Filed" clears it."""
    import os

    from conftest import async_playwright
    from test_web import _live_server

    gh = _Gh(
        gh_cli.GitHubRefusal("github: no GitHub authority configured for owner eerovil"),
    )
    app, gh = _app(tmp_path, monkeypatch, gh=gh)
    await app.state.work_overview.refresh()
    shots = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.goto(base + "/")
        box = page.locator("details#new-work")
        summary = box.locator("summary")
        await summary.wait_for()
        # Shut, it is one line: the page is no taller than its summary.
        assert await box.get_attribute("open") is None
        closed = await box.evaluate("el => el.getBoundingClientRect().height")
        assert closed <= await summary.evaluate("el => el.getBoundingClientRect().height") + 3

        await summary.click()
        form = box.locator("form")
        message = form.locator('textarea[name="message"]')
        repository = form.locator('select[name="repository"]')
        attach = form.locator("[data-new-work-attach]")
        assert await repository.input_value() == "eerovil/agentdeck"
        await repository.select_option("eerovil/agentdeck")
        assert await attach.is_visible()
        await form.locator('input[name="images"]').set_input_files(
            {"name": "shot.png", "mimeType": "image/png", "buffer": PNG}
        )
        await form.locator(".paste-preview").wait_for()
        # A project with nowhere to keep files hides Attach and drops the picked file.
        await repository.select_option("eerovil/game-mod")
        assert await attach.is_hidden()
        assert await form.locator(".paste-preview").count() == 0
        assert await form.locator("[data-new-work-spec-only]").is_hidden()
        await repository.select_option("eerovil/agentdeck")
        await message.fill("Usage chart labels overlap on phone")

        # A refusal answers in words and keeps what was typed.
        await form.locator('button[type="submit"]').click()
        result = box.locator("[data-new-work-result]")
        await result.locator('[data-new-work-receipt="refused"]').wait_for()
        assert "no GitHub authority" in await result.inner_text()
        assert await message.input_value() == "Usage chart labels overlap on phone"

        # No answer at all may still have filed: say so, keep the words.
        await page.route("**/works/new", lambda route: route.abort())
        await form.locator('button[type="submit"]').click()
        await result.locator('[data-new-work-receipt="uncertain"]').wait_for()
        assert "may have been filed" in await result.inner_text()
        assert await message.input_value() == "Usage chart labels overlap on phone"
        await page.unroute("**/works/new")

        # Filed: the link, a cleared form, and this repository remembered for next time.
        await form.locator('button[type="submit"]').click()
        await result.locator('[data-new-work-receipt="sent"]').wait_for()
        assert "Filed eerovil/agentdeck#77" in await result.inner_text()
        assert await message.input_value() == ""
        assert await page.evaluate(
            "() => localStorage.getItem('agentdeck:new-work:last-repository')"
        ) == "eerovil/agentdeck"
        if shots:
            await box.screenshot(path=os.path.join(shots, "new-work-front-filed.png"))
        assert len(_creates(gh)) == 2

        await page.reload()
        await page.locator("details#new-work summary").click()
        assert await page.locator(
            'details#new-work select[name="repository"]'
        ).input_value() == "eerovil/agentdeck"

        # The project is guessed from the words (#3367): a project named in them, for
        # free, else Haiku's pick once typing pauses; never over a project picked by hand.
        select = page.locator('details#new-work select[name="repository"]')
        message = page.locator('details#new-work textarea[name="message"]')
        guessed_note = page.locator("details#new-work [data-new-work-guessed]")
        # A bare repository name is a common word ("issues"), so it decides nothing.
        await message.fill("Tidy the issues")
        await page.wait_for_timeout(1300)
        assert await select.input_value() == "eerovil/agentdeck"
        assert await guessed_note.is_hidden()
        await message.fill("The Minecraft server forgets the world on restart")
        await page.wait_for_function(
            "document.querySelector('details#new-work select').value"
            " === 'eerovil/game-mod'"
        )
        assert await guessed_note.is_visible()
        asked = []

        async def haiku(route):
            asked.append(route.request.post_data)
            await route.fulfill(json={"repository": "Northwind/store"})

        await page.route("**/works/new/guess-project", haiku)
        await message.fill("The checkout shows the wrong shipping price for Finland")
        await page.wait_for_function(
            "document.querySelector('details#new-work select').value"
            " === 'Northwind/store'"
        )
        assert len(asked) == 1
        await select.select_option("eerovil/agentdeck")
        assert await guessed_note.is_hidden()
        await message.fill("Minecraft too: the checkout shows the wrong price")
        await page.wait_for_timeout(1300)
        assert await select.input_value() == "eerovil/agentdeck"
        assert len(asked) == 1
        await page.unroute("**/works/new/guess-project")
        await message.fill("")

        # "Guess for me" switched off: a named project moves nothing (#3367).
        app.state.db.record_guessing_enabled(False)
        await page.reload()
        await page.locator("details#new-work summary").click()
        assert await select.input_value() == "eerovil/agentdeck"
        await message.fill("The Minecraft server forgets the world on restart")
        await page.wait_for_timeout(1300)
        assert await select.input_value() == "eerovil/agentdeck"
        assert await guessed_note.is_hidden()
        await message.fill("")
        app.state.db.record_guessing_enabled(True)
        await page.reload()
        await page.locator("details#new-work summary").click()

        # A list filtered to one project files there (#3253); a pick already in
        # that project stays.
        await page.evaluate("""() => {
          const main = document.querySelector('.front-page main');
          for (const name of ['trailhead', 'minecraft']) {
            const row = document.createElement('article');
            row.setAttribute('data-work-project', name);
            main.appendChild(row);
          }
          window.AgentDeckFront.apply();
        }""")
        select = page.locator('details#new-work select[name="repository"]')
        await page.locator('[data-project-tab="trailhead"]').click()
        assert await select.input_value() == "Northwind/issues"
        if shots:
            await page.screenshot(path=os.path.join(shots, "new-work-follows-filter.png"))
        await select.select_option("Northwind/store")
        await page.locator("details#new-work summary").click()
        await page.locator("details#new-work summary").click()
        assert await select.input_value() == "Northwind/store"
        await page.locator('[data-project-tab="minecraft"]').click()
        assert await select.input_value() == "eerovil/game-mod"
        assert await page.locator("details#new-work [data-new-work-attach]").is_hidden()
        await browser.close()


def test_the_chat_page_offers_new_work_from_this_chat_on_its_issues_repository(
    tmp_path, monkeypatch
):
    app, _gh = _app(tmp_path, monkeypatch)
    key = "claude_code:test:sid1"
    app.state.app_state.add_issue(key, IssueRef("Northwind/store", 9))

    page = _client(app).get(f"/sessions/{key}")

    assert page.status_code == 200
    controls = page.text.split('<div class="detail-controls">', 1)[1]
    box = controls.split('<details class="new-chat new-work" id="session-new-work">', 1)[1]
    box = box.split("</details>", 1)[0]
    assert "New work from this chat" in box
    assert 'hx-post="/works/new?from=claude_code%3Atest%3Asid1"' in box
    assert 'data-new-work-default="Northwind/store"' in box
    assert 'value="Northwind/store" data-project="trailhead"' in box


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_chat_page_control_is_one_chip_until_opened(tmp_path, monkeypatch):
    import os

    from conftest import async_playwright
    from test_web import _live_server

    app, gh = _app(tmp_path, monkeypatch)
    key = "claude_code:test:sid1"
    app.state.app_state.add_issue(key, IssueRef("eerovil/agentdeck", 2400))
    shots = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.goto(base + f"/sessions/{key}")
        box = page.locator("details#session-new-work")
        await box.locator("summary").wait_for()
        # Closed, it is no taller than the other controls in its row.
        heights = await page.locator(".detail-controls > *").evaluate_all(
            "els => els.map(el => el.getBoundingClientRect().height)"
        )
        mine = await box.evaluate("el => el.getBoundingClientRect().height")
        assert mine <= max(height for height in heights if height != mine) + 4
        if shots:
            await page.locator(".detail-controls").screenshot(
                path=os.path.join(shots, "new-work-chat-closed.png")
            )

        await box.locator("summary").click()
        form = box.locator("form")
        assert await form.locator('select[name="repository"]').input_value() == (
            "eerovil/agentdeck"
        )
        await form.locator('textarea[name="message"]').fill(
            "Webhook tailer re-reads the whole stream after restart"
        )
        await form.locator('button[type="submit"]').click()
        await box.locator('[data-new-work-receipt="sent"]').wait_for()
        if shots:
            await box.screenshot(path=os.path.join(shots, "new-work-chat-filed.png"))
        ((_argv, stdin, _repo),) = _creates(gh)
        assert json.loads(stdin)["body"].startswith(
            "Webhook tailer re-reads the whole stream after restart"
        )
        await browser.close()
