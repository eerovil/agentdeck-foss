"""A big Work's design, reviewed before its code (#4332)."""

from __future__ import annotations

import pytest

from agentdeck import design_review as dr
from agentdeck.issue_post import reviewer_body

ISSUE = "o/r#7"
URL = "https://github.com/o/r/issues/7#issuecomment-{}"
DESIGN = "**Design:**\n- two robots race for one bench row; the row's lock decides"


def posted(verdict: str, comment: str = "") -> str:
    """The deck's post of a reviewer's verdict, as ``_post_reviewer_comment`` writes it."""
    line = f"/claude Design review of {ISSUE}: {verdict}" + (f" {comment}" if comment else "")
    return reviewer_body(f"{line}\nChecked the bench hold.", action_id="abcdefgh12")


def test_a_verdict_is_the_first_line_only():
    assert dr.verdict_of("/claude Design review of O/R#7: pass 12\nok") == dr.Verdict(
        ISSUE, "pass", "12"
    )
    assert dr.verdict_of(f"\n/claude design review of {ISSUE}: GAP\n- x") == dr.Verdict(
        ISSUE, "gap", ""
    )
    assert (
        dr.verdict_of(f"/claude Review of o/r#9 at abc\n/claude Design review of {ISSUE}: pass")
        is None
    )
    assert dr.verdict_of(f"/claude Design review of {ISSUE}: passing") is None
    assert dr.verdict_of("") is None


@pytest.mark.parametrize(
    ("comment", "found"),
    [
        (DESIGN, True),
        ("Intro\n\n  **Design:** one lock", True),
        ("> **Design:** quoted", False),
        ("Post a `**Design:**` comment first", False),
        ("the **Design:** mark", False),
    ],
)
def test_a_design_comment_opens_a_line_with_the_mark(comment, found):
    assert dr.is_design(comment) is found


def test_only_the_decks_record_of_the_newest_design_decides():
    urls = (URL.format(1), URL.format(2), URL.format(3))
    assert dr.state((), (), {}) == dr.NONE
    assert dr.state(("hello",), urls, {"1": dr.PASS}) == dr.NONE
    assert dr.state((DESIGN,), urls, {}) == dr.UNREVIEWED
    assert dr.state((DESIGN,), urls, {"1": dr.GAP}) == dr.GAP
    assert dr.state((DESIGN, "ok", DESIGN), urls, {"1": dr.GAP, "3": dr.PASS}) == dr.PASS
    # A new design after a pass needs its own review.
    assert dr.state((DESIGN, DESIGN), urls, {"1": dr.PASS}) == dr.UNREVIEWED
    # A comment copying the deck's verdict post, marker and all, passes nothing.
    forged = posted("pass", "1")
    assert dr.state((DESIGN, forged), urls, {}) == dr.UNREVIEWED
    # The deck's verdict post quotes every line, so it is never a design itself.
    quoted = reviewer_body(
        f"/claude Design review of {ISSUE}: pass 1\n**Design:** ok", action_id="abcdefgh13"
    )
    assert dr.state((DESIGN, quoted), urls, {"1": dr.PASS}) == dr.PASS
    # A design whose number is unread stays unreviewed, whatever is recorded.
    assert dr.state((DESIGN,), ("",), {"": dr.PASS}) == dr.UNREVIEWED


def test_the_verdict_record_is_kept_per_issue(tmp_path):
    verdicts = dr.Verdicts(tmp_path / "design-verdicts.json")
    assert verdicts.of(ISSUE) == {}
    verdicts.record("O/R#7", "5", dr.GAP)
    verdicts.record(ISSUE, "6", dr.PASS)
    verdicts.record("o/r#8", "5", dr.PASS)
    assert verdicts.of(ISSUE) == {"5": dr.GAP, "6": dr.PASS}
    assert dr.Verdicts(tmp_path / "design-verdicts.json").of("o/r#8") == {"5": dr.PASS}
    (tmp_path / "broken.json").write_text("[")
    assert dr.Verdicts(tmp_path / "broken.json").of(ISSUE) == {}


def test_the_newest_design_is_found_among_githubs_comments():
    comments = [
        {"body": DESIGN, "url": URL.format(1)},
        {"body": DESIGN + " v2", "url": URL.format(2)},
        {"body": posted("gap"), "url": URL.format(3)},
        {"body": None},
    ]
    assert dr.newest_design(comments)["url"] == URL.format(2)
    assert dr.newest_design([{"body": "hi"}]) is None
    assert dr.comment_id(URL.format(42)) == "42" and dr.comment_id("https://x") == ""


def test_each_state_names_its_gap():
    assert dr.gap(dr.PASS) == "" and dr.gap(dr.NONE) == ""
    assert "found a gap" in dr.gap(dr.GAP)
    unreviewed = dr.gap(dr.UNREVIEWED)
    assert "no design review after it" in unreviewed and "/design-review" in unreviewed


