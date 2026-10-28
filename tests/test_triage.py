from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentdeck.git_context import GitContext, PullRequestContext
from agentdeck.models import Capability, PendingInteraction, Session, SessionStatus
from agentdeck.triage import (
    CAUSE_AGENT_HANDOFF,
    CAUSE_BLOCKED_ISSUE,
    CAUSE_DIED_MID_TURN,
    CAUSE_LOGIN_EXPIRED,
    CAUSE_PENDING_INTERACTION,
    CAUSE_REPORTED_STATE,
    CAUSE_SESSION_WAITING,
    CAUSE_STALLED,
    CAUSE_TURN_FAILED,
    CAUSES,
    KIND_FINISHED,
    KIND_WAITING,
    AssistantInsight,
    card_priority,
    issue_is_closed,
    issue_is_merged,
    pill_review_commit,
    presented_attention_kind,
    resolve_deckhand_status,
    resolve_work_item_pill,
    stopped_card,
)


def _session(**overrides) -> Session:
    values = dict(
        key="codex:test:sid",
        account_key="codex:test",
        session_id="sid",
        status=SessionStatus.IDLE,
        last_role="agent",
        last_text="Finished the work.",
    )
    values.update(overrides)
    return Session(**values)


def _trigger(
    session: Session,
    *,
    context=None,
    interaction=None,
    report=None,
    now=None,
):
    """Name the card for a chat that has just stopped working.

    ``context`` is accepted and ignored: since #1450 nothing about GitHub reaches
    this decision, so a caller passing one is asserting that it changes nothing.
    """
    return stopped_card(session, interaction, reported_state=report)


# The three branches a chat's age never retires: only a person can clear them, so
# they stay worth saying however long they have waited (#944).
_AGE_EXEMPT_CAUSES = frozenset(
    {CAUSE_PENDING_INTERACTION, CAUSE_SESSION_WAITING, CAUSE_BLOCKED_ISSUE}
)


def test_pending_interaction_and_resting_question_are_waiting():
    interaction = PendingInteraction(
        id="i", kind="approval", thread_id="t", turn_id="u", title="Approve"
    )
    assert _trigger(_session(thinking=True), interaction=interaction).kind == KIND_WAITING
    assert _trigger(_session(question="Which?"), report="finished").kind == KIND_WAITING




def test_kanban_blocked_marker_and_stall_collapse_to_waiting():
    blocked = _session(
        worker_type="kanban",
        issue_status_kind="open",
        last_text="claude:blocked pending a decision",
    )
    assert _trigger(blocked).kind == KIND_WAITING
    assert _trigger(_session(stalled=True)).kind == KIND_WAITING






def test_shipped_pr_does_not_complete_the_session_handoff():
    shipped = GitContext(
        "eerovil/agentdeck",
        "feature",
        False,
        (PullRequestContext("eerovil/agentdeck", 1, "Done", "u", "merged"),),
    )
    assert _trigger(_session(), context=shipped, report="waiting").kind == KIND_WAITING
    assert _trigger(_session(), context=shipped, report="finished").kind == KIND_FINISHED
    assert _trigger(_session(), context=shipped).kind == KIND_FINISHED






def _branch_scenarios():
    """One scenario per branch ``structured_trigger`` can return a card from."""
    interaction = PendingInteraction(
        id="i", kind="approval", thread_id="t", turn_id="u", title="Approve"
    )
    open_pr = GitContext(
        "eerovil/agentdeck",
        "feature",
        False,
        (PullRequestContext("eerovil/agentdeck", 1, "Review", "u", "open"),),
    )
    return [
        (
            CAUSE_PENDING_INTERACTION,
            dict(session=_session(thinking=True), interaction=interaction),
        ),
        (CAUSE_SESSION_WAITING, dict(session=_session(question="Which?"))),
        (
            CAUSE_BLOCKED_ISSUE,
            dict(
                session=_session(
                    worker_type="kanban",
                    issue_status_kind="open",
                    last_text="claude:blocked pending a decision",
                )
            ),
        ),
        (CAUSE_STALLED, dict(session=_session(stalled=True))),
        (
            CAUSE_DIED_MID_TURN,
            dict(
                session=_session(
                    last_role="tool",
                    capabilities=frozenset({Capability.INJECT}),
                )
            ),
        ),
        (
            CAUSE_DIED_MID_TURN,
            dict(
                session=_session(
                    thinking=True,
                    last_progress=datetime.now(UTC) - timedelta(minutes=11),
                )
            ),
        ),
        (CAUSE_TURN_FAILED, dict(session=_session(turn_failed=True))),
        (CAUSE_REPORTED_STATE, dict(session=_session(), report="waiting")),
        (
            CAUSE_AGENT_HANDOFF,
            dict(session=_session(last_text=None), context=open_pr),
        ),
        (CAUSE_AGENT_HANDOFF, dict(session=_session())),
    ]














