from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROCEDURE = ROOT / "skills" / "review-fix-pr" / "PROCEDURE.md"


def test_the_full_review_runs_once_and_the_fixes_get_one_cheap_lane():
    """#1002: nothing capped the review, so a worker re-reviewed its own fixes until a
    round came back empty. On #994 that was four rounds and 14 reviewers over a one-line
    label, for $43. The cap and the one thing that replaces the extra rounds both have to
    be in the procedure, not only in the dispatch brief."""
    procedure = PROCEDURE.read_text()

    assert "The full review runs once per turn" in procedure
    assert "## 2.5. Re-check the fixes — one lane" in procedure
    assert 'SPAWN_SUBREVIEWER(prompt, "cheap")' in procedure
    # The re-check is bounded in both directions: one lane, and no fix-of-the-fix round.
    assert "spawn **exactly one**" in procedure
    assert "do not run this step a second time on the fix-of-the-fix" in procedure


def test_a_fix_that_adds_a_new_entry_point_gets_the_strong_lane():
    """#1148: on store PR #2876 a pass fixed a missed export by adding a cron rescue command
    selecting on status="new" — also an unpaid order's state — which would have posted
    abandoned orders as paid. The cheap lane's question ("is this fix broken?") cannot find
    that; only reading the new command as new code can, and a whole second pass is an
    expensive way to buy that read."""
    procedure = PROCEDURE.read_text()

    assert "### When a fix adds a new entry point" in procedure
    assert 'SPAWN_SUBREVIEWER(prompt, "strong")' in procedure
    # The escalation is scoped by what the fix added, not by how large it is.
    assert "Editing the body of one that already exists is not a new entry point" in procedure
    # It widens the lane, never the count — the #1002 cap and the dispatch briefs still hold.
    assert "This is still **one** lane and still one full review this turn" in procedure


def test_a_later_pass_reads_what_the_last_one_decided_and_lanes_do_not():
    """#1148: reruns kept re-arguing the same sub-threshold notes — missing locks, flag
    races, PII in debug logs — from scratch, because every pass starts blank. The Review
    note is already written; reading it back is what stops the churn. Briefing the lanes
    with it instead would blind the fresh pass that is the whole point of rerunning."""
    procedure = PROCEDURE.read_text()

    assert "### What earlier passes already decided" in procedure
    assert procedure.index("### What earlier passes already decided") < procedure.index(
        "## 1. Review"
    )
    assert "### Reconcile against the prior record" in procedure
    # Orchestrator-only, and after the lanes report.
    assert "not into any lane's brief" in procedure
    assert "Step 1 uses the\nrecord after the lanes report, never before." in procedure
    # It reports rather than suppresses.
    assert "still open — skipped in an earlier pass" in procedure
    assert "nothing here suppresses a finding a lane can" in procedure
    # And the note has to be written to be read back.
    assert "is also what a **later pass reads in step 0**" in procedure


def test_the_branch_is_synced_onto_its_base_before_the_review_pins_a_sha():
    """A rebase after the lanes ran invalidates the reviewed sha and buys a whole second
    pass — which is exactly how #994 got its third round."""
    procedure = PROCEDURE.read_text()
    sync = procedure.index("Sync onto the base now")
    assert sync < procedure.index("## 1. Review")
    assert "do not re-run the review over it" in procedure


def test_an_issue_thread_evidence_brief_overrides_the_default_pr_review_note():
    procedure = PROCEDURE.read_text()

    assert "poller brief assigns work evidence to the issue thread" in procedure
    assert "put that Review note in the terminal issue comment instead" in procedure
    assert "Keep the PR body to the concise fields the brief names" in procedure


def test_completion_prs_close_their_issue_and_body_updates_preserve_that_link():
    procedure = " ".join(PROCEDURE.read_text().split())

    assert "### Source issue closure" in procedure
    assert "`Closes #<issue>`" in procedure
    assert "`Closes <owner>/<repo>#<issue>`" in procedure
    assert "preserve an existing closing reference to the source issue verbatim" in procedure
    assert "never weaken or remove it" in procedure


def test_partial_prs_can_explicitly_opt_out_of_closing_the_issue():
    procedure = " ".join(PROCEDURE.read_text().split())

    assert "`PR intent: non-closing — <reason>`" in procedure
    assert "remove any existing closing keyword for the source issue" in procedure
    assert "Do not infer non-closing intent" in procedure
    assert "do not invent completion intent" in procedure


def test_the_ask_is_recorded_before_the_diff_is_read():
    """#1104: a card said "triage and fix this Sentry error" and the change that shipped also
    changed refund behaviour on purpose. Scope can only be judged against what was asked for,
    and an ask reconstructed after reading the diff is just a description of the diff."""
    procedure = PROCEDURE.read_text()

    assert "### What the card asked for" in procedure
    assert procedure.index("### What the card asked for") < procedure.index("## 1. Review")
    assert "agent/issue-<n>" in procedure
    assert "never of what was asked for" in procedure
    assert "Ask: not recorded" in procedure


def test_work_the_card_never_asked_for_is_reported_and_not_deleted():
    procedure = PROCEDURE.read_text()
    flat = " ".join(procedure.split())

    assert "### Beyond the card" in procedure
    assert "Beyond the card: nothing" in procedure
    # The architecture pass runs at every effort now, so the dimension is always there —
    # but a lane can still come back unusable, and the line is not allowed to vanish with it.
    assert "which is always there" in flat
    assert "came back unusable, compare the ask against the diff yourself" in flat
    # Reported whatever the dimension rated, and never fixed by unwinding the work.
    assert "Report the line whatever the dimension rated" in procedure
    assert "is not a finding to fix" in procedure


def test_every_flavour_still_defers_to_the_shared_procedure():
    for provider in ("claude", "codex", "cursor"):
        skill = (ROOT / "skills" / "review-fix-pr" / provider / "SKILL.md").read_text()
        assert "PROCEDURE.md" in skill
