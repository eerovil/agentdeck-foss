"""Every path that starts a leg elsewhere keeps the chat it came from (#1267).

Chat identity was built on the one flow a person drives by hand — the Continue
control (#1263) — and the deck has three more that start a session on another
account without anyone pressing anything: a poller follow-up chosen on headroom,
a Builder handover, and the Secretary desk moving off an account with no
headroom left. Each of those used to mint an unreserved token, which lands the
successor as a chat of its own; the same piece of work then read as several
unrelated conversations on the deck, and the URL published for it named a leg
that had stopped answering.

The tests here are the parity proof: the same fence, on every flow, including
across providers. Two refusals are deliberate and tested too — a delivery that
resumes the very leg it continues is not a switch, and a chat whose successor is
already held by a *live* start keeps it rather than forking. A fence whose start
died is stepped over on both flows now, not only on the button (#1620, #2127).
"""

import sqlite3
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck.app import create_app
from agentdeck.chat_identity import published_chat_reference
from agentdeck.config import (
    AccountConfig,
    AppConfig,
    HistoryConfig,
    InjectConfig,
)
from agentdeck.models import (
    Capability,
    InjectResult,
    Session,
    SessionStatus,
)

pytestmark = pytest.mark.anyio

CLAUDE_KEY = "claude_code:main:leg-one"


def _app(tmp_path, handle):
    """Two accounts on two providers, and a fake worker runtime behind them."""
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        inject=InjectConfig(enabled=True, max_message_chars=200),
        accounts=[
            AccountConfig(
                provider="claude_code", label="main", config_dir=str(tmp_path / "main")
            ),
            AccountConfig(provider="codex", label="alt", config_dir=str(tmp_path / "alt")),
        ],
    )
    app = create_app(config)
    app.state.runtime_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runtime"
    )
    app.state.app_state.update_session(
        Session(
            key=CLAUDE_KEY,
            account_key="claude_code:main",
            session_id="leg-one",
            status=SessionStatus.IDLE,
            title="Card work",
            cwd=tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
            last_activity=datetime.now(UTC),
        )
    )
    app.state.budget_db.ensure_session_chats((CLAUDE_KEY,))
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _register(app, key: str, account_key: str, cwd) -> None:
    """Let the deck's scan catch up with a leg the provider just started."""
    app.state.app_state.update_session(
        Session(
            key=key,
            account_key=account_key,
            session_id=key.rsplit(":", 1)[-1],
            status=SessionStatus.IDLE,
            title="Card work",
            cwd=cwd,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
            last_activity=datetime.now(UTC),
        )
    )


def _runtime(session_id: str, seen: list[dict]):
    def handle(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"accepted": True, "action": "spawned", "session_id": session_id}
        )

    return handle


async def _deliver(app, path: str, continues: str, issue_ref: str | None = None):
    body = {
        "key": "eerovil/agentdeck#1",
        "message": "carry on",
        "continues_session_url": continues,
    }
    if issue_ref is not None:
        body["issue_ref"] = issue_ref
    async with _client(app) as client:
        return await client.post(f"/api/workers/accounts/{path}/deliver", json=body)


# --- what a published URL names -------------------------------------------


def test_either_spelling_of_a_published_chat_url_names_the_same_thing():
    """Both spellings are live: old comments carry one, new ones the other."""
    assert published_chat_reference("https://deck.example/chats/abc123") == ("chat", "abc123")
    assert published_chat_reference(
        "https://deck.example/sessions/claude_code%3Amain%3Aleg-one"
    ) == ("session", "claude_code:main:leg-one")
    # A transcript URL is still that chat's URL — the brief hands workers one.
    assert published_chat_reference("https://deck.example/chats/abc/transcript.json") == (
        "chat",
        "abc",
    )
    assert published_chat_reference("https://github.com/eerovil/agentdeck/issues/1") is None
    assert published_chat_reference("") is None


# --- flow 3: a poller/worker follow-up on another account -------------------


