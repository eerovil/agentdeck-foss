"""The one Work action route and its controls (#2237, #2324, #2419).

Merge is the deck's own release of a Work it runs (#2895); the Poller's board commands were
retired with its cards (#3025). Every write (Merge, Close issue, a review, Pause) runs only
while the Work's operator attention still offers it, and every press is reserved under its
client action id before its one external write: a replay answers with what that press did, a
second press inside the window is refused unless the first is known to have failed, and a
write with no answer either way is never made again by the deck.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from public_copy import private_feature
from test_work_attention import (
    AT,
    GREEN,
    KEY,
    ORG,
    PR,
    WORK,
    _passed,
    _record,
    _reviewer,
    _row,
    _work,
)

from agentdeck import chat_review, gh_cli
from agentdeck import chat_review as _chat_review
from agentdeck.work_actions import (
    RECENT_ACTION_S,
    WRITES,
    Refused,
    WorkActionPress,
    buttons,
    plan,
    recent_press,
)
from agentdeck.work_attention import operator_attention
from agentdeck.work_facts import OutputFact
from agentdeck.work_overview import work_href
from agentdeck.work_story import work_status

ISSUE = f"{ORG}/agentdeck#5"
RETRY_PARK = _record(
    "Blocked", dispatch_failed=True, dispatch_failed_at=AT, dispatch_failed_refusal="at_capacity"
)
RELEASE_PARKED = _record(
    "Blocked", **_passed(),
    release_blocked={"why": "Auto-merge is disabled", "status": "Blocked", "generation": "gen-2"},
)
RELEASE_BLOCKING = _record(
    "Ready to merge",
    blocked_pending={"why": "checks failed", "merged": [], "steps": {}},
)


def _planned(tmp_path, record: dict, action: str, **extra):
    work = _work(tmp_path, record, **extra)
    return plan(action, work, operator_attention(work, work_status(work)))


# --- what each action does ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("record", "extra", "action", "kind", "body"),
    [
        (_record("In review"), {"rows": {PR.lower(): _row("merged")}}, "close", "close",
         "completed"),
        (_record("In review"), {"rows": {PR.lower(): _row("closed")}}, "close", "close",
         "not planned"),
        pytest.param(
            _record("In review"), {}, "chatgpt_review", "chatgpt_review", "",
            marks=private_feature(_chat_review),
        ),
        pytest.param(
            _record("In review"), {}, "agent_review", "agent_review", "",
            marks=private_feature(_chat_review),
        ),
    ],
    ids=["close-merged", "close-unmerged", "chatgpt-review", "agent-review"],
)
def test_each_deck_write_makes_one_fixed_write_on_the_source_issue(
    tmp_path, record, extra, action, kind, body
):
    step = _planned(tmp_path, record, action, **extra)

    assert (step.kind, step.issue, step.body) == (kind, ISSUE, body)


def test_merge_is_a_confirmed_form(tmp_path):
    """#2324: Merge no longer hands `/merge` to GitHub; it is one press, and it asks first."""
    work = _work(tmp_path, _record("Ready to merge", **_passed()), verifications=GREEN)
    attention = operator_attention(work, work_status(work))

    (control,) = [button for button in buttons(work, attention) if button.action == "merge"]

    assert (control.href, control.route) == ("", "action")
    assert control.confirm.startswith("Merge this Work?")


def test_merge_is_refused_for_a_work_the_deck_does_not_release(tmp_path):
    work = _work(tmp_path, _record("Ready to merge", **_passed()), verifications=GREEN)

    with pytest.raises(Refused, match="does not release"):
        plan("merge", work, operator_attention(work, work_status(work)))


def test_the_writes():
    assert WRITES == {
        "close", "agent_review", "chatgpt_review", "merge", "pause", "resume", "approve_merge",
        "withdraw_merge", "start",
    }


def test_merge_when_ready_and_withdraw_follow_the_releases_press(tmp_path):
    """#3320: offered from the release's own state on a Work not yet ready, like Pause."""
    work = _work(tmp_path, _record("In progress"), outcome="running")
    attention = operator_attention(work, work_status(work))
    assert plan("approve_merge", work, attention, approval="offered").kind == "deck_merge"
    assert plan("withdraw_merge", work, attention, approval="approved").kind == "deck_withdraw"
    for action, approval, why in [
        ("approve_merge", "", "does not hold this Work's merges"),  # auto_release, or not ours
        ("approve_merge", "approved", "already approved"),
        ("withdraw_merge", "offered", "no Merge approval stands"),
    ]:
        with pytest.raises(Refused, match=why):
            plan(action, work, attention, approval=approval)
    closed = _work(tmp_path, _record("In progress"), rows={PR.lower(): _row("merged")})
    with pytest.raises(Refused, match="no open pull request"):
        plan("approve_merge", closed, None, approval="offered")
    ready = _work(tmp_path, _record("Ready to merge", **_passed()), verifications=GREEN)
    with pytest.raises(Refused, match="Merge is offered instead"):
        plan("approve_merge", ready, operator_attention(ready, work_status(ready)),
             approval="offered")


