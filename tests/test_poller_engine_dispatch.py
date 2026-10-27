"""Slice 3 of #176: dispatch directives and at-most-once acknowledgement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentdeck.poller_engine import dispatch, leases
from agentdeck.poller_engine.dispatch import DispatchDecision

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


def test_new_dispatch_mints_lease_and_defaults():
    rec: dict = {}
    d = dispatch.new_dispatch(rec, "work")
    assert d["action"] == "work"
    assert d["attempts"] == 1
    assert d["sent_at"] is None
    assert d["id"]
    assert d["generation"] == leases.current(rec)  # bumped the card's lease
    assert rec["dispatch"] is d


def test_new_dispatch_accepts_explicit_generation():
    rec: dict = {"generation": "keep"}
    d = dispatch.new_dispatch(rec, "work", generation="keep")
    assert d["generation"] == "keep"
    assert rec["generation"] == "keep"  # not bumped when generation is supplied


def test_a_steer_and_its_retries_keep_the_running_generation_and_delivery_id():
    rec = {"generation": "live"}
    steer = dispatch.new_steer(rec, ["10", "11"])
    delivery_id = steer["id"]

    dispatch.retry_steer(steer)
    directive = dispatch.build_directive(
        rec, steer, repo="store", number=5, ref="store#5"
    )

    assert rec["generation"] == "live"
    assert steer["generation"] == "live"
    assert steer["id"] == delivery_id
    assert steer["control_ids"] == ["10", "11"]
    assert directive["steer"] is True


def test_build_directive_shape_and_branch():
    rec = {"branch": "feat/x"}
    d = dispatch.new_dispatch(rec, "work", generation="g1")
    msgs = [{"id": "1", "payload": "do it"}]
    directive = dispatch.build_directive(
        rec, d, repo="store", number=7, ref="store#7", messages=msgs
    )
    assert directive == {
        "action": "work",
        "repo": "store",
        "number": 7,
        "ref": "store#7",
        "generation": "g1",
        "dispatch_id": d["id"],
        # The Run is the generation (#2142).
        "run_id": "g1",
        "messages": msgs,
        "branch": "feat/x",
    }


def test_build_directive_omits_branch_when_absent():
    rec: dict = {}
    d = dispatch.new_dispatch(rec, "work")
    directive = dispatch.build_directive(rec, d, repo="store", number=1, ref="store#1")
    assert "branch" not in directive
    assert directive["messages"] == []


def test_budget_override_is_bound_to_the_dispatch_and_directive():
    rec: dict = {}
    d = dispatch.new_dispatch(rec, "work", allow_over_budget=True)
    directive = dispatch.build_directive(rec, d, repo="store", number=1, ref="store#1")

    assert d["allow_over_budget"] is True
    assert directive["allow_over_budget"] is True


def test_a_continuation_carries_the_previous_generations_session():
    rec = {
        "generation": "old-generation",
        "run_session": {
            "session_url": "https://deck.example/sessions/claude_code:main:old",
            "generation": "old-generation",
        },
    }

    d = dispatch.new_dispatch(rec, "resume", continuation=True)
    directive = dispatch.build_directive(
        rec, d, repo="store", number=1, ref="store#1"
    )

    assert directive["previous_session_url"] == (
        "https://deck.example/sessions/claude_code:main:old"
    )
    assert rec["generation"] != "old-generation"


def test_a_continuation_does_not_carry_an_unrelated_old_session():
    rec = {
        "generation": "current-generation",
        "run_session": {
            "session_url": "https://deck.example/sessions/claude_code:main:older",
            "generation": "older-generation",
        },
    }

    d = dispatch.new_dispatch(rec, "resume", continuation=True)
    directive = dispatch.build_directive(
        rec, d, repo="store", number=1, ref="store#1"
    )

    assert "previous_session_url" not in directive


def test_an_ordinary_followup_carries_the_cards_session_too():
    """The account is chosen on headroom, so any dispatch can change provider (#2127)."""
    rec = {
        "generation": "old-generation",
        "run_session": {
            "session_url": "https://deck.example/chats/abc",
            "generation": "old-generation",
        },
    }

    d = dispatch.new_dispatch(rec, "work")
    directive = dispatch.build_directive(rec, d, repo="store", number=1, ref="store#1")

    assert directive["previous_session_url"] == "https://deck.example/chats/abc"
    # The link is not a claim that an earlier turn left work unreported.
    assert "continuation" not in directive
    assert d.get("continuation") is None


def test_an_ordinary_followup_does_not_carry_a_stale_generations_session():
    rec = {
        "generation": "current-generation",
        "run_session": {
            "session_url": "https://deck.example/chats/older",
            "generation": "older-generation",
        },
    }

    d = dispatch.new_dispatch(rec, "work")
    directive = dispatch.build_directive(rec, d, repo="store", number=1, ref="store#1")

    assert "previous_session_url" not in directive
    assert "previous_session_url" not in d


def test_a_first_dispatch_has_no_session_to_carry():
    rec: dict = {}

    d = dispatch.new_dispatch(rec, "work")
    directive = dispatch.build_directive(rec, d, repo="store", number=1, ref="store#1")

    assert "previous_session_url" not in directive


def test_a_blank_or_malformed_session_link_is_not_carried():
    for links in (
        {"session_url": "", "generation": "gen"},
        {"session_url": "   ", "generation": "gen"},
        {"session_url": None, "generation": "gen"},
        {"generation": "gen"},
        {"session_url": "https://deck.example/chats/abc"},
        {"session_url": "https://deck.example/chats/abc", "generation": None},
    ):
        rec = {"generation": "gen", "run_session": dict(links)}
        d = dispatch.new_dispatch(rec, "work")
        directive = dispatch.build_directive(
            rec, d, repo="store", number=1, ref="store#1"
        )
        assert "previous_session_url" not in directive, links


def test_a_pre_2155_comment_links_registration_is_not_carried():
    """The `comment_links` fallback is gone: #2197 migrated every card that relied on it."""
    rec = {
        "generation": "gen",
        "comment_links": {
            "session_url": "https://deck.example/chats/abc",
            "session_generation": "gen",
        },
    }

    d = dispatch.new_dispatch(rec, "work")

    assert "previous_session_url" not in d


def test_a_card_holding_no_lease_does_not_match_an_empty_recorded_generation():
    """Two absent generations must not read as "the same lease" and carry a stale link."""
    for links in (
        {"session_url": "https://deck.example/chats/abc"},
        {"session_url": "https://deck.example/chats/abc", "generation": ""},
    ):
        for rec in (
            {"run_session": dict(links)},
            {"generation": "", "run_session": dict(links)},
        ):
            d = dispatch.new_dispatch(rec, "work")
            directive = dispatch.build_directive(
                rec, d, repo="store", number=1, ref="store#1"
            )
            assert "previous_session_url" not in directive, (rec, links)


def test_a_merge_review_directive_carries_every_pull_request_it_would_release():
    """#1338: told only the issue, a reviewer reviews the one PR on its own branch."""
    rec = {"review_prs": ["docker#80", "store#3054", "abacus#1810"]}
    review = dispatch.new_dispatch(rec, "merge-review")

    directive = dispatch.build_directive(rec, review, repo="issues", number=228, ref="issues#228")

    assert directive["pull_requests"] == ["docker#80", "store#3054", "abacus#1810"]


def test_only_a_review_dispatch_carries_the_release_set():
    """A work dispatch reviews nothing, so naming a release set there would only mislead."""
    rec = {"review_prs": ["docker#80"]}
    work = dispatch.new_dispatch(rec, "work")
    empty = dispatch.new_dispatch({}, "merge-review")

    assert "pull_requests" not in dispatch.build_directive(
        rec, work, repo="issues", number=228, ref="issues#228"
    )
    assert "pull_requests" not in dispatch.build_directive(
        {}, empty, repo="issues", number=228, ref="issues#228"
    )


def test_no_directive_carries_the_retired_automerge_flag():
    """#2502: a record left with an old standing-approval arm promises no verdict."""
    rec: dict = {"auto_review": True}
    for action in ("merge-fix", "work", "merge-review"):
        directive = dispatch.build_directive(
            rec, dispatch.new_dispatch(rec, action), repo="store", number=1, ref="store#1"
        )
        assert "automerge" not in directive, action


def test_a_spec_only_card_marks_its_worker_directive():
    rec = {"spec_only": True}
    work = dispatch.new_dispatch(rec, "work")

    directive = dispatch.build_directive(
        rec, work, repo="store", number=1, ref="store#1"
    )

    assert directive["spec_only"] is True
    assert "automerge" not in directive


def test_the_build_of_an_approved_spec_marks_its_worker_directive():
    work = dispatch.new_dispatch({}, "resume")
    work["spec_approved"] = True

    directive = dispatch.build_directive({}, work, repo="store", number=1, ref="store#1")

    assert directive["spec_approved"] is True
    assert "spec_only" not in directive
    # Label put back before the build was sent: the label wins.
    held = dispatch.build_directive(
        {"spec_only": True}, work, repo="store", number=1, ref="store#1"
    )
    assert "spec_approved" not in held


def test_acknowledged_takes_priority():
    d = dispatch.new_dispatch({}, "work")
    dispatch.mark_sent(d, _NOW)
    assert dispatch.reconcile(d, acknowledged=True, now=_NOW) is DispatchDecision.ACKNOWLEDGED


def test_recently_sent_is_awaited_never_resent():
    d = dispatch.new_dispatch({}, "work")
    dispatch.mark_sent(d, _NOW)
    # 30s later, within the 90s window -> await, do not re-send (at-most-once).
    later = _NOW + timedelta(seconds=30)
    assert dispatch.reconcile(d, acknowledged=False, now=later) is DispatchDecision.AWAITING


def test_awaiting_holds_even_when_legacy_budget_flag_is_false():
    d = dispatch.new_dispatch({}, "work")
    dispatch.mark_sent(d, _NOW)
    later = _NOW + timedelta(seconds=30)
    got = dispatch.reconcile(d, acknowledged=False, now=later, budget_ok=False)
    assert got is DispatchDecision.AWAITING


def test_never_sent_retries_immediately():
    d = dispatch.new_dispatch({}, "work")  # sent_at is None
    assert dispatch.reconcile(d, acknowledged=False, now=_NOW) is DispatchDecision.RETRY


def test_timed_out_retry_ignores_the_legacy_budget_flag():
    d = dispatch.new_dispatch({}, "work")
    dispatch.mark_sent(d, _NOW)
    later = _NOW + timedelta(seconds=120)  # past the 90s window
    got = dispatch.reconcile(d, acknowledged=False, now=later, budget_ok=False)
    assert got is DispatchDecision.RETRY


def test_timed_out_within_attempts_retries():
    d = dispatch.new_dispatch({}, "work")
    dispatch.mark_sent(d, _NOW)
    later = _NOW + timedelta(seconds=120)
    assert dispatch.reconcile(d, acknowledged=False, now=later) is DispatchDecision.RETRY


def test_max_attempts_without_ack_is_exhausted():
    d = dispatch.new_dispatch({}, "work")
    d["attempts"] = dispatch.DISPATCH_MAX_ATTEMPTS
    dispatch.mark_sent(d, _NOW)
    later = _NOW + timedelta(seconds=120)
    got = dispatch.reconcile(d, acknowledged=False, now=later)
    assert got is DispatchDecision.EXHAUSTED


def test_next_attempt_keeps_the_lease_and_run_and_bumps_count():
    """A retry keeps the Run the runtime froze for this delivery id (#2824).

    A fresh generation here was refused for good as ``delivery_correlation_conflict`` when
    the first attempt had landed, and then fenced that live worker as stale.
    """
    rec: dict = {}
    d = dispatch.new_dispatch(rec, "work")
    dispatch.mark_sent(d, _NOW)
    old_gen, old_run, old_id = d["generation"], d["run_id"], d["id"]
    dispatch.next_attempt(rec, d)
    assert (d["generation"], d["run_id"], d["id"]) == (old_gen, old_run, old_id)
    assert rec["generation"] == old_gen
    assert leases.holds_or_outgoing(rec, old_gen)  # the first attempt's worker keeps its lease
    assert d["attempts"] == 2
    assert d["sent_at"] is None


def test_a_continuation_records_the_lease_it_is_taking_over_from():
    """The successor is minted before anybody has received it, so the worker it continues
    keeps its own control command until the delivery settles (#1801)."""
    rec: dict = {"generation": "gen-1"}

    d = dispatch.new_dispatch(rec, "phase", continuation=True)

    assert d["supersedes"] == "gen-1"
    assert d["generation"] == leases.current(rec) != "gen-1"
    assert leases.holds_or_outgoing(rec, "gen-1")


def test_a_retry_of_a_continuation_still_points_at_the_worker_actually_holding_the_lease():
    rec: dict = {"generation": "gen-1"}
    d = dispatch.new_dispatch(rec, "phase", continuation=True)

    dispatch.next_attempt(rec, d)

    assert d["supersedes"] == "gen-1"
    assert leases.holds_or_outgoing(rec, "gen-1")


def test_an_ordinary_dispatch_records_no_predecessor_to_spare():
    rec: dict = {"generation": "gen-1"}

    d = dispatch.new_dispatch(rec, "work")

    assert "supersedes" not in d
    assert not leases.holds_or_outgoing(rec, "gen-1")


def test_a_steer_under_the_live_lease_has_no_predecessor_to_record():
    rec: dict = {"generation": "gen-1"}

    d = dispatch.new_steer(rec, ["c1"])

    assert "supersedes" not in d
    assert d["generation"] == "gen-1"


def test_the_chat_the_host_recorded_is_carried_to_the_next_dispatch():
    """#2155: the host's record replaces the worker's `links --session-url` registration."""
    rec = {
        "generation": "gen",
        "run_session": {
            "session_url": "https://deck.example/sessions/claude_code:main:s1",
            "generation": "gen",
        },
        # A pre-#2155 registration is never read for the chat link.
        "comment_links": {
            "session_url": "https://deck.example/chats/older",
            "session_generation": "gen",
        },
    }

    d = dispatch.new_dispatch(rec, "work")
    directive = dispatch.build_directive(rec, d, repo="store", number=1, ref="store#1")

    assert directive["previous_session_url"] == (
        "https://deck.example/sessions/claude_code:main:s1"
    )


def test_a_recorded_chat_from_another_generation_is_not_carried():
    rec = {
        "generation": "gen",
        "run_session": {"session_url": "https://deck.example/sessions/x", "generation": "old"},
    }

    d = dispatch.new_dispatch(rec, "work")
    directive = dispatch.build_directive(rec, d, repo="store", number=1, ref="store#1")

    assert "previous_session_url" not in directive


def test_only_the_run_holding_the_card_records_its_chat():
    rec = {"generation": "gen"}

    dispatch.record_run_session(rec, "https://deck.example/sessions/x", "old")
    dispatch.record_run_session(rec, "", "gen")
    assert "run_session" not in rec

    dispatch.record_run_session(rec, "https://deck.example/sessions/y", "gen")
    assert rec["run_session"] == {
        "session_url": "https://deck.example/sessions/y", "generation": "gen"
    }
    # A card holding no lease records nothing, even for an empty generation.
    empty: dict = {}
    dispatch.record_run_session(empty, "https://deck.example/sessions/z", "")
    assert empty == {}