async def test_a_worker_follow_up_on_another_account_is_the_next_leg(tmp_path):
    """The headline: the same card's next turn, on another provider, one chat."""
    seen: list[dict] = []
    app = _app(tmp_path, _runtime("leg-two", seen))
    chat = app.state.budget_db.chat_for_session(CLAUDE_KEY)

    response = await _deliver(
        app, "codex/alt", f"https://deck.example/chats/{chat.chat_id}"
    )

    assert response.status_code == 200
    linked = app.state.budget_db.chat_for_session("codex:alt:leg-two")
    assert linked.chat_id == chat.chat_id
    assert linked.legs == (CLAUDE_KEY, "codex:alt:leg-two")
    # Deck-side identity, not part of the delivery: the runtime never sees it.
    assert "continues_session_url" not in seen[0]


async def test_a_follow_up_through_an_old_leg_url_still_finds_the_chat(tmp_path):
    """Briefs carry whichever URL was canonical when they were written."""
    seen: list[dict] = []
    app = _app(tmp_path, _runtime("leg-two", seen))
    chat = app.state.budget_db.chat_for_session(CLAUDE_KEY)

    await _deliver(app, "codex/alt", f"https://deck.example/sessions/{CLAUDE_KEY}")

    assert app.state.budget_db.chat_for_session("codex:alt:leg-two").chat_id == chat.chat_id


async def test_a_delivery_that_resumes_its_own_leg_is_not_a_switch(tmp_path):
    """Most follow-ups land back where they were. That is not a new leg."""
    seen: list[dict] = []
    app = _app(tmp_path, _runtime("leg-one", seen))
    chat = app.state.budget_db.chat_for_session(CLAUDE_KEY)

    response = await _deliver(
        app, "claude_code/main", f"https://deck.example/chats/{chat.chat_id}"
    )

    assert response.status_code == 200
    assert app.state.budget_db.chat_for_session(CLAUDE_KEY).legs == (CLAUDE_KEY,)
    # And the reservation it took is released, so the next real move still links.
    app.state.runtime_http = httpx.AsyncClient(
        transport=httpx.MockTransport(_runtime("leg-two", seen)), base_url="http://runtime"
    )
    await _deliver(app, "codex/alt", f"https://deck.example/chats/{chat.chat_id}")
    assert app.state.budget_db.chat_for_session("codex:alt:leg-two").legs == (
        CLAUDE_KEY,
        "codex:alt:leg-two",
    )


async def test_a_follow_up_naming_no_chat_this_deck_knows_starts_its_own(tmp_path):
    """An unresolvable URL is a reason to start a chat, never to lose the turn."""
    seen: list[dict] = []
    app = _app(tmp_path, _runtime("leg-two", seen))

    response = await _deliver(app, "codex/alt", "https://deck.example/chats/not-a-chat")

    assert response.status_code == 200
    assert app.state.budget_db.chat_for_session("codex:alt:leg-two").legs == (
        "codex:alt:leg-two",
    )


async def test_a_third_leg_joins_the_end_of_the_chain_not_the_middle(tmp_path):
    """A dispatcher holding the *first* leg's URL still extends the chain's end."""
    seen: list[dict] = []
    app = _app(tmp_path, _runtime("leg-two", seen))
    chat = app.state.budget_db.chat_for_session(CLAUDE_KEY)
    await _deliver(app, "codex/alt", f"https://deck.example/chats/{chat.chat_id}")
    _register(app, "codex:alt:leg-two", "codex:alt", tmp_path)

    app.state.runtime_http = httpx.AsyncClient(
        transport=httpx.MockTransport(_runtime("leg-three", seen)), base_url="http://runtime"
    )
    response = await _deliver(app, "codex/alt", f"https://deck.example/sessions/{CLAUDE_KEY}")

    assert response.status_code == 200
    assert app.state.budget_db.chat_for_session("codex:alt:leg-three").legs == (
        CLAUDE_KEY,
        "codex:alt:leg-two",
        "codex:alt:leg-three",
    )


