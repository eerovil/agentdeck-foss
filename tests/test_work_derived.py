"""A Work's one status line, derived from what the deck already holds (#2895 step 3, D7).

The rule order is tested by feeding the pure function each input (D18); the card tests check
that the Work page, its category and buttons and the front page all read that one status
(#2944).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from public_copy import private_feature

from agentdeck import chat_board as _chat_board
from agentdeck import mac_runner as _mac_runner
from agentdeck.web.work_view import WORK_INPUTS_TOPIC
from agentdeck.work import WorkRef
from agentdeck.work_derived import (
    CLOSED,
    CUT_OFF,
    DIED,
    ENDED,
    FINISHED,
    HANDOFF_GRACE,
    LOGIN_EXPIRED,
    NUDGE_GRACE,
    NUDGED,
    PARKED,
    PR_HELD,
    PR_STALE,
    PR_STUCK,
    QUESTION,
    READY,
    REVIEW,
    REVIEW_ESCALATED,
    REVIEW_QUEUED,
    REVIEW_STUCK,
    REVIVING,
    SCHEDULED,
    SPEC_WAIT,
    STALLED,
    UNFINISHED,
    WAITING,
    WORKING,
    DerivedInputs,
    derived_status,
    first_sentence,
    progress_sentence,
    question_sentence,
)
from agentdeck.work_derived import (
    clock as deck_clock,
)
from agentdeck.work_facts import OutputFact, work_facts
from agentdeck.work_overview import build_work_card
from agentdeck.work_story import derived_story, front_row, work_status

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
AT = NOW - timedelta(minutes=20)
UNTIL = NOW + timedelta(hours=1)
WORK = WorkRef("eerovil/agentdeck", 2895)


def clock(value):
    return value.strftime("%H:%M") if value else ""


def status(**fields):
    return derived_status(DerivedInputs(**fields), now=NOW, clock=clock)


# --- the rule order (D7) ---


EVERYTHING = dict(
    question="Which branch should I use?",
    running=True,
    stalled=True,
    progress="Running tests",
    last_progress=AT,
    died=True,
    died_at=AT,
    closed=True,
    review_pr=12,
    waits=(("pull request eerovil/agentdeck#12", UNTIL),),
    last_text="Done. Pushed it.",
    ended_at=AT,
    pr=37,
)


@pytest.mark.parametrize(
    ("drop", "kind", "line"),
    [
        ((), QUESTION, "Needs you — Which branch should I use?"),
        (("question",), STALLED, "Needs you — no progress since 09:40"),
        (("question", "stalled"), WORKING, "Working — Running tests"),
        (("question", "running"), DIED, "Needs you — the agent stopped unexpectedly · 09:40"),
        (("question", "running", "died"), CLOSED, "Issue closed — finish it?"),
        (("question", "running", "died", "closed"), REVIEW, "In review"),
        (
            ("question", "running", "died", "closed", "review_pr"),
            WAITING,
            "Waiting on pull request eerovil/agentdeck#12, until 11:00",
        ),
        (
            ("question", "running", "died", "closed", "review_pr", "waits"),
            UNFINISHED,
            'Needs you — the agent ended without a next step · last said "Done." · 09:40',
        ),
        (
            ("question", "running", "died", "closed", "review_pr", "waits", "pr"),
            UNFINISHED,
            'Needs you — the agent ended without a next step · last said "Done." · 09:40',
        ),
    ],
)
def test_the_first_rule_that_matches_wins(drop, kind, line):
    defaults = DerivedInputs()
    fields = {
        key: (getattr(defaults, key) if key in drop else value) for key, value in EVERYTHING.items()
    }
    found = status(**fields)
    assert (found.kind, found.line) == (kind, line)


def test_a_turn_that_ended_with_no_text_just_says_so():
    assert status(ended_at=AT).line == "Needs you — the agent ended without a next step · 09:40"
    assert status().line == "Needs you — the agent ended without a next step"


def test_a_wait_on_its_own_pull_request_alone_says_what_that_waits_for():
    """#3097: "Waiting on pull request X" hid five Works stuck on something the line left out."""
    own = (("pull request o/r#9", UNTIL),)
    found = status(pr_waits=own, pr_hold="no test run started", pr_quiet_since=AT)
    assert found.line == "Waiting on pull request o/r#9 — no test run started, until 11:00"
    assert found.deadline == UNTIL  # before the stale moment, AT + 3 h
    soon = status(pr_waits=((own[0][0], None),), pr_quiet_since=AT)
    assert soon.deadline == AT + PR_STALE and soon.line == "Waiting on pull request o/r#9"
    stuck = status(pr_waits=own, pr_hold="x", pr_quiet_since=NOW - PR_STALE)
    assert stuck.kind == PR_STUCK and stuck.line == (
        "Needs you — pull request o/r#9 — x · nothing since 07:00"
    )
    findings = status(pr_waits=own, pr_findings_at=AT, pr_findings_told=True)
    assert "— review asked for changes at 09:40, sent to the agent," in findings.line
    held = status(pr_waits=own, pr_hold="held: spec-only label", pr_hold_person=True)
    assert (held.kind, held.summary) == (PR_HELD, "pull request o/r#9 held: spec-only label")
    # Any other wait still comes first, and so does a place in the merge queue.
    assert status(pr_waits=own, waits=(("its tests", None),)).line == "Waiting on its tests"
    queued = status(pr_waits=own, queue_pr=9, queue_place=1, queue_size=2)
    assert queued.kind == "queued"


def test_a_wait_names_its_nearest_deadline_and_carries_it():
    later = UNTIL + timedelta(hours=1)
    found = status(waits=(("its tests", None), ("a deploy", later), ("CI", UNTIL)))
    assert found.line == "Waiting on CI, until 11:00" and found.deadline == UNTIL
    assert status(waits=(("its tests", None),)).line == "Waiting on its tests"


def test_first_sentence_is_one_short_line():
    assert first_sentence("Done.  Pushed it.") == "Done."
    assert first_sentence("line one\nline two") == "line one"
    assert first_sentence("Is it? Yes.") == "Is it?"
    long = "x" * 300
    assert len(first_sentence(long)) == 140 and first_sentence(long).endswith("…")
    assert first_sentence("  ") == ""
    assert first_sentence("[abacus#2080](https://x/pull/2080) is ready.") == "abacus#2080 is ready."


def test_a_question_shows_the_question_not_its_preamble():
    """#2944: a question form often opens with what the agent found."""
    asked = "The alert is real evening load, not a broken rule. Raise the threshold? Or not."
    assert question_sentence(asked) == "Raise the threshold?"
    assert question_sentence("Pick one of these.") == "Pick one of these."
    assert status(question=asked).line == "Needs you — Raise the threshold?"


def test_working_shows_the_latest_progress_sentence_never_a_generic_label():
    """#2944: "Working — Using tools" says nothing."""
    assert progress_sentence("Read the code. Now running the tests.") == "Now running the tests."
    assert progress_sentence("Using tools") == "" and progress_sentence("Working…") == ""
    assert status(running=True, progress="Using tools").line == "Working"
    assert status(running=True, progress="Fixed it. Pushing.").line == "Working — Pushing."


def test_a_time_on_another_day_carries_its_date():
    """#2944: "no progress since 20:49" read as today when it was yesterday."""
    now = datetime.now(UTC)
    assert deck_clock(now) == now.astimezone().strftime("%H:%M")
    earlier = now - timedelta(days=2)
    local = earlier.astimezone()
    assert deck_clock(earlier) == f"{local.strftime('%b')} {local.day} {local.strftime('%H:%M')}"


def test_a_finished_work_stays_finished_until_a_message_wakes_it():
    assert status(finished=True, closed=True, died=True, died_at=AT).kind == FINISHED
    assert status(finished=True, closed=True, running=True).kind == WORKING


# --- on the card ---


def card(**fields):
    built = build_work_card(WORK, issue=None, title="A Work", facts=work_facts(WORK), trace=None)
    return replace(built, **fields)


@pytest.mark.parametrize(
    ("fields", "headline", "summary"),
    [
        ({"question": "Which branch?"}, "Needs you", "Which branch?"),
        ({"running": True, "progress": "Editing"}, "Working", "Editing"),
        ({"died": True, "died_at": AT}, "Needs you", "the agent stopped unexpectedly · 09:40"),
        ({"closed": True}, "Issue closed", "finish it?"),
        # #3141: the agent's live check of the merge, pending, then on the Finish line; a
        # status of its own (#3578).
        ({"closed": True, "live_pending": True, "waits": (("AgentDeck deploy abc1234", None),)},
         "Live checking", "waiting on AgentDeck deploy abc1234"),
        # With no wait: the agent's turn, then the deck's ask for a next step, say it too.
        ({"closed": True, "live_pending": True, "running": True, "progress": "Opening the page"},
         "Live checking", "Opening the page"),
        # #3578: merged with the issue still open is checked live too, asked or not, until a
        # result is recorded or another pull request opens.
        ({"merged": True, "waits": (("ski list in new admin; recheck Sentry", AT),)},
         "Live checking", "waiting on ski list in new admin; recheck Sentry, until 09:40"),
        ({"merged": True, "running": True}, "Live checking", ""),
        ({"merged": True, "live_result": "verified", "waits": (("its tests", None),)},
         "Waiting", "Waiting on its tests"),
        ({"merged": True, "pr_open": True, "running": True}, "Working", ""),
        ({"closed": True, "live_pending": True, "last_text": "Merged.", "ended_at": AT},
         "Needs you", 'no live check recorded · the agent ended without a next step · last said'
         ' "Merged." · 09:40'),
        ({"closed": True, "live_result": "verified", "live_line": "checked live: it shows"},
         "Issue closed", "finish it? · checked live: it shows"),
        ({"closed": True, "live_result": "failed", "live_line": "live check failed: 500"},
         "Needs you", "live check failed: 500"),
        # #3220: part of the merge runs nowhere yet, so it is not a Finish.
        ({"closed": True, "live_result": "not_live", "live_line": "not live yet: VPS connector"},
         "Needs you", "not live yet: VPS connector"),
        # #3297: with a wake-on of its own on the deploy, nobody is asked anything.
        ({"closed": True, "live_result": "not_live", "live_line": "not live yet: build 7",
          "waits": (("o/docker@548fcac1", None),)},
         "Live checking", "waiting on o/docker@548fcac1"),
        ({"review_pr": 9}, "In review", ""),
        ({"review_pr": 9, "review_since": AT}, "In review", "since 09:40"),
        ({"last_text": "All done.", "ended_at": AT}, "Needs you",
         'the agent ended without a next step · last said "All done." · 09:40'),
        ({"ended_at": AT}, "Needs you", "the agent ended without a next step · 09:40"),
        # #2956: the pull request is not the time, so it is not "ended at PR #37 · 09:40".
        ({"ended_at": AT, "pr": 37}, "Needs you",
         "the agent ended without a next step · 09:40"),
        ({"pr": 37}, "Needs you", "the agent ended without a next step"),
        ({"ready_pr": 40}, "Ready for merge", ""),
        ({"waits": (("its tests", None),)}, "Waiting", "Waiting on its tests"),
        ({"running": True, "stalled": True}, "Needs you", "no progress"),
        ({"cut_off": True, "cut_off_at": AT, "reviving": True}, "Waiting",
         "the runtime to revive the agent · cut off 09:40"),
        ({"cut_off": True, "cut_off_at": AT}, "Needs you",
         "the agent's process stopped mid-turn · 09:40"),
    ],
)
def test_the_work_page_reads_the_derived_line(fields, headline, summary):
    story = derived_story(card(derived=status(**fields)))
    assert (story.headline, story.summary) == (headline, summary)
    assert story.milestones == (), "a derived Work has no step strip (D1)"


