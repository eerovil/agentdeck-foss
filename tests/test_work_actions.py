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
from types import SimpleNamespace
from uuid import uuid4

import pytest
from public_copy import private_feature
from test_work_attention import HEAD, ORG, PR, STOPPED_CHAT, WORK, _derived, _row
from test_work_attention import _work as _work_of

from agentdeck import chat_review, gh_cli, spoken_merge
from agentdeck import chat_review as _chat_review
from agentdeck.voice_mode import VOICE_LINE
from agentdeck.work_actions import (
    RECENT_ACTION_S,
    TRIED_COMMENT,
    WRITES,
    Refused,
    WorkActionPress,
    buttons,
    plan,
    recent_press,
    tried_buttons,
)
from agentdeck.work_attention import operator_attention
from agentdeck.work_facts import OutputFact
from agentdeck.work_overview import work_href
from agentdeck.work_story import work_status

ISSUE = f"{ORG}/agentdeck#5"


def _idle(**extra):
    """A Work with its pull request open that waits on nobody."""
    return _work_of(**extra)


def _stopped(**extra):
    """A Needs retry Work: a chat on it stopped, and nothing restarts it by itself."""
    return _work_of(attention=(STOPPED_CHAT,), **extra)


def _reviewing(**extra):
    """A Work the deck runs whose ready pull request no reviewer took (#2960)."""
    return _work_of(derived=_derived("review_stuck", "No reviewer runs"), **extra)


def _ready(**extra):
    """A Work the deck runs, reviewed and tested and waiting on a person's Merge (#2960)."""
    return _work_of(derived=_derived("ready", "Reviewed and tested"), **extra)


def _unstarted(**extra):
    """An open issue no agent works on (#3558): Start agent, or Close issue (#3562)."""
    return replace(_work_of(**extra), start_label="agent")


def _tried(**extra):
    """A merged Work whose agent recorded that only a person can try it live (#4210)."""
    return _work_of(derived=_derived("try_wait", "Waiting for a try"), **extra)


def _planned(work, action: str):
    return plan(action, work, operator_attention(work, work_status(work)))


# --- what each action does ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("work", "action", "kind", "body"),
    [
        (_unstarted, "close", "close", "completed"),
        (_unstarted, "start", "add_label", "agent"),
        pytest.param(
            _reviewing, "chatgpt_review", "chatgpt_review", "",
            marks=private_feature(_chat_review),
        ),
        pytest.param(
            _reviewing, "agent_review", "agent_review", "",
            marks=private_feature(_chat_review),
        ),
    ],
    ids=["close", "start", "chatgpt-review", "agent-review"],
)
def test_each_deck_write_makes_one_fixed_write_on_the_source_issue(work, action, kind, body):
    step = _planned(work(), action)

    assert (step.kind, step.issue, step.body) == (kind, ISSUE, body)


def test_works_close_is_offered_only_while_the_work_waits_for_a_try():
    """#4210: one press, no confirm; Doesn't work is only a link to the message box."""
    work = _tried()

    ok, bad = tried_buttons(work)
    step = plan("tried_ok", work, None)

    assert (ok.action, ok.confirm, ok.route) == ("tried_ok", "", "action")
    assert (bad.action, bad.href) == ("tried_bad", f"{work.href}#work-message")
    assert tried_buttons(work, page=True)[1].href == "#work-message"
    assert tried_buttons(_idle()) == ()
    assert (step.kind, step.issue, step.body) == ("close", ISSUE, "completed")
    assert step.comment.startswith("Tried live by a person")
    for other in (_idle(), _ready()):
        with pytest.raises(Refused, match="not offered"):
            plan("tried_ok", other, operator_attention(other, work_status(other)))
    # A paused Work offers only Resume, so Works, close is neither shown nor run (review).
    paused = replace(work, paused=True)
    assert tried_buttons(paused) == ()
    with pytest.raises(Refused, match="not offered"):
        plan("tried_ok", paused, None)


def test_merge_is_a_confirmed_form():
    """#2324: Merge no longer hands `/merge` to GitHub; it is one press, and it asks first."""
    work = _ready()
    attention = operator_attention(work, work_status(work))

    (control,) = [button for button in buttons(work, attention) if button.action == "merge"]

    assert (control.href, control.route) == ("", "action")
    assert control.confirm.startswith("Merge this Work?")


def test_merge_is_refused_for_a_work_the_deck_does_not_release():
    work = _ready()

    with pytest.raises(Refused, match="does not release"):
        plan("merge", work, operator_attention(work, work_status(work)))


def test_the_writes():
    assert WRITES == {
        "close", "agent_review", "chatgpt_review", "merge", "pause", "resume", "approve_merge",
        "withdraw_merge", "start", "ship_now", "tried_ok",
    }


def test_merge_when_ready_and_withdraw_follow_the_releases_press():
    """#3320: offered from the release's own state on a Work not yet ready, like Pause."""
    work = _idle()
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
    closed = _idle(rows={PR.lower(): _row("merged")})
    with pytest.raises(Refused, match="no open pull request"):
        plan("approve_merge", closed, None, approval="offered")
    ready = _ready()
    with pytest.raises(Refused, match="Merge is offered instead"):
        plan("approve_merge", ready, operator_attention(ready, work_status(ready)),
             approval="offered")
    # #4294: a question over a ready pull request keeps its Merge, so the press is planned.
    asked = _work_of(derived=_derived("asked_in_chat", "Needs you", ready_pr=7))
    attention = operator_attention(asked, work_status(asked))
    assert plan("merge", asked, attention, deck_release=True).kind == "deck_merge"
    with pytest.raises(Refused, match="Merge is offered instead"):
        plan("approve_merge", asked, attention, approval="offered")


@pytest.mark.parametrize(
    ("work", "action", "status"),
    [
        (_ready, "close", 409),
        (_reviewing, "hide", 422),  # a link or unknown id is not a write
        (_reviewing, "rm -rf", 422),
    ],
    ids=["close-not-offered", "hide", "garbage"],
)
def test_an_action_the_work_does_not_offer_now_is_refused(work, action, status):
    with pytest.raises(Refused) as refused:
        _planned(work(), action)

    assert refused.value.status == status