@pytest.mark.parametrize(
    ("record", "action", "status"),
    [
        (_record("Ready to merge", **_passed()), "close", 409),
        (_record("In review"), "hide", 422),  # a link or unknown id is not a write
        (_record("In review"), "rm -rf", 422),
    ],
    ids=["close-not-offered", "hide", "garbage"],
)
def test_an_action_the_work_does_not_offer_now_is_refused(tmp_path, record, action, status):
    with pytest.raises(Refused) as refused:
        _planned(tmp_path, record, action)

    assert refused.value.status == status


@private_feature(_chat_review)
def test_a_card_that_releases_itself_offers_both_reviews(tmp_path):
    # Agent review is a separate Claude reviewer now (#2736), not the card's own worker, so a
    # card that releases itself offers it like ChatGPT review: nobody reviews their own work.
    self_releasing = replace(_work(tmp_path, _record("In review")), self_releasing=True)
    attention = operator_attention(self_releasing, work_status(self_releasing))
    offered = [b.action for b in buttons(self_releasing, attention)]
    assert "agent_review" in offered and "chatgpt_review" in offered


def test_a_work_that_needs_nobody_offers_nothing(tmp_path):
    work = _work(tmp_path, _record("In progress"), outcome="running")

    assert operator_attention(work, work_status(work)) is None
    for action in WRITES - {"pause"}:
        with pytest.raises(Refused):
            plan(action, work, None)
    # Pause is offered from the Work's own state, on any unfinished Work (#2559).
    assert plan("pause", work, None).kind == "deck_pause"


@private_feature(_chat_review)
def test_chatgpt_review_covers_the_whole_work_wherever_its_pull_requests_are(tmp_path):
    """#2637: one chat about the issue, listing every open pull request — no longer a refusal
    for one in another repository (#2234), nor a review of that one alone (#2395)."""
    work = _work(tmp_path, _record("In review"))
    elsewhere = OutputFact(
        f"https://github.com/{ORG}/store/pull/3374", f"{ORG}/store", 3374, "poller_card", "open"
    )
    gone = OutputFact(
        f"https://github.com/{ORG}/store/pull/3370", f"{ORG}/store", 3370, "poller_card", "closed"
    )
    work = replace(work, outputs=(*work.outputs, elsewhere, gone))

    step = plan("chatgpt_review", work, operator_attention(work, work_status(work)))

    assert (step.kind, step.issue) == ("chatgpt_review", ISSUE)
    assert step.pulls == (
        *(f"{o.repository}#{o.number}" for o in work.outputs[:-2]),
        f"{ORG}/store#3374",
    )


@private_feature(_chat_review)
def test_the_same_repository_in_another_case_is_not_another_repository(tmp_path):
    work = _work(tmp_path, _record("In review"))
    shouted = replace(work.outputs[0], repository=f"{ORG.upper()}/AgentDeck")
    work = replace(work, outputs=(shouted,))

    assert plan("chatgpt_review", work, operator_attention(work, work_status(work))).issue == ISSUE


@private_feature(_chat_review)
def test_the_row_and_the_page_offer_the_same_controls(tmp_path):
    work = _work(tmp_path, _record("In review"))
    attention = operator_attention(work, work_status(work))

    row = buttons(work, attention)
    page = buttons(work, attention, page=True)

    assert [b.action for b in row] == ["agent_review", "chatgpt_review", "review", "hide"]
    assert [b.action for b in page] == [b.action for b in row]
    review = row[-2]
    assert (review.href, review.external) == (PR, True)
    # Hide changes only this deck, so it posts to the visibility route, not the action one.
    assert (row[-1].route, row[0].route) == ("visibility", "action")
    parked = _work(tmp_path, RETRY_PARK)
    attention = operator_attention(parked, work_status(parked))
    assert [b.action for b in buttons(parked, attention)] == ["inspect", "hide"]
    # Inspect opens the Work page, so the Work page itself does not offer it; a hidden Work
    # does not offer Hide again.
    assert [b.action for b in buttons(parked, attention, page=True)] == ["hide"]
    assert [b.action for b in buttons(parked, attention, page=True, hidden=True)] == []
    merge = _work(tmp_path, _record("Ready to merge", **_passed()), verifications=GREEN)
    control, _hide = buttons(merge, operator_attention(merge, work_status(merge)))
    assert control.action == "merge" and control.href == ""


def _press(action="close", *, ago_s=0.0, outcome="done") -> WorkActionPress:
    at = (datetime.now(UTC) - timedelta(seconds=ago_s)).isoformat()
    return WorkActionPress("a" * 12, WORK.work_id, action, at, outcome=outcome)