async def test_a_fork_is_refused_without_refusing_the_turn(tmp_path):
    """V1 is linear. A second successor is refused; its work still runs."""
    seen: list[dict] = []
    app = _app(tmp_path, _runtime("leg-two", seen))
    chat = app.state.budget_db.chat_for_session(CLAUDE_KEY)
    # Somebody else already holds this chat's one successor slot.
    assert app.state.budget_db.reserve_chat_successor("other-token", CLAUDE_KEY) == "new"

    response = await _deliver(
        app, "codex/alt", f"https://deck.example/chats/{chat.chat_id}"
    )

    assert response.status_code == 200
    assert app.state.budget_db.chat_for_session("codex:alt:leg-two").legs == (
        "codex:alt:leg-two",
    )
    # And the slot the other continuation reserved is still theirs.
    assert app.state.budget_db.chat_for_session(CLAUDE_KEY).legs == (CLAUDE_KEY,)


async def test_a_delivery_steps_over_a_fence_whose_start_died(tmp_path):
    """A corpse of a fence must not split a card in two forever (#1620, #2127).

    The Continue button has always stepped over a reservation whose start is
    gone. The poller delivery did not, so one failed spawn left every later
    account switch on that card starting a chat of its own.
    """
    seen: list[dict] = []
    app = _app(tmp_path, _runtime("leg-two", seen))
    chat = app.state.budget_db.chat_for_session(CLAUDE_KEY)
    assert app.state.budget_db.reserve_chat_successor("in-flight", CLAUDE_KEY) == "new"
    app.state.injector.started_chat = lambda token: (
        _dead_start("failed") if token == "in-flight" else None
    )

    response = await _deliver(
        app, "codex/alt", f"https://deck.example/chats/{chat.chat_id}"
    )

    assert response.status_code == 200
    assert app.state.budget_db.chat_for_session("codex:alt:leg-two").legs == (
        CLAUDE_KEY,
        "codex:alt:leg-two",
    )


async def test_a_delivery_leaves_a_fence_whose_start_is_still_running(tmp_path):
    """Stepping over a dead fence must not mean stepping over a live one."""
    seen: list[dict] = []
    app = _app(tmp_path, _runtime("leg-two", seen))
    chat = app.state.budget_db.chat_for_session(CLAUDE_KEY)
    assert app.state.budget_db.reserve_chat_successor("in-flight", CLAUDE_KEY) == "new"
    app.state.injector.started_chat = lambda token: (
        _dead_start("accepted") if token == "in-flight" else None
    )

    response = await _deliver(
        app, "codex/alt", f"https://deck.example/chats/{chat.chat_id}"
    )

    assert response.status_code == 200
    # The turn still ran; it just ran as its own chat, and the slot is still theirs.
    assert app.state.budget_db.chat_for_session("codex:alt:leg-two").legs == (
        "codex:alt:leg-two",
    )
    assert app.state.budget_db.pending_chat_successor(CLAUDE_KEY)[0] == "in-flight"


async def test_the_fence_never_promotes_a_leg_the_caller_did_not_name(tmp_path):
    """A frozen leg must fence itself, not wherever the chat has moved on to.

    A replayed context delivery reserves against the leg it originally decided
    about. If the helper quietly resolved that to the chat's newest leg it would
    append a leg that delivery never decided about, so the store's "not the
    newest leg" refusal has to survive the step-over (#2127).
    """
    from agentdeck.web.chat_successor import reserve_chat_successor

    seen: list[dict] = []
    app = _app(tmp_path, _runtime("leg-two", seen))
    chat = app.state.budget_db.chat_for_session(CLAUDE_KEY)
    await _deliver(app, "codex/alt", f"https://deck.example/chats/{chat.chat_id}")
    _register(app, "codex:alt:leg-two", "codex:alt", tmp_path)
    request = type("R", (), {"app": app})()

    # CLAUDE_KEY is now the *first* of two legs, so it is nobody's fence to take.
    assert reserve_chat_successor(request, CLAUDE_KEY, "late-token") == "conflict"
    assert app.state.budget_db.chat_for_session(CLAUDE_KEY).legs == (
        CLAUDE_KEY,
        "codex:alt:leg-two",
    )