@pytest.mark.parametrize(
    ("fields", "section", "category", "labels"),
    [
        ({"question": "Which branch?", "chat_href": "/chats/a"}, "needs_you", "needs_answer",
         ["Open", "Hide"]),
        ({"last_text": "All done.", "ended_at": AT}, "needs_you", "needs_retry",
         ["Message", "Open", "Hide"]),
        ({"ready_pr": 40}, "needs_you", "ready_for_merge", ["Merge", "Open", "Hide"]),
        ({"ended_at": AT, "asks": True}, "active", None, []),
        ({"died": True, "died_at": AT}, "needs_you", "needs_retry", ["Message", "Open", "Hide"]),
        ({"closed": True}, "needs_you", "issue_closed", ["Finish", "Message", "Hide"]),
        # #3187: another chat closed the issue; its own pull request still waits for Merge.
        ({"closed": True, "pr_open": True, "ready_pr": 40}, "needs_you", "ready_for_merge",
         ["Merge", "Open", "Hide"]),
        ({"closed": True, "live_result": "failed", "live_line": "live check failed: 500"},
         "needs_you", "needs_answer", ["Open", "Finish", "Hide"]),
        ({"closed": True, "live_result": "not_live", "live_line": "not live yet: VPS connector"},
         "needs_you", "needs_answer", ["Open", "Hide"]),
        ({"closed": True, "live_result": "not_live", "live_line": "not live yet: build 7",
          "waits": (("o/docker@548fcac1", None),)}, "active", None, []),
        # A real question still needs an answer, wait or not (#3297).
        ({"closed": True, "live_result": "not_live", "question": "Deploy it by hand?",
          "waits": (("o/docker@548fcac1", None),)}, "needs_you", "needs_answer",
         ["Open", "Hide"]),
        ({"closed": True, "live_pending": True, "waits": (("AgentDeck deploy", None),)},
         "active", None, []),
        ({"running": True}, "active", None, []),
        ({"waits": (("its tests", None),)}, "active", None, []),
        ({"finished": True}, "finished", None, []),
        ({"cut_off": True, "cut_off_at": AT, "reviving": True}, "active", None, []),
        ({"cut_off": True, "cut_off_at": AT}, "needs_you", "needs_retry",
         ["Message", "Open", "Hide"]),
    ],
)
def test_the_front_page_follows_the_derived_status(fields, section, category, labels):
    """#2944: one status everywhere. A Work whose page needs a person is never quiet."""
    derived = card(derived=status(**fields))
    row = front_row(derived, current=False)
    assert row.section == section
    assert work_status(derived) == derived_story(derived)
    if category is None:
        assert row.attention is None and row.buttons == ()
    else:
        assert row.attention.category == category
        assert [button.label for button in row.buttons] == labels


def test_a_closed_issues_row_names_no_pull_request_and_no_live_note():
    """#3720: ten merged pull requests and a long live note made a dozen phone lines."""
    merged = tuple(
        OutputFact(
            url=f"https://github.com/eerovil/agentdeck/pull/{n}", repository="eerovil/agentdeck",
            number=n, found_by="registered", state="merged",
        )
        for n in (3626, 3631, 3636)
    )
    live = "checked live: Deck at 953e2edc: Linux unchanged; macOS start proven"
    closed = card(outputs=merged, derived=status(closed=True, live_line=live))
    assert front_row(closed, current=False).line == "Merged · checked live"
    # The Work page keeps the note.
    assert derived_story(closed).summary == f"finish it? · {live}"
    nothing = card(outputs=merged, derived=status(closed=True, live_line="nothing to check live"))
    assert front_row(nothing, current=False).line == "Merged · nothing to check live"


def test_a_died_turn_is_reported_as_agentdecks_bug():
    row = front_row(card(derived=status(died=True, died_at=AT)), current=True)
    assert row.attention.bug == "chat_stopped"


def test_a_turn_an_expired_sign_in_failed_asks_for_a_sign_in_and_is_not_a_bug():
    """#3354: the alt account's sign-in expired, every Work chat on it failed at once with the
    CLI's "Failed to authenticate" line, and the deck filed each one as its own bug."""
    from agentdeck.web.work_view import derived_for

    expired = status(died=True, died_at=AT, login_expired="alt")
    assert expired.kind == LOGIN_EXPIRED
    assert expired.line == "Needs you — the alt account's sign-in expired · 09:40"
    row = front_row(card(derived=expired), current=True)
    assert (row.section, row.attention.category, row.attention.bug) == (
        "needs_you", "needs_retry", "",
    )
    assert "Sign the account in again" in work_status(card(derived=expired)).you
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_router=SimpleNamespace(switched=lambda repository: True),
            assistant=None,
            wake_on=None,
            control_db=None,
        )
    )
    failed = "Failed to authenticate: OAuth session expired and could not be refreshed"
    session = _session(
        turn_failed=True, last_text=failed, account_key="claude_code:alt", key="chat-a"
    )
    snapshot = SimpleNamespace(state=_state(session))
    assert derived_for(app, snapshot, WORK, card()).kind == LOGIN_EXPIRED
    # Any other failure is still a death.
    session.last_text = "API Error: 500 Internal server error"
    assert derived_for(app, snapshot, WORK, card()).kind == DIED


def test_a_turn_the_usage_limit_stopped_waits_on_the_decks_own_retry():
    """#3305: a chat hit its account's session limit and the deck had already scheduled its
    retry for the reset, yet the Work read "the agent stopped unexpectedly" and filed a bug."""
    from agentdeck.quota_retry import message_id_for
    from agentdeck.web.work_view import derived_for

    reset = NOW + timedelta(hours=1)
    waiting = status(died=True, died_at=AT, retry_at=reset)
    assert (waiting.kind, waiting.deadline) == (WAITING, reset)
    assert "usage limit" in waiting.line
    assert not getattr(front_row(card(derived=waiting), current=True).attention, "bug", "")
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_router=SimpleNamespace(switched=lambda repository: True),
            assistant=None,
            wake_on=None,
            control_db=None,
        )
    )
    session = _session(turn_failed=True, automatic_retry_at=reset, key="chat-a")
    retry = message_id_for(session)
    pending = SimpleNamespace(state=_state(session), pending_message_ids=frozenset({retry}))
    assert derived_for(app, pending, WORK, card()).kind == WAITING
    # A cancelled or failed retry leaves the schedule: then it is a death again (#2928).
    gone = SimpleNamespace(state=_state(session), pending_message_ids=frozenset())
    assert derived_for(app, gone, WORK, card()).kind == DIED


def test_a_turn_a_restart_cut_off_waits_on_its_revival_and_is_never_a_bug():
    """#3008 review: a cut-off turn read ``died``, so every restart filed ``chat_stopped``
    bugs for chats the revival was about to bring back, and for ones it could not."""
    from agentdeck import work_bugs

    reviving = status(cut_off=True, cut_off_at=AT, reviving=True)
    assert reviving.kind == REVIVING
    # No revival coming: a worker that died alone while the runtime stayed up.
    past = status(cut_off=True, cut_off_at=AT)
    assert past.kind == CUT_OFF and not past.bug
    # A turn that failed on its own is still a death, cut off or not.
    assert status(cut_off=True, cut_off_at=AT, reviving=True, died=True).kind == DIED
    rows = []
    for derived in (reviving, past):
        row = front_row(card(derived=derived), current=True)
        assert not getattr(row.attention, "bug", "")
        rows.append(SimpleNamespace(attention=row.attention, stopped_at=AT))
    assert work_bugs.due(rows, now=NOW + timedelta(hours=1)) == []
    # The comparison still files a real death.
    died = front_row(card(derived=status(died=True, died_at=AT)), current=True)
    assert work_bugs.due(
        [SimpleNamespace(attention=died.attention, stopped_at=AT)], now=NOW
    )


def test_an_ended_rows_line_quotes_the_agent():
    """my-tasks#36: the quote, and no pull request beside it (#3720)."""
    fields = dict(last_text="The final test run passed. Done.", ended_at=AT, pr=37)
    row = front_row(card(derived=status(**fields)), current=False)
    assert row.section == "needs_you"
    assert row.line == (
        'the agent ended without a next step · last said "The final test run passed."'
        " · 09:40"
    )


def test_a_question_opens_the_chat_that_asked_it():
    row = front_row(card(derived=status(question="Which?", chat_href="/chats/a")), current=True)
    assert [(b.label, b.href) for b in row.buttons][0] == ("Open", "/chats/a")


def test_a_paused_derived_work_is_listed_as_paused():
    derived = card(derived=status(last_text="Noted.", ended_at=AT), paused=True, label_paused=True)
    assert front_row(derived, current=True).section == "paused"
    assert work_status(derived).headline == "Paused"


def test_the_work_page_reads_the_same_status_as_its_row():
    from agentdeck.web.work_view import WorkDetail

    derived = card(derived=status(last_text="All done.", ended_at=AT))
    detail = WorkDetail(card=derived, trace=None, workspace=None, chats=(), verifications=())
    assert detail.line == detail.status == derived_story(derived)
    assert detail.attention == front_row(derived, current=True).attention
    assert [b.label for b in detail.buttons] == ["Message", "Open", "Hide"]


def test_the_rendered_status_box_shows_the_derived_line():
    from fastapi.templating import Jinja2Templates

    from agentdeck import app as app_mod
    from agentdeck.web import render as render_mod
    from agentdeck.web.work_view import WorkDetail, render_work_sections

    templates = Jinja2Templates(directory=str(app_mod._TEMPLATES_DIR))
    render_mod.register_filters(templates)
    derived = card(derived=status(last_text="All done.", ended_at=AT))
    detail = WorkDetail(card=derived, trace=None, workspace=None, chats=(), verifications=())
    box = dict(render_work_sections(templates, detail))["status"]
    assert 'data-wd-derived="unfinished"' in box and "last said" in box and "All done." in box
    # The category line above the box does not repeat that line (#2944).
    assert box.count("All done.") == 1 and "Needs retry" in box


def test_a_work_without_a_derived_status_keeps_todays():
    work = card()
    assert work.derived is None
    assert work_status(work).milestones, "today's status still has its step strip"


# --- the inputs, gathered from the deck ---


def _session(**fields):
    base = dict(
        thinking=False,
        stalled=False,
        activity=None,
        last_progress=None,
        turn_failed=False,
        last_text="",
        last_role="agent",
        last_message_at=AT,
    )
    return SimpleNamespace(**{**base, **fields})


def _state(session):
    return SimpleNamespace(
        chat_by_id=lambda chat_id: None,
        current_session_for_chat=lambda chat: None,
        sessions={"chat-a": session},
        work_projection=lambda: SimpleNamespace(
            get=lambda ref: SimpleNamespace(
                ref=WORK, memberships=(SimpleNamespace(chat_id="chat-a", direct=True),)
            )
        ),
    )


def test_derived_for_reads_only_a_switched_work():
    from agentdeck.web.work_view import derived_for

    switched = {"eerovil/agentdeck"}
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_router=SimpleNamespace(switched=lambda repository: repository in switched),
            assistant=None,
            wake_on=None,
            control_db=None,
        )
    )
    snapshot = SimpleNamespace(state=_state(_session(turn_failed=True)))
    found = derived_for(app, snapshot, WORK, card())
    assert found is not None and found.kind == DIED
    switched.clear()
    assert derived_for(app, snapshot, WORK, card()) is None


def test_a_turn_a_runtime_death_cut_off_is_a_death_not_an_ending():
    """#3008: a restart killed the turn while its question was open. The question went with
    the process, so the chat read as ended and the deck asked it for a next step ten seconds
    later, racing the revival; it then read the next restart's kill as the person refusing."""
    from agentdeck.web.work_view import derived_for

    app = SimpleNamespace(
        state=SimpleNamespace(
            work_router=SimpleNamespace(switched=lambda repository: True),
            assistant=None,
            wake_on=None,
            control_db=None,
        )
    )
    cut = SimpleNamespace(
        state=_state(_session(turn_cut_off=True, turn_cut_off_at=AT, revival_due=True))
    )
    assert derived_for(app, cut, WORK, card()).kind == REVIVING
    # The runtime owes it nothing (it died alone, or its revival was given up): a person's.
    alone = SimpleNamespace(
        state=_state(_session(turn_cut_off=True, turn_cut_off_at=AT, revival_due=False))
    )
    assert derived_for(app, alone, WORK, card()).kind == CUT_OFF
    # Without the flag the same chat is an ordinary ending.
    ended = SimpleNamespace(state=_state(_session()))
    assert derived_for(app, ended, WORK, card()).kind not in {DIED, REVIVING, CUT_OFF}


def test_a_permission_prompt_holds_the_turn_like_a_question(monkeypatch):
    """#3138: Claude asked "Allow Bash?" for an ``rm -rf *`` even under bypass. Only a
    question-style prompt counted, so the held turn read as an ending, the deck asked it for a
    next step it could not read, and then filed the Work as an AgentDeck bug."""
    import agentdeck.providers as providers
    from agentdeck.web.work_view import derived_for

    pending = {"value": None}
    monkeypatch.setattr(providers, "pending_interaction_for", lambda *_: pending["value"])
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_router=SimpleNamespace(switched=lambda repository: True),
            assistant=SimpleNamespace(accounts=[]),
            wake_on=None,
            control_db=None,
        )
    )
    snapshot = SimpleNamespace(state=_state(_session(last_text="It failed again.")))
    assert derived_for(app, snapshot, WORK, card()).kind == UNFINISHED
    pending["value"] = SimpleNamespace(
        kind="permission", title="Allow Bash?", message="cd /x && rm -rf *", questions=()
    )
    held = derived_for(app, snapshot, WORK, card())
    assert (held.kind, held.line) == (QUESTION, "Needs you — Allow Bash?")
    pending["value"] = SimpleNamespace(kind="permission", title="", message="", questions=())
    assert derived_for(app, snapshot, WORK, card()).kind == QUESTION