def test_a_recent_press_holds_its_action_unless_it_is_known_to_have_failed():
    assert recent_press((_press(),), "close") is not None
    assert recent_press((_press(outcome=None),), "close") is not None  # may have landed
    assert recent_press((_press(outcome="failed"),), "close") is None
    assert recent_press((_press(ago_s=RECENT_ACTION_S + 1),), "close") is None
    assert recent_press((_press("chatgpt_review"),), "close") is None


# --- the deck's Merge presses (#2396) -------------------------------------------------------------


# --- the route -----------------------------------------------------------------------------------


class _Gh:
    """``gh_cli.run_once`` as the route calls it, answering from a script."""

    def __init__(self, *answers):
        self.calls: list[tuple[list[str], bytes | None, dict | None]] = []
        self.answers = list(answers) or [(0, b"https://github.com/c/1", b"")]

    async def __call__(self, argv, *, timeout, stdin=None, repo=None, owner=None):
        self.calls.append((list(argv), stdin, repo))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _route_app(tmp_path, monkeypatch, record: dict, *, gh: _Gh | None = None, **extra):
    from test_web import _app_with_state

    from agentdeck.web import routes_works
    from agentdeck.web.work_view import WorkDetail

    app = _app_with_state(tmp_path)
    work = _work(tmp_path, record, **extra)
    holder = {"card": work}

    async def detail(_app, ref, **_kw):
        assert ref == WORK
        return WorkDetail(card=holder["card"], trace=None, workspace=None, chats=(),
                          verifications=())

    monkeypatch.setattr(routes_works, "load_work_detail", detail)
    monkeypatch.setattr(routes_works.shutil, "which", lambda name: "/usr/bin/gh")
    gh = gh or _Gh()
    monkeypatch.setattr(gh_cli, "run_once", gh)
    return app, gh, holder


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


URL = work_href(WORK.work_id) + "/action"


def _post(client, action, action_id="press-0001", **headers):
    return client.post(URL, data={"action": action, "client_action_id": action_id},
                       headers=headers)


CLOSABLE = _record("In review")
CLOSED = {"rows": {PR.lower(): _row("closed")}}


def _closable_app(tmp_path, monkeypatch, **kwargs):
    return _route_app(tmp_path, monkeypatch, CLOSABLE, **CLOSED, **kwargs)


def test_a_write_the_work_does_not_offer_writes_nothing(tmp_path, monkeypatch):
    app, gh, _ = _route_app(tmp_path, monkeypatch, _record("Ready to merge", **_passed()))

    response = _post(_client(app), "close")

    assert response.status_code == 409 and "not offered" in response.text
    assert gh.calls == [] and app.state.control_db.load_work_actions(WORK.work_id) == ()


def test_a_close_runs_as_the_owners_non_human_authority(tmp_path, monkeypatch):
    app, gh, _ = _closable_app(tmp_path, monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "service-token")

    response = _post(_client(app), "close")

    assert response.status_code == 200
    assert 'data-wd-receipt="sent"' in response.text and f"Closed {ISSUE}" in response.text
    ((_argv, stdin, repo),) = gh.calls
    assert stdin is None and repo == f"{ORG}/agentdeck"
    (press,) = app.state.control_db.load_work_actions(WORK.work_id)
    assert (press.action, press.state, press.action_id) == ("close", "sent", "press-0001")


def test_a_refused_authority_is_a_failed_press_that_may_be_retried(tmp_path, monkeypatch):
    app, gh, _ = _closable_app(
        tmp_path, monkeypatch,
        gh=_Gh(gh_cli.GitHubRefusal("github: no GitHub authority configured for owner x")),
    )

    response = _post(_client(app), "close")

    assert response.status_code == 409 and "no GitHub authority" in response.text
    (press,) = app.state.control_db.load_work_actions(WORK.work_id)
    assert press.state == "failed"


def test_close_closes_the_source_issue_with_the_categorys_reason(tmp_path, monkeypatch):
    app, gh, _ = _route_app(
        tmp_path, monkeypatch, _record("In review"), rows={PR.lower(): _row("closed")}
    )

    response = _post(_client(app), "close")

    assert response.status_code == 200 and f"Closed {ISSUE}" in response.text
    assert gh.calls[0][0] == ["/usr/bin/gh", "issue", "close", "5", "--repo", f"{ORG}/agentdeck",
                              "--reason", "not planned"]


def test_an_action_the_work_no_longer_offers_is_refused_before_any_write(tmp_path, monkeypatch):
    app, gh, holder = _closable_app(tmp_path, monkeypatch)
    # The row offered Close; by the time the press lands the Work moved on.
    holder["card"] = _work(tmp_path, _record("In progress"), outcome="running")

    response = _post(_client(app), "close")

    assert response.status_code == 409 and "not offered" in response.text
    assert 'data-wd-receipt="refused"' in response.text
    assert gh.calls == [] and app.state.control_db.load_work_actions(WORK.work_id) == ()