async def test_two_unlinked_sessions_on_one_card_each_keep_the_card(tmp_path):
    """The fallback for when the linking itself fails (#2127, item 3).

    Nothing is pre-registered here: both sessions are created through the real
    delivery path, neither names a chat to continue, so they are two unrelated
    chats — exactly the state this card was filed about. Each must still answer
    "which card was this?" on its own, or the durable relation is worth nothing
    precisely when it is needed.
    """
    from agentdeck.models import IssueRef

    card = "Northwind/abacus#1913"
    seen: list[dict] = []
    app = _app(tmp_path, _runtime("codex-leg", seen))
    state = app.state.app_state

    await _deliver(app, "codex/alt", "", issue_ref=card)
    _register(app, "codex:alt:codex-leg", "codex:alt", tmp_path)
    app.state.runtime_http = httpx.AsyncClient(
        transport=httpx.MockTransport(_runtime("claude-leg", seen)), base_url="http://runtime"
    )
    await _deliver(app, "claude_code/main", "", issue_ref=card)
    _register(app, "claude_code:main:claude-leg", "claude_code:main", tmp_path)

    # Two chats, not one — the link was never asked for.
    assert app.state.budget_db.chat_for_session("codex:alt:codex-leg").legs == (
        "codex:alt:codex-leg",
    )
    assert app.state.budget_db.chat_for_session("claude_code:main:claude-leg").legs == (
        "claude_code:main:claude-leg",
    )
    # And each one still names the card, from its own stored registration.
    expected = (IssueRef("Northwind/abacus", 1913),)
    assert state.issues_for("codex:alt:codex-leg") == expected
    assert state.issues_for("claude_code:main:claude-leg") == expected


async def test_a_card_reference_the_deck_cannot_qualify_registers_nothing(tmp_path):
    """A wrong relation points a reader at another repository. Prefer none."""
    seen: list[dict] = []
    app = _app(tmp_path, _runtime("codex-leg", seen))
    state = app.state.app_state

    for bad in ("abacus#1913", "", "   ", "not a ref", "owner/repo", "owner/repo#x"):
        await _deliver(app, "codex/alt", "", issue_ref=bad)
        assert state.issues_for("codex:alt:codex-leg") == (), bad
    # Omitting the field entirely is the pre-#2127 caller, and must also be safe.
    await _deliver(app, "codex/alt", "")
    assert state.issues_for("codex:alt:codex-leg") == ()


async def test_registering_the_card_twice_does_not_duplicate_or_clobber(tmp_path):
    """Every dispatch on a card re-sends it, and a worker registers its own too."""
    from agentdeck.models import IssueRef

    card = "Northwind/abacus#1913"
    other = IssueRef("eerovil/agentdeck", 2127)
    seen: list[dict] = []
    app = _app(tmp_path, _runtime("codex-leg", seen))
    state = app.state.app_state

    await _deliver(app, "codex/alt", "", issue_ref=card)
    state.add_issue("codex:alt:codex-leg", other)
    await _deliver(app, "codex/alt", "", issue_ref=card)

    assert state.issues_for("codex:alt:codex-leg") == (
        IssueRef("Northwind/abacus", 1913),
        other,
    )


async def test_the_card_a_chat_works_is_answerable_from_either_leg(tmp_path):
    """Which chat worked this card must survive the account switch (#2127).

    The whole point of keeping the card one chat is that its work reads as one
    thing. The issue list is keyed by chat, not by leg, so the card registered
    while the work was on Claude answers from the Codex leg too — and the Codex
    leg's own registration answers from the Claude one.
    """
    from agentdeck.models import IssueRef

    seen: list[dict] = []
    app = _app(tmp_path, _runtime("leg-two", seen))
    state = app.state.app_state
    chat = app.state.budget_db.chat_for_session(CLAUDE_KEY)
    card = IssueRef("Northwind/abacus", 1913)
    state.add_issue(CLAUDE_KEY, card)

    await _deliver(app, "codex/alt", f"https://deck.example/chats/{chat.chat_id}")
    _register(app, "codex:alt:leg-two", "codex:alt", tmp_path)

    assert state.issues_for("codex:alt:leg-two") == (card,)
    assert state.issues_for(CLAUDE_KEY) == (card,)


