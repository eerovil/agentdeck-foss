"""The engine keeps what the current lease's worker last told a person (#2332).

The deck shows a parked card's question or block reason from it: the worker's chat is often
out of the deck's scan window by the time a person looks, while the engine reads the terminal
comment on every full wake anyway.
"""

from __future__ import annotations

from agentdeck.poller_engine.cycle import _record_park_note
from agentdeck.poller_engine.github import PARK_NOTE_LIMIT, TERMINAL_MARKER, GitHubAdapter
from agentdeck.poller_engine.reconcile import CardSnapshot

GEN = "d7534104ff254d70b39688db57c59d46"
REPORT = (
    "No code change or pull request was made.\n\n"
    "Blocked on external DNS/database connectivity; the durable fix belongs in the resolver."
)
BODY = (
    f"{REPORT}\n\n{TERMINAL_MARKER}{GEN} status=blocked -->\n\n---\n"
    "🤖 **Ohjaa Claudea uudella kommentilla**\n<!-- claude-bot -->"
)


def test_the_note_is_the_workers_words_without_the_marker_or_the_boards_footer():
    comments = [{"body": "unrelated"}, {"body": BODY}, {"body": "a later ordinary comment"}]

    assert GitHubAdapter._marker_note(comments, TERMINAL_MARKER, GEN) == REPORT
    assert GitHubAdapter._marker_note(comments, TERMINAL_MARKER, "other") is None
    assert GitHubAdapter._marker_note(comments, TERMINAL_MARKER, "") is None


def test_a_long_report_is_capped():
    body = "x" * (PARK_NOTE_LIMIT * 2) + f"\n{TERMINAL_MARKER}{GEN} status=blocked -->"

    note = GitHubAdapter._marker_note([{"body": body}], TERMINAL_MARKER, GEN)

    assert note is not None and len(note) <= PARK_NOTE_LIMIT


def test_a_long_report_keeps_the_lines_the_deck_reads_wherever_they_are():
    """#2343 review: a head cut dropped the question and the Blocked line at the report's end."""
    from agentdeck.work_wording import blocked_reason, question

    filler = "\n".join(f"- checked path {n} and found nothing new there." for n in range(80))
    report = (
        f"Investigated.\n\n{filler}\n\nBlocked on the Klarna API key; ask the owner.\n\n"
        f"{filler}\n\nShould the retry wait for the key, or be dropped?\n\nLive session: x"
    )
    body = f"{report}\n\n{TERMINAL_MARKER}{GEN} status=blocked -->"

    note = GitHubAdapter._marker_note([{"body": body}], TERMINAL_MARKER, GEN)

    assert len(report) > PARK_NOTE_LIMIT and note is not None and len(note) <= PARK_NOTE_LIMIT
    assert blocked_reason(note) == "the Klarna API key"
    assert question(note) == "Should the retry wait for the key, or be dropped?"
    assert note.endswith("Live session: x")


def _snapshot(note: str | None) -> CardSnapshot:
    return CardSnapshot(
        open=True, owned=True, status="Blocked", terminal_marker="blocked", terminal_note=note
    )


def test_the_note_is_kept_bound_to_the_lease_it_was_written_under():
    rec = {"generation": GEN}

    _record_park_note(rec, _snapshot(REPORT))

    assert rec["park_note"] == {"generation": GEN, "status": "blocked", "text": REPORT}
    # A wake with no terminal comment for this lease leaves the note as it was.
    _record_park_note(rec, _snapshot(None))
    assert rec["park_note"]["text"] == REPORT


def test_a_card_with_no_lease_keeps_no_note():
    rec: dict = {}

    _record_park_note(rec, _snapshot(REPORT))

    assert "park_note" not in rec