@pytest.mark.parametrize(
    "headers",
    [{"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}],
    ids=["origin", "fetch-site"],
)
def test_a_cross_site_press_is_refused(tmp_path, monkeypatch, headers):
    app, gh, _ = _closable_app(tmp_path, monkeypatch)

    response = _post(_client(app), "close", **headers)

    assert response.status_code == 403 and gh.calls == []


def test_a_replayed_press_answers_with_what_it_did_and_writes_once(tmp_path, monkeypatch):
    app, gh, _ = _closable_app(tmp_path, monkeypatch)
    monkeypatch.setattr(chat_review, "open_review", None)  # never reached
    client = _client(app)

    first = _post(client, "close")
    again = _post(client, "close")

    assert first.status_code == again.status_code == 200
    assert "Already sent" in again.text and len(gh.calls) == 1
    # The same id for another action is not a replay.
    other = _post(client, "chatgpt_review")
    assert other.status_code == 409 and "something else" in other.text and len(gh.calls) == 1


@pytest.mark.parametrize(
    "data",
    [{"action": "close"}, {"action": "close", "client_action_id": "short"},
     {"action": "close", "client_action_id": "bad id with spaces"}],
    ids=["missing", "too-short", "malformed"],
)
def test_a_press_without_a_usable_action_id_writes_nothing(tmp_path, monkeypatch, data):
    app, gh, _ = _closable_app(tmp_path, monkeypatch)
    client = _client(app)

    first = client.post(URL, data=data)
    replay = client.post(URL, data=data)

    for response in (first, replay):
        assert response.status_code == 422 and "no usable action ID" in response.text
    # Neither the press nor its replay was given an id of its own, so neither wrote.
    assert gh.calls == [] and app.state.control_db.load_work_actions(WORK.work_id) == ()


def test_a_second_press_inside_the_window_is_refused(tmp_path, monkeypatch):
    app, gh, _ = _closable_app(tmp_path, monkeypatch)
    client = _client(app)

    _post(client, "close", "press-0001")
    second = _post(client, "close", "press-0002")

    assert second.status_code == 409 and "was sent" in second.text and len(gh.calls) == 1


@pytest.mark.parametrize(
    "answer",
    [TimeoutError("timed out"), (1, b"", b"HTTP 502")],
    ids=["no-answer", "no-success"],
)
def test_an_uncertain_write_is_never_made_again(tmp_path, monkeypatch, answer):
    app, gh, _ = _closable_app(tmp_path, monkeypatch, gh=_Gh(answer))
    client = _client(app)

    first = _post(client, "close", "press-0001")
    replay = _post(client, "close", "press-0001")
    fresh = _post(client, "close", "press-0002")

    assert first.status_code == 202 and 'data-wd-receipt="uncertain"' in first.text
    assert "will not be sent again" in first.text
    assert replay.status_code == 202 and "may already have been sent" in replay.text
    assert fresh.status_code == 409 and "may have been sent" in fresh.text
    assert len(gh.calls) == 1
    (press,) = app.state.control_db.load_work_actions(WORK.work_id)
    assert press.outcome is None and press.state == "uncertain"


def test_a_press_known_not_to_have_been_sent_may_be_pressed_again(tmp_path, monkeypatch):
    app, gh, _ = _closable_app(tmp_path, monkeypatch)
    from agentdeck.web import routes_works

    monkeypatch.setattr(routes_works.shutil, "which", lambda name: None)
    client = _client(app)

    missing = _post(client, "close", "press-0001")
    monkeypatch.setattr(routes_works.shutil, "which", lambda name: "/usr/bin/gh")
    retried = _post(client, "close", "press-0002")

    assert missing.status_code == 409 and "gh is not available" in missing.text
    assert retried.status_code == 200 and len(gh.calls) == 1


@private_feature(_chat_review)
def test_chatgpt_review_is_the_existing_review_workflow(tmp_path, monkeypatch):
    app, gh, _ = _route_app(tmp_path, monkeypatch, _record("In review"))
    asked: list[tuple[str, bool]] = []

    async def open_review(config, target, *, start=True, pulls=(), board=None, releases=None):
        asked.append((target, start))
        assert pulls == (f"{ORG}/agentdeck#7",), "the open pull request rides along (#2637)"
        if len(asked) == 1:
            raise chat_review.ReviewError("this deck has no [chat_board] repository configured")
        return chat_review.ReviewOutcome(f"{ORG}/agentdeck", 5, "u", 12, True, task="p")

    monkeypatch.setattr(chat_review, "open_review", open_review)
    client = _client(app)

    refused = _post(client, "chatgpt_review", "press-0001")
    opened = _post(client, "chatgpt_review", "press-0002")

    assert refused.status_code == 409 and "no [chat_board] repository" in refused.text
    # A refusal is known not to have written, so the press is not held.
    assert opened.status_code == 200 and "chat #12 opened" in opened.text
    assert asked == [(ISSUE, True), (ISSUE, True)] and gh.calls == []


@private_feature(_chat_review)
def test_agent_review_opens_the_review_chat_and_starts_a_claude_reviewer_on_it(
    tmp_path, monkeypatch
):
    """#2736: the press is a Claude reviewer whose verdict the Poller accepts, not a
    ``/claude Review only`` follow-up that could post no verdict at all."""
    app, gh, _ = _route_app(tmp_path, monkeypatch, _record("In review"))
    asked: list[tuple[str, bool]] = []
    started: list[tuple[int, str]] = []

    async def open_review(config, target, *, start=True, pulls=(), board=None, releases=None):
        asked.append((target, start))
        assert pulls == (f"{ORG}/agentdeck#7",)
        return chat_review.ReviewOutcome(
            f"{ORG}/agentdeck", 5, "https://github.com/x/y/issues/12", 12, True,
            note="left for the deck to start",
        )

    class _Reviewer:
        async def start_on(self, row):
            started.append((row.number, row.title))
            # #2766: whichever kind took it, Claude or Codex, is named.
            return True, "Codex"

    monkeypatch.setattr(chat_review, "open_review", open_review)
    app.state.review_fallback = _Reviewer()

    response = _post(_client(app), "agent_review")

    assert response.status_code == 200, response.text
    assert "a Codex reviewer started on chat #12" in response.text
    # Opened without pressing ChatGPT, and the reviewer gets the chat's own review title.
    assert asked == [(ISSUE, False)]
    assert started == [(12, f"Review {ORG}/agentdeck#5")]
    assert gh.calls == []


@private_feature(_chat_review)
def test_agent_review_leaves_a_chat_someone_already_holds(tmp_path, monkeypatch):
    app, _gh, _ = _route_app(tmp_path, monkeypatch, _record("In review"))

    async def open_review(config, target, *, start=True, pulls=(), board=None, releases=None):
        return chat_review.ReviewOutcome(
            f"{ORG}/agentdeck", 5, "u", 12, False, note="#12 is in-progress"
        )

    class _Reviewer:
        async def start_on(self, row):
            raise AssertionError("a held chat gets no second reviewer")

    monkeypatch.setattr(chat_review, "open_review", open_review)
    app.state.review_fallback = _Reviewer()

    response = _post(_client(app), "agent_review")

    assert response.status_code == 200 and "#12 is in-progress" in response.text


def _merge_app(tmp_path, monkeypatch):
    """A Ready to merge Work."""
    return _route_app(
        tmp_path, monkeypatch, _record("Ready to merge", **_passed()), verifications=GREEN
    )


def test_a_merge_the_deck_does_not_release_writes_nothing(tmp_path, monkeypatch):
    app, gh, _ = _merge_app(tmp_path, monkeypatch)

    response = _post(_client(app), "merge")

    assert response.status_code == 409 and "does not release this Work" in response.text
    assert app.state.control_db.load_work_actions(WORK.work_id) == ()
    assert gh.calls == []


def test_merge_when_ready_is_not_sent_when_refused_and_may_be_pressed_again(
    tmp_path, monkeypatch
):
    """#3320 review: a press the release did not record is "not sent", and once a press is
    gone (withdrawn here) Merge when ready may be pressed again at once."""
    from agentdeck.web import routes_works
    from agentdeck.work_release import PressRefused

    app, _gh, _holder = _route_app(tmp_path, monkeypatch, _record("In review"))
    state = {"approval": "offered", "refuse": True}

    class Release:
        def handles(self, work_id):
            return True

        async def request_merge(self, work_id, *, fresh=False):
            assert fresh, "Merge when ready asks for a fresh press"
            if state.pop("refuse", False):
                raise PressRefused("eerovil/agentdeck#9's diff could not be read")
            state["approval"] = "approved"
            return "no review pass yet"

        async def withdraw_merge(self, work_id):
            state["approval"] = "offered"
            return "Merge approval withdrawn"

    app.state.work_release = Release()
    real = routes_works.load_work_detail

    async def detail(app_, ref, **kw):
        return replace(await real(app_, ref, **kw), approval=state["approval"])

    monkeypatch.setattr(routes_works, "load_work_detail", detail)
    client = _client(app)
    refused = _post(client, "approve_merge", "press-0001")
    assert refused.status_code == 409 and "not sent" in refused.text
    assert "could not be read" in refused.text
    assert _post(client, "approve_merge", "press-0002").status_code == 200
    assert _post(client, "approve_merge", "press-0003").status_code == 409, "already approved"
    assert _post(client, "withdraw_merge", "press-0004").status_code == 200
    assert _post(client, "approve_merge", "press-0005").status_code == 200, "again at once"


def test_a_merge_the_work_no_longer_offers_writes_nothing(tmp_path, monkeypatch):
    app, gh, _ = _merge_app(tmp_path, monkeypatch)
    from agentdeck.web import routes_works

    async def moved(_app, ref, **_kw):
        from agentdeck.web.work_view import WorkDetail

        return WorkDetail(card=_work(tmp_path, _record("In progress"), outcome="running"),
                          trace=None, workspace=None, chats=(), verifications=())

    monkeypatch.setattr(routes_works, "load_work_detail", moved)

    response = _post(_client(app), "merge")

    assert response.status_code == 409 and "not offered" in response.text
    assert app.state.control_db.load_work_actions(WORK.work_id) == ()
    assert gh.calls == []


def test_the_work_page_shows_the_controls_and_the_last_press(tmp_path, monkeypatch):
    app, _gh, _ = _closable_app(tmp_path, monkeypatch)
    from agentdeck.web.work_view import WorkDetail, render_work_sections

    client = _client(app)
    _post(client, "close")
    (press,) = app.state.control_db.load_work_actions(WORK.work_id)
    detail = WorkDetail(card=_work(tmp_path, CLOSABLE, **CLOSED), trace=None,
                        workspace=None, chats=(), verifications=(), last_action=press)

    status = dict(render_work_sections(app.state.templates, detail))["status"]

    assert f'hx-post="{URL}"' in status and 'value="close"' in status
    assert 'hx-confirm="Close this Work' in status
    assert 'data-work-last-action="close"' in status and f"Closed {ISSUE}" in status
    # Nothing on the page copies or opens GitHub.
    assert "data-github-handoff" not in status and "new_comment_field" not in status


def test_the_work_page_shows_merge_when_ready_then_approved_then_cleared(
    tmp_path, monkeypatch
):
    """#3320: inline beside the headline, and one line once a changed diff cleared it."""
    app, _gh, _ = _closable_app(tmp_path, monkeypatch)
    from agentdeck.web.work_view import WorkDetail, render_work_sections

    card = _work(tmp_path, _record("In progress"), outcome="running")

    def status(**extra):
        detail = WorkDetail(card=card, trace=None, workspace=None, chats=(), verifications=(),
                            **extra)
        return dict(render_work_sections(app.state.templates, detail))["status"]

    offered = status(approval="offered")
    assert 'value="approve_merge"' in offered and 'hx-confirm="Approve merging' in offered
    assert "data-work-merge-approved" not in offered
    approved = status(approval="approved")
    assert "data-work-merge-approved" in approved and 'value="withdraw_merge"' in approved
    cleared = status(approval="offered", approval_cleared="Merge approval cleared: x changed.")
    assert "data-work-merge-cleared" in cleared and "x changed." in cleared
    assert "approve_merge" not in status()


# --- in the browser ------------------------------------------------------------------------------


def _retry_app(tmp_path, monkeypatch):
    """A real Needs retry Work: a dispatch park the engine will not retry by itself."""
    from test_work_ui import _app, _board, _card

    from agentdeck.web import routes_works

    card = _card(status="Blocked", status_role="blocked", generation="gen-2",
                 dispatch_failed=True, dispatch_failed_at=AT,
                 state_path=str(tmp_path / "instances" / "agentdeck" / "state.json"),
                 state_key="agentdeck#2147")
    app = _app(tmp_path, board=_board(card))
    monkeypatch.setattr(routes_works.shutil, "which", lambda name: "/usr/bin/gh")
    gh = _Gh()
    monkeypatch.setattr(gh_cli, "run_once", gh)
    return app, gh, card


async def _no_overflow(page, width: int, scope: str = "main") -> None:
    overflow = await page.evaluate(
        """([w, scope]) => [...document.querySelectorAll(scope + ' *')].filter(
            node => node.getClientRects().length && node.getBoundingClientRect().right > w + 1
        ).map(node => node.className || node.tagName)""",
        [width, scope],
    )
    assert overflow == []


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_work_chat_presses_its_works_button_on_a_phone_not_beside_the_work(
    tmp_path, monkeypatch
):
    """#2972: on a phone the chat's Work buttons sit under its chips, and a press answers in
    their own slot. On a desktop, where the Work column beside the chat offers the same
    buttons, the chat shows none."""
    from conftest import async_playwright
    from test_web import _live_server

    app, _work, worker, _manual, _card = _retry_chat_app(tmp_path, monkeypatch)
    await app.state.work_overview.refresh()
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 760},
                                      service_workers="block")
        await page.goto(base + f"/sessions/{worker}")
        row = page.locator("[data-session-work-actions]")
        pause = row.locator('button[data-front-action="pause"]')
        await pause.wait_for()
        await _no_overflow(page, 390, "#session-action-bar")
        if screenshot_dir:
            await page.locator("#session-action-bar").screenshot(
                path=str(Path(screenshot_dir) / "issue-2972-chat-work-actions-390.png")
            )
        await pause.click()
        await row.locator(".work-action-result").get_by_text("Paused").wait_for()

        desk = await browser.new_page(viewport={"width": 1440, "height": 900},
                                      service_workers="block")
        await desk.goto(base + f"/sessions/{worker}")
        await desk.locator(".front-columns > .work-column .work-main").wait_for()
        assert not await desk.locator("[data-session-work-actions]").is_visible()
        await browser.close()

    (press,) = app.state.control_db.load_work_actions(_work.work_id)
    assert press.action == "pause"


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1024, 320])
async def test_a_needs_you_row_carries_no_action_and_swaps_in_place(
    tmp_path, monkeypatch, width
):
    """#2254: a front-page row names the wait and opens the Work; every action is there."""
    from conftest import async_playwright
    from test_web import _live_server
    from test_work_ui import WORK as UI_WORK
    from test_work_ui import _add

    app, gh, _card = _retry_app(tmp_path, monkeypatch)
    await app.state.work_overview.refresh()
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": width, "height": 700},
                                      service_workers="block")
        await page.add_init_script(
            "document.addEventListener('htmx:sseOpen', () => { window.__sseOpen = true; }, true)"
        )
        await page.goto(base + "/")
        row = page.locator(f'#works-stuck [data-work-id="{UI_WORK.work_id}"]')
        await row.wait_for()
        await page.wait_for_function("() => window.__sseOpen === true")
        assert await row.locator("[data-front-action], form").count() == 0
        assert await row.get_attribute("data-op-category") == "needs_retry"
        group = page.locator('#works-stuck [data-front-group="needs_retry"]')
        assert "Needs retry" in await group.text_content()
        assert await row.locator(".fr-body").get_attribute("href") == work_href(UI_WORK.work_id)
        await _no_overflow(page, width)
        assert await page.evaluate("() => document.documentElement.scrollWidth") <= width
        before = await row.get_attribute("data-work-rev")

        # A live update replaces the row whole; its menu comes back open.
        await row.evaluate("el => { el.__before = true; }")
        _add(app, "claude_code:test:worker", UI_WORK, title="Worker chat")
        await app.state.work_overview.refresh()
        await page.wait_for_function(
            "([id, rev]) => { const el = document.querySelector("
            "`#works-stuck [data-work-id=\"${id}\"]`);"
            " return el && !el.__before && el.dataset.workRev !== rev; }",
            arg=[UI_WORK.work_id, before],
        )
        assert await page.locator('#works-stuck [data-front-group="needs_retry"]').count() == 1
        assert gh.calls == []
        await browser.close()