def test_cause_is_carried_but_never_distinguishes_two_cards():
    """Push identity stays (session_key, headline), so cause is not compared."""
    reported = _trigger(_session(), report="waiting")
    waiting = _trigger(_session(question="Which?"))
    assert reported.cause != waiting.cause
    assert reported == waiting
    assert AssistantInsight("a", "waiting", "waiting").cause == ""


def test_waiting_sorts_before_finished_and_cards_are_not_deduplicated_here():
    waiting = AssistantInsight("a", "waiting", "waiting")
    finished = AssistantInsight("b", "finished", "finished")
    assert card_priority(waiting) < card_priority(finished)


def test_status_uses_attention_and_lifecycle_states():
    session = _session()
    waiting = AssistantInsight(session.key, "waiting", "waiting")
    finished = AssistantInsight(session.key, "finished", "finished")
    assert resolve_deckhand_status(session, waiting).state == "waiting"
    assert resolve_deckhand_status(session, finished).state == "finished"
    assert resolve_deckhand_status(session, finished, merged=True).state == "merged"
    assert presented_attention_kind(KIND_FINISHED, merged=True) == "merged"
    assert presented_attention_kind(KIND_WAITING, merged=True) == KIND_WAITING


def test_a_finished_handoff_whose_issue_closed_reads_closed():
    """A closed issue is as much a lifecycle end as a merged PR (#1113)."""
    finished = AssistantInsight("codex:test:sid", "finished", "finished")

    for kind in ("done", "dropped", "closed"):
        session = _session(issue_status_kind=kind)
        assert issue_is_closed(session)
        assert resolve_deckhand_status(
            session,
            finished,
        ).state == "closed"

    still_open = _session(issue_status_kind="open")
    assert not issue_is_closed(still_open)
    assert resolve_deckhand_status(
        still_open,
        finished,
    ).state == "finished"
    # No dispatched issue at all is not a closed one.
    assert not issue_is_closed(_session())

    assert presented_attention_kind(KIND_FINISHED, issue_closed=True) == "closed"
    assert presented_attention_kind(KIND_WAITING, issue_closed=True) == "closed"
    # A merged pull request is the more specific fact and keeps its own label.
    assert (
        presented_attention_kind(KIND_FINISHED, merged=True, issue_closed=True) == "merged"
    )


def test_a_finished_handoff_whose_dispatched_item_merged_reads_merged():
    """A merged work item colours the row even with no registered PR (#1744)."""
    finished = AssistantInsight("codex:test:sid", "finished", "finished")

    merged_item = _session(issue_status_kind="merged")
    assert issue_is_merged(merged_item)
    # A merge is not a closure: `closed` must not claim it out from under `merged`.
    assert not issue_is_closed(merged_item)
    assert resolve_deckhand_status(
        merged_item,
        finished,
        # No registered pull request, so nothing else can supply the merge.
        merged=False,
    ).state == "merged"

    for kind in ("open", "done", "dropped", "closed", None):
        assert not issue_is_merged(_session(issue_status_kind=kind))















# --- naming a stop (#1450) ---------------------------------------------------
#
# ``stopped_card`` no longer decides whether there is attention — the working ->
# not-working edge did that before it was called — so it always returns a card
# and these tests are about which words it picks.


def test_a_stop_always_produces_a_trigger():
    """The plainest stop there is: an agent replied and went quiet."""
    card = _trigger(_session(last_role="agent", last_text="Done."))

    assert (card.kind, card.cause) == (KIND_FINISHED, CAUSE_AGENT_HANDOFF)