def test_a_work_that_needs_nobody_offers_nothing():
    work = _idle()

    assert operator_attention(work, work_status(work)) is None
    for action in WRITES - {"pause"}:
        with pytest.raises(Refused):
            plan(action, work, None)
    # Pause is offered from the Work's own state, on any unfinished Work (#2559).
    assert plan("pause", work, None).kind == "deck_pause"


@private_feature(_chat_review)
def test_chatgpt_review_covers_the_whole_work_wherever_its_pull_requests_are():
    """#2637: one chat about the issue, listing every open pull request — no longer a refusal
    for one in another repository (#2234), nor a review of that one alone (#2395)."""
    work = _reviewing()
    found_by = "chat_registration"
    elsewhere = OutputFact(
        f"https://github.com/{ORG}/store/pull/3374", f"{ORG}/store", 3374, found_by, "open"
    )
    gone = OutputFact(
        f"https://github.com/{ORG}/store/pull/3370", f"{ORG}/store", 3370, found_by, "closed"
    )
    work = replace(work, outputs=(*work.outputs, elsewhere, gone))

    step = plan("chatgpt_review", work, operator_attention(work, work_status(work)))

    assert (step.kind, step.issue) == ("chatgpt_review", ISSUE)
    assert step.pulls == (
        *(f"{o.repository}#{o.number}" for o in work.outputs[:-2]),
        f"{ORG}/store#3374",
    )


@private_feature(_chat_review)
def test_the_same_repository_in_another_case_is_not_another_repository():
    work = _reviewing()
    shouted = replace(work.outputs[0], repository=f"{ORG.upper()}/AgentDeck")
    work = replace(work, outputs=(shouted,))

    assert plan("chatgpt_review", work, operator_attention(work, work_status(work))).issue == ISSUE


@private_feature(_chat_review)
def test_the_row_and_the_page_offer_the_same_controls():
    work = _reviewing()
    attention = operator_attention(work, work_status(work))

    row = buttons(work, attention)
    page = buttons(work, attention, page=True)

    assert [b.action for b in row][1:] == ["agent_review", "chatgpt_review", "review", "hide"]
    assert [b.action for b in page][1:] == [b.action for b in row][1:]
    review = row[-2]
    assert (review.href, review.external) == (PR, True)
    # Hide changes only this deck, so it posts to the visibility route, not the action one.
    assert (row[-1].route, row[1].route) == ("visibility", "action")
    parked = _stopped()
    attention = operator_attention(parked, work_status(parked))
    assert [b.action for b in buttons(parked, attention)] == ["inspect", "hide"]
    # Inspect opens the Work page, so the Work page itself does not offer it; a hidden Work
    # does not offer Hide again.
    assert [b.action for b in buttons(parked, attention, page=True)] == ["hide"]
    assert [b.action for b in buttons(parked, attention, page=True, hidden=True)] == []
    merge = _ready()
    control, *_rest = buttons(merge, operator_attention(merge, work_status(merge)))
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


def _route_app(tmp_path, monkeypatch, make=_reviewing, *, gh: _Gh | None = None, **extra):
    from test_web import _app_with_state

    from agentdeck.web import routes_works
    from agentdeck.web.work_view import WorkDetail

    app = _app_with_state(tmp_path)
    work = make(**extra)
    holder = {"card": work}

    async def detail(_app, ref, **_kw):
        assert ref == WORK
        return WorkDetail(card=holder["card"], workspace=None, chats=(), verifications=())

    monkeypatch.setattr(routes_works, "load_work_detail", detail)
    monkeypatch.setattr(routes_works.shutil, "which", lambda name: "/usr/bin/gh")
    gh = gh or _Gh()
    monkeypatch.setattr(gh_cli, "run_once", gh)
    return app, gh, holder


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


URL = work_href(WORK.work_id) + "/action"


def _post(client, action, action_id="press-0001", confirmed="", **headers):
    data = {"action": action, "client_action_id": action_id}
    if confirmed:
        data["confirmed"] = confirmed
    return client.post(URL, data=data, headers=headers)


def _closable_app(tmp_path, monkeypatch, **kwargs):
    return _route_app(tmp_path, monkeypatch, _unstarted, **kwargs)


def test_a_write_the_work_does_not_offer_writes_nothing(tmp_path, monkeypatch):
    app, gh, _ = _route_app(tmp_path, monkeypatch, _ready)

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


def test_close_closes_the_source_issue_as_completed(tmp_path, monkeypatch):
    app, gh, _ = _closable_app(tmp_path, monkeypatch)

    response = _post(_client(app), "close")

    assert response.status_code == 200 and f"Closed {ISSUE}" in response.text
    assert gh.calls[0][0] == ["/usr/bin/gh", "issue", "close", "5", "--repo", f"{ORG}/agentdeck",
                              "--reason", "completed"]


def _merged_open():
    """Every pull request merged, the issue still open, the chat's last reply a question (#4631)."""
    return replace(
        _unstarted(), start_label="", unsettled_registered=False,
        derived=_derived("asked_in_chat", "asked"),
        outputs=tuple(replace(o, state="merged") for o in _ready().outputs),
    )


def _no_pulls_open():
    """No pull request at all, the issue still open (#4631)."""
    return replace(_merged_open(), outputs=(), work_state="open", issue_title="Why?")


@pytest.mark.parametrize("made", [_merged_open, _no_pulls_open])
def test_closing_a_work_with_nothing_open_finishes_it_and_redraws_the_front_page(
    tmp_path, monkeypatch, made
):
    app, gh, holder = _route_app(tmp_path, monkeypatch, made)
    refreshed = []

    async def reread(keys, *, limit):
        refreshed.append((list(keys), limit))
        return 1

    app.state.issues = SimpleNamespace(refresh_issue_records=reread)

    class Overview:
        async def refresh(self):
            refreshed.append(app.state.control_db.work_signoffs()[WORK.work_id][1])

    app.state.work_overview = Overview()
    app.state.control_db.hide_work(WORK.work_id, None)

    assert _post(_client(app), "close").status_code == 200 and len(gh.calls) == 1
    read, finished = refreshed  # the closed issue is read, then the front page is rebuilt
    assert read == ([(WORK.repository, WORK.number)], 1) and finished is not None
    assert app.state.control_db.hidden_works() == {}
    assert app.state.control_db.work_live_check(WORK.work_id) is None