async def test_an_uncertain_delivery_leaves_no_reservation_behind(tmp_path):
    """A runtime that names no session started nothing to link (#1267)."""
    seen: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True, "action": "queued"})

    app = _app(tmp_path, handle)
    chat = app.state.budget_db.chat_for_session(CLAUDE_KEY)
    await _deliver(app, "codex/alt", f"https://deck.example/chats/{chat.chat_id}")

    # The next attempt is not told this chat already has a successor.
    app.state.runtime_http = httpx.AsyncClient(
        transport=httpx.MockTransport(_runtime("leg-two", seen)), base_url="http://runtime"
    )
    await _deliver(app, "codex/alt", f"https://deck.example/chats/{chat.chat_id}")
    assert app.state.budget_db.chat_for_session("codex:alt:leg-two").legs == (
        CLAUDE_KEY,
        "codex:alt:leg-two",
    )


# --- flow 4: the cross-provider chain, end to end ---------------------------


async def test_an_old_claude_url_controls_the_codex_leg_the_chat_moved_to(tmp_path):
    """Old alias → canonical chat → control on the provider it is on *now*.

    The acceptance case for a chained chat that crossed providers: the URL a
    worker published while the work was on Claude still addresses the chat, the
    canonical chat URL resolves to the same conversation, and a reply through
    either one reaches the Codex session that is actually live.
    """
    seen: list[dict] = []
    app = _app(tmp_path, _runtime("leg-two", seen))
    state = app.state.app_state
    chat = app.state.budget_db.chat_for_session(CLAUDE_KEY)

    await _deliver(app, "codex/alt", f"https://deck.example/chats/{chat.chat_id}")
    _register(app, "codex:alt:leg-two", "codex:alt", tmp_path)

    delivered = []

    async def start(account, session, provider, message, images=None, **kwargs):
        delivered.append((account.key, session.key, message))
        return InjectResult(True)

    app.state.injector.start = start

    async with _client(app) as client:
        old_url = await client.post(
            f"/sessions/{CLAUDE_KEY}/inject",
            data={"message": "through the old url"},
            headers={"origin": "http://test"},
        )
        canonical = await client.get(f"/chats/{chat.chat_id}")
        # A Work row with no leg in its snapshot links the chat id here (#3240).
        by_chat_id = await client.get(f"/sessions/{chat.chat_id}")
        unknown = await client.get("/sessions/" + "0" * 32)

    assert old_url.status_code == 202
    assert by_chat_id.status_code == 200
    assert 'data-session-key="codex:alt:leg-two"' in by_chat_id.text
    assert unknown.status_code == 404
    assert delivered == [("codex:alt", "codex:alt:leg-two", "through the old url")]
    assert canonical.status_code == 200
    assert state.chat_current_session_key(CLAUDE_KEY) == "codex:alt:leg-two"


# --- the Continue control grows the chain, not the working leg (#1582) -------


def _hand_back(app, tmp_path):
    """A chat that moved on and was then handed back to the account it left.

    Its chain ends at leg two while leg one is the leg actually running, which
    is the ordinary shape #1501 describes and not a broken state.
    """
    app.state.budget_db.append_chat_leg(CLAUDE_KEY, "codex:alt:leg-two")
    _register(app, "codex:alt:leg-two", "codex:alt", tmp_path)
    app.state.app_state.update_session(
        Session(
            key=CLAUDE_KEY,
            account_key="claude_code:main",
            session_id="leg-one",
            status=SessionStatus.LIVE,
            title="Card work",
            cwd=tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
            last_activity=datetime.now(UTC),
        )
    )