def test_a_stop_with_nothing_to_say_still_produces_a_card():
    """No last message, no report, no failure — it still stopped.

    Before #1450 this returned None and the chat vanished from the deck. The
    operator asked for an inbox that never forgets, so silence is a handoff too.
    """
    card = _trigger(_session(last_role="tool", last_text=""))

    assert card.kind == KIND_FINISHED


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"question": "Proceed?"}, CAUSE_SESSION_WAITING),
        ({"stalled": True}, CAUSE_STALLED),
        ({"turn_failed": True}, CAUSE_TURN_FAILED),
        # #3354: the account's sign-in expired, a person's to fix rather than AgentDeck's bug.
        (
            {
                "turn_failed": True,
                "last_text": "Failed to authenticate: OAuth session expired and could not be"
                " refreshed",
            },
            CAUSE_LOGIN_EXPIRED,
        ),
    ],
)
def test_the_reason_it_stopped_picks_the_words(overrides, expected):
    assert _trigger(_session(**overrides)).cause == expected


def test_a_pending_interaction_outranks_every_other_reason():
    """Most specific fact wins: something is literally waiting to be answered."""
    card = _trigger(
        _session(question="Proceed?", stalled=True, turn_failed=True),
        interaction=PendingInteraction(
            id="i1",
            kind="permission",
            thread_id="t1",
            turn_id=None,
            title="Allow?",
        ),
    )

    assert card.cause == CAUSE_PENDING_INTERACTION


def test_github_state_is_never_a_reason_a_chat_stopped():
    """A merged pull request does not change what the chat did.

    ``context`` is accepted and ignored; this pins that, because reintroducing a
    GitHub read here would reintroduce attention the chat never earned.
    """
    session = _session(last_role="agent", last_text="Shipped it.")
    merged = GitContext(
        repository="eerovil/agentdeck",
        branch="agent/issue-1450",
        dirty=False,
        pull_requests=(
            PullRequestContext(
                repository="eerovil/agentdeck",
                number=1,
                title="Ship the inbox",
                url="url",
                status="merged",
            ),
        ),
    )

    assert _trigger(session, context=merged) == _trigger(session)


def test_every_card_records_the_branch_that_named_it():
    for overrides in (
        {"last_role": "agent", "last_text": "Done."},
        {"question": "Proceed?"},
        {"stalled": True},
        {"turn_failed": True},
    ):
        assert _trigger(_session(**overrides)).cause in CAUSES


def _pull(
    number: int,
    status: str,
    *,
    draft: bool = False,
    head_sha: str | None = None,
    repository: str = "eerovil/agentdeck",
) -> PullRequestContext:
    return PullRequestContext(
        repository=repository,
        number=number,
        title="Ship it",
        url="url",
        status=status,
        draft=draft,
        head_sha=head_sha,
    )


def _git(*pulls: PullRequestContext, head_sha: str | None = None) -> GitContext:
    return GitContext(
        repository="eerovil/agentdeck",
        branch="agent/issue-1690",
        dirty=False,
        pull_requests=pulls,
        head_sha=head_sha,
    )


def test_work_item_pill_prefers_the_pull_request_over_the_issue():
    """A chat that reached a pull request is read through it, never through both."""
    session = _session(issue_status="open", issue_status_kind="open")
    pill = resolve_work_item_pill(session, _git(_pull(7, "open")))

    assert pill is not None
    assert pill.label == "pr open"
    assert pill.title == "pull request #7 is open"
    assert pill.is_open
    assert "issue" not in pill.label


def test_work_item_pill_falls_back_to_the_issue():
    session = _session(issue_status="closed", issue_status_kind="closed")

    pill = resolve_work_item_pill(session, _git())

    assert pill is not None
    assert pill.label == "issue closed"
    assert not pill.is_open


def test_work_item_pill_is_absent_without_an_issue_or_a_pull_request():
    """No work item means no words — the row falls back to a colour-only dot."""
    assert resolve_work_item_pill(_session(), None) is None
    assert resolve_work_item_pill(_session(), _git()) is None


def test_work_item_pill_reads_a_draft_pull_request_as_open():
    pill = resolve_work_item_pill(_session(), _git(_pull(9, "open", draft=True)))

    assert pill is not None
    assert pill.label == "pr draft"
    assert pill.is_open


def test_work_item_pill_speaks_for_the_pull_request_still_wanting_something():
    """Open outranks merged outranks closed, whatever order they resolved in."""
    pill = resolve_work_item_pill(
        _session(), _git(_pull(9, "closed"), _pull(8, "merged"), _pull(7, "open"))
    )

    assert pill is not None
    assert pill.label == "pr open"
    assert pill.title == "pull request #7 is open"

    shipped = resolve_work_item_pill(
        _session(), _git(_pull(9, "closed"), _pull(8, "merged"))
    )

    assert shipped is not None
    assert shipped.label == "pr merged"
    assert not shipped.is_open