@private_feature(_mac_runner)
def test_a_chat_waiting_on_the_macs_approval_needs_you_not_a_next_step(monkeypatch):
    """#3106: a chat that sent a Mac command and ended its turn waiting for the boss's tap read
    Needs retry, and the deck asked it for a next step it did not have."""
    from agentdeck.web.work_view import derived_for

    monkeypatch.setenv("AGENTDECK_MAC_RUNNER_URL", "https://mac.example:8443/")
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_router=SimpleNamespace(switched=lambda repository: True),
            assistant=None,
            wake_on=None,
            control_db=None,
        )
    )
    marks = {"chat-a": SimpleNamespace(state="waiting", detail="")}

    def found(text):
        state = _state(_session(key="chat-a", last_text=text))
        state.attention_report_for = lambda key: marks.get(key)
        return derived_for(app, SimpleNamespace(state=state), WORK, card())

    asked = found("**Approve this check** (HTTPS://Mac.Example:8443/#run-7).")
    assert (asked.kind, asked.line) == (QUESTION, "Needs you — approve the Mac command")
    row = front_row(card(derived=asked), current=True)
    assert row.section == "needs_you"
    assert ("Open", "HTTPS://Mac.Example:8443/#run-7") in [(b.label, b.href) for b in row.buttons]
    # A link anywhere else is never the Work's Open; the mark alone is still a question.
    for text in ("See https://mac.example:84430/#x. Shall I go on?", "https://evil.example/#x"):
        other = found(text)
        assert other.kind == QUESTION and not other.approval_href
    assert found("Shall I go on?").line == "Needs you — Shall I go on?"
    # The runtime's own mark (the loop guard), or another mark: not this rule.
    marks["chat-a"] = SimpleNamespace(state="waiting", detail="loop guard")
    assert found("https://mac.example:8443/#x").kind == UNFINISHED
    marks["chat-a"] = SimpleNamespace(state="finished", detail="")
    assert found("https://mac.example:8443/#x").kind == UNFINISHED


def test_the_real_router_answers_which_repositories_are_switched(tmp_path):
    """``derived_for`` asks the router itself; a fake with a public ``switched`` hid that the
    real one kept it private, so no deck ever showed the line."""
    from agentdeck.web.work_view import derived_for
    from agentdeck.work_router import WorkRouterService

    router = WorkRouterService(
        tmp_path,
        switched=lambda repository: repository == "eerovil/agentdeck",
        owner_of_issue=lambda repository, number: None,
        owner_of_pull=lambda repository, number: None,
        schedule=lambda message: message,
        start_review=lambda pull, work: None,
        own_logins=(),
    )
    assert router.switched("eerovil/agentdeck") and not router.switched("eerovil/other")
    app = SimpleNamespace(
        state=SimpleNamespace(work_router=router, assistant=None, wake_on=None, control_db=None)
    )
    snapshot = SimpleNamespace(state=_state(_session(turn_failed=True)))
    assert derived_for(app, snapshot, WORK, card()).kind == DIED


def test_a_finished_work_with_everything_settled_stays_finished():
    """#2944: a restart notice, the hand-over notice or "Noted." ends a turn too."""
    from agentdeck.web.work_view import _still_finished
    from agentdeck.work_facts import OutputFact

    pressed = NOW - timedelta(hours=1)
    control = SimpleNamespace(work_signoffs=lambda: {WORK.work_id: (pressed, pressed)})
    app = SimpleNamespace(state=SimpleNamespace(control_db=control))
    merged = OutputFact(
        url="https://github.com/eerovil/agentdeck/pull/9",
        repository="eerovil/agentdeck",
        number=9,
        found_by="registered",
        state="merged",
    )
    after = SimpleNamespace(last_message_at=pressed + timedelta(minutes=10))
    for outputs in ((), (merged,)):
        done = card(finished_at=pressed - timedelta(minutes=5), outputs=outputs)
        assert _still_finished(app, WORK.work_id, done, after) is True
    assert status(finished=True, last_text="Noted.", ended_at=NOW).kind == FINISHED


def test_a_turn_after_the_finish_press_reads_through_d7_again():
    """D15: with a pull request still open, finished until a message wakes it; once that
    turn ends, D7 applies again."""
    from agentdeck.web.work_view import _still_finished
    from agentdeck.work_facts import OutputFact

    pressed = NOW - timedelta(hours=1)
    control = SimpleNamespace(work_signoffs=lambda: {WORK.work_id: (pressed, pressed)})
    app = SimpleNamespace(state=SimpleNamespace(control_db=control))
    still_open = OutputFact(
        url="https://github.com/eerovil/agentdeck/pull/9",
        repository="eerovil/agentdeck",
        number=9,
        found_by="registered",
        state="open",
    )
    done = card(finished_at=pressed - timedelta(minutes=5), outputs=(still_open,))
    before = SimpleNamespace(last_message_at=pressed - timedelta(minutes=1))
    after = SimpleNamespace(last_message_at=pressed + timedelta(minutes=10))
    assert _still_finished(app, WORK.work_id, done, before) is True
    assert _still_finished(app, WORK.work_id, done, after) is False, "a woken turn ended since"
    assert _still_finished(app, WORK.work_id, done, None) is True
    assert _still_finished(app, WORK.work_id, replace(done, awaiting_finish=True), before) is False
    assert _still_finished(app, WORK.work_id, card(), before) is False, "never finished"
    no_record = SimpleNamespace(state=SimpleNamespace(control_db=None))
    assert _still_finished(no_record, WORK.work_id, done, after) is False, "closing time used"


def test_a_pending_live_check_with_no_wait_still_says_so():
    """#3141 review: with no wake-on, the turn and the ask for a next step name the check.

    #3578: the line is Live checking, and ``kind`` stays the rule's, which the nudge reads.
    """
    pending = {"closed": True, "live_pending": True}
    assert status(**pending, running=True, progress="Opening the page").line == (
        "Live checking — Opening the page"
    )
    asking = status(**pending, ended_at=NOW - timedelta(minutes=1), asks=True)
    assert (asking.kind, asking.line, asking.live_checking) == (
        ENDED, "Live checking — asking the agent for its next step", True
    )


def test_a_live_check_is_pending_from_the_ask_until_a_result_recorded_after_it():
    """#3141: a result recorded before the deck asked was about an earlier merge."""
    from agentdeck.web.work_view import _live_state
    from agentdeck.work_resolution import LiveCheck

    asked = {}
    checks = {}
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_release=SimpleNamespace(store=SimpleNamespace(get=lambda work_id: dict(asked))),
            control_db=SimpleNamespace(work_live_check=lambda work_id: checks.get(work_id)),
        )
    )
    assert _live_state(app, WORK.work_id).pending is False, "never asked"
    asked["live_check_asked"] = {"at": AT.timestamp(), "pulls": ["eerovil/agentdeck#9"]}
    checks[WORK.work_id] = LiveCheck(WORK.work_id, "verified", "ok", AT - timedelta(days=1))
    assert _live_state(app, WORK.work_id).pending is True, "an older result"
    checks[WORK.work_id] = LiveCheck(WORK.work_id, "failed", "500", AT + timedelta(minutes=5))
    assert _live_state(app, WORK.work_id) == type(_live_state(app, WORK.work_id))(
        pending=False, result="failed", line="live check failed: 500"
    )


def test_finish_then_a_message_then_a_turn_that_ends_reads_through_d7():
    """D15: after a sign-off, an ended follow-up turn reads through D7, never a refused Finish."""
    from agentdeck.web.work_view import _reads_closed

    pressed = NOW - timedelta(hours=1)
    awaiting = card(finished_at=pressed, awaiting_finish=True)
    signed = card(finished_at=pressed, awaiting_finish=False)
    assert _reads_closed(awaiting) is True
    assert _reads_closed(signed) is False
    later = status(closed=_reads_closed(signed), finished=False, last_text="Done.", ended_at=NOW)
    assert later.kind == UNFINISHED


def test_a_deck_run_works_verification_job_is_a_wait():
    """#2965: agentdeck#2974's verification job had no Run, so it never became the card's
    verification and the Work read as ended while its tests ran."""
    from agentdeck.web.work_view import derived_for

    session = _session(last_progress=AT, last_text="Queued the verification run.")
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_router=SimpleNamespace(switched=lambda repository: True),
            assistant=None,
            wake_on=None,
            control_db=None,
        )
    )
    snapshot = SimpleNamespace(state=_state(session), presentation=None)
    job = {"purpose": "verification", "work_id": WORK.work_id, "run_id": "", "state": "running"}
    assert derived_for(app, snapshot, WORK, card(), [job]).kind == WAITING
    for other in (
        {**job, "state": "done"},
        {**job, "work_id": "github_issue:eerovil/agentdeck#1"},
        {**job, "purpose": ""},
    ):
        assert derived_for(app, snapshot, WORK, card(), [other]).kind == UNFINISHED


def test_a_message_just_handed_to_the_chat_is_its_next_step():
    """#3084: a fired wake-on is gone from the registry seconds before the turn it starts shows
    as running, and the Work was asked for a next step in between."""
    from agentdeck.web.work_view import derived_for

    app = SimpleNamespace(
        state=SimpleNamespace(
            work_router=SimpleNamespace(switched=lambda repository: True),
            assistant=None,
            wake_on=None,
            control_db=None,
        )
    )
    now = datetime.now(UTC)
    session = _session(last_message_at=now - timedelta(minutes=10))

    def derived(handoffs):
        snapshot = SimpleNamespace(state=_state(session), message_handoffs=handoffs)
        return derived_for(app, snapshot, WORK, card())

    assert derived({}).kind == UNFINISHED
    handed = derived({"chat-a": now - timedelta(seconds=3)})
    assert handed.kind == WORKING and "the message it was just sent" in handed.line
    assert handed.deadline == now - timedelta(seconds=3) + HANDOFF_GRACE
    # Handed over longer ago than the grace (a row whose target never resolves): ended.
    assert derived({"chat-a": now - HANDOFF_GRACE - timedelta(seconds=1)}).kind == UNFINISHED
    # Delivered before the turn ended: that turn was its answer, and it ended.
    session.last_message_at = now
    assert derived({"chat-a": now - timedelta(seconds=3)}).kind == UNFINISHED
    assert derived({"other-chat": now}).kind == UNFINISHED


def test_a_turn_ended_with_a_background_command_running_is_not_working():
    """#683/#2783: a dev server left running keeps the chat `thinking`, but it is not work.
    #3405: within two hours of the turn's end it is the Work's wait, since Claude wakes the
    chat when the command exits; an older one is an ending again."""
    from agentdeck.models import BACKGROUND_TASK_ACTIVITY
    from agentdeck.web.work_view import derived_for

    session = _session(
        thinking=True,
        activity=BACKGROUND_TASK_ACTIVITY,
        last_progress=AT,
        last_text="Started the dev server.",
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_router=SimpleNamespace(switched=lambda repository: True),
            assistant=None,
            wake_on=None,
            control_db=None,
        )
    )
    snapshot = SimpleNamespace(state=_state(session), presentation=None)
    found = derived_for(app, snapshot, WORK, card())
    assert found.kind == UNFINISHED and "Started the dev server." in found.line
    ended = datetime.now(UTC) - timedelta(minutes=10)
    session.last_message_at = ended
    waiting = derived_for(app, snapshot, WORK, card())
    assert waiting.kind == WAITING and "its background command" in waiting.line
    assert waiting.deadline == ended + timedelta(hours=2)
    session.last_message_at = AT
    # Providers spell the agent's role "agent"; a person's last message is never quoted.
    session.last_role = "user"
    assert "last said" not in derived_for(app, snapshot, WORK, card()).line
    session.last_role = "agent"
    session.activity = "Running tests"
    assert derived_for(app, snapshot, WORK, card()).kind == WORKING