async def test_continue_extends_a_chat_working_on_an_earlier_leg(tmp_path, monkeypatch):
    """Pressing Continue on a handed-back chat starts a chat, not a refusal.

    The press resolves the leg that is *working*, which is right for delivering
    a message and wrong for growing the chain. Reserving succession from that
    leg refused the press outright, permanently, for any chat in this shape —
    and the refusal named a chat it gave no way to reach (#1582).
    """
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "can_start_session", lambda account: True)
    app = _app(tmp_path, _runtime("leg-three", []))
    _hand_back(app, tmp_path)
    assert app.state.app_state.chat_current_session_key(CLAUDE_KEY) == CLAUDE_KEY
    # The chat is a Work's agent: its new leg keeps the Work role (#3362).
    app.state.app_state.sessions[CLAUDE_KEY].owned_role = "work"
    started = {}

    async def start_new(*args, **kwargs):
        started.update(kwargs)
        return InjectResult(True)

    app.state.injector.start_new = start_new

    async with _client(app) as client:
        response = await client.post(
            f"/sessions/{CLAUDE_KEY}/continue",
            data={"account_key": "codex:alt"},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 202, response.text
    assert started["role"] == "work"
    # The reservation sits on the end of the chain, so the new leg lands there.
    assert (
        app.state.budget_db.reserve_chat_successor("another", "codex:alt:leg-two")
        == "conflict"
    )


async def test_a_continue_refused_for_a_start_in_flight_says_so_with_a_way_out(
    tmp_path, monkeypatch
):
    """The one conflict left is honest about itself and offers somewhere to go.

    A chat whose chain end is already reserved has a new chat on the way. That
    is worth saying, and worth linking, because the destination only becomes
    reachable once it exists (#1582).
    """
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "can_start_session", lambda account: True)
    app = _app(tmp_path, _runtime("leg-two", []))
    chat = app.state.budget_db.chat_for_session(CLAUDE_KEY)
    assert app.state.budget_db.reserve_chat_successor("in-flight", CLAUDE_KEY) == "new"

    async with _client(app) as client:
        response = await client.post(
            f"/sessions/{CLAUDE_KEY}/continue",
            data={"account_key": "codex:alt"},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 409
    assert "A new chat for this conversation is already starting." in response.text
    assert f'href="/chats/{chat.chat_id}"' in response.text
    assert "Open its current chat instead" not in response.text


async def test_a_continue_refuses_a_chat_whose_card_was_restarted_elsewhere(
    tmp_path, monkeypatch
):
    """#2862: once the Poller handed this chat's Work to a later Run, continuing the old chat
    would put a second worker on the card. Once that later Run is over it is still refused:
    nothing would stop a chat continued from a replaced Run when the card runs again."""
    from agentdeck import run_execution as rx
    from agentdeck.db import DeliveryRoute
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "can_start_session", lambda account: True)
    app = _app(tmp_path, _runtime("leg-two", []))
    db = app.state.budget_db
    work = "github_issue:eerovil/agentdeck#2861"
    for index, run_id in enumerate(("run-1", "run-2")):
        assert db.freeze_delivery_route(
            DeliveryRoute(
                f"poller:{run_id}", "f", work, run_id, "claude_code", "main", "agentdeck#2861",
                f"2026-10-11T09:0{index}:00+00:00",
            )
        )
    db.bind_session_run(CLAUDE_KEY, work, "run-1")

    async def start_new(*args, **kwargs):
        return InjectResult(True)

    app.state.injector.start_new = start_new

    async with _client(app) as client:
        refused = await client.post(
            f"/sessions/{CLAUDE_KEY}/continue",
            data={"account_key": "codex:alt"},
            headers={"origin": "http://test"},
        )
        assert refused.status_code == 409
        assert "already restarted this card" in refused.text

        # Run 2's admission stopped run 1; run 2 has since settled too. A chat continued
        # from run 1 now would be tracked by neither, so run 3 would start beside it.
        store = rx.OutcomeStore(tmp_path / "budgets.db")
        store.record(
            rx.ExecutionOutcome(
                work, "run-1", rx.STOPPED, "superseded_by:run-2",
                stop_requested_at=1.0, observed_at=1.0,
            )
        )
        store.record(rx.ExecutionOutcome(work, "run-2", rx.ENDED, "success", observed_at=1.0))
        left_behind = await client.post(
            f"/sessions/{CLAUDE_KEY}/continue",
            data={"account_key": "codex:alt"},
            headers={"origin": "http://test"},
        )

    assert left_behind.status_code == 409
    assert "stopped or replaced the run this chat worked for" in left_behind.text


