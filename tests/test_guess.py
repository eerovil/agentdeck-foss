"""A New work's project guess (#3367): the call, the account, the answer, the switch.

The next-message guess these tests also covered was removed in #3383.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from agentdeck import guess, guess_cli
from agentdeck.models import Account, UsageSnapshot

MAIN = Account("claude_code:main", "claude_code", "main", Path("/accounts/main"))
ALT = Account("claude_code:alt", "claude_code", "alt", Path("/accounts/alt"))
CODEX = Account("codex:main", "codex", "main", Path("/accounts/codex"))
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _usage(account: Account, pct: float) -> UsageSnapshot:
    return UsageSnapshot(account.key, pct, None, 10.0, None, NOW)


def test_a_reply_is_one_clean_line_and_none_means_no_guess():
    assert guess_cli.clean_reply('\n  "eerovil/agentdeck."\nsecond line') == "eerovil/agentdeck."
    assert guess_cli.clean_reply("NONE") is None
    assert guess_cli.clean_reply("none.") is None
    assert guess_cli.clean_reply("") is None
    assert len(guess_cli.clean_reply("y" * 1000)) == guess_cli.MAX_GUESS_CHARS


def test_a_claude_account_with_room_pays():
    accounts = [CODEX, MAIN, ALT]

    assert guess.guess_account(accounts, {}) == MAIN
    assert guess.guess_account(accounts, {}, ALT.key) == ALT
    assert guess.guess_account(accounts, {MAIN.key: _usage(MAIN, 100)}) == ALT
    full = {MAIN.key: _usage(MAIN, 100), ALT.key: _usage(ALT, 100.0)}
    assert guess.guess_account(accounts, full) is None


def test_a_project_answer_is_only_ever_one_of_the_listed_repositories():
    listed = ["eerovil/agentdeck", "Northwind/trailhead"]

    assert guess.chosen_repository("EEROVIL/AgentDeck.", listed) == "eerovil/agentdeck"
    assert guess.chosen_repository("eerovil/other", listed) is None
    assert guess.chosen_repository(None, listed) is None
    prompt = guess.project_prompt("Fix the cart", [("eerovil/agentdeck", "agentdeck")])
    assert "- eerovil/agentdeck (agentdeck)" in prompt and "Fix the cart" in prompt


def test_the_measured_call_loads_nothing_but_the_prompt():
    command = guess_cli.guess_command("/bin/claude")

    assert command[:4] == ["/bin/claude", "-p", "--model", "haiku"]
    for flag, value in (("--tools", ""), ("--setting-sources", "")):
        assert command[command.index(flag) + 1] == value
    assert {"--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"} <= set(
        command
    )


def test_the_settings_switch_turns_guessing_off_and_on(tmp_path):
    from fastapi.testclient import TestClient
    from test_web import _app_with_state

    app = _app_with_state(tmp_path)
    client = TestClient(app)

    page = client.get("/settings")
    assert 'name="enabled" value="1" checked' in page.text
    assert "next message" not in page.text  # that guess was removed (#3383)

    off = client.post("/settings/guessing", data={"enabled": "0"}, headers={"HX-Request": "true"})
    assert "guessing is off" in off.text
    assert not guess.guessing_enabled(app)

    on = client.post(
        "/settings/guessing", data={"enabled": ["0", "1"]}, headers={"HX-Request": "true"}
    )
    assert "guessing is on" in on.text
    assert guess.guessing_enabled(app)


async def test_no_more_than_two_guesses_run_at_once(monkeypatch):
    running, peak = 0, 0

    async def runner(account, prompt):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return "eerovil/agentdeck"

    monkeypatch.setattr(guess_cli, "run_claude", runner)
    app = SimpleNamespace(
        state=SimpleNamespace(accounts=[MAIN], app_state=SimpleNamespace(usage={}))
    )

    answers = await asyncio.gather(*(guess.ask(app, "which?") for _ in range(5)))

    assert answers == ["eerovil/agentdeck"] * 5
    assert peak == guess.MAX_CONCURRENT == 2