def test_an_uncertain_close_records_no_finish(tmp_path, monkeypatch):
    app, _gh, _ = _route_app(
        tmp_path, monkeypatch, _merged_open, gh=_Gh((1, b"", b"HTTP 404"))
    )

    assert _post(_client(app), "close").status_code == 202
    assert app.state.control_db.work_signoffs() == {}


def test_a_merged_works_close_is_worked_out_again_at_the_press(tmp_path, monkeypatch):
    app, gh, holder = _route_app(tmp_path, monkeypatch, _merged_open)
    for now in (
        replace(holder["card"], derived=_derived("question", "asked")),
        replace(holder["card"], outputs=_ready().outputs),
    ):
        holder["card"] = now
        assert _post(_client(app), "close").status_code == 409
    assert gh.calls == []


def test_close_is_confirmed_unless_the_works_merges_are_done():
    def confirm(work):
        shown = buttons(work, operator_attention(work, work_status(work)))
        return next(item.confirm for item in shown if item.action == "close")

    asks = "Close this Work's issue on GitHub?"
    assert confirm(_unstarted()) == confirm(_no_pulls_open()) == asks
    assert confirm(_merged_open()) == ""
    closed = tuple(replace(output, state="closed") for output in _merged_open().outputs)
    assert confirm(replace(_no_pulls_open(), outputs=closed)) == asks


def test_works_close_closes_records_verified_and_finishes_once(tmp_path, monkeypatch):
    app, gh, _ = _route_app(tmp_path, monkeypatch, _tried)
    order = []

    async def reread(_keys, *, limit):
        order.append("reread")

    async def refresh():
        order.append("refresh")

    app.state.issues = SimpleNamespace(refresh_issue_records=reread)
    app.state.work_overview = SimpleNamespace(refresh=refresh)
    client = _client(app)

    first = _post(client, "tried_ok")
    again = _post(client, "tried_ok")

    assert order == ["reread", "refresh"]

    assert first.status_code == again.status_code == 200 and len(gh.calls) == 1
    assert gh.calls[0][0][-4:] == ["--reason", "completed", "--comment", TRIED_COMMENT]
    check = app.state.control_db.work_live_check(WORK.work_id)
    assert (check.result, check.note) == ("verified", "tried live by a person")
    assert app.state.control_db.work_signoffs()[WORK.work_id][1] is not None


def test_an_unconfirmed_works_close_records_no_live_check(tmp_path, monkeypatch):
    app, gh, _ = _route_app(tmp_path, monkeypatch, _tried, gh=_Gh((1, b"", b"HTTP 404")))

    response = _post(_client(app), "tried_ok")

    assert response.status_code == 202  # it may have closed: nothing is recorded or retried
    assert app.state.control_db.work_live_check(WORK.work_id) is None


def test_an_action_the_work_no_longer_offers_is_refused_before_any_write(tmp_path, monkeypatch):
    app, gh, holder = _closable_app(tmp_path, monkeypatch)
    # The row offered Close; by the time the press lands the Work moved on.
    holder["card"] = _idle()

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
    app, gh, _ = _route_app(tmp_path, monkeypatch)
    asked: list[tuple[str, bool]] = []

    async def open_review(
        config, target, *, start=True, pulls=(), board=None, releases=None, earlier=None,
        shipped=None,
        pressed=False,
    ):
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
    app, gh, _ = _route_app(tmp_path, monkeypatch)
    asked: list[tuple[str, bool]] = []
    started: list[tuple[int, str]] = []

    async def open_review(
        config, target, *, start=True, pulls=(), board=None, releases=None, earlier=None,
        shipped=None,
        pressed=False,
    ):
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
    app, _gh, _ = _route_app(tmp_path, monkeypatch)

    async def open_review(
        config, target, *, start=True, pulls=(), board=None, releases=None, earlier=None,
        shipped=None,
        pressed=False,
    ):
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
    """A Ready for merge Work."""
    return _route_app(tmp_path, monkeypatch, _ready)


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

    app, _gh, _holder = _route_app(tmp_path, monkeypatch, _idle)
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


def _spoken_app(tmp_path, monkeypatch, make, *, cards=1):
    """The deck chat exists, the owner just spoke in it, and the overview lists the Work."""
    from types import SimpleNamespace

    from agentdeck import meta_chat
    from agentdeck.web import routes_works

    app, _gh, holder = _route_app(tmp_path, monkeypatch, make)
    card = holder["card"]
    other = replace(card, work_id=card.work_id.replace("agentdeck#", "games#"))

    class Overview:
        async def current(self, *, wait=0.0):
            return SimpleNamespace(cards=(card, other)[:cards])

        async def refresh(self):
            return None

    app.state.work_overview = Overview()
    state = app.state.app_state
    monkeypatch.setattr(routes_works, "get_db", lambda request: None)
    monkeypatch.setattr(meta_chat, "is_deck_chat", lambda st, db, key: key == "deck-key")
    monkeypatch.setattr(
        state, "session_key_for_token", {"deck-token": "deck-key", "w": "work-key"}.get
    )
    _hear(state, "Merge agentdeck 5")
    presses = []

    class Release:
        def handles(self, work_id):
            return True

        async def request_merge(self, work_id, *, fresh=False):
            presses.append((work_id, fresh))
            return "waits for its tests"

    app.state.work_release = Release()
    return app, presses


def _hear(state, words, send_id=None):
    """The owner's message from a page, delivered at once."""
    send_id = send_id or f"send-{uuid4().hex}"
    state.person_message_arrived("deck-key", send_id)
    state.person_message_accepted("deck-key", send_id, words)
    return send_id


def _aged(state, **ago):
    chat = state.chat_identity_key("deck-key")
    said = state.person_words[chat]
    state.person_words[chat] = said._replace(at=said.at - timedelta(**ago))


def _say(client, work="agentdeck#5", source="deck-token"):
    return client.post("/api/works/merge", json={"work": work, "from": source})