async def test_a_continue_refuses_a_chat_whose_run_was_stopped(tmp_path, monkeypatch):
    """#2862: a stopped Run reads as settled whatever its continuation does, so no later
    admission would park a chat continued from it; the latest Run's chat may go on only
    while its Run was not stopped."""
    from agentdeck import run_execution as rx
    from agentdeck.db import DeliveryRoute
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "can_start_session", lambda account: True)
    app = _app(tmp_path, _runtime("leg-two", []))
    db = app.state.budget_db
    work = "github_issue:eerovil/agentdeck#2861"
    assert db.freeze_delivery_route(
        DeliveryRoute(
            "poller:run-1", "f", work, "run-1", "claude_code", "main", "agentdeck#2861",
            "2026-10-11T09:00:00+00:00",
        )
    )
    db.bind_session_run(CLAUDE_KEY, work, "run-1")
    store = rx.OutcomeStore(tmp_path / "budgets.db")
    store.record(rx.ExecutionOutcome(work, "run-1", rx.ENDED, "usage_limit", observed_at=1.0))

    from types import SimpleNamespace

    from agentdeck.web.routes_actions import _continue_refusal

    request = SimpleNamespace(app=app)
    assert _continue_refusal(request, CLAUDE_KEY)[0] is None
    store.record(
        rx.ExecutionOutcome(
            work, "run-1", rx.STOPPED, "card_stopped", stop_requested_at=2.0, observed_at=2.0
        )
    )
    assert "stopped or replaced" in _continue_refusal(request, CLAUDE_KEY)[0]


def test_a_second_continue_refuses_a_chat_whose_card_was_restarted_elsewhere(tmp_path):
    """#2862: continuing a continued chat again still reads the Run its first leg served —
    the Continue copied it onto the new leg (#2881) — and refuses once a later Run took
    the card."""
    from agentdeck.db import DeliveryRoute

    app = _app(tmp_path, _runtime("leg-three", []))
    db = app.state.budget_db
    work = "github_issue:eerovil/agentdeck#2861"
    db.bind_session_run(CLAUDE_KEY, work, "run-1")
    assert db.reserve_chat_successor("b" * 32, CLAUDE_KEY) == "new"
    second_leg = "codex:alt:leg-two"
    db.record_session_token("b" * 32, second_leg)
    for index, run_id in enumerate(("run-1", "run-2")):
        assert db.freeze_delivery_route(
            DeliveryRoute(
                f"poller:{run_id}", "f", work, run_id, "claude_code", "main", "agentdeck#2861",
                f"2026-10-11T09:0{index}:00+00:00",
            )
        )
    assert db.latest_session_run(second_leg).run_id == "run-1"

    from types import SimpleNamespace

    from agentdeck.web.routes_actions import _continue_refusal

    assert "already restarted" in _continue_refusal(SimpleNamespace(app=app), second_leg)[0]


def test_a_continue_refuses_a_chat_whose_run_cannot_be_read(tmp_path):
    """Review of #2863: an unreadable store or a chain past the walk's cap does not prove the
    chat serves no Run, so Continue is refused rather than let through."""
    from types import SimpleNamespace

    from agentdeck.run_execution import MAX_CONTINUATIONS
    from agentdeck.web.routes_actions import _continue_refusal

    app = _app(tmp_path, _runtime("leg-two", []))
    db = app.state.budget_db
    db.bind_session_run(CLAUDE_KEY, "github_issue:eerovil/agentdeck#2861", "run-1")
    source = CLAUDE_KEY
    for index in range(MAX_CONTINUATIONS):
        token = f"{index:032d}"
        assert db.reserve_chat_successor(token, source) == "new"
        source = f"codex:alt:leg{index}"
        db.record_session_token(token, source)
    assert db.chat_run_binding(source, max_legs=1).run_id == "run-1"  # copied on (#2881)
    # Legs linked before #2881 carry no Run of their own; the walk goes back for them.
    db._write("DELETE FROM session_run_bindings WHERE session_key != ?", (CLAUDE_KEY,))

    assert db.chat_run_binding(source, max_legs=MAX_CONTINUATIONS + 1).run_id == "run-1"
    with pytest.raises(LookupError):
        db.chat_run_binding(source, max_legs=MAX_CONTINUATIONS)
    refusal, run = _continue_refusal(SimpleNamespace(app=app), source)
    assert "Could not tell" in refusal and run is None

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    db.chat_run_binding = broken
    assert "Could not tell" in _continue_refusal(SimpleNamespace(app=app), CLAUDE_KEY)[0]