def _derived_with_reviews(
    open_reviews,
    *,
    head="a" * 40,
    repository="eerovil/agentdeck",
    awaiting=None,
    draft=False,
    retried=False,
    passes=None,
    waits=(),
    entry=None,
    ended=AT,
    escalated=None,
):
    from agentdeck.web.work_view import derived_for
    from agentdeck.work_facts import OutputFact

    release = SimpleNamespace(
        store=SimpleNamespace(
            get=lambda work_id: {
                "awaiting_merge": awaiting or {}, "passes": passes or {}, **(entry or {})
            }
        )
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_router=SimpleNamespace(switched=lambda repository: True),
            assistant=None,
            wake_on=SimpleNamespace(registry=SimpleNamespace(for_chat=lambda chat: waits)),
            control_db=None,
            work_release=release,
            work_endings=SimpleNamespace(
                get=lambda work_id: (None, None), review_started=lambda chat: retried
            ),
        )
    )
    output = OutputFact(
        url=f"https://github.com/{repository}/pull/9",
        repository=repository,
        number=9,
        found_by="registered",
        state="open",
        head=head,
        draft=draft,
    )
    session = _session(last_text="Fixed it.", last_message_at=ended)
    snapshot = SimpleNamespace(
        state=_state(session), open_reviews=open_reviews, escalated_reviews=escalated or {}
    )
    return derived_for(app, snapshot, WORK, card(outputs=(output,)))


def _open(head, *, chat=5, newest=False, **fields):
    from agentdeck.web.work_view import OpenReview

    return OpenReview(chat=chat, head=head, newest=newest, **fields)


def test_in_review_only_while_a_review_chat_about_that_head_is_open():
    """D7 rule 6 reads the deck's record of review chats, not a reviewer's comments."""
    reviewing = {("eerovil/agentdeck", 9): (_open("a" * 40),)}
    assert _derived_with_reviews(reviewing).kind == REVIEW
    # The chat ended (any verdict, any reviewer): the head is no longer in review, and the
    # agent is asked for its next step.
    assert _derived_with_reviews({}).kind == ENDED
    # A chat about an older head, with a newer chat on record, says nothing about the new one.
    assert _derived_with_reviews(reviewing, head="b" * 40).kind == ENDED
    # A pull request in another repository than the card's is keyed by its own repository.
    other = {("eerovil/review-chats", 9): (_open("A" * 12),)}
    assert _derived_with_reviews(other, repository="eerovil/Review-Chats").kind == REVIEW
    assert _derived_with_reviews(other).kind == ENDED


def test_a_reviewers_escalation_needs_the_owner_not_a_review_wait():
    """#3216: agentdeck#3200's review escalated to the owner; its Work read waiting for review."""
    from agentdeck.web.work_view import EscalatedReview

    at = AT + timedelta(minutes=5)
    escalated = {
        ("eerovil/agentdeck", 9): EscalatedReview(
            5914, "eerovil/review-chats#5914", "a" * 40, at, "https://github.com/eerovil/agentdeck/issues/3200#issuecomment-6223939073"
        )
    }
    waits = (SimpleNamespace(
        kind="review", repository="eerovil/agentdeck", number=9, deadline=UNTIL.timestamp(),
        target="the review of pull request eerovil/agentdeck#9",
    ),)
    found = _derived_with_reviews({}, escalated=escalated, waits=waits)
    assert found.kind == REVIEW_ESCALATED and found.at == at
    assert found.line.startswith(
        "Needs you — the reviewer escalated it to you (review-chats#5914) · "
    )
    # The line opens the escalation comment, which holds the question (#3216 review).
    assert derived_story(card(derived=found)).summary_href == "https://github.com/eerovil/agentdeck/issues/3200#issuecomment-6223939073"
    row = front_row(card(derived=found), current=False)
    assert (row.section, row.attention.category) == ("needs_you", "needs_answer")
    # A push moves past the escalated head, and a newer review chat replaces it upstream.
    assert _derived_with_reviews({}, escalated=escalated, head="b" * 40).kind == ENDED


def test_ready_for_merge_once_the_release_waits_only_for_a_person():
    """#2960: issues#387, store#3548 and issues#668 were reviewed and tested and waited for
    a person's Merge, and read Stopped under Needs answer."""
    awaiting = {"eerovil/agentdeck#9": "a" * 40}
    found = _derived_with_reviews({}, awaiting=awaiting)
    assert (found.kind, found.line, found.pr) == (READY, "Ready for merge", 9)
    row = front_row(card(derived=found), current=False)
    assert (row.section, row.attention.category) == ("needs_you", "ready_for_merge")
    assert [button.label for button in row.buttons] == ["Merge", "Open", "Hide"]
    # A push since the release's answer is a head nobody reviewed: not ready.
    assert _derived_with_reviews({}, awaiting=awaiting, head="b" * 40).kind == ENDED
    # Another repository's pull request reads the same: no line names one (#3720).
    other = _derived_with_reviews(
        {}, repository="northwind/abacus",
        awaiting={"northwind/abacus#9": "a" * 40},
    )
    assert (other.line, other.pr) == ("Ready for merge", 9)
    # A review still open wins: the release only answers for a reviewed head.
    reviewing = {("eerovil/agentdeck", 9): (_open("a" * 40),)}
    assert _derived_with_reviews(reviewing, awaiting=awaiting).kind == REVIEW
    # #3324: the pass is recorded from the verdict comment and its chat closes seconds
    # later. The chat that passed it is answered, while a chat opened after it reviews again.
    at = (AT + timedelta(minutes=4)).timestamp()
    passes = {"eerovil/agentdeck#9": {"head": "a" * 40, "at": at}}
    answered = {("eerovil/agentdeck", 9): (_open("a" * 40, opened_at=AT),)}
    found = _derived_with_reviews(answered, awaiting=awaiting, passes=passes)
    assert found.kind == READY
    assert front_row(card(derived=found), current=False).attention.category == "ready_for_merge"
    again = {("eerovil/agentdeck", 9): (_open("a" * 40, opened_at=AT + timedelta(minutes=5)),)}
    assert _derived_with_reviews(again, awaiting=awaiting, passes=passes).kind == REVIEW
    # The rest of issues#791's sequence on that same head: a later review escalates to the
    # owner, who answers; a re-review the agent asks for then passes clean, Merge and all.
    from agentdeck.web.work_view import EscalatedReview

    escalated = {
        ("eerovil/agentdeck", 9): EscalatedReview(
            6384, "eerovil/review-chats#6384", "a" * 40, AT + timedelta(minutes=7)
        )
    }
    found = _derived_with_reviews({}, awaiting=awaiting, passes=passes, escalated=escalated)
    assert found.kind == REVIEW_ESCALATED, "the escalation still waits for the owner"
    assert front_row(card(derived=found), current=False).attention.category == "needs_answer"
    # The re-review's chat replaces the escalated one upstream (only the newest counts).
    opened = AT + timedelta(minutes=9)
    rereview = {("eerovil/agentdeck", 9): (_open("a" * 40, chat=6386, opened_at=opened),)}
    assert _derived_with_reviews(rereview, awaiting=awaiting, passes=passes).kind == REVIEW
    passes["eerovil/agentdeck#9"]["at"] = (AT + timedelta(minutes=12)).timestamp()
    found = _derived_with_reviews(rereview, awaiting=awaiting, passes=passes)
    assert found.kind == READY, "its chat still open for a few seconds after the pass"
    row = front_row(card(derived=found), current=False)
    assert row.attention.category == "ready_for_merge"
    assert [button.label for button in row.buttons] == ["Merge", "Open", "Hide"]


def test_a_closed_issue_with_an_open_pull_request_follows_the_pull_request():
    """#3187: trailhead issues#753 was closed by another chat while store#3593 was ready, read
    "Issue closed — finish it?", and was finished with store#3593 never merged."""
    found = status(closed=True, pr_open=True, ready_pr=3593)
    assert (found.kind, found.line, found.pr) == (
        READY, "Ready for merge · issue closed", 3593
    )
    review = status(closed=True, pr_open=True, review_pr=9, review_since=AT)
    assert review.line == "In review — since 09:40 · issue closed"
    assert review.summary == "since 09:40 · issue closed"
    failed = dict(live_result="failed", live_line="live check failed: 500")
    assert status(closed=True, pr_open=True, ready_pr=9, **failed).kind == READY
    # No registered pull request open: unchanged.
    assert status(closed=True, ready_pr=3593).line == "Issue closed — finish it?"
    assert status(closed=True, pr_open=True, finished=True).line == "Finished"


def test_a_wait_on_its_own_pull_request_does_not_hide_ready_for_merge():
    """#3003: trailhead issues#667, #679 and #680 were reviewed and tested, and read "Waiting on
    pull request …" because their agents waited for their own pull request to merge."""
    from agentdeck.wake_on import KIND_PULL_REQUEST, KIND_TIME, WakeRequest

    def wait(kind, repository="eerovil/agentdeck", number=9, note=""):
        return WakeRequest(
            id=f"w-{kind}-{number}", session_key="s", chat_id="chat-a", kind=kind,
            repository=repository, number=number, deadline=(NOW + timedelta(days=1)).timestamp(),
            note=note,
        )

    awaiting = {"eerovil/agentdeck#9": "a" * 40}
    own = (wait(KIND_PULL_REQUEST, repository="eerovil/AgentDeck"),)
    assert _derived_with_reviews({}, awaiting=awaiting, waits=own).kind == READY
    # Not ready yet: the same wait is what the Work waits on, and says what the pull request
    # itself waits for (#3097).
    fresh = datetime.now(UTC) - timedelta(minutes=5)
    found = _derived_with_reviews({}, waits=own, ended=fresh)
    assert found.kind == WAITING
    assert found.line.startswith(
        "Waiting on its pull request — not reviewed at its head, until "
    )
    # #3097: abacus#2084's pass was lost, and its Work read Waiting for a day.
    stuck = _derived_with_reviews({}, waits=own)
    assert stuck.kind == "pr_stuck" and stuck.line.endswith(f"nothing since {deck_clock(AT)}")
    row = front_row(card(derived=stuck), current=False)
    assert (row.section, row.attention.category, row.attention.bug) == (
        "needs_you", "needs_retry", ""
    )
    # The release's own hold, and findings after it, read in its words.
    since = (datetime.now(UTC) - timedelta(minutes=30)).timestamp()
    tests = {"hold": {"why": "x", "words": "no test run started", "since": since}}
    assert "— no test run started," in _derived_with_reviews({}, waits=own, entry=tests).line
    told = {**tests, "findings": {"at": since + 60, "told": True}}
    assert ", sent to the agent, until" in _derived_with_reviews({}, waits=own, entry=told).line
    lost = {**tests, "findings": {"at": since + 60, "told": False}}
    assert "not sent: no chat owns it" in _derived_with_reviews({}, waits=own, entry=lost).line
    # Findings from before the hold moved (a push since) are answered.
    old = {**tests, "findings": {"at": since - 60, "told": False}}
    assert "findings" not in _derived_with_reviews({}, waits=own, entry=old).line
    # #3097: store#3553 was reviewed and green, and held by the spec-only label.
    held = {"hold": {"why": "y", "words": "held: spec-only label", "person": True, "since": since}}
    found = _derived_with_reviews({}, waits=own, entry=held)
    assert found.line == "Needs you — its pull request held: spec-only label"
    row = front_row(card(derived=found), current=False)
    assert (row.section, row.attention.category) == ("needs_you", "release_blocked")
    # A wait on anything else still comes first: another pull request, or a render.
    for other in (
        wait(KIND_PULL_REQUEST, number=10),
        wait(KIND_PULL_REQUEST, repository="eerovil/other"),
        wait(KIND_TIME, repository="", number=None, note="the render"),
    ):
        assert _derived_with_reviews({}, awaiting=awaiting, waits=(other,)).kind == WAITING


def test_a_quiet_review_is_reported_as_agentdecks_bug_not_a_review_to_start():
    """#2960: every ready head is reviewed by itself, so there is no Needs review. A quiet
    chat is the deck's to retry first (also while it waits for room); only one that went
    quiet again after that retry is a bug."""
    quiet = dict(review_pr=9, review_since=AT, review_stuck_at=NOW - timedelta(minutes=1))
    queued = front_row(card(derived=status(**quiet)), current=False)
    assert (queued.section, queued.attention) == ("active", None)
    found = status(**quiet, review_retried=True)
    row = front_row(card(derived=found), current=False)
    assert (row.attention.category, row.attention.bug) == ("needs_retry", "review_stuck")