def _retry_chat_app(tmp_path, monkeypatch):
    """``_retry_app`` with one chat its Work owns and one no Work owns (#2972)."""
    from test_work_ui import WORK as UI_WORK
    from test_work_ui import _add

    app, gh, card = _retry_app(tmp_path, monkeypatch)
    worker = _add(app, "claude_code:test:worker", UI_WORK)
    manual = _add(app, "claude_code:test:manual")
    return app, UI_WORK, worker, manual, card


@pytest.mark.asyncio
async def test_a_work_chat_shows_its_works_buttons_under_the_chips(tmp_path, monkeypatch):
    """#2972: a chat its Work owns offers that Work's buttons under the Issue/PR chips, posting
    to the Work's own action route, with its own answer slot; a chat no Work owns offers none."""
    app, work, worker, manual, _card = _retry_chat_app(tmp_path, monkeypatch)
    await app.state.work_overview.refresh()
    client = _client(app)

    page = client.get(f"/sessions/{worker}").text
    row = page.split('id="session-action-bar"', 1)[1].split("data-session-work-actions", 1)[1]
    assert f'hx-post="{work_href(work.work_id)}/action"' in row
    assert 'data-front-action="pause"' in row and 'value="pause"' in row
    assert 'class="work-action-result"' in row
    # The header already links up to the Work, so Inspect is not repeated here.
    assert 'data-front-action="inspect"' not in row
    assert "data-session-work-actions" not in client.get(f"/sessions/{manual}").text
    # The press answers like it does on the Work page.
    assert "data-work-action" in row and 'hx-target="next .work-action-result"' in row


