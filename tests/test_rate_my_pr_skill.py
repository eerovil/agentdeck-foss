from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEW = ROOT / "skills" / "rate-my-pr" / "REVIEW.md"


def test_the_scope_dimension_measures_the_diff_against_the_card_it_came_from():
    """#1104: without the originating ask, the scope lane measures the diff against itself
    and never notices a change that grew well past what its card asked for."""
    review = REVIEW.read_text()

    assert "**Record the ask**" in review
    # Recorded before the diff is read, or it becomes a description of the diff.
    assert "Do this **before** reading the diff" in review
    assert "the ask and branch/PR intent from Step 1" in review
    assert "Did the change stay inside **the ask**" in review
    assert "could have shipped separately" in review
    assert "disclosure is not authorisation" in review


def test_the_beyond_the_ask_list_survives_a_sound_scope_rating():
    """A `sound` rating means the extra work looked defensible, not that anyone asked for
    it — the reader decides, so the list is reported outside both gates."""
    review = REVIEW.read_text()

    assert "beyond-the-ask list is **not** subject to" in review
    assert "Return that list even when you rate the dimension `sound`" in review
    assert "Beyond the ask: nothing" in review
    assert "Ask: not recorded" in review


def test_the_architecture_pass_still_has_exactly_its_six_tracked_dimensions():
    """skills-review.md pins the contract at six; the scope check extends dimension 4
    rather than adding a seventh."""
    review = REVIEW.read_text()
    start = review.index("Dimensions:")
    dimensions = review[start : review.index("Each dimension sub-reviewer returns")]

    for n in range(1, 7):
        assert f"\n{n}. **" in dimensions
    assert "\n7. **" not in dimensions


def _dimension_six(review: str) -> str:
    start = review.index("6. **Over-engineering**")
    return review[start : review.index("Each dimension sub-reviewer returns")]


def test_over_engineering_traces_each_mechanism_to_a_requirement():
    """#2387: `#2386` was reviewed clean at source +1240 because the lane only asked "is this
    YAGNI". It now inventories what was added and follows each piece to a decision."""
    review = REVIEW.read_text()
    six = _dimension_six(review)

    assert "**Record the size context**" in review
    assert "Line\n  counts are never the finding." in review
    for trigger in ("source additions ≥ 400", "a new source file ≥ 200 lines",
                    "a new parser or general-purpose normaliser", "even below the line counts"):
        assert trigger in six
    for step in ("**Inventory**", "**Load-bearing check**", "**Simpler path**", "**Classify**"):
        assert step in six
    assert "`Simplify:`" in six and "`Design question:`" in six
    assert "never a deletion" in six
    # A large change with a few leftover fields is a cleanup, not over-engineering (#2358).
    assert "at least a tenth" in six and "Below a tenth" in six
    # #3176: store#3554 kept a whole replaced upload path for twelve rounds, because the lane
    # only looked at what a change adds. The reuse check reads past the diff on every head.
    assert "**Reuse check — always runs.**" in six
    for kind in ("**Superseded code kept alive**", "**Two ways of doing one thing**",
                 "**New code where existing code would do**"):
        assert kind in six
    assert "quotes both sides" in six and "what removing or merging saves" in six
    # Code only: a removal that hangs on live data is a question naming the read, not a guess.
    assert "name the\n   exact read that settles it" in six
    assert "rate on the\n   reuse check alone" in six


def test_a_gated_over_engineering_flag_is_never_passed_as_a_note():
    review = REVIEW.read_text()

    assert "**A gated *Over-engineering* flag is always a finding to act on**" in review
    assert "`questionable` or `reconsider`" in review


def test_every_review_writes_all_six_architecture_verdicts_in_order():
    """An inline runner (ChatGPT has no sub-reviewers) could compress or skip a dimension and
    still say clean; the six-line block makes a skipped one visible (#2387)."""
    review = REVIEW.read_text()
    block = review[review.index("Architecture:\n- Right problem") :]
    names = ["Right problem", "Right approach", "Right place", "Scope & blast radius",
             "Use cases / UX", "Over-engineering"]
    positions = [block.index(f"- {name}: ") for name in names]
    assert positions == sorted(positions)
    assert "never reported clean" in review
    assert "did not finish and is\nnever clean" in review


def test_review_fix_pr_hands_a_design_question_back_instead_of_deleting():
    """#2387: a Design question means removing something the requester asked for, so the
    worker neither deletes it nor ships past it; it reports needs_more_info with it."""
    procedure = (ROOT / "skills" / "review-fix-pr" / "PROCEDURE.md").read_text()

    assert "Never act on a\n  `Design question:` item" in procedure
    assert "report `needs_more_info` with the question quoted" in procedure