def test_a_turn_that_ends_without_a_next_step_is_asked_about_once():
    """#2960: a chat that only reported ("Prod stays as it is, as you chose.") rested in
    Needs answer. Now the deck asks its agent once; a second such ending is a bug."""
    first = status(last_text="Prod stays as it is.", ended_at=AT, asks=True)
    assert (first.kind, first.line) == (ENDED, "Working — asking the agent for its next step")
    asked = NOW - timedelta(minutes=2)
    waiting = status(ended_at=AT, asks=True, nudged_at=asked, nudged_for=AT)
    assert waiting.kind == NUDGED and waiting.deadline == asked + NUDGE_GRACE
    assert front_row(card(derived=waiting), current=False).section == "active"
    # The answering turn ended without a next step again: AgentDeck reports it.
    again = status(
        last_text="Done.", ended_at=NOW, asks=True, nudged_at=asked, nudged_for=AT
    )
    assert again.kind == UNFINISHED and again.bug
    row = front_row(card(derived=again), current=False)
    assert (row.attention.category, row.attention.bug) == (
        "needs_retry", "ended_without_next_step"
    )
    assert [button.label for button in row.buttons] == ["Message", "Open", "Hide"]
    # Nothing ran within the grace.
    late = status(
        ended_at=AT, asks=True, nudged_at=NOW - NUDGE_GRACE, nudged_for=AT
    )
    assert late.kind == UNFINISHED and late.bug
    # A deck that does not ask (staging) shows the same line, never a bug.
    quiet = status(ended_at=AT)
    assert quiet.kind == UNFINISHED and not quiet.bug
    assert front_row(card(derived=quiet), current=False).attention.bug == ""


def _asking_app(tmp_path, cards_state):
    from agentdeck.work_endings import EndingLedger

    scheduled = []
    messages = SimpleNamespace(
        exists=lambda key: any(m.id == key for m in scheduled), schedule=scheduled.append
    )
    published = []
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_endings=EndingLedger(tmp_path / "work-endings.json", clock=lambda: NOW),
            scheduled_messages=messages,
            app_state=SimpleNamespace(
                **vars(cards_state), bus=SimpleNamespace(publish=published.append)
            ),
        )
    )
    return app, scheduled, published


def test_the_overview_pass_sends_one_request_and_forgets_a_work_that_rests_again(tmp_path):
    from agentdeck.web.work_view import ask_for_next_steps
    from agentdeck.work_endings import message_id

    app, scheduled, published = _asking_app(tmp_path, _state(_session()))
    ended = card(derived=status(ended_at=AT, asks=True))
    assert ask_for_next_steps(app, [ended]) == 1
    assert ask_for_next_steps(app, [ended]) == 1, "a repeat pass re-records, never re-sends"
    assert [m.id for m in scheduled] == [message_id(ended.work_id, AT)]
    assert scheduled[0].chat_id == "chat-a" and "question box" in scheduled[0].text
    assert published
    ledger = app.state.work_endings
    assert ledger.get(ended.work_id) == (NOW, AT)
    # Working on the answer keeps the request; a proper resting state forgets it.
    ask_for_next_steps(app, [card(derived=status(running=True))])
    assert ledger.get(ended.work_id) == (NOW, AT)
    ask_for_next_steps(app, [card(derived=status(question="Which?"))])
    assert ledger.get(ended.work_id) == (None, None)
    # A paused Work is not asked.
    assert ask_for_next_steps(app, [replace(ended, paused=True)]) == 0


def test_a_review_stuck_after_the_decks_own_reviewer_tells_the_work_chat_once(tmp_path):
    """#3356: store#3633's review stopped with no verdict and nothing told its Work chat."""
    from agentdeck.web.work_view import ask_for_next_steps
    from agentdeck.work_endings import review_stuck_id

    app, scheduled, _published = _asking_app(tmp_path, _state(_session()))
    past_quiet = dict(
        review_pr=9, review_since=AT, review_active=AT, review_chat=6508,
        review_stuck_at=NOW - timedelta(minutes=1), review_retried=True, review_held=True,
    )
    # Held for a person past the quiet deadline (needs-more-info, or a bare `blocked` with no
    # reviewer's `Blocked:`): stuck, but not failed, so the Work chat is not told.
    held = card(derived=status(**past_quiet))
    assert held.derived.kind == REVIEW_STUCK and not held.derived.review_failed
    assert ask_for_next_steps(app, [held]) == 0 and scheduled == []
    stuck = card(derived=status(**past_quiet, review_stopped_by_ours=True))
    assert stuck.derived.kind == REVIEW_STUCK and stuck.derived.review_failed
    assert ask_for_next_steps(app, [stuck]) == 1
    assert ask_for_next_steps(app, [stuck]) == 0, "told once per review chat"
    assert [m.id for m in scheduled] == [review_stuck_id(stuck.work_id, 6508)]
    assert scheduled[0].chat_id == "chat-a"
    assert "PR #9" in scheduled[0].text and "No reviewer said why." in scheduled[0].text
    assert "review-request" in scheduled[0].text
    assert ask_for_next_steps(app, [replace(stuck, paused=True)]) == 0
    # Nobody came even after the deck's retry: failed too, told once for its own chat.
    quiet = dict(past_quiet, review_held=False, review_chat=6509)
    nobody = card(derived=status(**quiet))
    assert nobody.derived.review_failed and ask_for_next_steps(app, [nobody]) == 1


def test_a_pass_that_failed_a_read_asks_no_work_for_a_next_step(tmp_path):
    """#3031: the job queue read timed out, so agentdeck#3008's queued verification job was
    missing from the pass and its Work read as ended; the deck asked it for a next step."""
    from agentdeck.web.work_view import ask_for_next_steps

    app, scheduled, _ = _asking_app(tmp_path, _state(_session()))
    ended = card(derived=status(ended_at=AT, asks=True))
    assert ask_for_next_steps(app, [ended], degraded=("runtime/jobs",)) == 0
    assert scheduled == []
    assert app.state.work_endings.get(ended.work_id) == (None, None)
    assert ask_for_next_steps(app, [ended]) == 1, "the next whole pass asks"


async def test_a_pass_while_the_runtime_restarts_is_degraded(monkeypatch):
    """#3487: a deploy restarted the runtime, so no open question could be read and each
    Work waiting on one read as ended; the deck asked it for a next step, cutting the
    question off. A web that has not reached the runtime yet knows no more than one that
    lost it; a runtime that answered, even with a refusal, is known."""
    from pathlib import Path

    import httpx

    from agentdeck.models import Account
    from agentdeck.providers import PROVIDERS
    from agentdeck.providers.claude_code.worker_client import ClaudeWorkerClient
    from agentdeck.web import work_view

    account = Account("claude_code:main", "claude_code", "main", Path("/tmp/cfg"))
    replies: list[int | None] = []

    def runtime(request):
        if replies[-1] is None:
            raise httpx.ConnectError("no socket", request=request)
        return httpx.Response(replies[-1], json={"workers": []})

    client = ClaudeWorkerClient(account, transport=httpx.MockTransport(runtime))
    monkeypatch.setitem(PROVIDERS["claude_code"]._workers, account.key, client)
    app = SimpleNamespace(state=SimpleNamespace(assistant=SimpleNamespace(accounts=[account])))

    def degraded() -> list[str]:
        found: list[str] = []
        token = work_view._DEGRADED.set(found)
        try:
            work_view._note_interactions_unknown(app)
        finally:
            work_view._DEGRADED.reset(token)
        return found

    assert degraded() == ["runtime_interactions"], "not reached yet"
    replies.append(None)
    await client.probe()
    assert degraded() == ["runtime_interactions"], "down"
    replies.append(200)
    await client.refresh()
    assert degraded() == []
    replies.append(None)
    await client.refresh()
    assert degraded() == ["runtime_interactions"], "lost again"
    replies.append(503)
    await client.refresh()
    assert degraded() == ["runtime_interactions"], "failing is not an answer"
    await client.probe()
    assert degraded() == ["runtime_interactions"], "nor through probe"
    replies.append(404)
    await client.refresh()
    assert degraded() == [], "workers switched off: nothing will be asked, so nothing is hidden"


async def test_an_ended_works_issue_is_reread_once_before_it_is_asked(tmp_path):
    """#3143: an errand agent closed its issue as its last act, the ``closed`` delivery was
    dropped, and the deck asked it for a next step, then reported it as a bug."""
    from agentdeck.web.work_view import (
        ENDING_REREAD_TRIES,
        ask_for_next_steps,
        reread_ended_issues,
    )

    app, scheduled, published = _asking_app(tmp_path, _state(_session()))
    reads, failing, unread = [], {2}, {6}

    async def refresh(refs, *, limit):
        reads.append(list(refs))
        if refs[0][1] in failing:
            raise RuntimeError("gh is down")
        return 0 if refs[0][1] in unread else 1

    app.state.issues = SimpleNamespace(refresh_issue_records=refresh)
    ended = card(derived=status(ended_at=AT, asks=True))
    held = await reread_ended_issues(app, [ended])
    assert held == {ended.work_id} and published == [WORK_INPUTS_TOPIC]
    assert ask_for_next_steps(app, [ended], held=held) == 0 and scheduled == []
    # Still open on the next pass: read once per turn end, so it is asked now.
    assert await reread_ended_issues(app, [ended]) == frozenset()
    assert ask_for_next_steps(app, [ended]) == 1

    def other(number, derived):
        return replace(card(derived=derived), work_id=WorkRef(WORK.repository, number).work_id)

    # A Work reported as a bug is read too; a person's ending, a degraded pass and a
    # working Work are neither read nor held.
    bug = other(1, status(ended_at=AT, asks=True, nudged_at=AT))
    quiet = other(3, status(ended_at=AT))
    working = other(4, status(running=True))
    reads.clear()
    assert await reread_ended_issues(app, [bug], degraded=("runtime/jobs",)) == frozenset()
    assert await reread_ended_issues(app, [bug, quiet, working]) == {bug.work_id}
    assert reads == [[(WORK.repository, 1)]]
    # #3144 review: a failed read holds the Work and is tried again on the next pass...
    failed = other(2, status(ended_at=AT, asks=True))
    assert await reread_ended_issues(app, [failed]) == {failed.work_id}
    failing.clear()
    assert await reread_ended_issues(app, [failed]) == {failed.work_id}
    assert await reread_ended_issues(app, [failed]) == frozenset(), "read now"
    # #3144 review: so does a read that refreshed nothing, such as another process's lease.
    leased = other(6, status(ended_at=AT, asks=True))
    assert await reread_ended_issues(app, [leased]) == {leased.work_id}
    unread.clear()
    assert await reread_ended_issues(app, [leased]) == {leased.work_id}
    assert await reread_ended_issues(app, [leased]) == frozenset(), "read now"
    # ...but only so often, so a GitHub outage cannot hold it from being asked for good.
    failing.add(5)
    stuck = other(5, status(ended_at=AT, asks=True))
    for _ in range(ENDING_REREAD_TRIES):
        assert await reread_ended_issues(app, [stuck]) == {stuck.work_id}
    assert await reread_ended_issues(app, [stuck]) == frozenset()


async def test_a_resumed_work_is_asked_only_once_the_refresher_shows_it_resumed(
    tmp_path, monkeypatch
):
    """#3103: northwind/issues#530 was resumed and the pass asked it at once, but
    delivery read the previous snapshot, where it was still paused, and refused the request.
    The ledger counted it as asked, so the next ending was reported as a bug."""
    from agentdeck.web import work_view
    from agentdeck.web.work_view import WorkOverview, WorkOverviewService

    app, scheduled, _ = _asking_app(tmp_path, _state(_session()))
    ended = card(derived=status(ended_at=AT, asks=True))
    service = WorkOverviewService(app)
    service.overview = WorkOverview((replace(ended, paused=True),))
    held: list[bool] = []
    real_schedule = app.state.scheduled_messages.schedule

    def schedule(message):
        held.append(any(c.paused for c in service.overview.cards))  # as ``paused_hold`` reads
        return real_schedule(message)

    async def load(_app, *, now, changed_since=None):
        return WorkOverview((ended,), degraded=degraded)

    async def scanned(_app):
        return True

    app.state.scheduled_messages.schedule = schedule
    monkeypatch.setattr(work_view, "load_work_overview", load)
    monkeypatch.setattr(work_view, "sessions_scanned", scanned)
    degraded: tuple[str, ...] = ("runtime/jobs",)
    await service.refresh()
    assert scheduled == [], "a pass that failed a read still asks nothing"
    degraded = ()
    await service.refresh()
    assert held == [False] and len(scheduled) == 1


def test_a_work_held_only_for_its_tests_is_asked_to_run_them(tmp_path):
    """#2999: agentdeck#2928's review passed, nobody told its agent, and the request listed
    every ending instead of the one that applied."""
    from agentdeck.web.work_view import ask_for_next_steps
    from agentdeck.work_release import ReleaseStore

    app, scheduled, _ = _asking_app(tmp_path, _state(_session()))
    store = ReleaseStore(tmp_path / "work-release.json")
    app.state.work_release = SimpleNamespace(store=store)
    ended = card(derived=status(ended_at=AT, asks=True))
    store.put(ended.work_id, {"passes": {}, "waiting_tests": True, "needs_verify": True})
    assert ask_for_next_steps(app, [ended]) == 1
    assert "its review passed" in scheduled[0].text
    assert "question box" not in scheduled[0].text


