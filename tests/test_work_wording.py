"""The front page's plain words for a Work's wait, and a parked Work's question (#2254)."""

from __future__ import annotations

import pytest

from agentdeck.work_wording import QUESTION_LIMIT, blocked_reason, plain_reason, question


@pytest.mark.parametrize(
    ("reason", "plain"),
    [
        ("Review stalled: brought forward 3 times and still not merged — nothing here will move "
         "it again", "Not merged after 3 update rounds"),
        ("Review stalled: reviewed, up to date with its base, and still not merged",
         "Reviewed, up to date, not merged"),
        ("Review stalled: 13 commits behind its base, and GitHub would not merge the base in — "
         "most likely a conflict somebody has to resolve", "Conflicts with its base branch"),
        ("The reviewer doesn't watch Northwind/abacus",
         "No reviewer watches Northwind/abacus"),
        ("Reviewed at abc1234; ready for a person to decide", "Reviewed at abc1234 · your call"),
        ("the delivery never landed; an attempt's outcome is unknown", "The worker never started"),
        ("The card is in Needs More Info", "Waiting for your answer on the issue"),
        # issues#434: one line, the owner dropped from every reference.
        ("Review stalled: brought forward 3 times and still not merged — nothing here will move "
         "it again on Northwind/store#3343; Northwind/abacus#1941 reviewed "
         "clean", "Not merged after 3 update rounds on store#3343; abacus#1941 reviewed clean"),
        ("acme/app#7: Review stalled: reviewed, up to date with its base, and still not merged "
         "(+1 more)", "app#7: Reviewed, up to date, not merged (+1 more)"),
        # #2327's prefix: the stall is about the merge.
        ("Not merged: brought forward 3 times and still not merged — nothing here will move "
         "it again", "Not merged after 3 update rounds"),
        ("Not merged: 13 commits behind its base, and GitHub would not merge the base in — "
         "most likely a conflict somebody has to resolve", "Conflicts with its base branch"),
        # Anything the table does not know is shown as it is.
        ("Auto-merge is disabled", "Auto-merge is disabled"),
        ("", ""),
    ],
    ids=["brought-forward", "up-to-date", "conflict", "unwatched", "reviewed", "never-started",
         "needs-more-info", "grouped", "more", "new-brought-forward", "new-conflict", "unknown",
         "empty"],
)
def test_known_engine_sentences_become_plain(reason, plain):
    assert plain_reason(reason) == plain


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("I checked the transfers.\n\nShould the **159 transfers** older than 30 days be "
         "cancelled, or flagged for a manual check?", "Should the 159 transfers older than 30 "
         "days be cancelled, or flagged for a manual check?"),
        ("Is this ok? Tell me which of `A` or `B` to take next?",
         "Tell me which of A or B to take next?"),
        ("The first batch is live on staging. Details follow.", ""),
        ("## Which one?\n- done", "Which one?"),
        ("", ""),
    ],
    ids=["last-question", "last-of-two", "no-question", "markdown", "empty"],
)
def test_the_question_is_what_the_worker_last_asked(text, asked):
    assert question(text) == asked


def test_a_long_question_is_cut_to_one_line():
    found = question("Why " + "x" * 400 + "?")
    assert len(found) == QUESTION_LIMIT and found.endswith("…")


# --- a Blocked card says what blocks it, and release parks read plainly (#2332) -------------

BAZAAR_230 = """Revalidated the database-host failure against current code and Sentry.

- Sentry currently has five related groups across unrelated request paths.

No code change or pull request was made.

Blocked on external DNS/database connectivity; the durable fix belongs in the production \
resolver/network or managed database service. If the error frequency rises, reopen.

Live session: https://example.test/sessions/x"""


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (BAZAAR_230, "external DNS/database connectivity"),
        (
            "**Blocked:** waiting for the Klarna API key. Then more.",
            "waiting for the Klarna API key",
        ),
        ("- Blocked by #12, which has to ship first.", "#12, which has to ship first"),
        ("Nothing blocks this; it is done.", ""),
        ("", ""),
    ],
    ids=["bazaar-230", "bold-colon", "bullet-by", "no-blocked-line", "empty"],
)
def test_the_block_reason_is_the_workers_own_blocked_line(text, reason):
    assert blocked_reason(text) == reason


def test_a_blocked_card_is_no_longer_called_a_question():
    assert plain_reason("The card is in Blocked") == "The card is in Blocked"
    assert plain_reason("The card is in Needs More Info") == "Waiting for your answer on the issue"


@pytest.mark.parametrize(
    ("reason", "plain"),
    [
        ("Auto-merge on `store#3369` is no longer armed on GitHub and its checks are not failing, "
         "so nothing will merge it. `/merge` again to re-arm.",
         "Auto-merge on store#3369 was switched off; merge again"),
        ("**Production deploy failed.** The merge is on the base branch but the deploy did not "
         "go green; a broken production deploy needs a human.",
         "Merged; the production deploy failed"),
        ("**Production deploy failed.** The merge is on the base branch but the deploy did not "
         "go green; a broken production deploy needs a human.; AgentDeck is re-checking the "
         "deploy", "Merged; the production deploy failed; AgentDeck is re-checking the deploy"),
        (
            "Northwind/abacus#1953 conflicts with master",
            "abacus#1953 conflicts with master",
        ),
    ],
    ids=["auto-merge-off", "deploy-failed", "deploy-failed-rechecking", "conflict"],
)
def test_release_parks_read_plainly(reason, plain):
    assert plain_reason(reason) == plain


def test_a_bazaar_277_report_gives_its_question():
    report = (
        "Investigated cancellation. Customers can cancel orders already sent to the warehouse.\n\n"
        "Should self-service cancellation be limited to `new` orders, with anything later "
        "going through support?\n\n<!-- ignored -->"
    )
    assert question(report).startswith("Should self-service cancellation be limited to new orders")
