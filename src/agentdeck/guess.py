"""A cheap guess for the person: a new Work's project (#3367).

One short Haiku call that sees only the typed description and the list of projects —
about 800 tokens in and 100 out — billed to an account's subscription like any other
Claude Code use. The next-message guess this module also made was removed in #3383:
the owner found no use for it.

The call runs the CLI with nothing of its own (see ``guess_cli``), so no instruction
file is loaded and nothing it reads can make it act. A guess is only ever a suggestion
the person can change; a failure, a timeout or an account at its limit means no guess.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from typing import Any

from . import guess_cli
from .guess_cli import NO_GUESS
from .models import Account

# Two guesses at once at most: each is a CLI process on a host that also runs every
# deck-owned agent, and nothing stops several New work forms asking at once.
MAX_CONCURRENT = 2
_slots = asyncio.Semaphore(MAX_CONCURRENT)

# A New work's project is guessed only from a description with something in it, and
# from at most this much of it.
PROJECT_GUESS_MIN_CHARS = 30
PROJECT_TEXT_CHARS = 4000

_PROJECT_PROMPT = """\
A person is filing a new task for one of these software projects. Reply with only the \
repository it belongs to, copied exactly from the list. If the text gives no sign of \
which one, reply {none}.

Projects:
{projects}

The task:
<<<
{text}
>>>"""


def guess_account(
    accounts: Iterable[Account], usage: dict[str, Any], preferred: str | None = None
) -> Account | None:
    """``preferred`` when it is a Claude account with room, else the first Claude one that does.

    An account with no usage reading, or none of its windows full, has room: a guess is
    tiny, so not knowing is no reason to go without.
    """
    claude = [account for account in accounts if account.provider_id == "claude_code"]

    def has_room(account: Account) -> bool:
        snapshot = usage.get(account.key)
        fullest = snapshot.max_pct() if snapshot is not None else None
        return fullest is None or fullest < 100

    ordered = sorted(claude, key=lambda account: account.key != preferred)
    return next((account for account in ordered if has_room(account)), None)


def project_prompt(text: str, projects: Sequence[tuple[str, str]]) -> str:
    """The prompt for ``text`` among ``(repository, project)`` pairs."""
    return _PROJECT_PROMPT.format(
        none=NO_GUESS,
        projects="\n".join(f"- {repository} ({project})" for repository, project in projects),
        text=text.strip()[:PROJECT_TEXT_CHARS],
    )


def chosen_repository(reply: str | None, repositories: Iterable[str]) -> str | None:
    """The listed repository the reply names, in the list's own spelling; never another."""
    if not reply:
        return None
    wanted = reply.strip().strip(".").casefold()
    return next((name for name in repositories if name.casefold() == wanted), None)


async def ask(app, prompt: str) -> str | None:
    """One Haiku call on a Claude account with room, or None when there is none."""
    account = guess_account(app.state.accounts, app.state.app_state.usage)
    if account is None:
        return None
    async with _slots:
        return await guess_cli.run_claude(account, prompt)


def guessing_enabled(app) -> bool:
    """The `/settings` "Guess for me" switch; on unless someone turned it off."""
    db = getattr(app.state, "db", None)
    return db is None or db.load_guessing_enabled()