def test_a_work_whose_verification_failed_is_not_asked_to_run_it_again(tmp_path):
    """#3001 review: the release waits on the tests of a red head too; that agent must fix it."""
    from agentdeck.web.work_view import ask_for_next_steps
    from agentdeck.work_release import ReleaseStore

    app, scheduled, _ = _asking_app(tmp_path, _state(_session()))
    store = ReleaseStore(tmp_path / "work-release.json")
    app.state.work_release = SimpleNamespace(store=store)
    ended = card(derived=status(ended_at=AT, asks=True))
    store.put(ended.work_id, {"passes": {}, "waiting_tests": True})
    assert ask_for_next_steps(app, [ended]) == 1
    assert "but its review passed" not in scheduled[0].text
    assert "question box" in scheduled[0].text


def test_a_schedule_definition_is_never_asked_for_a_next_step(tmp_path):
    """#2998: northwind/issues#602 ("Weekly SEO audit") stays open on purpose with
    the ``scheduled`` label. The deck asked its chat for a next step, then read Needs retry."""
    from agentdeck.web.work_view import ask_for_next_steps, derived_for

    facts = replace(work_facts(WORK), record={"labels": ["Scheduled "]})
    task = build_work_card(WORK, issue=None, title="Weekly SEO audit", facts=facts, trace=None)
    assert task.scheduled
    assert not card().scheduled
    app, scheduled, _ = _asking_app(tmp_path, _state(_session(last_text="Filed the run.")))
    app.state.work_router = SimpleNamespace(switched=lambda repository: True)
    app.state.assistant = app.state.wake_on = app.state.control_db = None
    snapshot = SimpleNamespace(state=_state(_session(last_text="Filed the run.")))
    found = derived_for(app, snapshot, WORK, task)
    assert (found.kind, found.line) == (SCHEDULED, "Scheduled — files a new card on its schedule")
    assert not found.bug
    # Already asked before this fix: the request is forgotten, never followed by a bug.
    app.state.work_endings.record(task.work_id, AT)
    assert ask_for_next_steps(app, [replace(task, derived=found)]) == 0
    assert scheduled == [] and app.state.work_endings.get(task.work_id) == (None, None)
    row = front_row(replace(task, derived=found), current=False)
    assert (row.section, row.attention) == ("active", None)
    assert derived_story(replace(task, derived=found)).headline == "Scheduled"
    # A running turn or a question still reads as itself.
    assert status(scheduled=True, running=True).kind == WORKING
    assert status(scheduled=True, question="Which day?").kind == QUESTION


def test_a_parked_work_waits_for_a_person_and_is_never_asked_for_a_next_step(tmp_path):
    """#3560: the owner told northwind/issues#653's agent to check only when asked.
    With nothing to wait on, the deck asked it for a next step after every answer, then read
    Needs retry. The ``parked`` label is how the agent says so."""
    from agentdeck.web.work_view import ask_for_next_steps, derived_for

    facts = replace(work_facts(WORK), record={"labels": ["Parked"]})
    task = build_work_card(WORK, issue=None, title="Box suggestions", facts=facts, trace=None)
    assert task.parked_label and not card().parked_label
    app, scheduled, _ = _asking_app(tmp_path, _state(_session(last_text="OK, no new cards.")))
    app.state.work_router = SimpleNamespace(switched=lambda repository: True)
    app.state.assistant = app.state.wake_on = app.state.control_db = None
    snapshot = SimpleNamespace(state=_state(_session(last_text="OK, no new cards.")))
    found = derived_for(app, snapshot, WORK, task)
    assert (found.kind, found.line, found.bug) == (PARKED, "Parked — waits for your message", False)
    assert ask_for_next_steps(app, [replace(task, derived=found)]) == 0 and scheduled == []
    assert front_row(replace(task, derived=found), current=False).section == "active"
    assert derived_story(replace(task, derived=found)).headline == "Parked"
    # An open pull request, a running turn or a question still reads as itself.
    pushed = status(parked=True, pr_open=True, last_text="Pushed.", ended_at=AT, asks=True)
    assert pushed.kind == ENDED
    assert status(parked=True, running=True).kind == WORKING
    assert status(parked=True, question="Which day?").kind == QUESTION


def test_a_posted_spec_waits_for_approval_and_is_never_asked_for_a_next_step(tmp_path):
    """#3247: #3239 and #3185 posted their spec, ended the turn, and the deck asked each for
    a next step it could not give: building waits on a person's approval."""
    from agentdeck.db import ChatNote
    from agentdeck.github_cache import GitHubMetadata
    from agentdeck.issue_latest_comment import _default_cache, cache_key
    from agentdeck.web.work_view import ask_for_next_steps, derived_for

    url = "https://github.com/eerovil/agentdeck/issues/2895#issuecomment-30"

    def note(comment_id, body="## Spec\n\n<!-- agentdeck-spec -->", **fields):
        return ChatNote(
            **{
                "repository": "eerovil/agentdeck", "comment_id": comment_id,
                "chat_id": "chat-a", "number": 2895, "is_pull": False,
                "author": "eerovil-agent", "url": url, "body": body, "body_html": None,
                "created_at": AT.isoformat(), "updated_at": AT.isoformat(), **fields,
            }
        )

    start = note(10, body="AgentDeck started an agent.\n<!-- agentdeck-work-start id=x -->")
    notes = [start, note(30)]
    facts = replace(work_facts(WORK), record={"labels": ["agent", "Spec-Only"]})
    spec = build_work_card(WORK, issue=None, title="A spec", facts=facts, trace=None)
    session = _session(last_text="The spec is on the issue.")
    app, scheduled, _ = _asking_app(tmp_path, _state(session))
    app.state.work_router = SimpleNamespace(switched=lambda repository: True)
    app.state.assistant = app.state.wake_on = None
    app.state.control_db = SimpleNamespace(chat_notes=lambda chat_id: list(notes))
    policy = SimpleNamespace(spec_only_label="spec-only")
    app.state.work_release = SimpleNamespace(ports=SimpleNamespace(policy=lambda repo: policy))
    snapshot = SimpleNamespace(state=_state(session))
    cache = _default_cache()
    key = cache_key("eerovil/agentdeck#2895")
    # No newest comment known yet: nothing proves nobody commented after the spec.
    assert derived_for(app, snapshot, WORK, spec).kind == ENDED
    cache.offer(key, GitHubMetadata({"id": 30, "seen": 30}))

    found = derived_for(app, snapshot, WORK, spec)
    assert (found.kind, found.line, found.summary_url) == (
        SPEC_WAIT, f"Needs you — approve the spec · posted {deck_clock(AT)}", url
    )
    assert ask_for_next_steps(app, [replace(spec, derived=found)]) == 0 and scheduled == []
    row = front_row(replace(spec, derived=found), current=False)
    assert (row.section, row.attention.category) == ("needs_you", "needs_answer")
    assert status(spec_posted_at=AT, running=True).kind == WORKING
    # No spec posted (only the start comment, or a progress comment without the spec's
    # marker), the agent's own comment after it, a person's comment after it, a pull request
    # open, or no spec-only label: the ending is asked about once, as before.
    for posted in ([start], [start, note(30, body="Reading the code first.")]):
        notes[:] = posted
        assert derived_for(app, snapshot, WORK, spec).kind == ENDED
    notes[:] = [start, note(30), note(32, body="Still working on it.")]
    assert derived_for(app, snapshot, WORK, spec).kind == ENDED
    notes[:] = [start, note(30)]
    cache.offer(key, GitHubMetadata({"id": 31, "seen": 31}))
    assert derived_for(app, snapshot, WORK, spec).kind == ENDED
    cache.offer(key, GitHubMetadata({"id": None, "seen": 31}))  # the newest was deleted
    assert derived_for(app, snapshot, WORK, spec).kind == ENDED
    cache.offer(key, GitHubMetadata({"id": 30, "seen": 31}))
    assert derived_for(app, snapshot, WORK, spec).kind == SPEC_WAIT
    opened = replace(spec, outputs=(SimpleNamespace(is_open=True, settled=False, draft=True,
                                                    repository="eerovil/agentdeck", number=9,
                                                    head="", state="open"),))
    assert derived_for(app, snapshot, WORK, opened).kind != SPEC_WAIT
    assert derived_for(app, snapshot, WORK, replace(spec, labels=("agent",))).kind == ENDED


def test_a_fresh_ending_is_asked_about_only_once_it_has_settled(tmp_path):
    """#2965: a person stopped a turn to type, the deck asked at once, and their own turn's
    ending was then judged as the answer to that request and filed as a bug."""
    from agentdeck.web.work_view import ask_for_next_steps
    from agentdeck.work_derived import ENDING_SETTLE

    just = status(ended_at=NOW - timedelta(seconds=5), asks=True)
    assert just.kind == ENDED and just.deadline == NOW - timedelta(seconds=5) + ENDING_SETTLE
    assert status(ended_at=AT, asks=True).deadline is None
    app, scheduled, _ = _asking_app(tmp_path, _state(_session()))
    fresh = datetime.now(UTC) - timedelta(seconds=5)
    assert ask_for_next_steps(app, [card(derived=status(ended_at=fresh, asks=True))]) == 0
    assert scheduled == [] and app.state.work_endings.get(card().work_id) == (None, None)


def test_no_ledger_no_request(tmp_path):
    """Staging runs without the router, so it never messages a real chat."""
    from agentdeck.web.work_view import ask_for_next_steps

    app, scheduled, _ = _asking_app(tmp_path, _state(_session()))
    app.state.work_endings = None
    assert ask_for_next_steps(app, [card(derived=status(ended_at=AT, asks=True))]) == 0
    assert scheduled == []


def test_in_review_at_the_newest_recorded_head_when_the_cards_head_lags():
    """#2943: my-tasks#36 read Stopped while review chat #5151 was open at PR #37's
    current head, because the card's cached head had not caught up with the push yet."""
    reviewing = {("eerovil/agentdeck", 9): (_open("9b421fca4333", chat=5151, newest=True),)}
    assert _derived_with_reviews(reviewing, head="de10ceba8af9" + "0" * 28).kind == REVIEW
    # And with no cached head at all.
    assert _derived_with_reviews(reviewing, head="").kind == REVIEW


def test_in_review_says_since_when_and_turns_to_needs_you_once_nobody_reviews():
    """#2943: agentdeck#2928's review chat sat open for two hours with no reviewer."""
    now = datetime.now(UTC)  # derived_for reads the real clock
    opened = now - timedelta(hours=2)
    last = now - timedelta(minutes=90)
    quiet = _open(
        "a" * 40, opened_at=opened, active_at=last, stuck_at=last + timedelta(minutes=30)
    )
    found = _derived_with_reviews({("eerovil/agentdeck", 9): (quiet,)})
    assert found.kind == REVIEW_QUEUED and found.review_chat == 5
    assert found.line.startswith("In review — waiting for a reviewer since ")
    found = _derived_with_reviews({("eerovil/agentdeck", 9): (quiet,)}, retried=True)
    assert found.kind == REVIEW_STUCK and found.pr == 9
    assert found.line.startswith("Needs you — no reviewer since ")
    story = derived_story(card(derived=found))
    assert story.headline == "Needs you" and story.summary.startswith("no reviewer since")
    # Still inside the window: in review, since the chat opened, and it flips by itself.
    soon = now + timedelta(minutes=5)
    fresh = replace(quiet, stuck_at=soon)
    found = _derived_with_reviews({("eerovil/agentdeck", 9): (fresh,)})
    assert found.kind == REVIEW and found.line.startswith("In review — since ")
    assert found.deadline == soon