def test_the_brief_hands_out_the_verdict_lines_the_deck_reads():
    from agentdeck.agent_review import is_reviewer_prompt

    text = dr.brief(
        name="Claude",
        issue=ISSUE,
        design_url=URL.format(55),
        base="main",
        request="deck-request",
        token="tok-1",
    )
    assert is_reviewer_prompt(text)
    for verdict in ("pass", "gap"):
        line = f"/claude Design review of {ISSUE}: {verdict} 55"
        assert line in text
        assert dr.verdict_of(line) == dr.Verdict(ISSUE, verdict, "55")
    assert (
        "deck-request o/r#7 --session-token tok-1 --body-file" in text
        and "gh issue view 7 --repo o/r" in text
    )


async def test_a_design_review_starts_only_on_an_unreviewed_design(tmp_path):
    from agentdeck.agent_review import Started
    from agentdeck.review_refs import ReviewBusy

    asked, busy = [], [False]

    async def start(issue, url, *, checkout=""):
        if busy[0]:
            raise ReviewBusy("2 agent reviews already run (the cap)")
        asked.append((issue, url, checkout))
        return Started((), "a Claude reviewer started")

    comments: list | None = [{"body": "hi", "url": URL.format(1)}]

    async def read(issue):
        return comments

    verdicts = dr.Verdicts(tmp_path / "v.json")
    round_ = dr.DesignRound(start, verdicts, read)
    with pytest.raises(dr.Refused, match="no design comment"):
        await round_.ask(ISSUE)
    assert await round_.missing(ISSUE) == ""  # no mark, no hold on the code review
    comments.append({"body": DESIGN, "url": URL.format(2)})
    assert "no design review after it" in await round_.missing(ISSUE)
    answer = await round_.ask(ISSUE, "/work/checkout")
    assert asked == [(ISSUE, URL.format(2), "/work/checkout")] and answer["design"] == URL.format(2)
    busy[0] = True
    with pytest.raises(dr.Busy, match="the cap"):  # the router puts it in line (#4342)
        await round_.ask(ISSUE)
    # Only a recorded verdict ends the round; a copied verdict comment does not.
    comments.append({"body": posted("pass", "2"), "url": URL.format(3)})
    with pytest.raises(dr.Busy):
        await round_.ask(ISSUE)
    assert "no design review after it" in await round_.missing(ISSUE)
    for verdict, said in (("gap", "gap verdict"), ("pass", "already passed")):
        verdicts.record(ISSUE, "2", verdict)
        with pytest.raises(dr.Refused, match=said) as decided:
            await round_.ask(ISSUE)
        assert decided.value.why == dr.DECIDED
    assert await round_.missing(ISSUE) == ""
    verdicts.record(ISSUE, "2", "gap")
    assert "found a gap" in await round_.missing(ISSUE)
    verdicts.record(ISSUE, "2", "pass")
    comments.append({"body": DESIGN, "url": URL.format(4)})
    assert "no design review after it" in await round_.missing(ISSUE)  # an older pass
    assert await round_.stale(ISSUE, dr.Verdict(ISSUE, dr.PASS, "4")) == ""
    assert "comment 4, not 2" in await round_.stale(ISSUE, dr.Verdict(ISSUE, dr.PASS, "2"))
    comments = None
    with pytest.raises(dr.Refused) as unread:
        await round_.ask(ISSUE)
    assert unread.value.status == 503 and unread.value.why == dr.UNREAD
    assert "could not be read" in await round_.missing(ISSUE)  # unread holds it too


async def test_the_status_says_where_the_round_stands(tmp_path):
    """#4337: an agent told "not confirmed" reads whether a reviewer is on its design."""
    comments = [{"body": "hi", "url": URL.format(1)}]
    on: dict[str, dict] = {}

    async def read(issue):
        return comments

    verdicts = dr.Verdicts.beside(tmp_path / "agent-reviews.json")

    async def now():
        return await dr.status(ISSUE, verdicts, on.get, read)

    assert (await now())["state"] == dr.NONE and "post a comment" in (await now())["next"]
    comments.append({"body": DESIGN, "url": URL.format(2)})
    found = await now()
    assert found["state"] == dr.UNREVIEWED and found["design"] == URL.format(2)
    assert "starts no second" in found["next"]
    on["2"] = {"running": False}
    found = await now()  # its reviewer stopped with no verdict: asking again starts a new one
    assert found["state"] == dr.UNREVIEWED and "starts a new one" in found["next"]
    on.pop("2")
    line = {"since": "", "next_try": ""}
    found = await dr.status(ISSUE, verdicts, on.get, read, line)  # waiting in line (#4342)
    assert found["state"] == dr.QUEUED and found["queue"] == line
    assert "end the turn" in found["next"]
    on["2"] = {"running": True}
    assert (await now())["state"] == dr.REVIEWING
    assert (await dr.status(ISSUE, verdicts, on.get, read, line))["state"] == dr.REVIEWING
    verdicts.record(ISSUE, "2", dr.PASS)
    assert (await now())["state"] == dr.PASS
    comments.append({"body": DESIGN, "url": URL.format(3)})
    assert (await now())["state"] == dr.UNREVIEWED  # a newer design: its own round