# --- the reviewed-commit tick (#2121) ---------------------------------------


def test_the_pill_commit_is_the_pull_request_head_not_the_checkout():
    """What would merge is the pull request's head, so that is what is claimed."""
    git = _git(_pull(7, "open", head_sha="a" * 40), head_sha="b" * 40)

    assert pill_review_commit(git) == ("eerovil/agentdeck", "a" * 40)


def test_the_pill_commit_is_the_pull_requests_own_repository():
    """A cross-repository pull request is reviewed where it lives, not where the
    checkout is, and the verdict is keyed by that repository."""
    git = _git(_pull(7, "open", head_sha="c" * 40, repository="eerovil/trailhead"))

    assert pill_review_commit(git) == ("eerovil/trailhead", "c" * 40)


def test_the_pill_commit_falls_back_to_the_checkout_head():
    """A row built before head shas were stored still has a commit to speak for
    — the one the chat page's own review chip means."""
    git = _git(_pull(7, "open"), head_sha="d" * 40)

    assert pill_review_commit(git) == ("eerovil/agentdeck", "d" * 40)


def test_the_pill_commit_is_absent_without_both_halves():
    """Naming one half is naming no commit: there is nothing to check a verdict
    against, so the row claims nothing."""
    assert pill_review_commit(None) is None
    assert pill_review_commit(_git()) is None
    assert pill_review_commit(_git(_pull(7, "open"))) is None
    assert (
        pill_review_commit(
            GitContext(repository=None, branch=None, dirty=False, head_sha="e" * 40)
        )
        is None
    )


def test_the_pill_speaks_for_the_preferred_pull_requests_head():
    """The tick follows the pull request the pill already speaks for, not some
    other one the chat happens to have open."""
    git = _git(
        _pull(9, "closed", head_sha="9" * 40),
        _pull(7, "open", head_sha="7" * 40),
    )

    assert pill_review_commit(git) == ("eerovil/agentdeck", "7" * 40)


def test_a_reviewed_pill_wears_a_tick_and_names_the_commit():
    pill = resolve_work_item_pill(
        _session(), _git(_pull(7, "open")), reviewed_sha="abc1234"
    )

    assert pill is not None
    assert pill.label == "pr open ✓"
    assert pill.title == (
        "pull request #7 is open (latest commit abc1234 passed review)"
    )
    # The rendered title is "work half · Deckhand state", and the optimistic
    # mark-done layer keeps the work half with `split(' · ')[0]`. A third
    # segment here would vanish the moment someone pressed ✓ on the row.
    assert " · " not in pill.title
    assert pill.reviewed_sha == "abc1234"
    assert pill.review_note == "latest commit abc1234 passed review"
    # The tick says nothing about whether the work item still wants something,
    # which is what the list's "hide closed" filter reads.
    assert pill.is_open


def test_an_unreviewed_pill_is_exactly_what_it_was_before():
    """No verdict must leave the pill byte-for-byte as it was, so the tick can
    never be read as the absence of one."""
    for reviewed in ("", "   "):
        pill = resolve_work_item_pill(
            _session(), _git(_pull(7, "open")), reviewed_sha=reviewed
        )

        assert pill is not None
        assert pill.label == "pr open"
        assert pill.title == "pull request #7 is open"
        assert pill.reviewed_sha == ""
        assert pill.review_note == ""


def test_the_tick_rides_whichever_work_item_the_pill_names():
    """A bare issue status is a pill too, and a reviewed commit is a fact about the
    chat rather than about which half won the pill."""
    issue = resolve_work_item_pill(
        _session(issue_status="merged", issue_status_kind="done"),
        _git(),
        reviewed_sha="abc1234",
    )

    assert issue is not None and issue.label == "issue merged ✓"
    assert issue.reviewed_sha == "abc1234"


def test_a_row_with_no_work_item_has_no_pill_to_tick():
    """The colour-only dot has no words, so there is nowhere to say which commit
    passed — and a tick with no commit named is a claim nobody can check."""
    assert resolve_work_item_pill(_session(), None, reviewed_sha="abc1234") is None