def test_the_stuck_rule_on_its_own():
    stuck_at = NOW - timedelta(minutes=1)
    found = status(review_pr=9, review_since=AT, review_active=AT, review_stuck_at=stuck_at)
    assert found.kind == REVIEW_QUEUED
    assert found.line == "In review — waiting for a reviewer since 09:40"
    found = status(
        review_pr=9, review_since=AT, review_active=AT, review_stuck_at=stuck_at,
        review_retried=True,
    )
    assert found.kind == REVIEW_STUCK
    assert found.line == "Needs you — no reviewer since 09:40"
    # #2974: a reviewer that ran and stopped (its chat labelled blocked) is not "no reviewer".
    found = status(
        review_pr=9, review_since=AT, review_active=AT, review_stuck_at=stuck_at,
        review_retried=True, review_held=True,
    )
    assert found.kind == REVIEW_STUCK and found.review_held
    assert found.line == "Needs you — the reviewer stopped since 09:40"
    assert derived_story(card(derived=found)).you == (
        "Open the review chat and read why the reviewer stopped"
    )
    # #3356: with the reason its `Blocked:` comment gave, and while it is still in review.
    why = status(
        review_pr=9, review_since=AT, review_active=AT, review_stuck_at=stuck_at,
        review_retried=True, review_held=True, review_held_why="Unable to publish findings.",
    )
    assert why.line == (
        "Needs you — the reviewer stopped since 09:40: Unable to publish findings."
    )
    why = status(
        review_pr=9, review_since=AT, review_active=AT, review_held=True,
        review_held_why="Unable to publish findings.", review_stuck_at=NOW + timedelta(minutes=1),
    )
    assert why.kind == REVIEW
    assert why.line.endswith(" · the last reviewer stopped: Unable to publish findings.")
    later = NOW + timedelta(minutes=1)
    found = status(review_pr=9, review_since=AT, review_active=AT, review_stuck_at=later)
    assert found.kind == REVIEW and found.deadline == later
    assert found.line == "In review — since 09:40"
    # #3326: ChatGPT could not be started on it, said in words while it is in review.
    found = status(review_pr=9, review_since=AT, review_start_failed="ChatGPT is not signed in")
    assert found.line == (
        "In review — since 09:40 · ChatGPT was not started: ChatGPT is not signed in"
    )
    # A reviewer running (no stuck time) never reads stuck.
    assert status(review_pr=9, review_since=AT).kind == REVIEW
    # #3192: a reviewer gave up and the deck opens the next chat. In review until the deck
    # stops trying, then the agent's ending as before.
    until = NOW + timedelta(minutes=50)
    found = status(review_pr=9, review_active=AT, review_reopen_until=until)
    assert found.kind == REVIEW and found.deadline == until
    assert found.line == (
        "In review — the reviewer gave up at 09:40, opening the next review"
    )
    gone = status(review_pr=9, review_active=AT, review_reopen_until=NOW - timedelta(minutes=1))
    assert gone.kind not in {REVIEW, REVIEW_QUEUED, REVIEW_STUCK}


def test_open_review_chats_are_read_by_the_pull_request_they_cover():
    from agentdeck.chat_board import ChatRow
    from agentdeck.web import work_view

    rows = {
        5: ChatRow(number=5, title="Review eerovil/agentdeck#9", state="open"),
        6: ChatRow(number=6, title="Review eerovil/agentdeck#9", state="closed"),
    }
    heads = {5: {("eerovil/agentdeck", 9): "a" * 40}, 6: {("eerovil/agentdeck", 9): "b" * 40}}
    service = SimpleNamespace(board=SimpleNamespace(rows=rows))
    app = SimpleNamespace(state=SimpleNamespace())
    original = work_view.chat_board_service
    work_view.chat_board_service = lambda state: service
    try:
        opened = work_view._chat_reviews(app, heads, state="open")
        closed = work_view._chat_reviews(app, heads)
    finally:
        work_view.chat_board_service = original
    assert opened == {("eerovil/agentdeck", 9): ("a" * 40,)}
    assert closed == {("eerovil/agentdeck", 9): ("b" * 40,)}


@private_feature(_chat_board)
def test_open_review_chats_carry_their_opening_activity_and_quiet_moment():
    """#2943: what the In review line reads — when the chat opened, its last sign of a
    reviewer (the chat's own activity or a fallback start), and whether its head is the
    newest the deck recorded for that pull request."""
    from agentdeck.chat_board import ChatBoard, ChatRow
    from agentdeck.chat_review import StartState
    from agentdeck.web import work_view

    pull = ("eerovil/agentdeck", 9)
    stopped = ("eerovil/agentdeck", 12)
    rows = {
        5: ChatRow(
            number=5,
            title="Review eerovil/agentdeck#9",
            state="open",
            created_at="2026-10-12T17:17:44Z",
            updated_at="2026-10-12T17:20:00Z",
        ),
        7: ChatRow(
            number=7, title="Review eerovil/agentdeck#9", state="open", labels=("blocked",)
        ),
        # #3356: a reviewer of ours asked a person something: waiting on them, not failed.
        4: ChatRow(
            number=4, title="Review eerovil/agentdeck#13", state="open",
            labels=("needs-more-info",), updated_at="2026-10-12T17:40:00Z",
        ),
        # #3356: a reviewer of ours ran on it, ended and left it blocked.
        6: ChatRow(
            number=6, title="Review eerovil/agentdeck#12", state="open", labels=("blocked",),
            updated_at="2026-10-12T17:40:00Z",
        ),
        # Another Work's chat, closed by a reviewer that gave up a minute ago (#3192).
        8: ChatRow(number=8, title="Review eerovil/agentdeck#11", state="closed"),
    }
    gave_up = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    endings = {8: ("gave_up", gave_up.timestamp())}
    other = ("eerovil/agentdeck", 10)
    asking = ("eerovil/agentdeck", 13)
    heads = {
        4: {asking: "e" * 40},
        5: {pull: "a" * 40}, 6: {stopped: "b" * 40}, 7: {pull: "c" * 40}, 8: {other: "d" * 40}
    }
    last_start = datetime(2026, 10, 12, 17, 37, tzinfo=UTC)
    fallback = SimpleNamespace(
        enabled=True,
        reviewers_on=lambda: {
            4: (last_start.timestamp(), False),
            5: (last_start.timestamp(), False),
            6: (last_start.timestamp(), False),
            7: (NOW.timestamp(), True),
        },
        quiet_after_s=lambda: 1800.0,
        switch=lambda: None,
        # #3326: ChatGPT's last start, pressed after chat 5 opened, was not sent.
        last_start=lambda: StartState(
            at=0.0, failures=((last_start.timestamp(), "ChatGPT is not signed in"),)
        ),
    )
    app = SimpleNamespace(state=SimpleNamespace(review_fallback=fallback))
    original = work_view.chat_board_service
    board = ChatBoard("eerovil/review-chats")
    board.rows, board.endings = rows, endings
    stopped_at = last_start.timestamp() + 60
    board.blocks = {
        6: [[1, "Unable to publish findings.", stopped_at]], 7: [[2, "Lifted.", 10.0]]
    }
    board.unblocked = {7: 20.0}
    work_view.chat_board_service = lambda state: SimpleNamespace(board=board)
    try:
        found = work_view._open_reviews(app, heads)
    finally:
        work_view.chat_board_service = original
    # #3356: our own reviewer stopped it, so it is stuck at once, with the reason it gave.
    [ours] = found[stopped]
    assert ours.stopped_by_ours and ours.held_why == "Unable to publish findings."
    assert ours.stuck_at == ours.active_at == datetime(2026, 10, 12, 17, 40, tzinfo=UTC)
    # A person's own `blocked` after our reviewer ended, with no `Blocked:` since: not failed.
    rows[6] = replace(rows[6], updated_at="2026-10-12T17:40:01Z")
    board.blocks[6] = [[1, "Unable to publish findings.", last_start.timestamp() - 60]]
    work_view.chat_board_service = lambda state: SimpleNamespace(board=board)
    try:
        [manual] = work_view._open_reviews(app, heads)[stopped]
    finally:
        work_view.chat_board_service = original
    assert manual.held and not manual.stopped_by_ours
    assert manual.stuck_at == manual.active_at + timedelta(minutes=30)
    del board.blocks[6]  # and with no `Blocked:` comment at all
    work_view.chat_board_service = lambda state: SimpleNamespace(board=board)
    try:
        [bare] = work_view._open_reviews(app, heads)[stopped]
    finally:
        work_view.chat_board_service = original
    assert bare.held and not bare.stopped_by_ours and bare.held_why == ""
    [waiting_on_you] = found[asking]
    assert waiting_on_you.held and not waiting_on_you.stopped_by_ours
    assert waiting_on_you.stuck_at == waiting_on_you.active_at + timedelta(minutes=30)
    # The give-up's next chat is the deck's to open: still in review until it tries no more.
    [reopening] = found[other]
    assert reopening.chat == 8 and reopening.newest and reopening.stuck_at is None
    assert reopening.active_at == gave_up and reopening.reopen_until == gave_up + timedelta(hours=1)
    quiet, running = found[pull]
    assert (quiet.chat, quiet.newest, running.chat, running.newest) == (5, False, 7, True)
    assert quiet.opened_at == datetime(2026, 10, 12, 17, 17, 44, tzinfo=UTC)
    assert quiet.active_at == last_start and not quiet.reviewing
    assert quiet.stuck_at == last_start + timedelta(minutes=30)
    # A reviewer of ours running on it: never stuck while it runs.
    assert running.reviewing and running.stuck_at is None
    # #2974: a chat a reviewer labelled blocked is held; an unlabelled one is not.
    assert running.held and not quiet.held
    assert running.held_why == "" and not running.stopped_by_ours, "a lifted block is past"
    # Chat 7 is labelled blocked, not waiting: a failed press is never its.
    assert quiet.start_failed == "ChatGPT is not signed in" and running.start_failed == ""
    # Without the fallback service nothing is called stuck, and no give-up is reopened.
    app.state.review_fallback = None
    work_view.chat_board_service = lambda state: SimpleNamespace(board=board)
    try:
        found = work_view._open_reviews(app, heads)
    finally:
        work_view.chat_board_service = original
    assert all(review.stuck_at is None for review in found[pull])


def test_a_review_going_quiet_is_a_moment_the_card_cache_and_refresher_wake_for():
    """#2943: the stuck call flips with no event, so its moment is a card deadline."""
    from agentdeck.web.work_view import time_deadline

    soon = NOW + timedelta(minutes=7)
    found = status(review_pr=9, review_since=AT, review_stuck_at=soon)
    without = time_deadline(NOW, card())
    assert without is None or without > soon, "the test needs the review to be the soonest"
    built = card(derived=found)
    assert time_deadline(NOW, built) == soon
    assert time_deadline(NOW, (built,)) == soon


def _app():
    return SimpleNamespace(
        state=SimpleNamespace(
            work_router=SimpleNamespace(switched=lambda repository: True),
            assistant=None,
            wake_on=None,
            control_db=None,
        )
    )


def test_the_stall_flag_is_read_as_the_provider_set_it():
    """#2956: liveness is the provider's to decide (the Claude provider only marks a live
    worker stalled), so the derived status takes the flag as it is."""
    from agentdeck.web.work_view import derived_for

    session = _session(stalled=True, last_text="Merged it.")
    snapshot = SimpleNamespace(state=_state(session), presentation=None)
    assert derived_for(_app(), snapshot, WORK, card()).kind == STALLED
    session.stalled = False
    assert derived_for(_app(), snapshot, WORK, card()).kind == UNFINISHED


@pytest.mark.parametrize(
    ("fields", "label"),
    [
        ({"question": "Which branch?"}, "Answer"),
        ({"last_text": "All done.", "ended_at": AT}, "Message"),
        ({"died": True, "died_at": AT}, "Message"),
        ({"closed": True}, "Message"),
        ({"running": True, "stalled": True}, "Message"),
    ],
)
def test_only_a_question_is_answered(fields, label):
    """#2956: an ended turn said "Answer" with nothing asked."""
    from agentdeck.work_actions import buttons

    derived = card(derived=status(**fields))
    # Every one of these kinds is filed under Needs answer; the label follows the kind alone.
    asked = SimpleNamespace(actions=("answer",), category="needs_answer", review_url="")
    assert [b.label for b in buttons(derived, asked)] == [label]
    assert "Answer" not in [b.label for b in front_row(derived, current=False).buttons] or (
        "question" in fields
    )


def test_ready_for_merge_names_the_clean_review_behind_it():
    """#2992: a Ready for merge said nothing of the review that made it ready, so a reviewed
    Ready could not be told from an unreviewed one."""
    from agentdeck.work_derived import clock

    at = datetime(2026, 10, 13, 10, 26, tzinfo=UTC)
    awaiting = {"eerovil/agentdeck#9": "a" * 40}
    passes = {
        "eerovil/agentdeck#9": {
            "head": "a" * 40, "at": at.timestamp(), "chat": "eerovil/review-chats#5166"
        }
    }
    found = _derived_with_reviews({}, awaiting=awaiting, passes=passes)
    summary = f"reviewed clean {clock(at)} (review-chats#5166)"
    assert (found.kind, found.line, found.summary) == (
        READY, f"Ready for merge — {summary}", summary,
    )
    story = derived_story(card(derived=found))
    assert story.summary == summary
    assert story.summary_href == "https://github.com/eerovil/review-chats/issues/5166"
    # A pass with no known review chat still says when; with no pass at all, as before.
    del passes["eerovil/agentdeck#9"]["chat"]
    bare = _derived_with_reviews({}, awaiting=awaiting, passes=passes)
    assert bare.summary == f"reviewed clean {clock(at)}" and not bare.review_ref
    assert derived_story(card(derived=bare)).summary_href == ""
    assert _derived_with_reviews({}, awaiting=awaiting).summary == ""