def test_a_spoken_merge_is_the_pages_press_once_per_message(tmp_path, monkeypatch):
    """#4643: the owner's words press the Work page's own button; a repeated call for the same
    message answers with what its press did."""
    from agentdeck.web import routes_works

    app, presses = _spoken_app(tmp_path, monkeypatch, _ready)
    client = _client(app)
    answer = _say(client)
    assert answer.status_code == 200, answer.text
    said = answer.json()
    assert (said["state"], said["work"], said["button"]) == ("sent", ISSUE, "Merge")
    assert "waits for its tests" in said["detail"]
    assert presses == [(WORK.work_id, False)]
    assert "Already sent" in _say(client).json()["detail"]
    (press,) = app.state.control_db.load_work_actions(WORK.work_id)
    assert press.action == "merge"
    with monkeypatch.context() as late:
        late.setattr(routes_works, "SPOKEN_MERGE_S", 0)
        assert "Already sent" in _say(client).json()["detail"], "a late repeat too"
    # Also when that press failed.
    db, state = app.state.control_db, app.state.app_state
    heard = state.person_words[state.chat_identity_key("deck-key")]
    with monkeypatch.context() as failed:
        row = replace(press, outcome="failed", result="why")
        failed.setattr(db, "work_action", lambda _id: row)
        assert "was not sent: why" in _say(client).json()["detail"]
        # A resend of the message takes the words back, and the repeat tells of the press.
        failed.setattr(db, "newest_spoken_press", lambda *_: row)
        _hear(state, heard.words, heard.send_id)
        told = _say(client).json()
        assert told["state"] == "earlier" and "was not sent: why" in told["detail"]
    assert "was sent" in _say(client).json()["detail"]
    # Said again right away: the page's own double-press window.
    _hear(app.state.app_state, "Mergeä agentdeck 5.")
    again = _say(client)
    assert (again.status_code, again.json()["state"]) == (409, "refused")
    assert len(presses) == 1


def test_a_spoken_merge_before_ready_presses_merge_when_ready(tmp_path, monkeypatch):
    from agentdeck.web import routes_works

    app, presses = _spoken_app(tmp_path, monkeypatch, _idle)
    refused = _say(_client(app))
    assert refused.status_code == 409 and "does not hold this Work's merges" in refused.text
    assert presses == []
    real = routes_works.load_work_detail

    approval = "offered"

    async def detail(app_, ref, **kw):
        return replace(await real(app_, ref, **kw), approval=approval)

    monkeypatch.setattr(routes_works, "load_work_detail", detail)
    answer = _say(_client(app)).json()
    assert (answer["state"], answer["button"]) == ("sent", "Merge when ready")
    approval = "approved"
    _hear(app.state.app_state, "yes, merge 5")
    standing = _say(_client(app)).json()
    assert standing["state"] == "sent" and "already pressed" in standing["detail"]
    assert presses == [(WORK.work_id, True)]


def test_a_spoken_merge_is_refused_without_the_owners_words_or_one_work(tmp_path, monkeypatch):
    app, presses = _spoken_app(tmp_path, monkeypatch, _ready, cards=2)
    client = _client(app)
    assert _say(client, source="w").status_code == 403, "another chat"
    assert _say(client, source="nobody").status_code == 403
    state = app.state.app_state
    state.sessions["deck-key"] = SimpleNamespace(key="deck-key")
    by_key = _say(client, source="deck-key")
    del state.sessions["deck-key"]
    assert by_key.status_code == 403, "the key is no proof: every chat can read it"
    assert _say(client, work="agentdeck#99").status_code == 404
    unclear = _say(client, work="5")
    assert unclear.status_code == 409 and len(unclear.json()["works"]) == 2
    _aged(state, hours=1)
    assert _say(client).status_code == 403, "a wake-on's turn, long after the owner spoke"
    state.withdraw_person_words("deck-key")
    assert _say(client).status_code == 403
    assert presses == [] and app.state.control_db.load_work_actions(WORK.work_id) == ()


def test_a_spoken_merge_needs_the_owners_own_words_for_that_work(tmp_path, monkeypatch):
    """Review of #4644: a recent message of the owner's is not consent to any merge."""
    app, presses = _spoken_app(tmp_path, monkeypatch, _ready)
    state, client = app.state.app_state, _client(app)
    for words in ("What is agentdeck 5 about?", "Merge agentdeck 6", "", "file an issue"):
        _hear(state, words)
        answer = _say(client)
        assert answer.status_code == 403 and "say: merge agentdeck 5" in answer.text, words
    _hear(state, "merge agentdeck 5")
    state.person_words.clear()
    assert _say(client).status_code == 403, "a restart forgot the words"
    # A newer message takes the request back.
    _hear(state, "merge agentdeck 5")
    _hear(state, "no, wait")
    assert _say(client).status_code == 403
    assert presses == [] and app.state.control_db.load_work_actions(WORK.work_id) == ()


def _during_read(monkeypatch, happens):
    """``happens`` once, while the spoken call reads the Work."""
    from agentdeck.web import routes_works

    real, todo = routes_works.load_work_detail, [happens]

    async def detail(app_, ref, **kw):
        read = await real(app_, ref, **kw)
        if todo:
            todo.pop()()
        return read

    monkeypatch.setattr(routes_works, "load_work_detail", detail)


@pytest.mark.parametrize(
    "happens", ["message", "delivering", "edited", "resent", "stop", "old", "overview"]
)
def test_a_request_taken_back_while_the_work_is_read_presses_nothing(
    tmp_path, monkeypatch, happens
):
    """Review round 2 of #4644: the check is made with the reservation, not before the reads."""
    app, presses = _spoken_app(tmp_path, monkeypatch, _ready)
    state = app.state.app_state
    _hear(state, "what is new", "send-old")
    asked = _hear(state, "merge agentdeck 5")
    act = {
        # An older message's id with other text, its words not read yet; a plain resend.
        "edited": lambda: state.person_message_arrived("deck-key", "send-old"),
        "resent": lambda: _hear(state, "merge agentdeck 5", asked),
        "message": lambda: _hear(state, "no, wait"),
        "delivering": lambda: state.person_message_arrived("deck-key", "send-later"),
        "stop": lambda: state.withdraw_person_words("deck-key"),
        "old": lambda: _aged(state, minutes=16),
        "overview": lambda: _hear(state, "no, wait"),
    }[happens]
    if happens == "overview":
        service = app.state.work_overview
        current = service.current

        async def slow(*, wait=0.0):
            act()
            return await current(wait=wait)

        service.current = slow
    else:
        _during_read(monkeypatch, act)
    answer = _say(_client(app))
    assert answer.status_code == 403 and "nothing was sent" in answer.text
    assert ("took the request back" in answer.text) is (happens != "overview")
    assert presses == [] and app.state.control_db.load_work_actions(WORK.work_id) == ()


