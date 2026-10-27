"""#181: choosing who conducts the Builder interview, and the brief that starts it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from agentdeck.poller_builder import (
    BuilderBrief,
    ContinuationBrief,
    add_leg,
    claim_handover,
    hold,
    is_project_id,
    next_account,
    plan_launch,
    read_run,
    read_runs,
    record_launch,
    run_path,
    usable,
)


@dataclass
class Usage:
    five_hour_pct: float | None = None
    seven_day_pct: float | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


_LABELS = {"claude_code:main": "main", "claude_code:alt": "alt", "codex:one": "codex"}


# --- can an account take the interview ---

def test_a_fresh_account_is_usable():
    ok, reason = usable(Usage(five_hour_pct=10.0, seven_day_pct=20.0))
    assert ok is True
    assert "20%" in reason


def test_high_usage_remains_usable():
    assert usable(Usage(five_hour_pct=99.0, seven_day_pct=100.0))[0] is True


def test_an_account_whose_usage_cannot_be_read_is_still_offered():
    ok, reason = usable(None)
    # Refusing to start because a usage probe is stale would be worse than falling back later.
    assert ok is True
    assert reason == "usage unknown"


def test_an_old_near_full_snapshot_is_still_observational():
    stale = Usage(five_hour_pct=99.0, fetched_at=datetime.now(UTC) - timedelta(hours=3))
    ok, reason = usable(stale)
    assert ok is True
    assert reason == "99% used"


def test_a_snapshot_with_no_fetch_time_is_still_observational():
    @dataclass
    class Undated:
        five_hour_pct: float | None = 99.0

    ok, reason = usable(Undated())
    assert ok is True
    assert reason == "99% used"


# --- planning the launch ---

def test_the_primary_runs_it_when_it_has_usage_left():
    plan = plan_launch(
        ["claude_code:main", "claude_code:alt"],
        {"claude_code:main": Usage(10.0), "claude_code:alt": Usage(10.0)},
        labels=_LABELS,
    )
    assert plan.ok
    assert plan.account_key == "claude_code:main"
    assert plan.fallbacks == ("claude_code:alt",)
    assert "main" in plan.detail


def test_high_usage_does_not_move_the_interview_off_the_primary():
    plan = plan_launch(
        ["claude_code:main", "claude_code:alt", "codex:one"],
        {
            "claude_code:main": Usage(99.0),
            "claude_code:alt": Usage(98.0),
            "codex:one": Usage(4.0),
        },
        labels=_LABELS,
    )
    assert plan.account_key == "claude_code:main"
    # The operator's own order is preserved, and usage remains visible.
    assert [choice.key for choice in plan.considered][:2] == [
        "claude_code:main",
        "claude_code:alt",
    ]
    assert all(choice.usable for choice in plan.considered[:2])
    assert "99%" in plan.considered[0].reason


def test_high_usage_on_every_account_does_not_hold_the_builder():
    plan = plan_launch(
        ["claude_code:main", "claude_code:alt"],
        {"claude_code:main": Usage(99.0), "claude_code:alt": Usage(100.0)},
        labels=_LABELS,
    )
    assert plan.ok
    assert plan.holding is False
    assert plan.account_key == "claude_code:main"


def test_an_empty_offer_is_not_a_launch():
    assert plan_launch([], {}).ok is False


def test_the_first_chosen_account_is_never_also_a_fallback():
    plan = plan_launch(
        ["claude_code:main", "claude_code:alt"],
        {"claude_code:main": Usage(99.0), "claude_code:alt": Usage(1.0)},
    )
    assert plan.account_key == "claude_code:main"
    assert plan.account_key not in plan.fallbacks


# --- resuming when one runs dry mid-interview ---

def test_the_next_account_after_the_current_one_takes_over():
    order = ["claude_code:main", "claude_code:alt", "codex:one"]
    usage = {"claude_code:main": Usage(99.0), "claude_code:alt": Usage(5.0)}
    assert next_account(order, "claude_code:main", usage) == "claude_code:alt"


def test_a_builder_never_lands_back_on_an_account_it_exhausted():
    order = ["claude_code:main", "claude_code:alt"]
    usage = {"claude_code:main": Usage(2.0), "claude_code:alt": Usage(3.0)}
    # main is fresh again on paper, but the interview has already moved past it.
    assert next_account(order, "claude_code:alt", usage) == ""


def test_next_account_preserves_order_regardless_of_usage():
    order = ["a", "b", "c"]
    usage = {"b": Usage(99.0), "c": Usage(1.0)}
    assert next_account(order, "a", usage) == "b"


def test_nobody_left_is_an_empty_answer():
    assert next_account(["a", "b"], "b", {"a": Usage(1.0)}) == ""


# --- the durable run: what continues when a session cannot (#268) ---

def _launch(root, **overrides):
    fields = {
        "manifest": "/repo/poller.toml",
        "overlay": "/host/overlay.toml",
        "cwd": "/repo",
        "order": ["claude_code:main", "claude_code:alt", "codex:one"],
        "account_key": "claude_code:main",
        "session_token": "tok-1",
    }
    project = overrides.pop("project", "reference")
    fields.update(overrides)
    return record_launch(root, project, **fields)


def test_a_launch_is_recorded_so_the_run_outlives_the_request(tmp_path):
    _launch(tmp_path)

    # Read back through a fresh call: nothing may live only in the launching process.
    run = read_run(tmp_path, "reference")
    assert run is not None
    assert run.manifest == "/repo/poller.toml"
    assert run.overlay == "/host/overlay.toml"
    assert run.cwd == "/repo"
    assert run.order == ("claude_code:main", "claude_code:alt", "codex:one")
    assert run.account_key == "claude_code:main"
    assert run.leg.session_token == "tok-1"


def test_an_unrecorded_project_is_nothing_not_a_crash(tmp_path):
    assert read_run(tmp_path, "reference") is None
    assert read_runs(tmp_path) == []


def test_a_run_survives_a_torn_write_through_its_backup(tmp_path):
    _launch(tmp_path)
    add_leg(tmp_path, "reference", account_key="claude_code:alt", session_token="tok-2")
    run_path(tmp_path, "reference").write_text("{ not json")

    # A half-written file is not a lost run: the last valid value is still on disk.
    run = read_run(tmp_path, "reference")
    assert run is not None and run.account_key == "claude_code:main"


def test_a_project_id_may_only_be_a_plain_name():
    # The id names a file under the poller root, so traversal is not a naming style.
    assert is_project_id("reference") is True
    assert is_project_id("ref-2.0_x") is True
    assert is_project_id("../../etc/passwd") is False
    assert is_project_id("a/b") is False
    assert is_project_id("") is False


def test_the_current_account_is_the_last_leg_and_the_rest_are_its_fallbacks(tmp_path):
    _launch(tmp_path)
    run = add_leg(tmp_path, "reference", account_key="claude_code:alt", session_token="tok-2")
    assert run.account_key == "claude_code:alt"
    assert run.fallbacks == ("codex:one",)
    # The order itself is the operator's and is never rewritten by a handover.
    assert run.order == ("claude_code:main", "claude_code:alt", "codex:one")


def test_a_run_asks_who_is_next_using_its_own_recorded_order(tmp_path):
    _launch(tmp_path)
    run = read_run(tmp_path, "reference")
    usage = {"claude_code:alt": Usage(99.0), "codex:one": Usage(3.0)}
    # Pairing the run's current account with a re-read of the launch form would answer "nobody".
    assert run.next_account(usage) == "claude_code:alt"


def test_a_repeated_press_is_the_same_handover_not_a_second_one(tmp_path):
    _launch(tmp_path)
    run = read_run(tmp_path, "reference")
    intent = run.handover_intent

    assert claim_handover(tmp_path, "reference", intent) is not None
    # Two legs on one draft is the outcome this exists to prevent.
    assert claim_handover(tmp_path, "reference", intent) is None
    assert read_run(tmp_path, "reference").handover_intent != intent


def test_a_claim_is_durable_before_the_successor_starts(tmp_path):
    _launch(tmp_path)
    claim_handover(tmp_path, "reference", read_run(tmp_path, "reference").handover_intent)
    # Nothing has been added yet — an uncertain handover must read as done, not as untried.
    assert read_run(tmp_path, "reference").attempts == 1
    assert len(read_run(tmp_path, "reference").legs) == 1


def test_a_stale_intent_id_is_refused(tmp_path):
    _launch(tmp_path)
    assert claim_handover(tmp_path, "reference", "reference:handover:7") is None
    assert claim_handover(tmp_path, "reference", "") is None


def test_running_out_of_accounts_holds_visibly(tmp_path):
    _launch(tmp_path, account_key="codex:one")
    run = read_run(tmp_path, "reference")
    assert run.next_account({}) == ""

    held = hold(tmp_path, "reference", "every remaining account is out of usage")
    # A run that quietly looked finished would be the worst outcome.
    assert held.holding is True
    assert "out of usage" in held.detail
    assert read_run(tmp_path, "reference").holding is True


def test_a_started_successor_clears_the_hold(tmp_path):
    _launch(tmp_path)
    hold(tmp_path, "reference", "nobody left")
    run = add_leg(tmp_path, "reference", account_key="claude_code:alt", session_token="tok-2")
    assert run.holding is False
    assert run.account_key == "claude_code:alt"


def test_every_recorded_run_is_listed_in_project_order(tmp_path):
    _launch(tmp_path, project="zeta")
    _launch(tmp_path, project="alpha")
    assert [run.project for run in read_runs(tmp_path)] == ["alpha", "zeta"]


# --- the brief that starts the session ---

def test_the_brief_names_the_paths_and_carries_the_playbook():
    brief = BuilderBrief(
        project="reference",
        repo_path="/repo/poller.toml",
        overlay_path="/host/overlay.toml",
        fallbacks=("claude_code:alt",),
    )
    text = brief.render()
    assert "reference" in text
    assert "/repo/poller.toml" in text and "/host/overlay.toml" in text
    assert "claude_code:alt" in text
    # The playbook is what makes an ordinary session a Builder session.
    assert "read-only" in text.lower()
    assert "not activation" in text.lower()


def test_the_brief_still_works_when_the_playbook_file_is_missing(tmp_path):
    brief = BuilderBrief(
        project="reference",
        repo_path="/repo/poller.toml",
        overlay_path="/host/overlay.toml",
        playbook=tmp_path / "absent.md",
    )
    text = brief.render()
    # A missing playbook degrades to the instruction, never to an empty prompt.
    assert "validated draft" in text
    assert "/repo/poller.toml" in text


def test_the_continuation_brief_leads_with_the_draft_and_links_the_previous_leg():
    brief = ContinuationBrief(
        project="reference",
        repo_path="/repo/poller.toml",
        overlay_path="/host/overlay.toml",
        fallbacks=("codex:one",),
        transcript_url="http://deck/sessions/claude_code:main:abc/transcript.json",
    )
    text = brief.render()
    # The same effort, not a new interview: the draft carries it and the transcript is a pointer.
    assert "same Builder effort" in text
    assert "/repo/poller.toml" in text and "/host/overlay.toml" in text
    assert "claude_code:main:abc/transcript.json" in text
    # A pointer, not an instruction to read it (#1485). The drafts carry the interview; the
    # previous leg's transcript is the whole of it and is a targeted fallback only.
    assert "as a fallback only" in text
    assert "only" in text and "specific question" in text
    assert "curl" not in text
    assert "codex:one" in text
    # Re-asking what the operator already answered is the one thing a handover must not do.
    assert "ask only for what is not" in text
    assert "not activation" in text.lower()


def test_the_continuation_brief_works_without_a_transcript_to_point_at():
    text = ContinuationBrief(
        project="reference", repo_path="/repo/poller.toml", overlay_path="/host/overlay.toml"
    ).render()
    # An unbound previous leg loses the pointer, never the draft. Anchored on the pointer's own
    # wording rather than on `curl`, which the brief no longer says either way (#1485), and not
    # on the bare word "transcript", which the playbook body uses for something else entirely.
    assert "/transcript.json" not in text
    assert "as a fallback only" not in text
    assert "/repo/poller.toml" in text


def test_the_playbook_stops_at_the_draft_and_says_who_activates():
    from agentdeck.poller_builder import PLAYBOOK

    text = PLAYBOOK.read_text()
    assert "Discovery is read-only" in text
    assert "not activation" in text
    assert "Release Controller" in text
    # It must not invite the Builder to activate what it drafted.
    assert "do not run `activate --apply`" in text.lower()