def test_a_work_chat_adds_nothing_before_the_works_are_read(tmp_path, monkeypatch):
    """Read from the front page's snapshot alone: before the first one, the chat is as it was."""
    app, _work, worker, _manual, _card = _retry_chat_app(tmp_path, monkeypatch)
    assert "data-session-work-actions" not in _client(app).get(f"/sessions/{worker}").text


def test_chat_work_actions_drops_links_to_the_same_chat_and_adds_pause(tmp_path):
    from types import SimpleNamespace

    from agentdeck.web import work_view
    from agentdeck.work_actions import ActionButton

    card = SimpleNamespace(work_id="w", paused=False, href="/works/w")
    overview = SimpleNamespace(cards=(card,), front=SimpleNamespace(hidden=()))
    app = SimpleNamespace(state=SimpleNamespace(work_overview=SimpleNamespace(overview=overview)))
    offered = (
        ActionButton("open", "Open", "/chats/c1?work=w"),
        ActionButton("open", "Open", "/chats/other"),
        ActionButton("inspect", "Inspect", "/works/w"),
        ActionButton("close", "Close issue"),
    )
    stub = SimpleNamespace(where_href="")
    pause = ActionButton("pause", "Pause")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(work_view, "work_status", lambda _card: stub)
        patch.setattr(work_view, "waiting_story", lambda *_a: None)
        patch.setattr(work_view, "operator_attention", lambda *_a: object())
        patch.setattr(work_view, "buttons", lambda *_a, **_k: offered)
        patch.setattr(work_view, "pause_control", lambda _card: pause)
        named, shown = work_view.chat_work_actions(app, "w", ("/chats/c1", "/sessions/k"))
        app.state.work_overview.overview = SimpleNamespace(
            cards=(card,), front=SimpleNamespace(hidden=(SimpleNamespace(card=card),))
        )
        _, when_hidden = work_view.chat_work_actions(app, "w", ())
        assert work_view.chat_work_actions(app, "missing", ()) == (None, ())
        assert work_view.chat_work_actions(app, None, ()) == (None, ())

    assert named is card
    assert [(b.action, b.href) for b in shown] == [
        ("open", "/chats/other"), ("close", ""), ("pause", "")
    ]
    assert [b.action for b in when_hidden][-2:] == ["unhide", "pause"]