def test_a_call_refused_at_the_reservation_tells_of_the_other_calls_press(tmp_path, monkeypatch):
    """Design note 4: call A pressed while B read the Work, then the owner spoke again."""
    app, presses = _spoken_app(tmp_path, monkeypatch, _ready)
    state, client, db = app.state.app_state, _client(app), app.state.control_db
    chat = state.chat_identity_key("deck-key")

    def other_message_pressed():
        db.reserve_work_action("a" * 40, WORK.work_id, "merge", spoken_by=(chat, "send-a"))
        _hear(state, "no, wait")

    _during_read(monkeypatch, other_message_pressed)
    monkeypatch.setattr("agentdeck.work_actions.recent_press", lambda *a, **k: None)
    answer = _say(client).json()
    assert answer["state"] == "earlier" and "may have been sent" in answer["detail"]
    assert presses == []


def test_a_repeated_call_after_the_words_are_gone_finds_the_press(tmp_path, monkeypatch):
    """Design notes 2 and 3: after a withdrawal or a restart, a repeat tells of the earlier
    press and that it pressed nothing, and never says a withdrawn Merge when ready stands."""
    from agentdeck.web import routes_works

    app, presses = _spoken_app(tmp_path, monkeypatch, _idle)
    real, approval = routes_works.load_work_detail, "offered"

    async def detail(app_, ref, **kw):
        return replace(await real(app_, ref, **kw), approval=approval)

    monkeypatch.setattr(routes_works, "load_work_detail", detail)
    state, client = app.state.app_state, _client(app)
    assert _say(client).json()["state"] == "sent"
    approval = "approved"
    state.person_words.clear()
    repeat = _say(client).json()
    assert repeat["state"] == "earlier" and repeat["button"] == "Merge when ready"
    assert "pressed nothing" in repeat["detail"] and "It still stands." in repeat["detail"]
    assert repeat["pressed_at"]
    # The owner pressed Withdraw, then said something that is no plain request.
    approval = "offered"
    _hear(state, "do merge it now")
    assert "does not stand now" in _say(client).json()["detail"]
    assert len(presses) == 1
    # Old enough to be another matter.
    monkeypatch.setattr(routes_works, "SPOKEN_MERGE_S", 0)
    assert _say(client).status_code == 403


def test_a_message_arrives_grants_and_is_resent_once(tmp_path, monkeypatch):
    """Design notes 1 and 2: one order per chat for messages and withdrawals; a resend, also
    after a restart, gives back nothing that was taken back."""
    from agentdeck.state import AppState

    app, presses = _spoken_app(tmp_path, monkeypatch, _ready)
    state, client = app.state.app_state, _client(app)
    chat = state.chat_identity_key("deck-key")

    def words():
        said = state.person_words.get(chat)
        return said.words if said is not None else None

    def arrive(send_id):
        state.person_message_arrived("deck-key", send_id)

    def accept(send_id, text="merge agentdeck 5"):
        state.person_message_accepted("deck-key", send_id, text)

    _hear(state, "merge agentdeck 5", "send-one")
    assert words() == "merge agentdeck 5"
    # A resend takes back like any message, and grants nothing.
    arrive("send-one")
    assert words() is None
    accept("send-one")
    assert words() is None
    # A resend after a newer message.
    _hear(state, "no, wait")
    arrive("send-one"), accept("send-one")
    assert words() is None
    # Stop between arrival and acceptance, and then a resend.
    arrive("send-two")
    state.withdraw_person_words("deck-key")
    accept("send-two")
    arrive("send-two"), accept("send-two")
    assert words() is None and _say(client).status_code == 403
    # A resend between arrival and acceptance.
    arrive("send-three"), arrive("send-three"), accept("send-three")
    assert words() is None
    # An older message accepted late does not stand over a newer one.
    arrive("send-four"), arrive("send-five"), accept("send-five", "no"), accept("send-four")
    assert words() == "no"
    # Edited text under an older message's id takes back what a newer one said.
    _hear(state, "what is new", "send-old")
    _hear(state, "merge agentdeck 5")
    assert words()
    _hear(state, "no, wait", "send-old")
    assert words() is None
    _hear(state, "no")
    # No id of the page's own: it only takes back.
    arrive(None), accept(None)
    assert words() is None
    # A send that could not be stored grants nothing, and neither does its resend.
    with monkeypatch.context() as broken:
        broken.setattr(state.db, "record_person_send", lambda *a: None)
        _hear(state, "merge agentdeck 5", "send-six")
        assert words() is None
    # The same id again, the write working, over a newer message: nothing stands.
    _hear(state, "no, wait")
    _hear(state, "merge agentdeck 5", "send-six")
    assert words() is None
    # After a restart.
    _hear(state, "merge agentdeck 5", "send-seven")
    again = AppState(db=state.db)
    again.person_message_arrived("deck-key", "send-seven")
    again.person_message_accepted("deck-key", "send-seven", "merge agentdeck 5")
    assert again.person_words == {}
    assert presses == []


def test_a_spoken_merge_of_a_number_two_projects_have_names_the_project(tmp_path, monkeypatch):
    app, presses = _spoken_app(tmp_path, monkeypatch, _ready, cards=2)
    state, client = app.state.app_state, _client(app)
    for words in ("merge 5", "merge games 5"):
        _hear(state, words)
        assert _say(client).status_code == 403, words
    assert presses == []
    _hear(state, "merge agent deck 5")
    assert _say(client).json()["state"] == "sent", "heard as two words"


