"""Web research for an agent whose own search cannot reach a site, through Codex (#4290).

Claude's web search refuses reddit.com and its fetch cannot open it; Codex's live web search
reads Reddit threads. ``agentdeck research`` hands a question to an AgentDeck-owned Codex chat
with a brief that asks for the page URL and a verbatim quote behind every claim, and prints
Codex's answer as it is. The owner chose to trust Codex here rather than re-read each quoted
page: an agent that needs a page itself opens it with ``agentdeck read``.
"""

from __future__ import annotations

RESEARCH_BRIEF = """\
You are a web researcher. Use your web search to answer the question below; web search is
the only tool you need, and this chat has no shell or files. Read discussions themselves (Reddit,
Hacker News, Stack Overflow, forums, GitHub issues) as well as articles when the question is
about people's experiences or opinions.

Back every claim with the page it came from: its full URL and a short quote (under 30 words)
copied word for word from that page. Never paraphrase inside quotation marks. If a page would
not open, say so rather than guessing what it says.

End your answer with a section that starts with the line `Sources:` and lists each page you
used: its full URL and the quote you took from it.

The question:
"""


def build_prompt(question: str) -> str:
    return RESEARCH_BRIEF + question.strip() + "\n"


_WINDOWS = ("five_hour_pct", "seven_day_pct")


def roomiest_account(usage: list[dict], provider: str) -> str | None:
    """Of ``provider``'s accounts in the deck's ``/api/usage`` rows, the usable one whose
    fullest window has most room, or ``None`` when none has room (#4437)."""
    rows = [
        row
        for row in usage
        if str(row.get("account_key", "")).startswith(f"{provider}:")
        and not any((row.get(window) or 0) >= 100 for window in _WINDOWS)
    ]
    if not rows:
        return None

    def used(row: dict) -> tuple[int, float]:
        known = [float(row[window]) for window in _WINDOWS if row.get(window) is not None]
        return (1, 0.0) if not known or row.get("stale") else (0, max(known))

    return min(rows, key=used)["account_key"]


def research_account(usage: list[dict]) -> str | None:
    """The Codex account to research on: the usable one whose fullest window has most room.

    An account with any window at its limit is skipped, as ``scripts/upstream_upgrade.py``
    skips it: a delegation there is refused. One whose usage is unknown or stale sorts after
    every known one. ``None`` when no Codex account has room.
    """
    return roomiest_account(usage, "codex")
