"""Count the deck's expensive steps, so a test can hold each change to a budget (#2865).

ADR 0007 says work per change has a budget. Timing a step on a shared, busy host flakes;
counting how many times it ran does not. Each expensive step calls ``count`` with its own
name, and ``tests/test_change_budget.py`` drives a change through the real code and asserts
how many of each it cost — one chat change rebuilds that chat's display copy once and builds
no Work card for a Work it is not in.

The counts are process-wide and only ever grow; a test reads the difference across the
change it makes (``counting``). Counting is a dict increment, cheap enough to leave on.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager

# The steps counted. A display copy of one chat, a Work card, a whole Work overview pass,
# one read of the poller board from disk, and one fetch of a runtime worker snapshot.
CHAT_DISPLAY = "chat_display"
SESSION_PRESENTATION = "session_presentation"
WORK_CARD = "work_card"
WORK_OVERVIEW = "work_overview"
BOARD_READ = "board_read"
WORK_PROJECTION = "work_projection"
# One tail read of a chat's transcript, and one live chat-page fragment render, for
# however many tabs show the chat.
CHAT_TAIL = "chat_tail"
# One replay of a chat onto the leg it moved to (an account switch), for every tab on it.
CHAT_CATCH_UP = "chat_catch_up"
CHAT_FRAGMENT = "chat_fragment"
# One front-page or /works row rendered to HTML (#3228): once per revision, for every tab.
WORK_ROW = "work_row"
# One Work's card inputs read and compared (#3228): skipped for a Work no change named.
WORK_CARD_CHECK = "work_card_check"
# One build of a Work page's sections (#3228), for every tab showing it.
WORK_PAGE_BUILD = "work_page_build"
# One read and render of the /pollers rows (#3228), for every tab showing it.
POLLER_ROWS = "poller_rows"
# One Work page section rendered (#3228): only the sections whose inputs moved.
WORK_SECTION = "work_section"
# One Run or workspace fragment rendered for a Work page section (#3228).
WORK_FRAGMENT = "work_fragment"

_counts: Counter[str] = Counter()


def count(step: str, n: int = 1) -> None:
    """Record that ``step`` ran ``n`` more times."""
    _counts[step] += n


def snapshot() -> Counter[str]:
    """A copy of every count so far."""
    return Counter(_counts)


@contextmanager
def counting() -> Iterator[Counter[str]]:
    """Yield a counter that holds, once the block ends, what the block cost."""
    before = snapshot()
    spent: Counter[str] = Counter()
    try:
        yield spent
    finally:
        after = snapshot()
        spent.update(
            {step: after[step] - before[step] for step in after if after[step] != before[step]}
        )
