"""The session chip for the current commit and the review it can start (#1127)."""

from __future__ import annotations

from datetime import UTC, datetime

from agentdeck.git_context import GitContext
from agentdeck.head_review import REVIEW_VERDICTS, review_brief, reviewable_commit
from agentdeck.inject import DelegationStatus
from agentdeck.models import (
    Capability,
    HeadReviewLaunch,
    PollerReviewVerdict,
    Session,
    SessionStatus,
)
from agentdeck.state import AppState
from agentdeck.web.render import session_head_review_chip

SHA = "a" * 40
OTHER_SHA = "b" * 40


class _Injector:
    """Only the one thing the chip asks an injector: is that review still going?"""

    def __init__(self, statuses=None) -> None:
        self._statuses = dict(statuses or {})

    def delegation_status(self, delegation_id: str) -> DelegationStatus | None:
        return self._statuses.get(delegation_id)


def _session() -> Session:
    return Session(
        key="claude_code:test:sid1",
        account_key="claude_code:test",
        session_id="sid1",
        status=SessionStatus.LIVE,
        capabilities=frozenset({Capability.TRANSCRIPT}),
    )


def _context(**kwargs) -> GitContext:
    values = {
        "repository": "eerovil/agentdeck",
        "branch": "agent/issue-1127",
        "dirty": False,
        "head_sha": SHA,
    }
    values.update(kwargs)
    return GitContext(**values)


def _verdict(verdict: str = "pass", sha: str = SHA) -> PollerReviewVerdict:
    now = datetime.now(UTC)
    return PollerReviewVerdict(
        repo="eerovil/agentdeck",
        sha=sha,
        verdict=verdict,
        result="none",
        reviewed_at=now,
        recorded_at=now,
    )


def _state() -> AppState:
    return AppState(None, None)


def test_a_commit_nobody_reviewed_offers_a_review():
    chip = session_head_review_chip(_session(), _context(), _state(), _Injector())

    assert chip is not None
    assert chip.state == "unreviewed"
    assert chip.short_sha == SHA[:7]
    assert chip.label == f"{SHA[:7]} · review & fix?"
    assert chip.sha == SHA


def test_a_recorded_verdict_replaces_the_offer_with_the_verdict():
    state = _state()
    state.record_review_verdict(_verdict("changes-needed"))

    chip = session_head_review_chip(_session(), _context(), state, _Injector())

    assert chip is not None
    assert chip.state == "reviewed"
    assert chip.verdict == "changes-needed"
    assert chip.verdict_kind == "changes"
    assert chip.label == f"{SHA[:7]} · changes-needed"


def test_a_verdict_about_another_commit_is_not_this_commit_s_verdict():
    state = _state()
    state.record_review_verdict(_verdict(sha=OTHER_SHA))

    chip = session_head_review_chip(_session(), _context(), state, _Injector())

    assert chip is not None
    assert chip.state == "unreviewed"


def test_the_repository_spelling_does_not_decide_whether_a_commit_is_reviewed():
    # A verdict carries the repository GitHub named; the chip asks with whatever
    # the checkout's own remote says. Those are one repository.
    state = _state()
    state.record_review_verdict(_verdict())

    chip = session_head_review_chip(
        _session(), _context(repository="EeroVil/AgentDeck"), state, _Injector()
    )

    assert chip is not None
    assert chip.state == "reviewed"


def test_a_running_review_is_shown_as_running_and_links_to_its_chat():
    state = _state()
    state.note_head_review_launch(
        HeadReviewLaunch(
            repo="eerovil/agentdeck",
            sha=SHA,
            delegation_id="deleg-1",
            started_at=datetime.now(UTC),
        )
    )
    injector = _Injector(
        {
            "deleg-1": DelegationStatus(
                "deleg-1", "running", "codex:test", session_key="codex:test:thread-9"
            )
        }
    )

    chip = session_head_review_chip(_session(), _context(), state, injector)

    assert chip is not None
    assert chip.state == "reviewing"
    assert chip.review_session_key == "codex:test:thread-9"
    assert chip.label == f"{SHA[:7]} · reviewing…"