@pytest.mark.parametrize(
    ("message", "asked"),
    [
        ("Merge 390.", True),
        ("Okay, merge the big-shop 390 now please", True),
        ("Paina merge-nappia big shopin 390", True),
        ("mergeä työ #390:n", True),
        ("Merge eerovil/big-shop#390", True),
        (f"Merge 390\n\n{VOICE_LINE}", True),
        ("Merge 390?", False),
        ("Can you merge 390", False),
        ("Don't merge 390", False),
        ("älä mergeä 390", False),
        ("merge 390 after 391", False),
        ("merge 390 when its tests pass", False),
        ("was 390 merged", False),
        ("merge it", False),
        ("390", False),
        ("merge 3900", False),
        ("merge big-shopper 390", False),
        ('It says "merge 390"', False),
        ("> merge 390", False),
        ("<pasted_content>merge 390</pasted_content>", False),
        ("What is this:\nmerge 390", False),
    ],
)
def test_only_a_plain_request_is_the_owners_word_to_merge(message, asked):
    assert spoken_merge.asks(message, "eerovil/big-shop", 390) is asked


def test_a_project_with_a_namesake_is_named_with_its_owner():
    def asks(message):
        return spoken_merge.asks(message, "eerovil/shop", 390, others=["acme/shop"])

    assert not asks("merge shop 390") and not asks("merge acme shop 390")
    assert asks("merge eerovil shop 390")