# --- a fence whose start is gone is not a fence (#1620) ----------------------


def _dead_start(state: str):
    """A start record the injector still holds, whose outcome is terminal."""
    from agentdeck.inject import InjectionStatus, StartedChat

    return StartedChat(
        token="in-flight",
        account_key="codex:alt",
        cwd="/tmp",
        message="carry on",
        origin="continue",
        source_session_key=CLAUDE_KEY,
        status=InjectionStatus(state),
    )


async def test_a_continue_steps_over_a_reservation_whose_start_died(tmp_path, monkeypatch):
    """A start that never became a chat must not fence its source forever (#1620).

    The reservation is deleted only when the start is refused up front. One that
    was accepted and then failed left the row behind with nothing to claim it,
    and every later Continue on that chat answered "a new chat for this
    conversation is already starting" — permanently, and the only way out it
    offered was reloading the same dead end.
    """
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "can_start_session", lambda account: True)
    app = _app(tmp_path, _runtime("leg-two", []))
    assert app.state.budget_db.reserve_chat_successor("in-flight", CLAUDE_KEY) == "new"
    app.state.injector.started_chat = lambda token: (
        _dead_start("failed") if token == "in-flight" else None
    )

    started: list[dict] = []

    async def start_new(*args, **kwargs):
        started.append(kwargs)
        return InjectResult(True)

    app.state.injector.start_new = start_new

    async with _client(app) as client:
        response = await client.post(
            f"/sessions/{CLAUDE_KEY}/continue",
            data={"account_key": "codex:alt"},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 202, response.text
    assert len(started) == 1
    # The corpse is gone and the live press owns the fence in its place.
    assert app.state.budget_db.pending_chat_successor(CLAUDE_KEY) is not None
    assert app.state.budget_db.pending_chat_successor(CLAUDE_KEY)[0] != "in-flight"


async def test_a_reservation_this_process_cannot_name_is_live_only_briefly(tmp_path):
    """A web restart drops the start record while the spawn keeps going (#1620).

    So an unnamed reservation is not evidence of a dead start on its own — until
    the status strip's own give-up window has passed, after which nothing will
    ever claim it.
    """
    from datetime import timedelta

    from agentdeck.web.chat_successor import LOST_RECHECKS as _LOST_RECHECKS
    from agentdeck.web.chat_successor import blocking_start_is_live as _blocking_start_is_live

    app = _app(tmp_path, _runtime("leg-two", []))
    app.state.injector.started_chat = lambda token: None
    injector = app.state.injector
    now = datetime.now(UTC)

    assert _blocking_start_is_live(injector, "in-flight", now)
    assert not _blocking_start_is_live(
        injector, "in-flight", now - timedelta(seconds=_LOST_RECHECKS + 1)
    )
    # Accepted, but the provider never named a session: the status strip calls
    # that a dead end too.
    app.state.injector.started_chat = lambda token: _dead_start("complete")
    assert not _blocking_start_is_live(injector, "in-flight", now)


async def test_the_start_status_releases_the_fence_it_settles_against(tmp_path):
    """The dead end never has to be stepped over if the strip clears it (#1620)."""
    app = _app(tmp_path, _runtime("leg-two", []))
    assert app.state.budget_db.reserve_chat_successor("in-flight", CLAUDE_KEY) == "new"
    app.state.injector.started_chat = lambda token: (
        _dead_start("failed") if token == "in-flight" else None
    )

    async with _client(app) as client:
        page = await client.get("/partials/starting-chat/in-flight")

    assert page.status_code == 200
    assert app.state.budget_db.pending_chat_successor(CLAUDE_KEY) is None