def test_a_review_that_ended_without_a_verdict_offers_the_button_again():
    # Every way a launch can stop being a running review — it failed, it finished
    # without reporting, or its record has aged out — has to leave a pressable
    # chip rather than one stuck on "reviewing…".
    state = _state()
    state.note_head_review_launch(
        HeadReviewLaunch(
            repo="eerovil/agentdeck",
            sha=SHA,
            delegation_id="deleg-1",
            started_at=datetime.now(UTC),
        )
    )
    for status in (
        DelegationStatus("deleg-1", "failed", "codex:test", reason="no headroom"),
        DelegationStatus("deleg-1", "complete", "codex:test", session_key="codex:test:t"),
        None,
    ):
        injector = _Injector({"deleg-1": status} if status is not None else {})
        chip = session_head_review_chip(_session(), _context(), state, injector)
        assert chip is not None
        assert chip.state == "unreviewed"


def test_there_is_no_chip_without_a_commit_to_name():
    state, injector = _state(), _Injector()

    assert session_head_review_chip(_session(), None, state, injector) is None
    assert (
        session_head_review_chip(_session(), _context(head_sha=None), state, injector)
        is None
    )
    assert (
        session_head_review_chip(
            _session(), _context(repository=None), state, injector
        )
        is None
    )


def test_uncommitted_changes_are_carried_on_the_chip_rather_than_hiding_it():
    chip = session_head_review_chip(
        _session(), _context(dirty=True), _state(), _Injector()
    )

    assert chip is not None
    assert chip.dirty is True
    assert chip.state == "unreviewed"
    assert chip.label == f"{SHA[:7]} · commit first"


def test_reviewable_commit_follows_the_branch_rule():
    assert reviewable_commit(_context()) == ("eerovil/agentdeck", SHA)
    assert reviewable_commit(None) is None
    assert reviewable_commit(_context(head_sha=None)) is None
    assert reviewable_commit(_context(repository="")) is None


def test_the_brief_names_the_commit_and_the_call_that_records_the_verdict():
    brief = review_brief(
        repo="eerovil/agentdeck",
        sha=SHA,
        base_url="http://127.0.0.1:8756",
        branch="agent/issue-1127",
    )

    assert SHA in brief
    assert "eerovil/agentdeck" in brief
    assert "agent/issue-1127" in brief
    assert "http://127.0.0.1:8756/api/review-verdicts" in brief
    for verdict in REVIEW_VERDICTS:
        assert verdict in brief
    assert "Run the installed `review-fix-pr` skill exactly once" in brief
    assert "do not substitute `rate-my-pr`" in brief
    assert "apply supported findings, commit them" in brief
    assert "Do not merge or deploy" in brief
    assert "Use `pass` only when the skill found nothing to change" in brief
    assert "Use `changes-needed` when the skill changed" in brief
    assert "uncommitted changes" not in brief


def test_the_brief_checks_the_live_checkout_before_the_skill_runs():
    brief = review_brief(
        repo="eerovil/agentdeck", sha=SHA, base_url="http://127.0.0.1:8756"
    )

    assert "`git rev-parse HEAD` is exactly the named commit" in brief
    assert "`git status --porcelain` is empty" in brief
    assert "Stop without recording a verdict if either check fails" in brief
    assert "must not absorb" in brief


def test_a_verdict_recorded_anywhere_is_seen_by_the_commit_it_is_about():
    state = _state()

    assert state.review_verdict("eerovil/agentdeck", SHA) is None
    state.record_review_verdict(_verdict())
    recorded = state.review_verdict("eerovil/agentdeck", SHA)
    assert recorded is not None
    assert recorded.verdict == "pass"
    # Reading a commit nobody reviewed is remembered too: the chip asks on every
    # stream tick, and the answer only changes when a verdict is recorded.
    assert state.review_verdict("eerovil/agentdeck", OTHER_SHA) is None
    assert ("eerovil/agentdeck", OTHER_SHA) in state._review_verdicts