def test_ship_now_is_offered_at_findings_and_ships_through_the_router(tmp_path, monkeypatch):
    """#4212: Ship now, fix later shows inline beside the headline only on a Work the deck
    releases whose open pull request stands at review findings, and the press is the router's
    ``ship_now`` for each of them, answered with the findings left to this Work."""
    from agentdeck.web import routes_works
    from agentdeck.web.work_view import WorkDetail, render_work_sections
    from agentdeck.work_router import AgainRefused

    app, _gh, _holder = _route_app(tmp_path, monkeypatch, _idle)
    card = _idle()

    def status(**extra):
        detail = WorkDetail(card=card, workspace=None, chats=(), verifications=(), **extra)
        return dict(render_work_sections(app.state.templates, detail))["status"]

    offered = status(shippable=("eerovil/agentdeck#7",), deck_release=True)
    assert "data-work-ship-now" in offered and 'value="ship_now"' in offered
    assert 'hx-confirm="Ship this Work as it is?' in offered
    assert "data-work-ship-now" not in status(shippable=("eerovil/agentdeck#7",))
    assert "data-work-ship-now" not in status(deck_release=True)
    # D10: the confirm quotes the findings' first line, and posts the head it showed them
    # at; D12: a shipped pull request says so on its Work's page, and the status redraws live
    # when that record appears.
    quoted = status(
        shippable=("eerovil/agentdeck#7",),
        deck_release=True,
        ship_findings=(("eerovil/agentdeck#7", "1. <a>"),),
    )
    assert "The findings start: eerovil/agentdeck#7: 1. &lt;a&gt;" in quoted
    assert f'name="confirmed" value="eerovil/agentdeck#7@{HEAD}"' in quoted
    # Every pull request it would ship is named with its own findings (#4212 review).
    from agentdeck.work_actions import ship_now_control

    two = SimpleNamespace(
        paused=False,
        outputs=(
            SimpleNamespace(repository="a/b", number=1, head="1" * 40),
            SimpleNamespace(repository="a/b", number=2, head="2" * 40),
        ),
    )
    both = ship_now_control(two, True, ("a/b#1", "a/b#2"), (("a/b#1", "1. leaks a token"),))
    assert "It ships 2 pull requests with their findings:\n- a/b#1: 1. leaks a token\n" in (
        both.confirm
    )
    assert "- a/b#2: its findings kept no words" in both.confirm
    assert both.confirmed == f"a/b#1@{'1' * 40} a/b#2@{'2' * 40}"
    landed = status(shipped=("eerovil/agentdeck#7",))
    assert 'data-work-shipped="eerovil/agentdeck#7"' in landed
    assert "with its review findings, fixed next in this Work" in landed
    assert "data-work-shipped" not in offered
    from agentdeck.web.work_view import SECTION_INPUTS, _shipped_of

    before = WorkDetail(card=card, workspace=None, chats=(), verifications=())
    after = WorkDetail(
        card=card, workspace=None, chats=(), verifications=(), shipped=("a/b#9",)
    )
    assert SECTION_INPUTS["status"](before, "") != SECTION_INPUTS["status"](after, "")
    ref = SimpleNamespace(repository="a/b", number=7, work_id="github_issue:a/b#7")
    reader = SimpleNamespace(
        shipped_by_work=lambda work: (
            [
                SimpleNamespace(pull="a/b#9", work="a/b#7", fix=""),
                # #4797: the owner left this one's findings as shipped; no fix comes.
                SimpleNamespace(pull="a/b#8", work="a/b#7", fix="declined"),
            ]
            if work == "a/b#7"
            else []
        )
    )
    assert _shipped_of(reader, ref) == ("a/b#9",)
    # #4585: one shipped in an earlier round of the Work is not this round's line.
    earlier = SimpleNamespace(earlier_outputs=(SimpleNamespace(repository="a/b", number=9),))
    assert _shipped_of(reader, ref, earlier) == ()
    assert _shipped_of(SimpleNamespace(), ref) == ()
    # A press refused after it passed the head offers Ship now again, quoting the findings
    # at the head it passed (a base update carried the pass to the live one), until marked.
    from agentdeck.review_history import ACCEPTED, FINDINGS, PASS, SHIP_NOTE, ReviewEvent
    from agentdeck.web.work_view import _ship_findings, _shippable

    old, mid, new = "1" * 40, "3" * 40, "2" * 40
    events = [
        ReviewEvent("a/b#9", old, 1.0, FINDINGS, "f", {"text": "1. off"}, 0),
        ReviewEvent("a/b#9", old, 2.0, ACCEPTED, "a", {"note": SHIP_NOTE}, 1),
        ReviewEvent("a/b#9", mid, 3.0, PASS, "c", {"note": SHIP_NOTE, "carried_from": old}, 2),
        ReviewEvent("a/b#9", new, 4.0, PASS, "d", {"note": SHIP_NOTE, "carried_from": mid}, 3),
    ]
    marked: set[str] = set()
    shown = SimpleNamespace(
        outputs=(SimpleNamespace(repository="a/b", number=9, head=new, is_open=True),)
    )
    snapshot = SimpleNamespace(
        review_events={"a/b#9": events},
        state=SimpleNamespace(ship_unfinished=lambda pull, head: head not in marked),
    )
    assert _shippable(snapshot, shown) == ("a/b#9",)
    assert _ship_findings(snapshot, shown, ("a/b#9",)) == (("a/b#9", "1. off"),)
    marked.add(old)
    assert _shippable(snapshot, shown) == ()
    events[1:] = [ReviewEvent("a/b#9", new, 3.0, ACCEPTED, "a", {"note": "keep it"}, 1)]
    marked.clear()
    assert _shippable(snapshot, shown) == ()  # an owner's own acceptance is no Ship now
    # A fix pushed on top of the findings, still in review or not yet asked about: the
    # reviewed commit is offered at its own head, to ship in a pull request of its own
    # (#4212 review), until this Work shipped it; never once the fix has a verdict.
    from agentdeck.review_history import ASKED

    events[:] = [
        ReviewEvent("a/b#9", old, 1.0, FINDINGS, "f", {"text": "1. off"}, 0),
        ReviewEvent("a/b#9", new, 2.0, ASKED, "n", {}, 1),
    ]
    records: list[SimpleNamespace] = []
    split = SimpleNamespace(
        work_id="github_issue:a/b#7",
        outputs=(SimpleNamespace(repository="a/b", number=9, head=new, is_open=True),),
    )
    snapshot.state.shipped_by_work = lambda work: records if work == "a/b#7" else []
    assert _shippable(snapshot, split) == (f"a/b#9@{old}",)
    assert _ship_findings(snapshot, split, (f"a/b#9@{old}",)) == (("a/b#9", "1. off"),)
    button = ship_now_control(
        SimpleNamespace(paused=False, outputs=split.outputs), True, (f"a/b#9@{old}",),
        (("a/b#9", "1. off"),),
    )
    assert button.confirmed == f"a/b#9@{old}"
    assert f"a/b#9 (at {old[:12]}, in a new pull request; the fix stays in a/b#9)" in (
        button.confirm
    )
    records.append(SimpleNamespace(pull="a/b#20", head=old, shipped=1.0))
    assert _shippable(snapshot, split) == ()
    records.clear()
    events.append(ReviewEvent("a/b#9", new, 3.0, PASS, "p", {}, 2))
    assert _shippable(snapshot, split) == ()

    shipped: list[tuple[str, int]] = []

    class Router:
        enabled = True

        async def ship_now(self, repository, number, head=""):
            shipped.append((repository, number, head))
            if len(shipped) == 1:
                raise AgainRefused("eerovil/agentdeck#9 moved past cccccccccccc")
            if len(shipped) == 3:
                return {"head": "a" * 40, "state": "running", "outcome": "still running"}
            return {"head": "a" * 40, "outcome": "review pass recorded"}

    class Release:
        def handles(self, work_id):
            return True

    app.state.work_router = Router()
    app.state.work_release = Release()
    real = routes_works.load_work_detail
    holder = {"shippable": ()}

    async def detail(app_, ref, **kw):
        return replace(
            await real(app_, ref, **kw), shippable=holder["shippable"], deck_release=True
        )

    monkeypatch.setattr(routes_works, "load_work_detail", detail)
    client = _client(app)
    nothing = _post(client, "ship_now", "press-0001")
    assert nothing.status_code == 409 and "no open pull request stands" in nothing.text
    assert shipped == []
    holder["shippable"] = ("eerovil/agentdeck#7",)
    # A press whose confirm showed none of it, or another head, ships nothing (#4212 review).
    unshown = _post(client, "ship_now", "press-0004")
    assert unshown.status_code == 409 and "changed since the page showed them" in unshown.text
    stale = _post(client, "ship_now", "press-0005", confirmed=f"eerovil/agentdeck#7@{'d' * 40}")
    assert stale.status_code == 409 and shipped == []
    shown = f"eerovil/agentdeck#7@{HEAD}"
    refused = _post(client, "ship_now", "press-0002", confirmed=shown)
    assert refused.status_code == 409 and "moved past" in refused.text
    done = _post(client, "ship_now", "press-0003", confirmed=shown)
    assert done.status_code == 200 and "this Work then fixes its findings" in done.text
    assert shipped == [("eerovil/agentdeck", 7, HEAD)] * 2
    # A press still running past the wait reads as under way, not shipped (#4558), and is a
    # real second press straight after the first: the recent-press guard lets Ship now
    # through, as the merge service joins a running press (#4559 review). The same press id
    # sent again still writes nothing.
    from agentdeck import work_actions

    slow = _post(client, "ship_now", "press-0006", confirmed=shown)
    assert slow.status_code == 200 and "is under way" in slow.text, slow.text
    assert "this Work then fixes its findings" not in slow.text
    calls = len(shipped)
    replay = _post(client, "ship_now", "press-0006", confirmed=shown)
    assert len(shipped) == calls, replay.text
    # With a second pull request left, it is named as not started and not pressed (#4559).
    import asyncio

    shipped.clear()
    shipped.append("refused already")
    shipped.append("shipped already")
    step = work_actions.Step(
        "deck_ship_now",
        "eerovil/agentdeck",
        3,
        pulls=(f"eerovil/agentdeck#7@{HEAD}", f"eerovil/agentdeck#8@{'d' * 40}"),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(work_router=Router())))
    outcome, receipt = asyncio.run(routes_works._write(request, step, "Ship now", "press-0007"))
    assert outcome == "done" and "Ship now of eerovil/agentdeck#7 is under way" in receipt
    assert "Not started yet: eerovil/agentdeck#8" in receipt
    assert shipped[2:] == [("eerovil/agentdeck", 7, HEAD)]
    # Shipped, then refused: the first refusal stops the rest, as the API does (#4559 review).

    class Stops:
        async def ship_now(self, repository, number, head=""):
            shipped.append(number)
            if number == 8:
                raise AgainRefused("its review has not come back with findings")
            return {"head": head, "outcome": "review pass recorded"}

    shipped.clear()
    three = replace(step, pulls=(*step.pulls, f"eerovil/agentdeck#9@{'e' * 40}"))
    request.app.state.work_router = Stops()
    outcome, receipt = asyncio.run(routes_works._write(request, three, "Ship now", "press-0008"))
    assert outcome == "done" and shipped == [7, 8], receipt
    assert "eerovil/agentdeck#8 not shipped" in receipt
    assert "Not started: eerovil/agentdeck#9" in receipt


