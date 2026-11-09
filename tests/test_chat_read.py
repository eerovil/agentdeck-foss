"""Reading an old chat in parts, without waking it (#4169)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck import __main__ as cli
from agentdeck import chat_read
from agentdeck.app import create_app
from agentdeck.chat_read import Query, continuation_note, last_exchange, lines_of
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig
from agentdeck.models import Capability, Session, SessionStatus, TranscriptEvent
from agentdeck.providers import PROVIDERS
from agentdeck.web.chat_successor import continuation_note as web_note

KEY = "claude_code:main:old"
WHEN = datetime(2026, 11, 7, 9, 30, tzinfo=UTC)


def _events():
    return [
        TranscriptEvent(0, "user", "Fix the login page", ts=WHEN),
        TranscriptEvent(1, "assistant", "Reading it."),
        TranscriptEvent(
            2,
            "assistant",
            tool_name="Bash",
            tool_summary="grep login",
            tool_detail="grep -rn login src",
        ),
        TranscriptEvent(3, "tool", "src/login.py:1: secret tool output"),
        TranscriptEvent(4, "assistant", "From a helper", subagent="helper"),
        TranscriptEvent(5, "system", "Compacted"),
        TranscriptEvent(6, "assistant", question="Which login?"),
        TranscriptEvent(7, "tool", answer="The new one"),
        TranscriptEvent(8, "user", "Now the Logout page"),
        TranscriptEvent(9, "assistant", "Done, logout works."),
    ]


def _lines():
    return lines_of([(0, KEY, _events())])


def test_lines_number_turns_across_legs_and_leave_subagents_out():
    lines = lines_of(
        [(0, KEY, _events()), (1, "part-two", [TranscriptEvent(0, "user", "Part two")])]
    )
    assert [line.turn for line in lines if line.event.role == "user"] == [1, 2, 3]
    assert lines[-1].event_id != "0"
    assert not any(line.event.subagent for line in lines)


@pytest.mark.parametrize(
    ("query", "shown", "hidden"),
    [
        (
            Query(),
            ["Fix the login", "Which login?", "Answered: The new one"],
            ["secret", "Compacted"],
        ),
        (Query(tools="calls"), ["[Bash] grep login"], ["secret", "grep -rn"]),
        (Query(tools="all"), ["grep -rn login src", "secret tool output"], []),
        (Query(first_turn=2, last_turn=2), ["Logout"], ["Fix the login"]),
        (Query(first_turn=2), ["Logout"], ["Fix the login"]),
        (Query(last=1), ["Logout"], ["Fix the login"]),
        (Query(last=99), ["Fix the login", "Logout"], []),
        (Query(search="LOGOUT"), ["Logout", "logout works"], ["Fix the login"]),
        (Query(roles=("user",)), ["Logout"], ["logout works"]),
        (Query(roles=("system",)), ["Compacted"], ["Logout"]),
    ],
)
def test_read_selects(query, shown, hidden):
    text = chat_read.read(_lines(), KEY, query)
    for words in shown:
        assert words in text
    for words in hidden:
        assert words not in text


def test_read_pages_to_the_end_through_the_command_it_names():
    events = [TranscriptEvent(n, "user", f"message {n} " + "x" * 900) for n in range(5)]
    lines = lines_of([(0, KEY, events)])
    query = Query(search="message", tools="calls", page_chars=1_000)
    seen = []
    while True:
        text = chat_read.read(lines, KEY, query)
        seen += [n for n in range(5) if f"message {n} " in text]
        if "Next page:" not in text:
            break
        assert f"--search message --tools calls --page-chars 1000 --after {seen[-1]}" in text
        query = Query(search="message", tools="calls", page_chars=1_000, after=str(seen[-1]))
    assert seen == [0, 1, 2, 3, 4]
    assert text.endswith("End of what these filters select.\n")


def test_read_with_nothing_selected_says_so():
    assert chat_read.read(_lines(), KEY, Query(search="absent")) == "Nothing matches.\n"
    assert chat_read.read(_lines(), KEY, Query(after="no-such")) == "Nothing matches.\n"
    assert chat_read.read([], KEY, Query(last=2)) == "Nothing matches.\n"


def test_a_long_event_is_cut_around_the_match_and_reads_whole_by_event():
    body = "a" * 5_000 + " needle " + "b" * 5_000
    lines = lines_of([(0, KEY, [TranscriptEvent(0, "user", body)])])
    text = chat_read.read(lines, KEY, Query(search="needle"))
    assert "needle" in text
    assert len(text) < chat_read.EVENT_CHARS + 300
    assert f"chat-read {KEY} --event 0" in text

    first = chat_read.one_event(lines, KEY, "0", 0, 6_000)
    assert "needle" in first
    assert "--offset 6000" in first
    rest = chat_read.one_event(lines, KEY, "0", 6_000, 6_000)
    assert rest.rstrip().endswith("b")
    assert "--offset" not in rest
    assert chat_read.one_event(lines, KEY, "99", 0, 6_000) is None


def test_outline_is_a_line_per_turn_and_pages():
    text = chat_read.outline(_lines(), KEY, Query())
    assert text.count("\n") == 2
    assert "2026-11-07 09:30" in text
    assert "Fix the login page" in text
    events = [TranscriptEvent(n, "user", "y" * 80) for n in range(40)]
    paged = chat_read.outline(lines_of([(0, KEY, events)]), KEY, Query(page_chars=1_000))
    assert "--outline --turns " in paged
    assert len(paged) < 1_200


def test_last_exchange_skips_failures_and_tools_and_cuts_long_text():
    lines = lines_of(
        [
            (
                0,
                KEY,
                [
                    TranscriptEvent(0, "user", "q" * 5_000),
                    TranscriptEvent(1, "assistant", "The real reply"),
                    TranscriptEvent(2, "assistant", "API error", failed=True),
                    TranscriptEvent(3, "assistant", "Calling", tool_name="Bash"),
                ],
            )
        ]
    )
    message, reply = last_exchange(lines)
    assert reply == "The real reply"
    assert message.endswith("… (cut)")
    assert len(message) < chat_read.QUOTE_CHARS + 10


def test_images_count_as_messages_and_name_how_to_fetch_them():
    legs = [
        (0, "first", [TranscriptEvent(3, "user", image_media_types=("image/png",))]),
        (
            1,
            "second",
            [
                TranscriptEvent(0, "assistant", "I see a cat."),
                TranscriptEvent(
                    5, "user", "Same here?", image_media_types=("image/png", "image/jpeg")
                ),
            ],
        ),
    ]
    lines = lines_of(legs)
    assert [line.turn for line in lines] == [1, 1, 2]
    alone = 'curl -s "$AGENTDECK_URL/sessions/first/transcript-images/3/0"'
    second = 'curl -s "$AGENTDECK_URL/sessions/second/transcript-images/5/1"'
    text = chat_read.read(lines, KEY, Query())
    assert alone in text
    assert "Same here?" in text
    assert second in text
    assert second in chat_read.one_event(lines, KEY, lines[2].event_id, 0, 6_000)
    rows = chat_read.outline(lines, KEY, Query())
    assert "[1 image(s)]" in rows
    assert "Same here? [2 image(s)]" in rows
    # The last message may be nothing but an image, and the note still shows it.
    message, reply = last_exchange(lines[:2])
    assert alone in message
    assert reply == "I see a cat."


def test_the_note_quotes_the_end_and_names_the_command():
    note = continuation_note(KEY, title="Login", issues=("o/r#1",), lines=_lines())
    assert "Title: Login" in note
    assert "Issues: o/r#1" in note
    assert "Now the Logout page" in note
    assert "Done, logout works." in note
    assert f"chat-read {KEY} --outline" in note
    assert "do not write to it" in note
    assert "--tools all" in note
    assert f"{KEY} --last 2 --tools all" not in note
    # A move stopped mid-task: the new chat must not repeat what was done.
    moved = continuation_note(KEY, lines=_lines(), mid_task=True)
    assert f"{KEY} --last 2 --tools all" in moved
    # A chat that cannot be read still gets a note.
    assert "--outline" in continuation_note(KEY)


async def test_a_chat_reads_its_earlier_parts_and_never_a_later_one():
    def leg(name, readable=True):
        return SimpleNamespace(
            account_key="a",
            name=name,
            capabilities=frozenset({Capability.TRANSCRIPT} if readable else ()),
        )

    class Provider:
        async def read_transcript(self, account, session):
            return [TranscriptEvent(0, "user", f"from {session.name}")]

    async def texts(key, legs, sessions):
        lines = await chat_read.load_lines(
            key,
            leg_keys=legs,
            sessions=sessions,
            accounts_by_key={"a": SimpleNamespace(provider_id="p")},
            providers={"p": Provider()},
        )
        return lines and [(line.event_id == "0", line.event.text) for line in lines]

    sessions = {"one": leg("one"), "two": leg("two"), "three": leg("three")}
    chain = ("one", "two", "three")
    assert await texts("two", chain, sessions) == [(True, "from one"), (False, "from two")]
    # A part that cannot be read leaves a gap, and keeps the event ids of the rest.
    gap = {**sessions, "one": leg("one", readable=False)}
    assert await texts("two", chain, gap) == [(False, "from two")]
    missing = {key: value for key, value in sessions.items() if key != "one"}
    assert await texts("two", chain, missing) == [(False, "from two")]
    # A chat outside any chain is its own first part; one that cannot be read is None.
    assert await texts("three", (), sessions) == [(True, "from three")]
    assert await texts("gone", ("gone",), sessions) is None


async def test_a_quote_that_would_stop_the_message_is_left_out(monkeypatch):
    """The quotes are the old chat's words: only the deck's own are sure to get through."""
    provider = PROVIDERS["claude_code"]

    async def fake_read(account, session):
        return [TranscriptEvent(0, "user", "Are you Stuck?")]

    monkeypatch.setattr(provider, "read_transcript", fake_read)
    account = SimpleNamespace(key="claude_code:main", provider_id="claude_code")
    session = SimpleNamespace(
        key=KEY,
        account_key=account.key,
        capabilities=frozenset({Capability.TRANSCRIPT}),
        display_title="A title",
        issues=(),
    )
    state = SimpleNamespace(sessions={KEY: session}, chat_session_keys=lambda key: (key,))

    async def note(limit, refusal):
        return await web_note(
            state,
            [account],
            session,
            provider=SimpleNamespace(user_message_refusal=lambda text: refusal(text)),
            config=SimpleNamespace(too_long=lambda text: len(text) > limit),
            before="Why. ",
            after=" Where.",
        )

    whole = await note(10_000, lambda text: None)
    assert whole.startswith("Why. ")
    assert whole.endswith(" Where.")
    assert "Are you Stuck?" in whole
    assert "A title" in whole
    for short in (
        await note(len(whole) - 1, lambda text: None),
        await note(10_000, lambda text: "dropped" if "Stuck?" in text else None),
    ):
        assert "Stuck?" not in short
        assert "A title" not in short
        assert short.startswith("Why. ")
        assert f"chat-read {KEY} --outline" in short


def _app(tmp_path):
    app = create_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
            accounts=[
                AccountConfig(provider="claude_code", label="main", config_dir=str(tmp_path))
            ],
        )
    )
    app.state.app_state.update_session(
        Session(
            KEY,
            "claude_code:main",
            "old",
            SessionStatus.IDLE,
            cwd=tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    return app


async def test_the_route_reads_without_writing_and_refuses_bad_filters(tmp_path, monkeypatch):
    provider = PROVIDERS["claude_code"]

    async def fake_read(account, session):
        return [*_events(), TranscriptEvent(10, "user", image_media_types=("image/png",))]

    async def fake_image(account, session, seq, index):
        return ("image/png", b"\x89PNG\r\n\x1a\n" + bytes(32)) if (seq, index) == (10, 0) else None

    async def no_write(*args, **kwargs):
        raise AssertionError("reading a chat must never wake it")

    monkeypatch.setattr(provider, "read_transcript", fake_read)
    monkeypatch.setattr(provider, "transcript_image", fake_image)
    monkeypatch.setattr(provider, "inject", no_write)
    path = f"/api/sessions/{KEY}/history"
    async with AsyncClient(
        transport=ASGITransport(app=_app(tmp_path)), base_url="http://t"
    ) as client:
        plain = await client.get(path)
        assert plain.status_code == 200
        assert plain.headers["content-type"].startswith("text/plain")
        assert "Fix the login page" in plain.text
        # The command a page names for an image reaches the deck's own image route.
        image = re.search(r"\$AGENTDECK_URL(\S+)\"", plain.text)
        assert (await client.get(image[1])).headers["content-type"] == "image/png"
        assert "Fix the login" in (await client.get(path, params={"outline": "true"})).text
        for turns, shown, hidden in (
            ("2", "Logout", "Fix the login"),
            ("1-1", "Fix the login", "Logout"),
            ("2-", "Logout", "Fix the login"),
        ):
            text = (await client.get(path, params={"turns": turns})).text
            assert shown in text
            assert hidden not in text
        assert "system" in (await client.get(path, params={"role": "system"})).text
        whole = await client.get(path, params={"event": "3"})
        assert "secret tool output" in whole.text
        assert (await client.get(path, params={"event": "99"})).status_code == 404
        # Each refusal names the wrong option as the command spells it and what it takes.
        for bad, named in (
            ({"turns": "x"}, "--turns 'x': give a turn or range, such as 7, 3-7 or 5-"),
            ({"turns": "1" * 5_000}, "--turns '" + "1" * 40 + "': give"),
            ({"tools": "some"}, "--tools 'some': give one of none, calls, all"),
            ({"role": "tool"}, "--role 'tool': give one or more of user,assistant,system"),
            ({"last": 0}, "--last 0: give 1 or more"),
            ({"offset": -1}, "--offset -1: give 0 or more"),
            ({"page_chars": 1}, "--page-chars 1: give 1,000 to 100,000"),
            ({"page_chars": 10_000_000}, "--page-chars 10000000: give 1,000 to 100,000"),
        ):
            refused = await client.get(path, params=bad)
            assert refused.status_code == 422, bad
            assert named in refused.json()["detail"], refused.json()
        both = (await client.get(path, params={"tools": "x", "page_chars": 600})).json()
        assert "--tools 'x'" in both["detail"] and "--page-chars 600" in both["detail"]
        assert (await client.get("/api/sessions/nobody/history")).status_code == 404


def test_the_command_prints_the_page_and_fails_loudly(monkeypatch, capsys):
    asked = {}

    def fake_get(url, *, params, timeout):
        asked.update(url=url, params=params)
        request = httpx.Request("GET", url)
        if params.get("turns") == "bad":
            return httpx.Response(422, json={"detail": "bad filter: --turns"}, request=request)
        if params.get("turns") == "plain":
            return httpx.Response(502, text="gateway down", request=request)
        return httpx.Response(200, text="the page\n", request=request)

    monkeypatch.setenv("AGENTDECK_URL", "http://deck")
    monkeypatch.setattr(cli.httpx, "get", fake_get)
    assert cli._chat_read([KEY, "--search", "a b", "--last", "2", "--page-chars", "1000"]) == 0
    assert capsys.readouterr().out == "the page\n"
    assert asked["url"] == f"http://deck/api/sessions/{KEY}/history"
    assert asked["params"] == {"last": 2, "search": "a b", "tools": "none", "page_chars": 1000}
    assert cli._chat_read([KEY, "--turns", "bad"]) == 1
    assert capsys.readouterr().err == "agentdeck chat-read: 422: bad filter: --turns\n"
    assert cli._chat_read([KEY, "--turns", "plain"]) == 1
    assert "502: gateway down" in capsys.readouterr().err