def test_no_derived_line_names_a_pull_request():
    """#3720: the front page names no pull request; the Work page lists them."""
    lines = [
        status(pr=37).line,
        status(ready_pr=37, ready_reviewed_at=AT).line,
        status(queue_pr=37, queue_place=3, queue_size=5).line,
        status(escalated_pr=37, escalated_at=AT).line,
        status(review_pr=37, review_since=AT).line,
        status(review_pr=37, review_since=AT, review_reopen_until=AT + timedelta(hours=1)).line,
        status(review_pr=37, review_active=AT, review_stuck_at=AT, review_retried=True).line,
    ]
    assert all("37" not in line for line in lines), lines
    assert status(pr=37).pr == 37, "the number stays for the deck's own use"


def test_merged_pull_requests_on_an_open_issue_are_the_agents_to_close():
    """#2960 part 2: every PR merged with the issue open is the agent's call, asked once
    like any ending without a next step; "Issue closed" is said of a closed issue only."""
    from agentdeck.web.work_view import derived_for
    from agentdeck.work_facts import OutputFact

    def output(number, state):
        return OutputFact(
            url=f"https://github.com/eerovil/agentdeck/pull/{number}",
            repository="eerovil/agentdeck",
            number=number,
            found_by="registered",
            state=state,
        )

    snapshot = SimpleNamespace(state=_state(_session(last_text="Done.")), presentation=None)
    merged = derived_for(_app(), snapshot, WORK, card(outputs=(output(9, "merged"),)))
    assert merged.kind == UNFINISHED  # ``_app`` keeps no ledger: the deck does not ask
    asking = _app()
    asking.state.work_endings = SimpleNamespace(get=lambda work_id: (None, None))
    found = derived_for(asking, snapshot, WORK, card(outputs=(output(9, "merged"),)))
    assert found.kind == ENDED
    closed = card(outputs=(output(9, "merged"),), awaiting_finish=True, finished_at=AT)
    assert derived_for(_app(), snapshot, WORK, closed).kind == CLOSED
    draft = replace(output(37, "open"), draft=True)
    ended = derived_for(_app(), snapshot, WORK, card(outputs=(draft,)))
    assert ended.kind == UNFINISHED and ended.pr == 37 and "#37" not in ended.line


def test_a_handed_over_work_is_paused_by_its_issues_label():
    """#2944: with no live card, the manifest's paused label on the cached issue pauses it."""
    from agentdeck.work_overview import build_work_card

    facts = replace(work_facts(WORK), record={"labels": ["Paused "]})
    built = build_work_card(WORK, issue=None, title="t", facts=facts, paused_label="paused")
    assert built.paused and built.label_paused and built.pause_label == "paused"
    unlabelled = replace(facts, record={"labels": []})
    assert not build_work_card(WORK, issue=None, title="t", facts=unlabelled,
                               paused_label="paused").paused
    assert not build_work_card(WORK, issue=None, title="t", facts=facts).paused


def test_a_manifests_paused_label_is_part_of_the_cached_cards_key(monkeypatch):
    """#2956: a manifest change to the paused label changes a card nothing else does."""
    from agentdeck.web import work_view

    label = {"value": "paused"}
    monkeypatch.setattr(work_view, "_deck_paused_label", lambda app, repo: label["value"])
    monkeypatch.setattr(work_view, "_derived_key", lambda app, snapshot, ref: ())
    monkeypatch.setattr(work_view, "_can_send", lambda app: lambda key: False)
    snapshot = SimpleNamespace(
        state=SimpleNamespace(sessions={}, session_revision=None),
        legs={}, owned={}, presentation=None, insight_states={}, titles={},
        agent_titles={}, chat_titles={}, backlog=None, merges={}, board=None,
        projection=SimpleNamespace(works_for_chat=lambda chat: ()),
    )

    def key():
        return work_view._card_inputs(
            _app(), snapshot, WORK, None, jobs=[], outcomes=None, claims=None,
            facts=work_facts(WORK), evidence_since=None, current_trace=None,
        )[0]

    before = key()
    label["value"] = "on-hold"
    assert key() != before


def test_a_stopped_work_whose_issue_still_reads_open_is_read_again():
    """#2960: my-tasks#38 was closed by its agent at 16:32:08, a second after the
    deck cached the issue; the closed delivery never came, so it sat as Stopped for a day.
    A stopped deck-run Work with an open record goes to the overview's bounded re-read."""
    from agentdeck.web.work_view import stopped_open_issues

    def facts(state):
        return SimpleNamespace(record={"state": state, "is_pr": False})

    stopped = card(derived=status(last_text="Work closed.", ended_at=AT))
    assert stopped.derived.kind == UNFINISHED
    working = replace(
        card(derived=status(running=True)), work_id="github_issue:eerovil/agentdeck#1"
    )
    carded = replace(card(), work_id="github_issue:eerovil/agentdeck#2")
    found = stopped_open_issues(
        [stopped, working, carded],
        {
            stopped.work_id: facts("open"),
            working.work_id: facts("open"),
            carded.work_id: facts("open"),
        },
    )
    assert found == (("eerovil/agentdeck", 2895),)
    # A record already closed, or none at all, is not read again by this rule.
    assert stopped_open_issues([stopped], {stopped.work_id: facts("closed")}) == ()
    assert stopped_open_issues([stopped], {}) == ()


@private_feature(_chat_board)
async def test_a_quiet_review_gets_one_reviewer_started_by_the_deck(tmp_path, monkeypatch):
    """#2960 part 2: "no reviewer on PR #N" started the fallback by hand; the deck does it,
    once per review chat, and only a second quiet review needs the owner."""
    from agentdeck.chat_board import ChatRow
    from agentdeck.inject import RUNTIME_UNREACHABLE
    from agentdeck.web.work_view import start_quiet_reviews
    from agentdeck.work_endings import EndingLedger

    started = []

    room = {"left": True, "refusal": "every reviewer is busy"}

    async def start_on(row, *, by_person=True):
        assert by_person is False, "the deck's retry keeps the reviewer cap"
        if not room["left"]:
            return False, room["refusal"]
        started.append(row.number)
        return True, "Claude"

    row = ChatRow(number=5151, title="Review eerovil/agentdeck#2895")
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_endings=EndingLedger(tmp_path / "work-endings.json", clock=lambda: NOW),
            review_fallback=SimpleNamespace(start_on=start_on),
            chat_board=SimpleNamespace(
                board=SimpleNamespace(rows={5151: row}), config=SimpleNamespace(enabled=True)
            ),
        )
    )
    quiet = status(
        review_pr=9, review_since=AT, review_stuck_at=NOW - timedelta(minutes=1),
        review_chat=5151,
    )
    assert quiet.kind == REVIEW_QUEUED
    stuck = card(derived=quiet)
    monkeypatch.setattr(
        "agentdeck.web.work_view.chat_board_service", lambda state: state.chat_board
    )
    room["left"] = False
    assert await start_quiet_reviews(app, [stuck]) == 0, "no room: left to the queue"
    assert not app.state.work_endings.review_started(5151), "the retry is not spent"
    # #2964: three quiet reviews started in one pass after a deploy were each refused while
    # another session of the account was starting, and each spent its one retry.
    room["refusal"] = "Claude: a new session is already starting for this account"
    assert await start_quiet_reviews(app, [stuck]) == 0, "another start under way: ask again"
    assert not app.state.work_endings.review_started(5151), "the retry is not spent"
    # #3285: the first pass after a host restart, before the account's runtime answered.
    room["refusal"] = "Claude: " + RUNTIME_UNREACHABLE
    assert await start_quiet_reviews(app, [stuck]) == 0, "runtime coming up: ask again"
    assert not app.state.work_endings.review_started(5151), "the retry is not spent"
    room["left"] = True
    assert await start_quiet_reviews(app, [stuck]) == 1
    assert await start_quiet_reviews(app, [stuck]) == 0, "once per chat"
    assert await start_quiet_reviews(app, [replace(stuck, paused=True)]) == 0
    assert started == [5151]


@private_feature(_chat_board)
async def test_the_deck_starts_one_quiet_review_per_pass(
    tmp_path, monkeypatch
):
    """#2967: three quiet reviews started in one pass collided on "a new session is already
    starting for this account", and each spent its one retry although nothing launched."""
    from agentdeck.chat_board import ChatRow
    from agentdeck.web.work_view import start_quiet_reviews
    from agentdeck.work_endings import EndingLedger

    started = []
    refuse = {"on": True}

    async def start_on(row, *, by_person=True):
        if refuse["on"]:
            return False, "a new session is already starting for this account"
        started.append(row.number)
        return True, "Claude"

    rows = {n: ChatRow(number=n, title=f"Review eerovil/agentdeck#{n}") for n in (5160, 5163)}
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_endings=EndingLedger(tmp_path / "work-endings.json", clock=lambda: NOW),
            review_fallback=SimpleNamespace(start_on=start_on),
            chat_board=SimpleNamespace(
                board=SimpleNamespace(rows=rows), config=SimpleNamespace(enabled=True)
            ),
        )
    )
    monkeypatch.setattr(
        "agentdeck.web.work_view.chat_board_service", lambda state: state.chat_board
    )
    cards = [
        card(
            work_id=f"w{n}",
            derived=status(
                review_pr=9, review_since=AT, review_stuck_at=NOW - timedelta(minutes=1),
                review_chat=n,
            ),
        )
        for n in rows
    ]
    assert await start_quiet_reviews(app, cards) == 0
    ledger = app.state.work_endings
    assert not ledger.review_started(5160) and not ledger.review_started(5163)
    refuse["on"] = False
    assert await start_quiet_reviews(app, cards) == 1, "one start per pass"
    assert started == [5160] and not ledger.review_started(5163)
    assert await start_quiet_reviews(app, cards) == 1
    assert started == [5160, 5163]
    assert await start_quiet_reviews(app, cards) == 0, "once per chat"


def test_a_work_chat_whose_turn_ended_wakes_the_unwatched_overview():
    """#2960 review: the request must not wait for the owner to open the deck. Read from what
    each chat holds: an idle chat whose last message is newer than the last pass saw."""
    from agentdeck.web.work_view import WorkOverview, WorkOverviewService

    sessions = {"leg-a": _session(thinking=True, activity="Editing", last_message_at=AT)}
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_endings=object(), app_state=SimpleNamespace(sessions=sessions)
        )
    )
    service = WorkOverviewService(app)
    assert service._turn_ended() is True, "nothing built yet: the first pass looks at all"
    service.overview = WorkOverview()
    service._seen_end = service._newest_end()  # as the pass records it
    assert service._turn_ended() is False, "still working"
    sessions["leg-a"].thinking = False
    sessions["leg-a"].last_message_at = AT + timedelta(minutes=1)
    assert service._turn_ended() is True
    service._seen_end = service._newest_end()
    assert service._turn_ended() is False, "one ending owes one pass"
    # A chat started, and ended, between two looks still counts: no turn start is tracked.
    sessions["leg-new"] = _session(last_message_at=AT + timedelta(minutes=2))
    assert service._turn_ended() is True
    app.state.work_endings = None
    assert service._turn_ended() is False, "a deck that does not ask never wakes for it"




def test_a_work_in_the_merge_queue_reads_its_place():
    """#3022: behind another pull request it rests in the queue, not as an ending the deck
    asks about; first in line and re-testing it reads its round."""
    from agentdeck.work_derived import QUEUED

    behind = status(queue_pr=9, queue_place=3, queue_size=5, ended_at=NOW, asks=True)
    assert behind.kind == QUEUED and behind.line == "In merge queue — 3rd of 5"
    first = status(
        queue_pr=9, queue_place=1, queue_size=5, queue_round=2, waits=(("its tests", None),)
    )
    assert first.line == (
        "In merge queue — first of 5 · re-testing after master moved, round 2"
    )
    # First in line with nothing running owes its re-test: asked like any other ending.
    owed = status(queue_pr=9, queue_place=1, queue_size=5, queue_round=1, ended_at=NOW, asks=True)
    assert owed.kind == ENDED
    # A running turn or a question still comes first.
    assert status(queue_pr=9, queue_place=2, queue_size=2, running=True).kind == WORKING
    assert status(queue_pr=9, queue_place=12, queue_size=21).line.endswith("12th of 21")
    assert status(queue_pr=9, queue_place=21, queue_size=21).line.endswith("21st of 21")