def test_a_merge_the_work_no_longer_offers_writes_nothing(tmp_path, monkeypatch):
    app, gh, _ = _merge_app(tmp_path, monkeypatch)
    from agentdeck.web import routes_works

    async def moved(_app, ref, **_kw):
        from agentdeck.web.work_view import WorkDetail

        return WorkDetail(card=_idle(), workspace=None, chats=(), verifications=())

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
    detail = WorkDetail(card=_unstarted(), workspace=None, chats=(), verifications=(),
                        last_action=press)

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

    card = _idle()

    def status(**extra):
        detail = WorkDetail(card=card, workspace=None, chats=(), verifications=(), **extra)
        return dict(render_work_sections(app.state.templates, detail))["status"]

    offered = status(approval="offered")
    assert 'value="approve_merge"' in offered and 'hx-confirm="Approve merging' in offered
    assert "data-work-merge-approved" not in offered
    approved = status(approval="approved")
    assert "data-work-merge-approved" in approved and 'value="withdraw_merge"' in approved
    # #3869: what an armed press waits for stays in sight after another press, once.
    from agentdeck.web.work_view import render_changed_sections

    waits = {"approval": "approved", "approval_waits": "Merge armed: now x."}
    after_star = status(last_action=_press("star", ago_s=10), **waits)
    assert 'data-work-merge-armed>Merge armed: now x.</p>' in after_star
    after_merge = status(last_action=_press("merge", ago_s=10), **waits)
    assert after_merge.count("Merge armed: now x.") == 1, "the receipt says it already"
    # Only the wait moved: the live page renders the status again (#3869 review).
    detail = WorkDetail(card=card, workspace=None, chats=(), verifications=(), **waits)
    before = render_changed_sections(app.state.templates, detail, previous={})
    moved = replace(detail, approval_waits="Merge armed: now y.")
    after = render_changed_sections(app.state.templates, moved, previous=before)
    assert "now y." in after["status"][1]
    # #3880: a review waiting on the agent's pre-review checklist says so, and moves live.
    held = "Pre-review: waiting for the agent to go through its checklist on o/r#9."
    assert f"data-work-pre-review>{held}</p>" in status(pre_review_waits=held)
    released = render_changed_sections(
        app.state.templates, replace(detail, pre_review_waits=held), previous=before
    )
    assert held in released["status"][1]
    # #4172: a Work held by a pull request outside the repository that merges on its own.
    why = "o/robot merges on its own, but this Work also has a pull request in o/store"
    assert f"data-work-merge-reason>{why}</p>" in status(approval="offered", approval_reason=why)
    assert "data-work-merge-reason" not in status(approval_reason=why)
    from types import SimpleNamespace

    from agentdeck.web.work_view import _shown_pulls

    rows = (OutputFact("u", "O/Robot", 2, "registered", state="open", draft=True),)
    assert _shown_pulls(SimpleNamespace(outputs=rows)) == [("o/robot#2", "open", True)]
    held_live = render_changed_sections(
        app.state.templates, replace(detail, approval="offered", approval_reason=why),
        previous=before,
    )
    assert why in held_live["status"][1], "the reason moves live with the release's read"
    cleared = status(approval="offered", approval_cleared="Merge approval cleared: x changed.")
    assert "data-work-merge-cleared" in cleared and "x changed." in cleared
    assert "approve_merge" not in status()


# --- in the browser ------------------------------------------------------------------------------


def _retry_app(tmp_path, monkeypatch):
    """A real Needs retry Work: its worker chat stopped, and nothing restarts it by itself."""
    from test_work_ui import WORK as UI_WORK
    from test_work_ui import _add, _app, _attention, _insight

    from agentdeck.web import routes_works

    app = _app(tmp_path)
    worker = _add(app, "claude_code:test:worker", UI_WORK, title="Worker chat")
    _attention(app, _insight(worker, "stalled"))
    monkeypatch.setattr(routes_works.shutil, "which", lambda name: "/usr/bin/gh")
    gh = _Gh()
    monkeypatch.setattr(gh_cli, "run_once", gh)
    return app, gh, worker


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

    app, gh, _worker = _retry_app(tmp_path, monkeypatch)
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

        # A live update replaces the row whole. The chat is renamed, a real change to the
        # card: the first pass already shows the chat, so adding it again changes nothing.
        await row.evaluate("el => { el.__before = true; }")
        _add(app, "claude_code:test:worker", UI_WORK, title="Worker chat, renamed")
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

    app, gh, worker = _retry_app(tmp_path, monkeypatch)
    manual = _add(app, "claude_code:test:manual")
    return app, UI_WORK, worker, manual, gh


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


def test_a_merge_press_reads_as_done_once_the_work_merged():
    """issues#485: a press's receipt reads in the past tense once it was acted on."""
    from agentdeck.web.work_view import WorkDetail

    merge = _press("merge", ago_s=3000)
    open_ = WorkDetail(_idle(), None, (), (), last_action=merge)
    merged = WorkDetail(_idle(rows={PR.lower(): _row("merged")}), None, (), (), last_action=merge)

    assert open_.last_action_followed == ""
    assert merged.last_action_followed == "Merge asked; merged"
    # #3869: while the press stands, its receipt says what it waits for now, not what held it
    # when pressed; the merge still reads as done.
    line = "Merge armed: it merges with no second press once nothing holds it. Now: x."
    armed = WorkDetail(
        _idle(), None, (), (), last_action=merge, approval="approved", approval_waits=line
    )
    assert armed.last_action_followed == line
    assert replace(armed, card=merged.card).last_action_followed == "Merge asked; merged"
    when_ready = replace(armed, last_action=_press("approve_merge", ago_s=60))
    assert when_ready.last_action_followed == line
    assert replace(armed, approval="offered").last_action_followed == ""