# --- a press acted on reads in the past tense (#2460) -------------------------------------------


def test_a_merge_press_reads_as_done_once_the_work_merged(tmp_path):
    """issues#485: a press's receipt reads in the past tense once it was acted on."""
    from types import SimpleNamespace

    from agentdeck.run_trace import RunSession
    from agentdeck.web.work_view import WorkDetail

    work = _work(tmp_path, _record("In review"))

    def detail(bound_ago_s, *, action, card=work):
        bound = datetime.now(UTC) - timedelta(seconds=bound_ago_s)
        session = RunSession("c:a:w", "c:a:w", "claude_code", "a", bound)
        trace = SimpleNamespace(current=SimpleNamespace(sessions=(session,)))
        return WorkDetail(card, trace, None, (), (), last_action=action)

    merge = _press("merge", ago_s=3000)
    assert detail(2900, action=merge).last_action_followed == ""
    merged = replace(work, deploying=True)
    assert detail(2900, action=merge, card=merged).last_action_followed == "Merge asked; merged"


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_red_run_on_an_old_head_leaves_the_work_page_ready_for_merge(
    tmp_path, monkeypatch
):
    """issues#485 (#2460): the page said Needs retry, Verification failed and "Look at the
    verification" beside "CI passed". A red run on a head the pull request moved past, with
    Drone green on the new one, is now ready for a person to merge on GitHub (#3025); the old
    failure stays in Result, muted and naming the new head."""
    from conftest import async_playwright
    from test_web import _live_server
    from test_work_attention import OLDER, _ci, _red

    run_id = _work(tmp_path, _record("In review"), reviewer=_reviewer([KEY])).run_id
    app, _gh, _holder = _route_app(
        tmp_path, monkeypatch, _record("In review"), reviewer=_reviewer([KEY]),
        verifications=_red(run_id, OLDER), ci=_ci("green"),
    )
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 390, "height": 900}, service_workers="block"
        )
        page = await context.new_page()
        await page.goto(base + work_href(WORK.work_id))
        status = page.locator("#work-status .wd-op")
        await status.wait_for()
        assert await status.get_attribute("data-op-category") == "ready_for_merge"
        assert await page.locator('#work-status [data-front-action="review"]').count() == 1
        head = await page.locator("#work-status .wd-status-head").text_content()
        assert "Ready for you to merge" in head
        red = page.locator("#work-result .wk-verify.is-failed.is-superseded")
        assert "the pull request's head is now" in await red.text_content()
        await _no_overflow(page, 390, "#work-status")
        if screenshot_dir:
            await page.locator("main.work-main").screenshot(
                path=str(Path(screenshot_dir) / "issue-2460-work-red-old-head-390.png")
            )
        await browser.close()
