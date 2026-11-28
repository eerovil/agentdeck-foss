"""A chat registers a GitHub event and is woken once when it lands (#2775).

Fixtures go through `WebhookLog`, the same appender the live ingress route uses, so the
matching is asserted over real stream bytes. Scheduling is stubbed: what is under test is
which message each registration produces and how often, not the injection path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from public_copy import private_feature

from agentdeck import chat_board as _chat_board
from agentdeck import chat_review as _chat_review
from agentdeck import wake_on
from agentdeck.app import create_app
from agentdeck.config import (
    AccountConfig,
    AppConfig,
    GitHubWebhookConfig,
    HistoryConfig,
    RestartNudgeConfig,
)
from agentdeck.deck_notices import ROUTINE_LINE
from agentdeck.github_webhook_log import Repository, WebhookLog
from agentdeck.models import PullRequestRef
from agentdeck.providers.instructions import agentdeck_session_instructions, guide_topics
from agentdeck.wake_on import (
    LIVE_READ_AGAIN_S,
    LIVE_READ_CAP_S,
    MERGE_HOLD_S,
    MERGE_HOLD_SUFFIX,
    MERGE_TOLD_S,
    RECOVER_RETRY_S,
    RECOVER_UNCERTAIN_NOTE,
    REVIEW_TIMEOUT_MINUTES,
    STOP_SETTLE_S,
    NeedsYouStop,
    NothingToReport,
    WakeOnRegistry,
    WakeOnService,
    WakeRequest,
    WakeRequestError,
    merge_notice_told,
    parse_request,
    read_live,
    register_deck_wait,
    registry_path,
    seen_result,
    settled_head,
    status_is_ci,
    timeout_message,
)
from agentdeck.webhook_tailer import Delivery, WebhookTailer
from agentdeck.work_router import AgainBusy, AgainRefused

REPO = Repository(owner="eerovil", name="agentdeck")
OTHER = Repository(owner="eerovil", name="trailhead")
SHA = "abcdef1234567890abcdef1234567890abcdef12"


def deliver(root, *, event: str, delivery: str, repository: Repository = REPO, **fields):
    payload = {
        "repository": {
            "full_name": repository.slug,
            "name": repository.name,
            "owner": {"login": repository.owner},
        },
        **fields,
    }
    WebhookLog(root).append(
        body=json.dumps(payload).encode(),
        headers={"X-GitHub-Delivery": delivery, "X-GitHub-Event": event},
    )


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).timestamp() - 60

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def root(tmp_path):
    return tmp_path / "github-webhooks"


@pytest.fixture
def scheduled():
    return []


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def here():
    """Whether the owner is in a needs-you wait's chat (#3464); flip ``here[0]``."""
    return [True]


@pytest.fixture
def service(root, scheduled, clock, here):
    def schedule(message):
        scheduled.append(message)
        return message

    return WakeOnService(
        root,
        schedule=schedule,
        message_exists=lambda message_id: any(m.id == message_id for m in scheduled),
        clock=clock,
        present=lambda request: here[0],
    )


def register(service, session_key="claude_code:alt:one", **body):
    fields = parse_request(**body)
    return service.register(session_key, session_key, **fields)


def drain(service) -> None:
    tailer = WebhookTailer(service._tailer.root, consumer="wake-on")
    service._tailer = tailer
    service.apply(list(tailer.deliveries()))
    tailer.commit()


def test_a_merge_wakes_the_chat_once_with_what_happened(root, service, scheduled):
    request = register(service, pull_request="eerovil/agentdeck#12")
    deliver(
        root,
        event="pull_request",
        delivery="d1",
        action="opened",
        number=12,
        pull_request={"number": 12},
    )
    deliver(
        root,
        event="pull_request",
        delivery="d2",
        action="closed",
        number=11,
        pull_request={"number": 11, "merged": True},
    )
    deliver(
        root,
        event="pull_request",
        delivery="d3",
        action="closed",
        number=12,
        pull_request={"number": 12, "merged": True, "merge_commit_sha": SHA},
    )
    deliver(
        root,
        event="pull_request",
        delivery="d4",
        action="closed",
        number=12,
        pull_request={"number": 12, "merged": True, "merge_commit_sha": SHA},
    )
    drain(service)

    assert [message.id for message in scheduled] == [f"wake-on:{request.id}"]
    assert "pull request eerovil/agentdeck#12 was merged as abcdef123456" in scheduled[0].text
    # A merge is routine (#4253); a pull request closed unmerged is not.
    assert scheduled[0].text.startswith(ROUTINE_LINE + "\n\nAgentDeck wake-on: ")
    assert scheduled[0].chat_id == "claude_code:alt:one"
    assert service.registry.all() == ()


def test_a_review_wait_ends_quietly_on_the_verdict_and_wakes_only_on_its_timeout(
    root, service, scheduled, clock
):
    """#3183: the deck hands the verdict to the chat itself, so that message is the wake."""
    request = register(service, review="eerovil/agentdeck#12", timeout_minutes=30)
    assert request.target == "the review of pull request eerovil/agentdeck#12"
    kept = register(service, pull_request="eerovil/agentdeck#12")
    other = register(
        service, "claude_code:alt:two", review="eerovil/agentdeck#12", timeout_minutes=30
    )
    deliver(
        root,
        event="pull_request",
        delivery="d1",
        action="closed",
        number=12,
        pull_request={"number": 12, "merged": False},
    )
    drain(service)
    assert [message.id for message in scheduled] == [f"wake-on:{kept.id}"]
    second = register(service, review="eerovil/agentdeck#13", timeout_minutes=30)
    # A verdict on one pull request leaves the wait on another (#3183 review); none is the Work.
    assert service.registry.answer_reviews("claude_code:alt:one", ["Eerovil/AgentDeck#12"]) == 1
    assert service.registry.answer_reviews("claude_code:alt:one", ["eerovil/agentdeck#99"]) == 0
    # #4089: a verdict naming no pull request cannot say whose it is, so of several waits it
    # ends none; metso#77's wait went with store#3849's findings.
    third = register(service, review="eerovil/agentdeck#14", timeout_minutes=30)
    assert service.registry.answer_reviews("claude_code:alt:one", None) == 0
    assert service.registry.answer_reviews("claude_code:alt:one", ["eerovil/agentdeck#14"]) == 1
    assert third.id not in {item.id for item in service.registry.all()}
    assert service.registry.answer_reviews("claude_code:alt:one", None) == 1
    assert second.id not in {item.id for item in service.registry.all()}
    assert scheduled[1:] == []
    # #3389: a review wait defaults to 20 minutes, the others keep 120.
    default = register(service, "claude_code:alt:three", review="eerovil/agentdeck#12")
    assert default.deadline - default.registered_at == 20 * 60
    assert parse_request(pull_request="eerovil/agentdeck#12")["timeout_s"] == 120 * 60
    clock.now += 31 * 60
    service.expire_due()
    assert [message.id for message in scheduled[1:]] == [
        f"wake-on:{other.id}",
        f"wake-on:{default.id}",
    ]
    text = scheduled[2].text
    assert text.startswith(
        "AgentDeck wake-on: no verdict reached this chat for the review of pull request"
        " eerovil/agentdeck#12 within 20 minutes. Check once that the reviewer is making progress"
    )
    for step in (
        "`gh pr view 12 --repo eerovil/agentdeck --json headRefOid,isDraft`",
        "Is a review chat open for that head?",
        '/review-request` with `{"pull_request": "eerovil/agentdeck#12", "no_verdict": true,'
        ' "note": "no review chat started for this head"}`',
        "do not ask the person first",
        "register this review wait again and end the turn",
        "this is your second timeout on the same head with no review queued",
        '`"outcome": "already asked"` means that review is queued',
        "tell the person in the question box",
    ):
        assert step in text
    assert "within 30 minutes" in scheduled[1].text
    assert "the review chat the Work page links" in text
    assert "`Review <owner/repo>#<your issue number>` in `eerovil/review-chats`" in (
        timeout_message(default, "eerovil/review-chats")
    )
    # #4064: when no reviewer can start, the timeout says so instead of sending the worker
    # to ask for another chat nobody reads; a health read that fails changes nothing.
    down = register(service, "claude_code:alt:four", review="eerovil/agentdeck#12")
    broken = register(
        service, "claude_code:alt:five", review="eerovil/agentdeck#12", timeout_minutes=30
    )
    service._reviewer_down = lambda: "ChatGPT has failed to start 3 times since 16:53 UTC"
    clock.now += 21 * 60
    service.expire_due()
    service._reviewer_down = lambda: 1 / 0
    clock.now += 10 * 60
    service.expire_due()
    texts = {message.id: message.text for message in scheduled}
    assert (
        "the reviewer is not running: ChatGPT has failed to start 3 times since 16:53 UTC."
        in (texts[f"wake-on:{down.id}"])
    )
    assert "Do not ask for a fresh review" in texts[f"wake-on:{down.id}"]
    assert "Check once that the reviewer is making progress" in texts[f"wake-on:{broken.id}"]


@private_feature(_chat_board)
def test_a_review_chat_that_stops_wakes_its_review_waits_unless_a_verdict_lands(
    root, scheduled, clock
):
    """#3316: a reviewer that could not post its verdict, or a repeat review that posted none,
    left the Work's review wait running for hours on nothing. #3611: the same for a chat that
    waited only on the merge; a verdict in time gives that wait its own deadline back."""
    chats = Repository(owner="eerovil", name="review-chats")
    service = WakeOnService(
        root,
        schedule=lambda message: scheduled.append(message) or message,
        message_exists=lambda message_id: any(m.id == message_id for m in scheduled),
        clock=clock,
        review_chats="eerovil/review-chats",
    )
    blocked = register(service, review="acmecorp/bazaar#351", timeout_minutes=120)
    repeat = register(service, "claude_code:alt:two", review="eerovil/agentdeck#12")
    untouched = register(service, "claude_code:alt:three", review="eerovil/agentdeck#13")
    # The chat's other pull request, which its closing line does not name, is still under review.
    still = register(service, "claude_code:alt:four", review="eerovil/agentdeck#14")
    # #3453: this chat asked for a fresh review, still pending behind an older chat.
    asked = service.register(
        "claude_code:alt:five",
        "claude_code:alt:five",
        asked=True,
        **parse_request(review="eerovil/agentdeck#15"),
    )
    merge = register(
        service, "claude_code:alt:six", pull_request="acmecorp/bazaar#351", timeout_minutes=120
    )
    told = register(
        service, "claude_code:alt:two", pull_request="eerovil/agentdeck#12", timeout_minutes=120
    )
    other = register(service, "claude_code:alt:three", pull_request="eerovil/agentdeck#13")
    # A clean pass usually reaches the chat before the reviewer's closing line: that line is
    # no news then, and a review chat opened after the pass still cuts.
    passed = register(
        service, "claude_code:alt:seven", pull_request="eerovil/agentdeck#17", timeout_minutes=120
    )
    later = register(
        service, "claude_code:alt:eight", pull_request="eerovil/agentdeck#18", timeout_minutes=120
    )
    service.registry.answer_reviews("claude_code:alt:seven", ["eerovil/agentdeck#17"])
    service.registry.answer_reviews("claude_code:alt:eight", ["eerovil/agentdeck#18"])
    # The pass came first and the merge wait after it: the closing line is no news either.
    service.registry.answer_reviews("claude_code:alt:nine", ["eerovil/agentdeck#19"])
    after = register(
        service, "claude_code:alt:nine", pull_request="eerovil/agentdeck#19", timeout_minutes=120
    )

    def chat(number, target, pull, head=SHA):
        return {
            "number": number,
            "title": f"Review {target}",
            "state": "open",
            "html_url": f"https://github.com/eerovil/review-chats/issues/{number}",
            "body": f"<!-- agentdeck: review-head {head} {pull} -->",
        }

    bazaar = chat(6231, "acmecorp/bazaar#344", "acmecorp/bazaar#351")
    why = "Review found no actionable findings, but the release verdict was not posted: 404."
    deliver(
        root,
        event="issue_comment",
        delivery="c1",
        repository=chats,
        action="created",
        issue=bazaar,
        comment={"body": why},
    )
    deliver(
        root,
        event="issues",
        delivery="l1",
        repository=chats,
        action="labeled",
        issue=bazaar,
        label={"name": "blocked"},
    )
    closing = f"Reviewed agentdeck#12 at `{SHA[:12]}`: 1 outstanding finding (sent)."
    two = chat(6226, "eerovil/agentdeck#11", "eerovil/agentdeck#12")
    two["body"] += f"\n<!-- agentdeck: review-head {SHA} eerovil/agentdeck#14 -->"
    deliver(
        root,
        event="issue_comment",
        delivery="c2",
        repository=chats,
        action="created",
        issue=two,
        comment={"body": closing},
    )
    # The older chat closing on its own summary does not end the wait on the review asked for.
    opened = datetime.fromtimestamp(clock.now - 600, UTC).isoformat()
    old = {**chat(6230, "eerovil/agentdeck#16", "eerovil/agentdeck#15"), "created_at": opened}
    deliver(
        root,
        event="issue_comment",
        delivery="c4",
        repository=chats,
        action="created",
        issue=old,
        comment={"body": f"Reviewed agentdeck#15 at `{SHA[:12]}`: pass."},
    )
    for number, pull, opened_at in (
        (6240, "eerovil/agentdeck#17", clock.now - 600),
        (6241, "eerovil/agentdeck#18", clock.now + 0.5),
        (6242, "eerovil/agentdeck#19", clock.now - 600),
    ):
        created = datetime.fromtimestamp(opened_at, UTC).isoformat()
        deliver(
            root,
            event="issue_comment",
            delivery=f"c{number}",
            repository=chats,
            action="created",
            issue={**chat(number, pull, pull), "created_at": created},
            comment={"body": f"Reviewed {pull.partition('/')[2]} at `{SHA[:12]}`: pass."},
        )
    # Not a review chat, and not a closing line: neither cuts anything short.
    deliver(
        root,
        event="issues",
        delivery="l2",
        repository=chats,
        action="labeled",
        issue={**chat(7, "x", "eerovil/agentdeck#13"), "title": "Something else"},
        label={"name": "blocked"},
    )
    deliver(
        root,
        event="issue_comment",
        delivery="c3",
        repository=chats,
        action="created",
        issue=chat(8, "eerovil/agentdeck#11", "eerovil/agentdeck#13"),
        comment={"body": "Reviewed the protocol; reading the diff next."},
    )
    clock.now += 1
    drain(service)
    assert scheduled == []
    deadlines = {item.id: item.deadline for item in service.registry.all()}
    assert deadlines[untouched.id] == untouched.deadline
    assert deadlines[still.id] == still.deadline
    assert deadlines[asked.id] == asked.deadline
    assert deadlines[blocked.id] < blocked.deadline
    assert deadlines[merge.id] < merge.deadline and deadlines[told.id] < told.deadline
    assert deadlines[other.id] == other.deadline
    assert deadlines[passed.id] == passed.deadline and deadlines[later.id] < later.deadline
    assert deadlines[after.id] == after.deadline
    assert {item.id: item.stopped for item in service.registry.all()}[repeat.id].startswith(
        "the review chat https://github.com/eerovil/review-chats/issues/6226 closed its review"
    )
    # Registered again after the stop, it is a fresh wait.
    again = register(service, "claude_code:alt:two", review="eerovil/agentdeck#12")
    assert again.id == repeat.id and again.stopped == ""
    # The verdict reached one chat in time: its wait ends quietly as before.
    assert service.registry.answer_reviews("claude_code:alt:two", ["eerovil/agentdeck#12"]) == 1
    # A stop settles from when the delivery was received, which is wall time, not the fake
    # clock: step past it from the real now, or a slow run leaves it not yet due (#3649).
    clock.now = datetime.now(UTC).timestamp() + STOP_SETTLE_S
    service.expire_due()
    assert sorted(message.id for message in scheduled) == sorted(
        [f"wake-on:{blocked.id}", f"wake-on:{merge.id}", f"wake-on:{later.id}"]
    )
    by_id = {message.id: message.text for message in scheduled}
    merged = by_id[f"wake-on:{merge.id}"]
    assert "No verdict reached this chat for pull request acmecorp/bazaar#351" in merged
    assert why in merged and "register the merge wait again" in merged
    text = by_id[f"wake-on:{blocked.id}"]
    assert "register the merge wait again" not in text
    assert "review-chats/issues/6231 was labelled `blocked`" in text and why in text
    assert "No verdict reached this chat for the review of pull request acmecorp/bazaar#351" in text
    assert '{"pull_request": "acmecorp/bazaar#351", "no_verdict": true,' in text
    assert "without asking the person" in text
    held = {item.id: item for item in service.registry.all()}
    assert set(held) == {untouched.id, still.id, asked.id, told.id, other.id, passed.id, after.id}
    assert (held[told.id].deadline, held[told.id].stopped) == (told.deadline, "")


def test_a_pull_request_closed_unmerged_says_so(root, service, scheduled):
    register(service, pull_request="https://github.com/eerovil/agentdeck/pull/12")
    deliver(
        root,
        event="pull_request",
        delivery="d1",
        action="closed",
        number=12,
        pull_request={"number": 12, "merged": False},
    )
    drain(service)
    assert "was closed without merging" in scheduled[0].text
    assert scheduled[0].text.startswith("AgentDeck wake-on: ")


def test_a_commit_wait_takes_the_first_finished_result_on_that_commit(root, service, scheduled):
    register(service, commit=f"EeroVil/AgentDeck@{SHA[:7].upper()}")
    # In progress, another commit, another repository: none of them is the result.
    deliver(root, event="status", delivery="d1", state="pending", sha=SHA, context="drone")
    deliver(
        root,
        event="check_suite",
        delivery="d2",
        action="completed",
        check_suite={"head_sha": "f" * 40, "conclusion": "success", "app": {"name": "x"}},
    )
    deliver(
        root,
        event="status",
        delivery="d3",
        repository=OTHER,
        state="success",
        sha=SHA,
        context="drone",
    )
    # A job its ``if:`` skipped says nothing about the commit (#3910).
    for delivery, conclusion in (("d3s", "skipped"), ("d3n", "NEUTRAL")):
        deliver(
            root,
            event="check_run",
            delivery=delivery,
            action="completed",
            check_run={"head_sha": SHA, "name": "deploy", "conclusion": conclusion},
        )
    deliver(
        root,
        event="status",
        delivery="d4",
        state="failure",
        sha=SHA,
        context="continuous-integration/drone/push",
        target_url="https://ci/1",
    )
    drain(service)

    assert len(scheduled) == 1
    assert "continuous-integration/drone/push" in scheduled[0].text
    assert "finished: failure (https://ci/1)" in scheduled[0].text
    assert scheduled[0].text.startswith("AgentDeck wake-on: ")  # a failure is never routine


def test_a_commit_wait_on_a_replaced_head_is_dropped_without_a_wake(root, scheduled, clock, caplog):
    """#3271: the head moved or the pull request merged, so nothing will report on it."""
    caplog.set_level(logging.INFO, logger="agentdeck.wake_on")
    old, new, other = "1" * 40, "2" * 40, "3" * 40
    registered = {"claude_code:alt:one": (PullRequestRef("EeroVil/AgentDeck", 76),)}
    service = WakeOnService(
        root,
        schedule=lambda message: scheduled.append(message) or message,
        message_exists=lambda message_id: False,
        clock=clock,
        pull_requests=lambda key: registered.get(key, ()),
    )
    moved = register(service, commit=f"eerovil/agentdeck@{old[:8]}")
    register(service, commit=f"eerovil/agentdeck@{new}", name="test")
    unrelated = register(service, commit=f"eerovil/agentdeck@{other}")
    pull = register(service, pull_request="eerovil/agentdeck#76")
    # Another chat that never registered #76 keeps its wait on the old head.
    bystander = register(service, "claude_code:alt:two", commit=f"eerovil/agentdeck@{old}")
    deliver(
        root,
        event="pull_request",
        delivery="d1",
        action="synchronize",
        number=76,
        before=old,
        after=new,
        pull_request={"number": 76, "head": {"sha": new}},
    )
    drain(service)
    assert moved.id not in {item.id for item in service.registry.all()}
    assert "pull request eerovil/agentdeck#76: its head moved" in caplog.text
    deliver(
        root,
        event="pull_request",
        delivery="d2",
        action="closed",
        number=76,
        pull_request={"number": 76, "merged": True, "head": {"sha": new}},
    )
    drain(service)
    assert [message.id for message in scheduled] == [f"wake-on:{pull.id}"]
    assert {item.id for item in service.registry.all()} == {unrelated.id, bystander.id}
    assert f"@{new} named like 'test': pull request eerovil/agentdeck#76: it merged" in caplog.text


def test_the_decks_review_check_is_no_ci_result_for_a_commit_wait(root, service, scheduled):
    """A wait on CI must not end on the deck's own review check (#4152)."""
    register(service, commit=f"eerovil/agentdeck@{SHA}")
    deliver(
        root,
        event="check_run",
        delivery="d1",
        action="completed",
        check_run={"head_sha": SHA, "name": "AgentDeck review", "conclusion": "failure"},
    )
    deliver(
        root,
        event="check_suite",
        delivery="d2",
        action="completed",
        check_suite={
            "head_sha": SHA,
            "conclusion": "failure",
            "app": {"slug": "agentdeck-eerovil"},
        },
    )
    drain(service)
    assert scheduled == []
    deliver(
        root,
        event="check_run",
        delivery="d3",
        action="completed",
        check_run={"head_sha": SHA, "name": "tests", "conclusion": "success"},
    )
    drain(service)
    assert len(scheduled) == 1


def test_a_commit_wait_wakes_on_a_completed_check_run(root, service, scheduled):
    register(service, commit=f"eerovil/agentdeck@{SHA}", name="lint")
    deliver(
        root,
        event="check_run",
        delivery="d1",
        action="created",
        check_run={"head_sha": SHA, "name": "lint", "status": "in_progress"},
    )
    deliver(
        root,
        event="check_run",
        delivery="d2",
        action="completed",
        check_run={"head_sha": SHA, "name": "tests", "conclusion": "success"},
    )
    drain(service)
    assert scheduled == []
    deliver(
        root,
        event="check_run",
        delivery="d3",
        action="completed",
        check_run={
            "head_sha": SHA,
            "name": "lint",
            "conclusion": "failure",
            "html_url": "https://github.com/eerovil/agentdeck/runs/9",
        },
    )
    drain(service)
    assert len(scheduled) == 1
    assert "check run lint on eerovil/agentdeck@abcdef123456 finished: failure" in (
        scheduled[0].text
    )
    assert "(https://github.com/eerovil/agentdeck/runs/9)" in scheduled[0].text
    # A wait that names a check takes it even skipped: that is the answer it asked for.
    register(service, commit=f"eerovil/agentdeck@{SHA}", name="deploy")
    deliver(
        root,
        event="check_run",
        delivery="d4",
        action="completed",
        check_run={"head_sha": SHA, "name": "deploy", "conclusion": "skipped"},
    )
    drain(service)
    assert "check run deploy on eerovil/agentdeck@abcdef123456 finished: skipped" in (
        scheduled[-1].text
    )


def test_a_name_filter_waits_for_the_named_check(root, service, scheduled):
    register(service, commit=f"eerovil/agentdeck@{SHA}", name="vercel")
    deliver(
        root,
        event="workflow_run",
        delivery="d1",
        action="completed",
        workflow_run={"head_sha": SHA, "name": "tests", "conclusion": "success"},
    )
    drain(service)
    assert scheduled == []
    deliver(root, event="status", delivery="d2", state="success", sha=SHA, context="Vercel")
    drain(service)
    assert len(scheduled) == 1 and "Vercel" in scheduled[0].text


@pytest.mark.parametrize(
    ("wanted", "context"),
    [
        ("drone/pr", "continuous-integration/drone/pr"),
        ("pr", "continuous-integration/drone/pr"),
        ("promote", "continuous-integration/drone/promote/staging"),
        ("Drone Promote", "continuous-integration/drone/promote/staging"),
    ],
)
def test_a_name_matches_whole_words_not_a_substring(root, service, scheduled, wanted, context):
    # #3172: "drone/pr" fired on drone/promote/staging, so the real drone/pr woke nobody.
    register(service, commit=f"eerovil/agentdeck@{SHA}", name=wanted)
    for n, other in enumerate(
        [
            "continuous-integration/drone/push",
            "continuous-integration/drone/promote/staging",
            "continuous-integration/drone/pr",
        ]
    ):
        if other != context:
            deliver(root, event="status", delivery=f"o{n}", state="success", sha=SHA, context=other)
    drain(service)
    assert scheduled == []
    deliver(root, event="status", delivery="hit", state="success", sha=SHA, context=context)
    drain(service)
    assert len(scheduled) == 1 and context in scheduled[0].text


def test_a_deployment_wait_wakes_on_a_finished_deployment_status(root, service, scheduled):
    register(service, deployment="eerovil/agentdeck", name="production")
    deliver(root, event="status", delivery="d1", state="pending", sha=SHA, context="production")
    deliver(
        root,
        event="deployment_status",
        delivery="d2",
        action="created",
        deployment_status={"state": "in_progress", "environment": "production"},
        deployment={"sha": SHA},
    )
    deliver(
        root,
        event="deployment_status",
        delivery="d3",
        action="created",
        deployment_status={
            "state": "success",
            "environment": "Production",
            "environment_url": "https://site",
        },
        deployment={"sha": SHA},
    )
    drain(service)
    assert len(scheduled) == 1
    assert "deployment to Production" in scheduled[0].text
    assert "finished: success (https://site)" in scheduled[0].text


def test_a_deployment_wait_wakes_on_a_status_only_deploy_like_vercel(root, service, scheduled):
    # mens-choir-website: Vercel reports deploys only as commit statuses, and
    # its check suite completes about a minute before the status turns success.
    register(service, deployment=f"eerovil/agentdeck@{SHA[:10]}", name="vercel")
    deliver(
        root,
        event="check_suite",
        delivery="d1",
        action="completed",
        check_suite={"head_sha": SHA, "conclusion": "success", "app": {"name": "Vercel"}},
    )
    deliver(root, event="status", delivery="d2", state="pending", sha=SHA, context="Vercel")
    deliver(root, event="status", delivery="d3", state="success", sha="f" * 40, context="Vercel")
    deliver(root, event="status", delivery="d4", state="success", sha=SHA, context="drone")
    drain(service)
    assert scheduled == []

    deliver(
        root,
        event="status",
        delivery="d5",
        state="success",
        sha=SHA,
        context="Vercel",
        target_url="https://vercel.com/x",
    )
    drain(service)
    assert len(scheduled) == 1
    assert "commit status Vercel" in scheduled[0].text
    assert "finished: success (https://vercel.com/x)" in scheduled[0].text


def deploy_run(root, delivery, conclusion, at, *, action="completed", sha=SHA, name="deploy"):
    """A ``check_run`` delivery for a deploy that is one job of a workflow (#4662)."""
    run = {"head_sha": sha, "name": name, "started_at": at, "conclusion": conclusion}
    if conclusion:
        run["completed_at"] = at
    deliver(root, event="check_run", delivery=delivery, action=action, check_run=run)


def test_a_named_deployment_wait_wakes_on_a_check_run_that_ended_as_a_deploy(
    root, service, scheduled, clock
):
    """#4662: a site deploying in a workflow job, ``deploy-cloudflare``, is reported by GitHub
    only as a check run. A named wait takes a success or a failure (``timed_out`` told as
    one); a skipped or cancelled run, a run still going, a workflow run and an unnamed wait
    do not."""
    # Around the test's own clock: a fixed date fails once the failure grace is past (#4675).
    start = datetime.fromtimestamp(clock.now, UTC)

    def at(seconds: int) -> str:
        return (start + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    named = register(service, deployment=f"eerovil/agentdeck@{SHA[:10]}", name="deploy")
    unnamed = register(service, deployment=f"eerovil/agentdeck@{SHA[:10]}")
    deploy_run(root, "d1", "", at(0), action="created")
    deploy_run(root, "d2", "skipped", at(1), action="created")
    deploy_run(root, "d3", "cancelled", at(2))
    deliver(
        root,
        event="workflow_run",
        delivery="d4",
        action="completed",
        workflow_run={"head_sha": SHA, "name": "deploy", "conclusion": "success"},
    )
    deploy_run(root, "d5", "success", at(3), name="build")
    drain(service)
    assert scheduled == []
    deploy_run(root, "d6", "timed_out", at(5))
    drain(service)
    assert len(scheduled) == 1 and "finished: failure" in scheduled[0].text
    assert [held.id for held in service.registry.all()] == [unnamed.id]
    assert named.id in scheduled[0].id


@pytest.mark.parametrize(
    "skipped_action_order", [("created", "completed"), ("completed", "created")]
)
def test_a_skipped_check_run_after_a_deploy_does_not_hide_its_success(
    root, service, skipped_action_order
):
    """#4662 design review: GitHub sends a skipped run as a ``created`` delivery already
    concluded and a ``completed`` one, in either order; neither is the deploy's newest state.
    A run started again with no conclusion yet is: it is waited on."""
    deploy_run(root, "d1", "success", "2026-11-25T10:00:00Z")
    for n, action in enumerate(skipped_action_order):
        deploy_run(root, f"s{n}", "skipped", "2026-11-25T11:00:00Z", action=action)
    request = parse_request(deployment=f"eerovil/agentdeck@{SHA}", name="deploy")
    request.pop("timeout_s")
    probe = WakeRequest(id="", session_key="k", chat_id="", **request)
    ended = service.seen_deploy_result(probe)
    assert ended is not None and ended.state == "success"
    deploy_run(root, "r1", "", "2026-11-25T12:00:00Z", action="created")
    assert service.seen_deploy_result(probe) is None


def test_a_deck_deploy_wait_wakes_once_the_running_deck_includes_the_commit(root, scheduled):
    asked = []

    def deck(revision, included):
        def contains(sha, rev):
            asked.append((sha, rev))
            return included

        return WakeOnService(
            root,
            schedule=lambda message: scheduled.append(message) or message,
            message_exists=lambda message_id: any(m.id == message_id for m in scheduled),
            revision=lambda: revision,
            contains=contains,
        )

    # The process still runs the old code: no wake, and the answer is asked once.
    before = deck("1" * 40, included=False)
    register(before, deck_deploy=SHA[:9].upper())
    before.expire_due()
    before.expire_due()
    assert scheduled == [] and asked == [(SHA[:9], "1" * 40)]

    # The deploy restarts the web process on the new code; that process wakes the chat.
    after = deck("2" * 40, included=True)
    after.expire_due()
    after.expire_due()
    assert len(scheduled) == 1
    assert "this deck now runs 222222222222, which includes commit abcdef123" in (scheduled[0].text)
    assert after.registry.all() == ()


def test_a_deck_deploy_wait_waits_for_the_serving_worker_and_its_skills(
    root, scheduled, clock, tmp_path
):
    # A replacement worker runs its startup before the supervisor hands it the listener,
    # and the deploy installs bundled skills only after the web reload (#3584).
    from agentdeck.deployment import skills_sha
    from agentdeck.wake_on import SKILLS_RETRY_S

    state = tmp_path / "deploy"
    marker = state / "skills_sha"
    service = WakeOnService(
        root,
        schedule=lambda message: scheduled.append(message) or message,
        message_exists=lambda message_id: any(m.id == message_id for m in scheduled),
        revision=lambda: "2" * 40,
        contains=lambda sha, rev: rev == "2" * 40,
        clock=clock,
        skills_sha=lambda: skills_sha(state),
    )
    service.set_accepting(False)
    register(service, deck_deploy=SHA)
    state.mkdir()
    marker.write_text("1" * 40 + "\n")
    service.expire_due()
    assert scheduled == [] and service.registry.all()

    # Serving now, but the skills are behind: asked again on a short retry, not kept.
    service.set_accepting(True)
    assert service._changed.is_set()
    service.expire_due()
    assert scheduled == [] and service.next_due() == clock.now + SKILLS_RETRY_S

    marker.write_text("2" * 40 + "\n")
    clock.now += SKILLS_RETRY_S
    service.expire_due()
    assert len(scheduled) == 1
    # A deploy that landed is routine (#4253): the notice opens with the routine line.
    assert scheduled[0].text.startswith(
        ROUTINE_LINE + "\n\nAgentDeck wake-on: the web worker serving this deck now runs"
        " 222222222222"
    )
    assert "bundled skills are installed at 222222222222" in scheduled[0].text

    # No marker at all (no deploy timer, as in development) does not hold a wait.
    marker.unlink()
    register(service, deck_deploy=SHA)
    service.expire_due()
    assert len(scheduled) == 2 and "bundled skills" not in scheduled[1].text


def test_a_deck_deploy_check_git_cannot_answer_is_asked_again_not_kept(root, scheduled, clock):
    answers = [None, True]
    asked = []

    def contains(sha, rev):
        asked.append(sha)
        return answers.pop(0)

    service = WakeOnService(
        root,
        schedule=lambda message: scheduled.append(message) or message,
        message_exists=lambda message_id: any(m.id == message_id for m in scheduled),
        revision=lambda: "2" * 40,
        contains=contains,
        clock=clock,
    )
    register(service, deck_deploy=SHA)
    service.expire_due()
    service.expire_due()  # within the retry pause: git is not asked again yet
    assert scheduled == [] and len(asked) == 1
    clock.now += 61
    service.expire_due()
    assert len(asked) == 2 and len(scheduled) == 1


def test_an_unknown_revision_never_wakes_a_deck_deploy_wait(root, scheduled):
    service = WakeOnService(
        root,
        schedule=lambda message: scheduled.append(message) or message,
        message_exists=lambda message_id: False,
        revision=lambda: None,
        contains=lambda sha, rev: pytest.fail("git must not be asked without a revision"),
    )
    register(service, deck_deploy=SHA)
    service.expire_due()
    assert scheduled == []


def test_the_app_checks_deploys_against_the_revision_it_started_from(tmp_path, monkeypatch):
    import agentdeck.app as app_module

    revisions = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(app_module, "_revision", lambda: next(revisions))
    app = create_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
            restart_nudge=RestartNudgeConfig(file=str(tmp_path / "restart-nudge.json")),
            github_webhook=GitHubWebhookConfig(state_dir=str(tmp_path / "github-webhooks")),
        )
    )
    # Read once at startup; a later move of the checkout is not what this process runs.
    assert app.state.revision == "a" * 40
    assert app.state.wake_on._revision() == "a" * 40


def test_the_deck_deploy_check_reads_real_git_ancestry(tmp_path):
    import subprocess

    from agentdeck.wake_on import _git_contains

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "1")
    first = git("rev-parse", "HEAD")
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "2")
    second = git("rev-parse", "HEAD")
    assert _git_contains(tmp_path, first[:8], second) is True
    assert _git_contains(tmp_path, second, first) is False
    # A commit this checkout has not fetched is "cannot say", not "no".
    assert _git_contains(tmp_path, "deadbeef", second) is None


def test_an_unnamed_deployment_wait_ignores_commit_statuses(root, service, scheduled):
    # Drone reports CI as commit statuses; without a name nothing says which status
    # is the deploy, so only a deployment status may end the wait.
    register(service, deployment="eerovil/agentdeck")
    deliver(
        root,
        event="status",
        delivery="d1",
        state="success",
        sha=SHA,
        context="continuous-integration/drone/push",
    )
    deliver(root, event="status", delivery="d2", state="success", sha=SHA, context="Vercel")
    drain(service)
    assert scheduled == []
    deliver(
        root,
        event="deployment_status",
        delivery="d3",
        action="created",
        deployment_status={"state": "success", "environment": "production"},
        deployment={"sha": SHA},
    )
    drain(service)
    assert len(scheduled) == 1 and "deployment to production" in scheduled[0].text


def test_a_delivery_received_before_the_registration_does_not_fire(root, service, scheduled, clock):
    deliver(
        root,
        event="pull_request",
        delivery="d1",
        action="closed",
        number=12,
        pull_request={"number": 12, "merged": True},
    )
    clock.now = datetime.now(UTC).timestamp() + 5
    register(service, pull_request="eerovil/agentdeck#12", timeout_minutes=10)
    drain(service)
    assert scheduled == []
    assert len(service.registry.all()) == 1


def test_a_deploy_wait_the_deck_registered_on_merge_carries_its_live_check(
    root, service, scheduled, clock
):
    """#4253: the deck registers the deploy wait itself when it merges, instead of asking the
    chat to; the wait's message is a routine notice that says so and carries the live check,
    on a landed deploy and on a timeout alike, and a renewal by the chat keeps it."""
    from agentdeck.deck_notices import ROUTINE_LINE
    from agentdeck.wake_on import deck_deploy_message, fired_message

    then = "Check it live, then record the live check."
    request = service.register(
        "claude_code:alt:one", "claude_code:alt:one", then=then,
        **parse_request(deployment="eerovil/store@abc1234", timeout_minutes=5),
    )
    renewed = register(service, deployment="eerovil/store@abc1234", timeout_minutes=5)
    assert (renewed.id, renewed.then) == (request.id, then), "the chat's own renewal keeps it"
    assert WakeOnRegistry(registry_path(root)).all()[0].then == then, "kept across a restart"

    landed = fired_message(
        request, SimpleNamespace(summary="deployment succeeded", state="success")
    )
    assert landed.startswith(ROUTINE_LINE)
    assert "The deck registered this wait for" in landed and landed.endswith(then)
    assert "You asked" not in landed
    deck = replace(request, kind="deck_deploy", sha="abc1234")
    deployed = deck_deploy_message(deck, "f" * 40)
    assert deployed.startswith(ROUTINE_LINE) and deployed.endswith(then)
    assert "You asked" not in deployed
    assert "You asked" in deck_deploy_message(replace(deck, then=""), "f" * 40)

    for _ in range(2):  # its own round, then the one more round (#4080)
        clock.now += 5 * 60
        service.expire_due()
        service.expire_due()
    [timed_out] = scheduled
    assert "when it merged your pull request" in timed_out.text
    assert timed_out.text.endswith(then)


def test_a_wait_that_times_out_still_wakes_the_chat_once(root, service, scheduled, clock):
    request = register(service, pull_request="eerovil/agentdeck#12", timeout_minutes=5)
    service.expire_due()
    assert scheduled == []
    clock.now += 5 * 60
    service.expire_due()
    service.expire_due()
    assert scheduled == []  # one more round first (#4080)
    clock.now += 5 * 60
    service.expire_due()
    service.expire_due()
    assert [message.id for message in scheduled] == [f"wake-on:{request.id}"]
    assert "nothing arrived for pull request eerovil/agentdeck#12 within 10 minutes" in (
        scheduled[0].text
    )


def test_registering_again_gives_a_spent_round_back(root, service, scheduled, clock):
    body = {"pull_request": "eerovil/agentdeck#12", "timeout_minutes": 5}
    register(service, **body)
    clock.now += 5 * 60
    service.expire_due()
    register(service, **body)
    clock.now += 5 * 60
    service.expire_due()
    assert scheduled == []
    clock.now += 5 * 60
    service.expire_due()
    assert len(scheduled) == 1


def test_the_extra_round_survives_a_restart_and_a_delivery_still_ends_it(
    root, service, scheduled, clock
):
    request = register(service, pull_request="eerovil/agentdeck#12", timeout_minutes=5)
    clock.now = request.deadline
    service.expire_due()
    restarted = WakeOnService(
        root,
        schedule=lambda message: scheduled.append(message) or message,
        message_exists=lambda _id: False,
        clock=clock,
    )
    (held,) = restarted.registry.all()
    assert (held.deadline, held.extra_s) == (request.deadline + 5 * 60, 0.0)
    restarted.apply(
        [
            Delivery(
                stream="eerovil/agentdeck",
                delivery_id="d1",
                event="pull_request",
                received_at=datetime.fromtimestamp(clock.now + 1, UTC).isoformat(),
                headers={},
                raw_body=json.dumps(
                    {"action": "closed", "number": 12, "pull_request": {"merged": True}}
                ).encode(),
                offset=0,
            )
        ]
    )
    assert len(scheduled) == 1 and "merged" in scheduled[0].text
    assert restarted.registry.all() == ()


def test_a_renewal_or_a_stopped_review_beats_the_extra_round(root, service, scheduled, clock):
    body = {"pull_request": "eerovil/agentdeck#12", "timeout_minutes": 5}
    request = register(service, **body)
    clock.now += 5 * 60
    renewed = register(service, **body)  # lands between the deadline pass's read and its write
    service.registry.extend(request.id, clock.now)
    assert service.registry.all() == (renewed,)
    # A merge wait a stopped review brought forward says so at once.
    service.registry.update(
        replace(renewed, stopped="no verdict", cut_from=renewed.deadline, deadline=clock.now)
    )
    service.expire_due()
    assert len(scheduled) == 1 and "nothing arrived" not in scheduled[0].text


def test_a_wait_stored_before_the_extra_round_times_out_at_once(root, service, scheduled, clock):
    request = register(service, pull_request="eerovil/agentdeck#12", timeout_minutes=5)
    service.registry.update(replace(request, extra_s=0.0))
    clock.now += 5 * 60
    service.expire_due()
    assert len(scheduled) == 1


def settling_service(root, scheduled, clock, **hooks):
    return WakeOnService(
        root,
        schedule=lambda message: scheduled.append(message) or message,
        message_exists=lambda message_id: any(m.id == message_id for m in scheduled),
        clock=clock,
        **hooks,
    )


async def settle(service) -> None:
    service.expire_due()
    while service._side_tasks:
        await asyncio.gather(*list(service._side_tasks))


async def test_a_timed_out_wait_is_read_from_github_and_waits_again_while_running(
    root, scheduled, clock
):
    """#4253: past its extra round, the deck reads a merge or CI wait itself; still running,
    it waits again silently up to a day; a result, a failed read or the cap wakes the chat."""
    answers: list[object] = []
    reads: list[str] = []

    async def fetch(path):
        reads.append(path)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    notes: dict[str, tuple[str, str]] = {}
    service = settling_service(
        root,
        scheduled,
        clock,
        live_read=lambda request: read_live(request, fetch),
        note=lambda chat, note_id, text: notes.setdefault(note_id, (chat, text)) is not None,
    )
    pull = register(service, pull_request="eerovil/agentdeck#12", timeout_minutes=5)
    clock.now += 5 * 60
    await settle(service)  # the extra round first, with no read
    assert reads == [] and scheduled == []

    # Still open: one read, waited again; not woken, and not read again before it is due.
    clock.now += 5 * 60
    answers.append({"state": "open"})
    await settle(service)
    assert reads == ["repos/eerovil/agentdeck/pulls/12"] and scheduled == []
    (held,) = service.registry.all()
    assert held.deadline == clock.now + LIVE_READ_AGAIN_S
    await settle(service)
    assert len(reads) == 1
    # #4306: its chat hears it with its next message, once per wait however often it is read.
    assert list(notes) == [f"wake-on:still-running:{pull.id}"]
    assert notes[f"wake-on:still-running:{pull.id}"][0] == pull.chat_id
    assert "is waiting again" in notes[f"wake-on:still-running:{pull.id}"][1]
    clock.now = held.deadline
    answers.append({"state": "open"})
    await settle(service)
    assert scheduled == [] and len(notes) == 1
    (held,) = service.registry.all()

    # Merged: a routine wake that says the deck read it.
    clock.now = held.deadline
    answers.append({"state": "closed", "merged": True, "merge_commit_sha": SHA})
    await settle(service)
    assert [m.id for m in scheduled] == [f"wake-on:{pull.id}"]
    text = scheduled[0].text
    assert text.startswith(ROUTINE_LINE) and "was merged as abcdef123456" in text
    assert "the deck read it from GitHub itself" in text
    assert service.registry.all() == ()

    # A note the deck could not keep leaves the wait as before: waiting again, silently.
    scheduled.clear()
    service._note = lambda chat, note_id, text: (_ for _ in ()).throw(OSError("disk"))
    still = register(service, pull_request="eerovil/agentdeck#13", timeout_minutes=5)
    clock.now += 5 * 60
    service.expire_due()  # the extra round
    clock.now += 5 * 60
    answers.append({"state": "open"})
    await settle(service)
    assert [item.id for item in service.registry.all()] == [still.id] and scheduled == []
    service.registry.remove([still.id])
    service._note = None

    # A failed read falls back to the timeout message, saying what the deck tried.
    scheduled.clear()
    commit = register(service, commit=f"eerovil/agentdeck@{SHA}", timeout_minutes=5)
    clock.now += 5 * 60
    service.expire_due()  # the extra round
    clock.now += 5 * 60
    answers.append(RuntimeError("gh api exited 1"))
    await settle(service)
    assert len(scheduled) == 1 and "nothing arrived for CI" in scheduled[0].text
    assert "the read failed (gh api exited 1)" in scheduled[0].text

    # Still running at the day's cap: the timeout message, saying the deck kept reading.
    scheduled.clear()
    commit = register(service, commit=f"eerovil/agentdeck@{SHA}", timeout_minutes=5)
    clock.now += 5 * 60
    service.expire_due()  # the extra round
    clock.now += 5 * 60
    running = {"check_runs": [{"name": "ci", "status": "in_progress", "conclusion": None}]}
    while not scheduled:
        answers.extend([running, {"statuses": []}])
        await settle(service)
        (held,) = service.registry.all() or (None,)
        if held is not None:
            assert held.deadline <= commit.registered_at + LIVE_READ_CAP_S
            clock.now = held.deadline
    assert clock.now == commit.registered_at + LIVE_READ_CAP_S
    assert "it was still running, so it waited again, up to a day" in scheduled[0].text

    # Nothing will ever report (#4253 review): no checks at all, or only skipped ones for an
    # unnamed wait. One read ends the wait with the timeout message saying what it found.
    skipped = {"check_runs": [{"name": "lint", "status": "completed", "conclusion": "skipped"}]}
    for runs, found in (
        ({"check_runs": []}, "the commit has no check runs or commit statuses"),
        (skipped, "its only finished checks were skipped or neutral (lint)"),
    ):
        scheduled.clear()
        reads.clear()
        register(service, commit=f"eerovil/agentdeck@{SHA}", timeout_minutes=5)
        clock.now += 5 * 60
        service.expire_due()  # the extra round
        clock.now += 5 * 60
        answers.extend([runs, {"statuses": []}])
        await settle(service)
        assert len(reads) == 2 and len(scheduled) == 1
        assert "nothing arrived for CI" in scheduled[0].text
        assert found in scheduled[0].text and "stopped waiting" in scheduled[0].text
        assert service.registry.all() == ()


async def test_a_live_read_says_what_the_commit_checks_finished_with():
    request = WakeRequest(
        id="w", session_key="k", chat_id="k", kind="commit", repository="o/r", sha=SHA
    )

    def reading(runs, statuses):
        async def fetch(path):
            return {"check_runs": runs} if "check-runs" in path else {"statuses": statuses}

        return fetch

    done = {"status": "completed"}
    green = await read_live(
        request,
        reading(
            [{**done, "name": "test", "conclusion": "success"}],
            [{"context": "drone", "state": "success"}],
        ),
    )
    assert green.state == "success" and "2 checks" in green.summary
    # A commit status may be a deploy, so only check runs alone are surely CI (#4253).
    assert not green.ci
    runs_only = await read_live(
        request, reading([{**done, "name": "test", "conclusion": "success"}], [])
    )
    assert runs_only.ci and runs_only.state == "success"
    # #4726: the statuses a result rests on are named, so a repository that names its deploy
    # status can tell they are CI; a name on the wait filters them as it filters the result.
    assert green.statuses == ("drone",) and runs_only.statuses == ()
    both = [{"context": "drone/pr", "state": "success"}, {"context": "Vercel", "state": "success"}]
    assert (await read_live(request, reading([], both))).statuses == ("drone/pr", "Vercel")
    named = WakeRequest(
        id="w", session_key="k", chat_id="k", kind="commit", repository="o/r", sha=SHA, name="pr"
    )
    assert (await read_live(named, reading([], both))).statuses == ("drone/pr",)
    # A skipped job says nothing; a failing status is the result.
    red = await read_live(
        request,
        reading(
            [{**done, "name": "deploy", "conclusion": "skipped"}],
            [{"context": "drone", "state": "failure"}],
        ),
    )
    assert red.state == "failure" and "not passing: drone" in red.summary
    # Only a skipped or neutral check: nothing to tell an unnamed wait, as by webhook; a
    # wait that names that check takes it (#4253 review).
    skipped = reading([{**done, "name": "deploy", "conclusion": "skipped"}], [])
    assert isinstance(await read_live(request, skipped), NothingToReport)
    assert (await read_live(replace(request, name="deploy"), skipped)).state == "skipped"
    # Pending somewhere: still running. Nothing at all: nothing will report.
    assert await read_live(request, reading([], [{"context": "x", "state": "pending"}])) is None
    assert isinstance(await read_live(request, reading([], [])), NothingToReport)
    # A named wait counts only its own check.
    named = replace(request, name="drone")
    assert (
        await read_live(
            named,
            reading(
                [{"status": "queued", "name": "test"}], [{"context": "drone", "state": "success"}]
            ),
        )
    ).state == "success"


async def test_a_timed_out_review_wait_first_reads_back_a_verdict_its_webhook_dropped(
    root, scheduled, clock
):
    """#4478: before saying no verdict came, the deck reads the posted verdict back once. Taken
    in, it ends the wait itself, or a held wait hears where it was; nothing found or a failed
    read times out as before; an unanswered call is read once more, a renewal earning a fresh
    retry, then times out saying a late verdict stands. A merge wait reads nothing."""
    from agentdeck.services_link import ServicesUncertain

    answers: list[object] = []
    calls: list[tuple[str, int]] = []
    service = None

    async def recover_review(repository, number):
        calls.append((repository, number))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if answer == "ends":  # taken in: the verdict's own delivery ended the wait
            service.registry.answer_reviews("claude_code:alt:one", [f"{repository}#{number}"])
            return "https://x/c1"
        return answer

    service = settling_service(
        root,
        scheduled,
        clock,
        recover_review=recover_review,
        reviewer_down=lambda: "the reviewer is paused",
    )

    def timed_out(answer, number=12, **body):
        answers.append(answer)
        request = register(service, **(body or {"review": f"eerovil/agentdeck#{number}"}))
        service.registry.update(replace(request, deadline=clock.now))
        return request

    timed_out("ends")
    await settle(service)
    assert calls == [("eerovil/agentdeck", 12)] and scheduled == []
    assert service.registry.all() == ()
    held = timed_out("https://x/c2", 13)
    await settle(service)
    [message] = scheduled
    assert message.id == f"wake-on:{held.id}" and "posted at https://x/c2" in message.text
    assert "do not ask for a fresh review" in message.text
    for answer in ("", RuntimeError("boom")):
        scheduled.clear()
        timed_out(answer, 14)
        await settle(service)
        [message] = scheduled
        assert "no verdict reached this chat" in message.text
        assert "the reviewer is paused" in message.text
        assert RECOVER_UNCERTAIN_NOTE not in message.text
    scheduled.clear()
    unsure = timed_out(ServicesUncertain(), 15)
    await settle(service)
    [moved] = service.registry.all()
    assert scheduled == [] and moved.deadline == clock.now + RECOVER_RETRY_S
    # A renewal before the retry is a fresh timeout: its own uncertain call is retried too.
    service.registry.update(replace(moved, deadline=clock.now + 600))
    clock.now += 600
    answers.append(ServicesUncertain())
    await settle(service)
    [moved] = service.registry.all()
    assert scheduled == [] and moved.deadline == clock.now + RECOVER_RETRY_S
    clock.now += RECOVER_RETRY_S
    answers.append(ServicesUncertain())
    await settle(service)
    [message] = scheduled
    assert message.id == f"wake-on:{unsure.id}" and message.text.endswith(RECOVER_UNCERTAIN_NOTE)
    assert "no verdict reached this chat" in message.text
    assert service.registry.all() == () and service._recovery_uncertain == {}
    calls.clear()
    timed_out("", pull_request="eerovil/agentdeck#16")
    service.registry.update(
        replace(service.registry.all()[0], deadline=clock.now, cut_from=clock.now)
    )
    await settle(service)
    assert calls == [] and answers == [""]


async def test_a_review_chat_closing_with_no_verdict_is_asked_again_by_the_deck(
    root, scheduled, clock
):
    """#4253: the deck asks for a fresh review itself; only a refusal or a reviewer that is
    down wakes the chat, saying what the deck tried. A label stop still wakes it as before."""
    asks: list[tuple] = []
    refusals: list[str] = []
    down = [""]

    async def ask_review(repository, number, note):
        asks.append((repository, number, note))
        return refusals.pop(0) if refusals else ""

    notes: dict[str, tuple[str, str]] = {}
    service = settling_service(
        root,
        scheduled,
        clock,
        ask_review=ask_review,
        reviewer_down=lambda: down[0],
        note=lambda chat, note_id, text: notes.setdefault(note_id, (chat, text)) is not None,
    )
    stopped = "the review chat https://x/9 closed its review, saying:\n\nno verdict"

    def stop(**body):
        request = register(service, **body)
        cut = replace(request, stopped=stopped, deadline=clock.now)
        if request.kind == "pull_request":
            cut = replace(cut, cut_from=request.deadline)
        service.registry.update(cut)
        return request

    review = stop(review="eerovil/agentdeck#12")
    merge = stop(pull_request="eerovil/agentdeck#12")
    await settle(service)
    assert scheduled == [] and [ask[:2] for ask in asks] == [("eerovil/agentdeck", 12)] * 2
    assert "no verdict" in asks[0][2]
    held = {item.id: item for item in service.registry.all()}
    assert held[review.id].stopped == "" and held[review.id].asked_at == clock.now
    assert held[review.id].deadline == clock.now + REVIEW_TIMEOUT_MINUTES * 60
    assert (held[merge.id].stopped, held[merge.id].deadline) == ("", merge.deadline)
    # #4306: each accepted re-ask is a pending note saying why, never a wake.
    assert sorted(note_id.rsplit(":", 1)[0] for note_id in notes) == sorted(
        f"wake-on:reask:{item.id}" for item in (review, merge)
    )
    assert all("asked for a fresh review itself" in text for _, text in notes.values())
    assert all("no verdict" in text and chat == review.chat_id for chat, text in notes.values())
    notes.clear()

    # Refused: the chat hears the refusal and is told not to ask again.
    service.registry.update(replace(held[review.id], stopped=stopped, deadline=clock.now))
    refusals.append("this head was already asked again twice")
    await settle(service)
    assert len(scheduled) == 1
    assert "it was refused: this head was already asked again twice" in scheduled[0].text
    assert "Do not ask again" in scheduled[0].text
    assert notes == {}, "a refusal wakes the chat instead"

    # The reviewer is down: no ask, and the chat is told why.
    scheduled.clear()
    asks.clear()
    down[0] = "no reviewer account is signed in"
    stop(review="eerovil/agentdeck#12")
    await settle(service)
    assert asks == [] and len(scheduled) == 1
    assert "did not ask for a fresh review itself" in scheduled[0].text
    assert "no reviewer account is signed in" in scheduled[0].text

    # A label stop wakes the chat with today's message, and asks nothing.
    scheduled.clear()
    down[0] = ""
    request = register(service, review="eerovil/agentdeck#13")
    service.registry.update(
        replace(request, stopped="the review chat x was labelled `blocked`", deadline=clock.now)
    )
    await settle(service)
    assert asks == [] and "ask for the review again yourself" in scheduled[0].text


@private_feature(_chat_review)
async def test_a_review_chat_closed_on_github_with_no_verdict_is_asked_again(
    root, scheduled, clock
):
    """#4253 review: the close itself, from its webhook, starts the deck's own re-ask once the
    settle window passes; a verdict in that window, or an earlier stop's note, wins."""
    asks: list[tuple] = []

    async def ask_review(repository, number, note):
        asks.append((repository, number, note))
        return ""

    chats = Repository(owner="eerovil", name="review-chats")
    service = settling_service(
        root, scheduled, clock, ask_review=ask_review, review_chats="eerovil/review-chats"
    )
    waiting = register(service, review="eerovil/agentdeck#12")
    answered = register(service, "claude_code:alt:two", review="eerovil/agentdeck#14")
    background = register(service, "claude_code:alt:three", review="eerovil/agentdeck#16")

    def chat(number, pull, body=""):
        return {
            "number": number,
            "title": f"Review {pull}",
            "state": "closed",
            "html_url": f"https://github.com/eerovil/review-chats/issues/{number}",
            "body": body or f"<!-- agentdeck: review-head {SHA} {pull} -->",
        }

    # A chat for #18 that lists #16 only as already passed background posts no verdict on
    # #16, so its close leaves #16's wait alone (#4599).
    listing = (
        f"- `eerovil/agentdeck#18` at commit `{SHA}`\n"
        f"- `eerovil/agentdeck#16` at commit `{SHA}` — {_chat_review.PASSED_LINE}\n\n"
        f"<!-- agentdeck: review-head {SHA} eerovil/agentdeck#18 -->\n"
        f"<!-- agentdeck: review-head {SHA} eerovil/agentdeck#16 -->"
    )
    deliver(
        root, event="issues", delivery="x72", repository=chats, action="closed",
        issue=chat(72, "eerovil/agentdeck#18", listing),
    )

    clock.now += 1
    deliver(
        root, event="issue_comment", delivery="c1", repository=chats, action="created",
        issue=chat(70, "eerovil/agentdeck#12"), comment={"body": "Ran out of time."},
    )
    for number, pull in ((70, "eerovil/agentdeck#12"), (71, "eerovil/agentdeck#14")):
        deliver(
            root, event="issues", delivery=f"x{number}", repository=chats, action="closed",
            issue=chat(number, pull),
        )
    drain(service)
    held = {item.id: item for item in service.registry.all()}
    assert "closed its review: it was closed" in held[waiting.id].stopped
    assert "Ran out of time." in held[waiting.id].stopped
    assert not held[background.id].stopped
    # A closing line naming only the background one cuts no one's wait, not the reviewed one's.
    said = SimpleNamespace(
        stream="eerovil/review-chats",
        event="issue_comment",
        payload={
            "action": "created",
            "issue": chat(72, "eerovil/agentdeck#18", listing),
            "comment": {"body": "Reviewed eerovil/agentdeck#16 at abcdef1: clean"},
        },
    )
    assert wake_on.review_stop(said, "eerovil/review-chats") is None
    said.payload["comment"]["body"] = "Reviewed eerovil/agentdeck#18 at abcdef1: clean"
    stop = wake_on.review_stop(said, "eerovil/review-chats")
    assert stop is not None and stop.pulls == ("eerovil/agentdeck#18",)
    service.registry.answer_reviews("claude_code:alt:two", ["eerovil/agentdeck#14"])
    clock.now = datetime.now(UTC).timestamp() + STOP_SETTLE_S + 5  # deliveries: wall clock
    await settle(service)

    assert [ask[:2] for ask in asks] == [("eerovil/agentdeck", 12)] and scheduled == []
    assert answered.id not in {item.id for item in service.registry.all()}, "its verdict won"


@pytest.mark.parametrize(
    ("deploy", "context", "expected"),
    [
        ("promote/production", "continuous-integration/drone/pr", True),
        ("promote/production", "continuous-integration/drone/push", True),
        ("promote/production", "continuous-integration/drone/promote/production", False),
        ("promote/production", "continuous-integration/drone/promote/staging", False),
        ("Promote/Production", "ci/PROMOTE-staging", False),
        ("promote/production", "Continuous-Integration/Drone/PR", True),  # whatever the case
        ("promote/production", "ci/promoted", False),  # not a build of a pull request or push
        ("promote/production", "Vercel", False),  # another service's status may be a deploy
        ("promote/production", "ci/pusher", False),  # a whole word, not a substring
        # The manifest may write the deploy's whole name: a build is still a build.
        ("drone/promote/production", "continuous-integration/drone/pr", True),
        ("drone/push", "continuous-integration/drone/push", False),  # the deploy itself
        ("", "continuous-integration/drone/pr", False),  # no deploy named: nothing is sure
        ("///", "continuous-integration/drone/pr", False),
        ("promote/production", "", False),
        ("Vercel", "Vercel", False),
    ],
)
def test_a_commit_status_is_ci_only_beside_a_named_deploy_of_another_kind(
    deploy, context, expected
):
    """#4726: with the status names real Drone repositories report."""
    assert status_is_ci(deploy, context) is expected


def test_green_ci_on_a_ready_head_and_an_already_told_merge_end_without_a_message(
    root, scheduled, clock
):
    """#4253: the release merges a ready head that went green, and its merge notice already
    told the chat of a merge; a draft's, a failing or a deploy's result still wakes the chat."""
    ready: list[bool] = [True]
    notices: list[str] = []
    notes: dict[str, tuple[str, str]] = {}
    service = settling_service(
        root,
        scheduled,
        clock,
        ready_head=lambda request: ready[0],
        release_notices=lambda chat_id, since: list(notices),
        note=lambda chat, note_id, text: notes.setdefault(note_id, (chat, text)) is not None,
    )

    def status(delivery, state):
        deliver(
            root,
            event="check_suite",
            delivery=delivery,
            action="completed",
            check_suite={"head_sha": SHA, "conclusion": state, "app": {"name": "ci"}},
        )
        drain(service)

    first = register(service, commit=f"eerovil/agentdeck@{SHA}")
    status("g1", "success")
    assert scheduled == [] and service.registry.all() == ()
    # #4306: a pending note instead, once per chat and commit.
    note_id = f"wake-on:ci-green:{first.chat_id}:eerovil/agentdeck@{SHA}"
    assert list(notes) == [note_id] and f"CI passed at {SHA[:12]}" in notes[note_id][1]
    register(service, commit=f"eerovil/agentdeck@{SHA}")
    status("g1b", "success")
    assert scheduled == [] and len(notes) == 1
    # A note not kept is the silence of before.
    service._note = lambda chat, note_id, text: False
    register(service, commit=f"eerovil/agentdeck@{SHA}")
    status("g1c", "success")
    assert scheduled == [] and service.registry.all() == ()
    register(service, commit=f"eerovil/agentdeck@{SHA}")
    status("r1", "failure")
    assert len(scheduled) == 1  # a failure wakes, ready or not
    # A deploy at that head is not CI, in either form GitHub sends it: a deployment status,
    # or a commit status as Vercel sends. Its result still wakes the chat waiting on it.
    register(service, commit=f"eerovil/agentdeck@{SHA}")
    deliver(
        root,
        event="deployment_status",
        delivery="p1",
        action="created",
        deployment_status={"state": "success", "environment": "preview"},
        deployment={"sha": SHA},
    )
    drain(service)
    assert len(scheduled) == 2 and "deployment to preview" in scheduled[1].text
    register(service, commit=f"eerovil/agentdeck@{SHA}", name="Vercel")
    deliver(root, event="status", delivery="p2", state="success", sha=SHA, context="Vercel")
    drain(service)
    assert len(scheduled) == 3 and "commit status Vercel" in scheduled[2].text
    # #4726: Drone reports CI as a commit status. Where the manifest names the deploy status,
    # a green status that is not of its kind is CI the deck settles; the deploy and its
    # staging sibling, a red one, and any status in a repository naming no deploy still wake.
    drone = "continuous-integration/drone/"
    names = {"eerovil/agentdeck": "promote/production"}
    service._deploy_name = lambda repository: names.get(repository, "")

    def commit_status(delivery, context, state="success", name=""):
        fields = {"name": name} if name else {}
        register(service, commit=f"eerovil/agentdeck@{SHA}", **fields)
        deliver(root, event="status", delivery=delivery, state=state, sha=SHA, context=context)
        drain(service)

    commit_status("d1", drone + "pr")
    commit_status("d2", drone + "push", name="drone/push")
    assert len(scheduled) == 3 and service.registry.all() == ()
    commit_status("d3", drone + "promote/production")
    commit_status("d4", drone + "promote/staging")
    commit_status("d5", drone + "pr", state="failure")
    assert [item.text.split(" finished: ")[0].split()[-3] for item in scheduled[3:]] == [
        drone + "promote/production",
        drone + "promote/staging",
        drone + "pr",
    ]
    names.clear()
    commit_status("d6", drone + "pr")
    assert len(scheduled) == 7, "no deploy name: any status may be the deploy"
    names["eerovil/agentdeck"] = "promote/production"
    ready[0] = False
    commit_status("d7", drone + "pr")
    assert len(scheduled) == 8, "a draft's green status still wakes"
    del scheduled[3:]
    ready[0] = True
    service._deploy_name = lambda repository: ""
    ready[0] = False  # a draft, or a head the cache does not know
    register(service, commit=f"eerovil/agentdeck@{SHA}")
    status("g2", "success")
    assert len(scheduled) == 4 and scheduled[3].text.startswith(ROUTINE_LINE)
    # #4253 review: the cached row the deck reads, in order: ready, merged, then green CI
    # landing after the merge, which the release's deploy wait carries on from: no message.
    # A pull request closed without a merge settles nothing.
    row = {"state": "OPEN", "isDraft": False, "headRefOid": SHA.upper(), "mergedAt": None}
    service._ready_head = lambda request: settled_head(row, request.sha)
    register(service, commit=f"eerovil/agentdeck@{SHA}")
    row.update(state="MERGED", mergedAt="2026-11-11T07:00:00Z")
    status("g3", "success")
    assert len(scheduled) == 4 and service.registry.all() == ()
    row.update(state="CLOSED", mergedAt=None)
    register(service, commit=f"eerovil/agentdeck@{SHA}")
    status("g4", "success")
    assert len(scheduled) == 5
    assert not settled_head({**row, "state": "OPEN", "isDraft": True}, SHA)
    assert not settled_head({"state": "OPEN", "headRefOid": "f" * 40}, SHA)

    def merged(delivery, number):
        deliver(
            root,
            event="pull_request",
            delivery=delivery,
            action="closed",
            number=number,
            pull_request={"number": number, "merged": True},
        )
        drain(service)

    notices.append("work-release:eerovil/agentdeck#7:live-check:eerovil/agentdeck#12")
    register(service, pull_request="eerovil/agentdeck#12")
    register(service, pull_request="eerovil/agentdeck#13")
    merged("m1", 12)
    merged("m2", 13)
    assert len(scheduled) == 6 and "agentdeck#13 was merged" in scheduled[5].text
    assert service.registry.all() == ()
    assert merge_notice_told(
        ["work-release:x:live-check:EeroVil/AgentDeck#12,o/r#1"], "eerovil/agentdeck#12"
    )
    assert not merge_notice_told(
        ["work-release:x:ready:eerovil/agentdeck#12"], "eerovil/agentdeck#12"
    )


def test_a_timeout_sends_a_result_the_deck_already_holds(root, service, scheduled, clock):
    finished = {"sha": SHA, "context": "ci", "state": "success"}
    deliver(root, event="status", delivery="d1", updated_at="2026-11-04T10:00:00Z", **finished)
    deliver(
        root,
        event="deployment_status",
        delivery="d2",
        action="created",
        deployment_status={
            "state": "success",
            "environment": "production",
            "updated_at": "2026-11-04T10:00:00Z",
        },
        deployment={"sha": SHA},
    )
    # Both are older than the waits, so only the read at the timeout can find them.
    clock.now = datetime.now(UTC).timestamp() + 5
    register(service, commit=f"eerovil/agentdeck@{SHA}", timeout_minutes=5)
    register(service, deployment=f"eerovil/agentdeck@{SHA}", timeout_minutes=5)
    drain(service)
    assert scheduled == []
    clock.now += 5 * 60
    service.expire_due()
    assert service.registry.all() == ()
    assert "commit status ci" in scheduled[0].text
    assert "deployment to production" in scheduled[1].text
    # #4253 review: a deploy the deck waits on after its merge, finished before that wait was
    # stored, wakes the chat at the next deadline pass, with the live check it carries.
    fields = parse_request(deployment=f"eerovil/agentdeck@{SHA}")
    register_deck_wait(service.registry, root, "k", "k", then="Check it live.", **fields)
    fields = parse_request(deployment=f"eerovil/agentdeck@{'f' * 40}")
    pending = register_deck_wait(service.registry, root, "k", "k", then="Later.", **fields)
    service.expire_due()
    assert service.registry.all() == (pending,)
    assert "deployment to production" in scheduled[2].text
    assert scheduled[2].text.endswith("Check it live.")


def deploy_state(root, delivery, state, at, *, sha=SHA, environment="production"):
    deliver(
        root,
        event="deployment_status",
        delivery=delivery,
        action="created",
        deployment_status={"state": state, "environment": environment, "updated_at": at},
        deployment={"sha": sha},
    )


@pytest.mark.parametrize(
    ("states", "told"),
    [
        (["failure"], "finished: failure"),
        (["success", "failure"], "finished: failure"),
        (["failure", "success"], "finished: success"),
        (["success", "in_progress"], None),
    ],
)
def test_a_timeout_tells_the_newest_state_of_a_deploy(
    root, service, scheduled, clock, states, told
):
    # Stored in reverse, so the source's own time decides and not the arrival.
    for n, state in reversed(list(enumerate(states))):
        deploy_state(root, f"d{n}", state, f"2026-11-04T10:0{n}:00Z")
    clock.now = datetime.now(UTC).timestamp() + 5
    register(service, deployment=f"eerovil/agentdeck@{SHA}", timeout_minutes=5)
    drain(service)
    clock.now += 5 * 60
    service.expire_due()
    if told:
        assert len(scheduled) == 1 and told in scheduled[0].text
    else:
        assert scheduled == [] and len(service.registry.all()) == 1


def test_a_timeout_finds_a_named_status_deploy_and_only_for_a_named_wait(
    root, service, scheduled, clock
):
    finished = {"sha": SHA, "context": "Vercel", "state": "failure"}
    deliver(root, event="status", delivery="d1", updated_at="2026-11-04T10:00:00Z", **finished)
    clock.now = datetime.now(UTC).timestamp() + 5
    register(service, deployment=f"eerovil/agentdeck@{SHA}", name="vercel", timeout_minutes=5)
    unnamed = register(service, deployment=f"eerovil/agentdeck@{SHA}", timeout_minutes=5)
    drain(service)
    clock.now += 5 * 60
    service.expire_due()
    woken = [m for m in scheduled if not m.id.startswith("wake-on:build-failed:")]
    assert len(woken) == 1 and "commit status Vercel" in woken[0].text
    assert [held.id for held in service.registry.all()] == [unnamed.id]
    # To the unnamed wait the status is a check on its commit that failed (#4445): told once,
    # and the wait goes on.
    told = [m.id.rsplit(":", 1)[0] for m in scheduled if m not in woken]
    assert told == [f"wake-on:build-failed:{unnamed.id}"]


def test_a_timeout_reads_a_named_deploy_across_both_kinds_of_report(
    root, service, scheduled, clock
):
    finished = {"sha": SHA, "context": "Vercel", "state": "success"}
    deliver(root, event="status", delivery="d1", updated_at="2026-11-04T10:00:00Z", **finished)
    deploy_state(root, "d2", "in_progress", "2026-11-04T10:01:00Z", environment="Vercel – Preview")
    clock.now = datetime.now(UTC).timestamp() + 5
    register(service, deployment=f"eerovil/agentdeck@{SHA}", name="vercel", timeout_minutes=5)
    drain(service)
    clock.now += 5 * 60
    service.expire_due()
    assert scheduled == [] and len(service.registry.all()) == 1


def test_a_timeout_on_a_wait_with_no_commit_counts_only_later_deploys(
    root, service, scheduled, clock
):
    now = datetime.now(UTC)
    deploy_state(root, "d1", "success", (now - timedelta(hours=1)).isoformat())
    clock.now = now.timestamp() + 5
    first = register(service, deployment="eerovil/agentdeck", timeout_minutes=5)
    drain(service)
    clock.now += 5 * 60
    service.expire_due()
    assert scheduled == [] and service.registry.all()[0].id == first.id
    # Not drained: the tailer is behind, so only the read at the timeout finds it.
    later = (now + timedelta(minutes=1)).isoformat()
    deploy_state(root, "d2", "failure", later, sha="b" * 40)
    clock.now += 5 * 60
    service.expire_due()
    assert len(scheduled) == 1 and "finished: failure" in scheduled[0].text
    assert service.registry.all() == ()


def test_an_unreadable_stream_does_not_stop_a_timeout(root, service, scheduled, clock, monkeypatch):
    def broken(_request):
        raise OSError("gone")

    monkeypatch.setattr(service, "seen_result", broken)
    register(service, commit=f"eerovil/agentdeck@{SHA}", timeout_minutes=5)
    clock.now += 5 * 60
    service.expire_due()
    clock.now += 5 * 60
    service.expire_due()
    assert len(scheduled) == 1 and "within 10 minutes" in scheduled[0].text


def test_the_next_deadline_is_the_earliest_timeout_or_retry(root, service, clock):
    """#2865: the service sleeps until this rather than looking every five seconds."""
    assert service.next_due() is None
    first = register(service, pull_request="eerovil/agentdeck#12", timeout_minutes=10)
    register(service, pull_request="eerovil/agentdeck#13", timeout_minutes=5)
    assert service.next_due() == clock.now + 5 * 60
    service.registry.remove([item.id for item in service.registry.all() if item != first])
    assert service.next_due() == first.deadline


@pytest.mark.asyncio
async def test_a_timeout_fires_at_its_deadline_and_a_registration_replans(root):
    """No delivery and no tick: the wait times out at its own deadline, and the deadline
    task learns of a registration made while it was sleeping with nothing to wait for."""
    import asyncio

    saved: list = []
    fired = asyncio.Event()

    def schedule(message):
        saved.append(message)
        fired.set()
        return message

    service = WakeOnService(root, schedule=schedule, message_exists=lambda message_id: False)
    service.start()
    try:
        await asyncio.sleep(0.1)  # asleep with nothing registered
        fields = parse_request(pull_request="eerovil/agentdeck#12")
        fields["timeout_s"] = 0.1  # twice over: a merge wait gets one more round (#4080)
        request = service.register("claude_code:alt:one", "chat-1", **fields)
        await asyncio.wait_for(fired.wait(), timeout=5)
    finally:
        await service.stop()
    assert [message.id for message in saved] == [f"wake-on:{request.id}"]
    assert "nothing arrived" in saved[0].text


def test_a_failed_store_write_keeps_the_wait_and_retries_the_same_message(root, clock):
    saved, attempts = [], []

    def schedule(message):
        attempts.append(message)
        if len(attempts) <= 2:
            return None  # the store write failed, on the match and on the pass's retry
        saved.append(message)
        return message

    service = WakeOnService(
        root,
        schedule=schedule,
        message_exists=lambda message_id: any(m.id == message_id for m in saved),
        clock=clock,
    )
    request = register(service, pull_request="eerovil/agentdeck#12")
    deliver(
        root,
        event="pull_request",
        delivery="d1",
        action="closed",
        number=12,
        pull_request={"number": 12, "merged": True},
    )
    drain(service)

    # Not lost: the wait is still held, owing the merge message, and survives a restart.
    assert saved == []
    (held,) = WakeOnRegistry(registry_path(root)).all()
    assert "was merged" in held.pending_text
    # The deadline passing does not swap the owed merge message for a timeout one.
    clock.now += 3 * 60 * 60
    service.expire_due()
    assert [m.id for m in saved] == [f"wake-on:{request.id}"]
    assert "was merged" in saved[0].text
    assert service.registry.all() == ()


def test_a_message_already_saved_under_the_id_ends_the_wait(root, clock):
    request_ids = []

    def schedule(message):
        request_ids.append(message.id)
        return None  # refused: another text is already stored under this id

    service = WakeOnService(root, schedule=schedule, message_exists=lambda _id: True, clock=clock)
    register(service, pull_request="eerovil/agentdeck#12", timeout_minutes=1)
    clock.now += 60
    service.expire_due()
    clock.now += 60
    service.expire_due()
    service.expire_due()
    assert len(request_ids) == 1
    assert service.registry.all() == ()


def test_a_first_start_skips_history_only_when_nothing_waits(root, service, scheduled):
    deliver(
        root,
        event="pull_request",
        delivery="d0",
        action="closed",
        number=1,
        pull_request={"number": 1, "merged": True},
    )
    assert service.skip_history_if_nothing_waits() is True
    assert list(service._tailer.deliveries()) == []


def test_a_lost_offset_replays_when_waits_are_held(root, service, scheduled, clock):
    deliver(
        root,
        event="pull_request",
        delivery="d0",
        action="closed",
        number=12,
        pull_request={"number": 12, "merged": False},
    )
    clock.now = datetime.now(UTC).timestamp()
    register(service, pull_request="eerovil/agentdeck#12")
    deliver(
        root,
        event="pull_request",
        delivery="d1",
        action="closed",
        number=12,
        pull_request={"number": 12, "merged": True},
    )

    # The tailer has no offset file, but a wait is held: nothing is skipped.
    assert service.skip_history_if_nothing_waits() is False
    service.apply(list(service._tailer.deliveries()))

    # The merge that landed while the deck was down still wakes the chat; the close
    # received before the wait was registered does not.
    assert len(scheduled) == 1 and "was merged" in scheduled[0].text


def test_registering_the_same_wait_twice_keeps_one(service):
    first = register(service, commit=f"eerovil/agentdeck@{SHA}", name="CI")
    second = register(service, commit=f"eerovil/agentdeck@{SHA}", name="ci")
    assert first.id == second.id
    assert len(service.registry.all()) == 1


def test_registering_again_keeps_a_delivery_that_landed_in_between(root, service, scheduled, clock):
    clock.now = datetime.now(UTC).timestamp()
    first = register(service, pull_request="eerovil/agentdeck#12", timeout_minutes=10)
    deliver(
        root,
        event="pull_request",
        delivery="d1",
        action="closed",
        number=12,
        pull_request={"number": 12, "merged": True},
    )
    clock.now = datetime.now(UTC).timestamp() + 1
    second = register(service, pull_request="eerovil/agentdeck#12", timeout_minutes=30)
    assert second.id == first.id
    assert second.registered_at == first.registered_at
    assert second.deadline == clock.now + 30 * 60

    # Tailed only after both registrations: the merge between them still wakes the chat.
    drain(service)
    assert len(scheduled) == 1 and "was merged" in scheduled[0].text


def test_registrations_survive_a_restart(root, service):
    request = register(service, deployment="eerovil/agentdeck@abc1234")
    assert WakeOnRegistry(registry_path(root)).all() == (request,)


def test_a_wait_a_newer_deck_wrote_survives_the_older_code(root, service):
    """A deploy runs old and new code together; the old one dropped every wait carrying a
    field it did not know, so all pending waits vanished (#3560)."""
    kept = register(service, after_minutes=60, waiting_for="the render")
    path = registry_path(root)
    raw = json.loads(path.read_text())
    raw["requests"][0]["field_from_a_newer_deck"] = "x"
    path.write_text(json.dumps(raw))

    older = WakeOnRegistry(path)
    assert older.all() == (kept,)
    other = register(service, pull_request="eerovil/agentdeck#5")
    older.remove([other.id])  # a write by the older code keeps the newer field
    assert json.loads(path.read_text())["requests"] == [
        {**raw["requests"][0], "field_from_a_newer_deck": "x"}
    ]


def test_a_chat_cannot_hold_unbounded_waits(service):
    for number in range(10):
        register(service, pull_request=f"eerovil/agentdeck#{number + 1}")
    with pytest.raises(WakeRequestError, match="already waits on 10"):
        register(service, pull_request="eerovil/agentdeck#99")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"pull_request": "eerovil/agentdeck#1", "commit": f"eerovil/agentdeck@{SHA}"},
        {"pull_request": "agentdeck#1"},
        {"commit": "eerovil/agentdeck"},
        {"commit": "eerovil/agentdeck@nothex"},
        {"commit": "eerovil/agentdeck@abc12"},
        {"deployment": "not a repo"},
        {"commit": f"eerovil/agentdeck@{SHA}", "name": " / "},
        {"pull_request": "eerovil/agentdeck#1", "name": "ci"},
        {"deck_deploy": "main"},
        {"deck_deploy": SHA, "name": "ci"},
        {"deck_deploy": SHA, "commit": f"eerovil/agentdeck@{SHA}"},
        {"pull_request": "eerovil/agentdeck#1", "timeout_minutes": 0},
        {"pull_request": "eerovil/agentdeck#1", "timeout_minutes": 24 * 60 + 1},
        {"at": "25:00"},
        {"at": "tomorrow"},
        {"at": "11:30", "pull_request": "eerovil/agentdeck#1"},
        {"after_minutes": 0},
        {"after_minutes": 14 * 24 * 60 + 1},
        {"at": "2026-11-26T09:00:00+02:00"},  # 15 days past the test's now below
        # #4256: a check belongs to a time wait, runs a command, and runs within the wait.
        {"pull_request": "eerovil/agentdeck#1", "check": {"command": "true"}},
        {"after_minutes": 300, "check": {"command": "  "}},
        {"after_minutes": 300, "check": "true"},
        {"after_minutes": 300, "check": {"command": "true", "every_minutes": 4}},
        {"after_minutes": 300, "check": {"command": "true", "every_minutes": 300}},
        {"after_minutes": 300, "check": {"command": "true", "every_minutes": True}},
        {"after_minutes": 300, "check": {"command": "true", "every": 10}},
        {"after_minutes": 300, "check": {"command": "true", "cwd": "relative/dir"}},
        {"after_minutes": 300, "check": {"command": "true", "cwd": False}},
        {"after_minutes": 300, "check": {"command": "true", "cwd": 0}},
        {"after_minutes": 300, "check": {"command": "x" * 2001}},
        {"after_minutes": 30, "timeout_minutes": 30},
        {"after_minutes": 30, "name": "ci"},
        {"after_minutes": 30, "waiting_for": "x" * 81},
        {"pull_request": "eerovil/agentdeck#1", "waiting_for": "the merge"},
        {"pull_request": "eerovil/agentdeck#1", "at": "", "after_minutes": 5},
        {"review": "agentdeck#1"},
        {"review": "eerovil/agentdeck#1", "pull_request": "eerovil/agentdeck#1"},
        {"review": "eerovil/agentdeck#1", "name": "ci"},
        {"needs_you": "agentdeck"},
        {"needs_you": "all", "only": "review"},
        {"needs_you": "all", "name": "ci"},
        {"needs_you": "all", "timeout_minutes": 7 * 24 * 60 + 1},
        {"pull_request": "eerovil/agentdeck#1", "only": "answer"},
    ],
)
def test_a_malformed_registration_is_refused(body):
    with pytest.raises(WakeRequestError):
        parse_request(**body, now=datetime(2026, 11, 11, 8, tzinfo=timezone(timedelta(hours=2))))


def test_a_time_wait_wakes_the_chat_at_its_time_naming_what_it_waits_on(
    root, service, scheduled, clock
):
    """#2968: a render on another machine reports nothing to GitHub; the time is the wake."""
    # #4266: a check a week after a change ships fits, up to two weeks ahead.
    week = parse_request(
        at="2026-11-18T09:00:00+02:00",
        waiting_for="the week's token report",
        now=datetime(2026, 11, 11, 8, tzinfo=timezone(timedelta(hours=2))),
    )
    assert week["timeout_s"] == (7 * 24 + 1) * 3600
    assert parse_request(after_minutes=14 * 24 * 60)["timeout_s"] == 14 * 24 * 3600
    request = register(service, after_minutes=90, waiting_for="the  render on the GTX 970")
    assert request.target == "the render on the GTX 970"
    # Its Work stays current until the wait fires, however long it sits quiet; the owner's
    # needs-you wait keeps no Work current.
    from agentdeck.web.work_view import wait_works, waiting_work_ids
    from agentdeck.work import WorkEvidence, WorkMembership, WorkProvenance, WorkRef

    def work(number, chat, evidence=WorkEvidence.REGISTERED_ISSUE):
        ref = WorkRef("eerovil/agentdeck", number)
        provenance = (WorkProvenance(evidence=evidence),)
        return SimpleNamespace(
            work_id=ref.work_id, memberships=(WorkMembership(ref, chat, provenance),)
        )

    register(service, "claude_code:alt:deck", needs_you="all")
    projection = SimpleNamespace(
        works=(
            work(1, "claude_code:alt:one"),
            work(2, "claude_code:alt:one", WorkEvidence.DELEGATED),
            work(3, "claude_code:alt:deck"),
            work(4, "claude_code:alt:two"),
        )
    )
    app = SimpleNamespace(state=SimpleNamespace(wake_on=service))
    assert waiting_work_ids(app, projection) == {"github_issue:eerovil/agentdeck#1"}
    # Two Works waiting at once, each by its own chat; a restart reads both from the file.
    register(service, "claude_code:alt:two", after_minutes=120, waiting_for="the backup")
    both = {"github_issue:eerovil/agentdeck#1", "github_issue:eerovil/agentdeck#4"}
    assert waiting_work_ids(app, projection) == both
    restarted = SimpleNamespace(
        state=SimpleNamespace(
            wake_on=SimpleNamespace(registry=WakeOnRegistry(registry_path(root)))
        )
    )
    assert waiting_work_ids(restarted, projection) == both
    deliver(
        root,
        event="pull_request",
        delivery="d1",
        action="closed",
        number=1,
        pull_request={"number": 1, "merged": True},
    )
    drain(service)
    clock.now += 89 * 60
    service.expire_due()
    assert scheduled == []
    clock.now += 60
    service.expire_due()
    service.expire_due()
    assert [message.id for message in scheduled] == [f"wake-on:{request.id}"]
    assert "it is time to check on the render on the GTX 970" in scheduled[0].text
    # Only its own wait fired: the other Work still waits.
    assert sorted(wait.kind for wait in service.registry.all()) == ["needs_you", "time"]
    assert waiting_work_ids(app, projection) == {"github_issue:eerovil/agentdeck#4"}
    # #4272: a chat directly in two Works keeps only the one its wait is for current.
    def work_in(repo, number, chat):
        ref = WorkRef(repo, number)
        provenance = (WorkProvenance(evidence=WorkEvidence.REGISTERED_ISSUE),)
        return SimpleNamespace(
            work_id=ref.work_id, memberships=(WorkMembership(ref, chat, provenance),)
        )

    shared = "claude_code:alt:both"
    busy = "github_issue:eerovil/trailhead#8"
    two = SimpleNamespace(
        works=(work_in("eerovil/agentdeck", 7, shared), work_in("eerovil/trailhead", 8, shared))
    )
    service.registry.remove([wait.id for wait in service.registry.all()])
    unsure = register(service, shared, after_minutes=7 * 24 * 60, waiting_for="the report")
    assert waiting_work_ids(app, two) == frozenset()  # neither named: neither held
    service.registry.remove([unsure.id])
    named = register(
        service,
        shared,
        after_minutes=7 * 24 * 60,
        waiting_for="the report",
        work_id="Eerovil/Trailhead#8",
    )
    assert named.work_id == busy and named.payload()["work_id"] == busy
    assert waiting_work_ids(app, two) == {busy}
    assert waiting_work_ids(restarted, two) == {busy}  # the file keeps the Work it names
    # The same wait for the other Work is a wait of its own, not a renewal of this one
    # (#4272 review).
    other = register(
        service,
        shared,
        after_minutes=7 * 24 * 60,
        waiting_for="the report",
        work_id="eerovil/agentdeck#7",
    )
    assert other.id != named.id and other.work_id == "github_issue:eerovil/agentdeck#7"
    assert waiting_work_ids(app, two) == {busy, other.work_id}
    service.registry.remove([other.id])
    service.registry.remove([named.id])
    ci = register(service, shared, commit="eerovil/trailhead@abc1234")  # its repository says
    assert waiting_work_ids(app, two) == {busy}
    service.registry.remove([ci.id])
    # A pull request wait in a repository both Works share follows the pull request's own
    # registration (#3392).
    third = work_in("eerovil/trailhead", 9, shared)
    three = SimpleNamespace(works=(*two.works, third))
    register(service, shared, pull_request="eerovil/trailhead#20")
    assert waiting_work_ids(app, three) == frozenset()
    pull = PullRequestRef("eerovil/trailhead", 20, work_id=third.work_id)
    # Another chat registering the same pull request for another Work says nothing of this
    # chat's wait (#4272 review): only the waiting chat's own registrations count.
    theirs = PullRequestRef("eerovil/trailhead", 20, work_id=busy)
    app.state.app_state = SimpleNamespace(
        session_pull_requests={"claude_code:alt:other": (theirs,)}
    )
    assert waiting_work_ids(app, three) == frozenset()
    app.state.app_state = SimpleNamespace(
        session_pull_requests={shared: (pull,), "claude_code:alt:other": (theirs,)},
        chat_session_keys=lambda key: (key,),
    )
    assert waiting_work_ids(app, three) == {third.work_id}
    # Two legs of the one chat naming two Works leave it undecided (#4272 review).
    app.state.app_state = SimpleNamespace(
        session_pull_requests={shared: (pull,), "claude_code:alt:leg2": (theirs,)},
        chat_session_keys=lambda key: (key, "claude_code:alt:leg2"),
    )
    assert waiting_work_ids(app, three) == frozenset()
    # Nor does the pull request's repository break that tie when the two Works are in two
    # repositories (#4272 review round 3): both stay undecided, for status and currentness.
    service.registry.remove([wait.id for wait in service.registry.all()])
    across = register(service, shared, pull_request="eerovil/agentdeck#20")
    mine = PullRequestRef("eerovil/agentdeck", 20, work_id="github_issue:eerovil/agentdeck#7")
    yours = PullRequestRef("eerovil/agentdeck", 20, work_id=busy)
    app.state.app_state = SimpleNamespace(
        session_pull_requests={shared: (mine,), "claude_code:alt:leg2": (yours,)},
        chat_session_keys=lambda key: (key, "claude_code:alt:leg2"),
    )
    assert waiting_work_ids(app, two) == frozenset()
    [held] = [w for w in service.registry.all() if w.id == across.id]
    both = [w.work_id for w in two.works]
    assert wait_works(app.state.app_state, held, both) == set(both)
    # A wait naming a Work its chat is not in holds nothing.
    service.registry.remove([wait.id for wait in service.registry.all()])
    register(service, shared, after_minutes=60, work_id=busy)
    assert waiting_work_ids(app, SimpleNamespace(works=(two.works[0],))) == frozenset()
    with pytest.raises(WakeRequestError, match="work_id"):
        parse_request(after_minutes=5, work_id="not a work")
    with pytest.raises(WakeRequestError, match="work_id"):
        parse_request(needs_you="all", work_id="eerovil/agentdeck#1")


def needs_you_stop(work=1, *, category="needs_answer", at=0.0, repo="eerovil/agentdeck", **kw):
    return NeedsYouStop(
        work_id=f"github_issue:{repo}#{work}",
        category=category,
        label={"needs_answer": "Needs answer", "ready_for_merge": "Ready for merge"}[category],
        title=f"Work {work}",
        issue=f"{repo}#{work}",
        issue_url=f"https://github.com/{repo}/issues/{work}",
        page=f"/works/github_issue:{work}",
        reason=kw.pop("reason", "Which colour?"),
        stopped_at=at,
        **kw,
    )


def test_a_needs_you_wait_tells_each_new_stop_once_and_stays_registered(
    service, scheduled, clock, here
):
    """#3446: the owner's deck chat hears of every Work that starts needing them."""
    request = register(service, "claude_code:alt:deck", needs_you="Eerovil/AgentDeck")
    later = clock.now + 5
    older = needs_you_stop(1, at=clock.now - 5)  # already waiting at registration
    asked = needs_you_stop(2, at=later)
    ready = needs_you_stop(3, category="ready_for_merge", at=later, reason="")
    elsewhere = needs_you_stop(4, at=later, repo="eerovil/trailhead")
    own = needs_you_stop(5, at=later, chats=("claude_code:alt:deck",))
    stops = [older, asked, ready, elsewhere, own]
    assert service.needs_you(stops) == 2
    assert service.needs_you(stops) == 0  # a second pass repeats nothing
    text = scheduled[0].text
    assert "Needs answer — Work 2 (eerovil/agentdeck#2" in text
    assert '"Which colour?"' in text and "/works/github_issue:2" in text
    assert "Ready for merge — Work 3" in scheduled[1].text
    # #3481: an answer goes into the question box, which the message says how to reach.
    assert "GET $AGENTDECK_URL/api/works/eerovil/agentdeck/2/question" in text
    assert "/question" not in scheduled[1].text
    assert 'where it says "message", send it to that chat as a message' in text
    # Not spent: the wait stays, remembering only the stops still listed.
    [held] = service.registry.all()
    assert held.id == request.id and len(held.told) == 2
    service.needs_you([asked])
    assert service.registry.all()[0].told == (asked.key,)
    # A degraded pass forgets nothing, and the same Work stopping anew is told again.
    service.needs_you([], complete=False)
    assert service.registry.all()[0].told == (asked.key,)
    again = needs_you_stop(2, at=later + 60)
    assert service.needs_you([again]) == 1
    assert len({message.id for message in scheduled}) == 3
    assert service.needs_you_waited()
    # A chat that moved to a new leg is still the Work's own chat: keyed by its identity.
    fields = parse_request(needs_you="all")
    moved = service.register("claude_code:alt:old-leg", "chat:deck", **fields)
    service.needs_you([needs_you_stop(6, at=moved.registered_at + 1, chats=("chat:deck",))])
    assert len(scheduled) == 4 and scheduled[-1].chat_id == "claude_code:alt:deck"
    # #3464: with the owner out of the chat a stop is marked told, not saved up for later.
    away = needs_you_stop(7, at=moved.registered_at + 2)
    here[0] = False
    assert service.needs_you([away]) == 0
    here[0] = True
    assert service.needs_you([away]) == 0 and len(scheduled) == 4
    # In the chat means a page of it holds the chat's topic, as the chat page does.
    from agentdeck.events import EventBus
    from agentdeck.state import chat_topic
    from agentdeck.web.work_view import owner_in_chat

    state = SimpleNamespace(bus=EventBus(), chat_identity_key=lambda key: "chat:deck")
    assert not owner_in_chat(state, moved)
    with state.bus.subscribe(chat_topic("chat:deck")):
        assert owner_in_chat(state, moved)
    assert not owner_in_chat(state, moved)


def test_a_needs_you_wait_filters_by_category_and_times_out_with_a_rearm_hint(
    service, scheduled, clock, here
):
    register(service, needs_you="all", only="merge", timeout_minutes=60)
    later = clock.now + 5
    assert service.needs_you([needs_you_stop(1, at=later)]) == 0
    assert service.needs_you([needs_you_stop(2, category="ready_for_merge", at=later)]) == 1
    clock.now += 3600
    service.expire_due()
    assert "Register it again" in scheduled[-1].text
    assert service.registry.all() == ()
    assert parse_request(needs_you="all")["timeout_s"] == 7 * 24 * 3600
    # #3464: with the owner away, its timeout ends it without a message.
    register(service, needs_you="all", timeout_minutes=60)
    here[0], told = False, len(scheduled)
    clock.now += 3600
    service.expire_due()
    assert len(scheduled) == told and service.registry.all() == ()


def test_a_needs_you_wake_whose_save_failed_is_retried_on_a_timer(root, clock):
    """#3446 review: a lone question must not wait for some other Work to change."""
    saved, failing = [], [True]

    def schedule(message):
        if failing[0]:
            return None
        saved.append(message)
        return message

    here = [True]
    service = WakeOnService(
        root,
        schedule=schedule,
        message_exists=lambda _id: False,
        clock=clock,
        present=lambda request: here[0],
    )
    register(service, needs_you="all")
    assert service.needs_you([needs_you_stop(1, at=clock.now + 5)]) == 0
    assert service.next_due() == clock.now + 10
    # A degraded pass that dropped the Work keeps its retry (#3446 review).
    service.needs_you([], complete=False)
    assert service.next_due() == clock.now + 10
    failing[0] = False
    clock.now += 10
    service.expire_due()
    assert len(saved) == 1 and "Work 1" in saved[0].text
    assert service.next_due() > clock.now + 3600  # only the wait's own week is left
    # #3464: the retry asks the same presence check, so an owner who left is not woken.
    failing[0] = True
    away = needs_you_stop(2, at=clock.now + 5)
    assert service.needs_you([away]) == 0
    failing[0], here[0] = False, False
    clock.now += 10
    service.expire_due()
    assert len(saved) == 1 and service.registry.all()[0].told == (away.key,)


def test_a_needs_you_wait_notes_a_told_work_that_no_longer_needs_the_owner(root, clock):
    """#4358: a Work the chat was woken for that leaves Needs you leaves a note that wakes
    nothing, so the chat stops saying it waits on the person. Only for Works it was told of."""
    scheduled, notes, keeping, here = [], {}, [True], [True]

    def note(chat, note_id, text):
        if not keeping[0]:
            return False
        notes.setdefault(note_id, (chat, text))
        return True

    service = WakeOnService(
        root,
        schedule=lambda message: scheduled.append(message) or message,
        message_exists=lambda message_id: any(m.id == message_id for m in scheduled),
        clock=clock,
        present=lambda request: here[0],
        note=note,
    )
    request = register(service, "claude_code:alt:deck", needs_you="all")
    later = clock.now + 5
    asked, ready = needs_you_stop(1, at=later), needs_you_stop(2, at=later)
    assert service.needs_you([asked, ready]) == 2
    here[0] = False
    away = needs_you_stop(3, at=later)  # marked told, never reported
    assert service.needs_you([asked, ready, away]) == 0
    here[0] = True
    # A degraded pass that dropped them says nothing.
    service.needs_you([], complete=False)
    assert notes == {}
    # Work 1 asks a new question: it still needs the owner, so no note, only the new wake.
    again = needs_you_stop(1, at=later + 60)
    where = {"github_issue:eerovil/agentdeck#2": "Working; pull requests #9 open"}.get
    assert service.needs_you([again, ready], where=where) == 1 and notes == {}
    # Work 2 moved on, Work 3 (never told) too: one note, for Work 2, and no wake.
    keeping[0] = False  # the store fails once: kept owed, retried on the deadline timer
    service.needs_you([again], where=where)
    assert notes == {} and len(scheduled) == 3
    assert service.next_due() == clock.now + 10
    keeping[0] = True
    clock.now += 10
    service.expire_due()  # no further overview pass needed (#4361 review)
    [(note_id, (chat, text))] = notes.items()
    assert chat == "claude_code:alt:deck" and note_id.startswith(f"wake-on:{request.id}:")
    assert text.startswith('eerovil/agentdeck#2 (Needs answer: Work 2) no longer needs')
    assert "Where it is now: Working; pull requests #9 open." in text
    assert len(scheduled) == 3
    service.needs_you([again])  # noted once
    [held] = service.registry.all()
    # Work 1's first stop stays until the Work leaves: then one note, for the newest.
    assert [key for key, _ in held.reported] == [asked.key, again.key] and len(notes) == 1
    # The reported stops survive a reload from disk.
    reloaded = WakeOnService(
        root, schedule=lambda m: m, message_exists=lambda _id: False, clock=clock
    )
    assert reloaded.registry.all()[0].reported == held.reported
    # Out of Needs you altogether, with nowhere known: the note still goes.
    service.needs_you([])
    assert len(notes) == 2 and "Where it is now" not in list(notes.values())[-1][1]
    assert service.registry.all()[0].reported == ()
    # #4361 review: an answer-only wait whose Work moves on to Ready for merge gets no note,
    # since the Work still needs the person; once it leaves Needs you, it does.
    service.registry.remove([request.id])
    register(service, "claude_code:alt:deck", needs_you="all", only="answer")
    asked = needs_you_stop(8, at=clock.now + 5)
    assert service.needs_you([asked]) == 1
    merge = needs_you_stop(8, category="ready_for_merge", at=clock.now + 6)
    assert service.needs_you([merge]) == 0 and len(notes) == 2
    service.needs_you([])
    assert len(notes) == 3 and list(notes.values())[-1][1].startswith("eerovil/agentdeck#8")


def test_a_time_wait_check_stays_silent_until_it_says_it_happened(root, service, scheduled, clock):
    """#4256: a check runs every interval until the deadline; exit 3 wakes nobody, exit 0
    wakes the chat once with its output, and two workers never run the same run twice."""
    from agentdeck.wake_on import CHECK_NOT_YET, MAX_TIME_WAIT_MINUTES

    assert MAX_TIME_WAIT_MINUTES == 14 * 24 * 60
    request = register(
        service,
        after_minutes=3 * 24 * 60,  # past the old one-day cap
        waiting_for="the error not coming back",
        check={"command": "sentry-search", "every_minutes": 60, "cwd": str(root.parent)},
    )
    assert request.payload()["check"] == {
        "command": "sentry-search",
        "cwd": str(root.parent),
        "every_minutes": 60,
        "next_run": clock.now + 3600,
        "not_yet_runs": 0,
    }
    assert service.next_due() == clock.now + 3600 and service.due_checks() == []
    clock.now += 3600
    claimed = service.due_checks()
    assert [item.id for item in claimed] == [request.id]
    assert service.due_checks() == []  # claimed: its next run moved an hour on
    service.check_done(claimed[0], CHECK_NOT_YET, "0 events\n", "")
    service.expire_due()
    assert scheduled == []
    (held,) = service.registry.all()
    assert held.check_runs == 1 and "printed: 0 events" in held.check_last
    clock.now += 3600
    (second,) = service.due_checks()
    service.check_done(second, 0, "SENTRY-12 seen again at 14:02", "")
    assert [m.id for m in scheduled] == [f"wake-on:{request.id}"]
    assert "says it happened (exit 0 at" in scheduled[0].text
    assert "SENTRY-12 seen again at 14:02" in scheduled[0].text
    assert service.registry.all() == ()
    service.check_done(second, 0, "late duplicate", "")  # the wait is gone: nothing more
    assert len(scheduled) == 1


def test_a_time_wait_check_error_wakes_and_the_deadline_quotes_the_last_not_yet(
    root, service, scheduled, clock
):
    """#4256: any exit but 0 and 3 wakes with the reason; a check that only ever said not yet
    wakes at the deadline with its newest run; a renewal starts the check afresh."""
    from agentdeck.wake_on import CHECK_NOT_YET

    broken = register(
        service, after_minutes=120, waiting_for="the job", check={"command": "jq -e .ok"}
    )
    clock.now += 3600
    (run,) = service.due_checks()
    service.check_done(run, 5, "jq: error: syntax", "exit 5")
    assert "went wrong at" in scheduled[0].text and "(exit 5)" in scheduled[0].text
    assert "jq: error: syntax" in scheduled[0].text and "exit 3 means not yet" in scheduled[0].text
    assert scheduled[0].id == f"wake-on:{broken.id}"

    quiet = register(
        service, after_minutes=150, waiting_for="the log line", check={"command": "grep -q x"}
    )
    clock.now += 3600
    (run,) = service.due_checks()
    service.check_done(run, CHECK_NOT_YET, "", "")
    clock.now += 60
    (in_flight,) = service.registry.all()  # a run of the old command, still going...
    again = register(
        service, after_minutes=90, waiting_for="The log line", check={"command": "grep -q y"}
    )
    assert again.id == quiet.id and again.check_runs == 0 and again.check_command == "grep -q y"
    assert again.check_at == clock.now + 3600 and again.check_gen == in_flight.check_gen + 1
    # ...ends after the renewal: neither its success nor its "not yet" touches the new wait.
    service.check_done(in_flight, 0, "old command matched", "")
    service.check_done(in_flight, CHECK_NOT_YET, "old", "")
    assert len(scheduled) == 1 and service.registry.all()[-1].check_runs == 0
    clock.now += 3600
    (run,) = service.due_checks()
    service.check_done(run, CHECK_NOT_YET, "no match", "")
    clock.now += 30 * 60
    assert service.due_checks() == []  # the next run falls after the deadline
    service.expire_due()
    assert [m.id for m in scheduled][-1] == f"wake-on:{quiet.id}"
    text = scheduled[-1].text
    assert "has come, and your check never said it happened" in text
    assert "said not yet 1 time(s)" in text and "printed: no match" in text



def test_a_check_result_and_the_other_workers_renewal_never_cross(root, service, scheduled, clock):
    """#4256: two web workers share the registry during a deploy. A renewal by the other
    one before a run ends turns that run's result away; one that lands after the result
    was settled, while its wake is being sent, finds the wake already owed and changes
    nothing, so the chat gets exactly that one message."""
    from agentdeck import wake_on
    from agentdeck.wake_on import CHECK_NOT_YET

    other = WakeOnRegistry(service.registry.path)

    def renew(command):
        fields = parse_request(after_minutes=150, waiting_for="the fix", check={"command": command})
        key = "claude_code:alt:one"
        return wake_on.register(other, key, key, clock=clock, **fields)

    request = register(service, after_minutes=150, waiting_for="the fix", check={"command": "a"})
    clock.now += 3600
    (run,) = service.due_checks()
    renew("b")  # the other worker, while "a" runs
    service.check_done(run, 0, "a found it", "")
    service.check_done(run, CHECK_NOT_YET, "a: nothing", "")
    assert scheduled == []
    (held,) = service.registry.all()
    assert held.check_command == "b" and held.check_runs == 0 and not held.pending_text

    clock.now += 3600
    (run,) = service.due_checks()
    sending = service._schedule

    def renewed_while_sending(message):
        renew("c")
        return sending(message)

    service._schedule = renewed_while_sending
    service.check_done(run, 0, "b found it", "")
    assert [m.id for m in scheduled] == [f"wake-on:{request.id}"]
    assert "b found it" in scheduled[0].text
    assert service.registry.all() == ()

def _watch(service, session_key="claude_code:alt:one", **extra):
    body = {
        "watch": "the TypeError in the sync job",
        "after_minutes": 3 * 24 * 60,
        "work_id": "eerovil/agentdeck#41",
        "check": {"command": "sentry-search", "every_minutes": 60},
        **extra,
    }
    return register(service, session_key, **body)


@pytest.mark.asyncio
async def test_a_watch_reopens_its_issue_with_the_output_and_only_then_tells_the_chat(
    root, scheduled, clock
):
    """#4466: a watch's not-seen runs and a lone error stay silent; exit 0 owes a reopen and
    no message, which neither web worker can send early; a failed reopen is tried again a
    minute on; once it lands, the chat hears once what the deck did. Its Work stays unheld."""
    from agentdeck.wake_on import CHECK_NOT_YET, REOPEN_RETRY_S, WATCH_MARKER

    reopens = []
    answers = [False, True]

    async def reopen(repository, number, marker, comment):
        reopens.append((repository, number, marker, comment))
        return answers.pop(0)

    def schedule(message):
        scheduled.append(message)
        return message

    def make():
        return WakeOnService(
            root,
            schedule=schedule,
            message_exists=lambda message_id: any(m.id == message_id for m in scheduled),
            clock=clock,
            reopen=reopen,
        )

    service, other = make(), make()
    request = _watch(service)
    assert request.kind == "watch" and request.target == "the TypeError in the sync job"
    assert request.payload()["check"]["errors_in_a_row"] == 0
    # A finished Work keeps nothing current for its watch.
    from agentdeck.web.work_view import waiting_work_ids
    from agentdeck.work import WorkEvidence, WorkMembership, WorkProvenance, WorkRef

    ref = WorkRef("eerovil/agentdeck", 41)
    membership = WorkMembership(
        ref, "claude_code:alt:one", (WorkProvenance(evidence=WorkEvidence.REGISTERED_ISSUE),)
    )
    projection = SimpleNamespace(
        works=(SimpleNamespace(work_id=ref.work_id, memberships=(membership,)),)
    )
    app = SimpleNamespace(state=SimpleNamespace(wake_on=service))
    assert waiting_work_ids(app, projection) == frozenset()

    clock.now += 3600
    (run,) = service.due_checks()
    service.check_done(run, 1, "HTTP 502", "exit 1")  # one failed read: counted, silent
    clock.now += 3600
    (run,) = service.due_checks()
    service.check_done(run, CHECK_NOT_YET, "0 events", "")  # a clean read clears the count
    (held,) = service.registry.all()
    assert held.check_errors == 0 and held.check_runs == 1 and scheduled == []

    clock.now += 3600
    (run,) = service.due_checks()
    service.check_done(run, 0, "SENTRY-12 seen again at 14:02", "")
    (held,) = service.registry.all()
    assert held.reopen_owed and not held.pending_text and scheduled == []
    # No more runs while the reopen is owed, and neither worker's pass sends anything yet.
    clock.now += 3600
    assert service.due_checks() == [] and other.due_checks() == []
    # Both workers start a try at once: the claim lets one through, and that one fails.
    other.expire_due()
    service.expire_due()
    assert scheduled == [] and held.id in other._settling and held.id in service._settling
    await asyncio.gather(*other._side_tasks, *service._side_tasks)
    assert len(reopens) == 1 and scheduled == []
    (held,) = service.registry.all()
    assert held.reopen_owed and held.reopen_tries == 1 and not held.reopen_claimed_at
    assert service.next_due() == clock.now + REOPEN_RETRY_S
    await service._reopen_watch(held)  # too soon: not taken
    assert len(reopens) == 1
    clock.now += REOPEN_RETRY_S
    # The other worker claimed it and crashed: this one waits out the claim.
    assert other.registry.claim_reopen(held.id, clock.now) is not None
    await service._reopen_watch(held)
    assert len(reopens) == 1
    clock.now += 600
    await service._reopen_watch(held)
    assert len(reopens) == 2
    repository, number, marker, comment = reopens[-1]
    assert (repository, number) == ("eerovil/agentdeck", 41)
    assert marker == WATCH_MARKER.format(id=request.id) and comment.endswith(marker)
    assert "SENTRY-12 seen again at 14:02" in comment
    # Only what was seen: no command (it may hold a secret), no claim the reopen landed.
    assert "sentry-search" not in comment and "reopened" not in comment
    [message] = scheduled
    assert message.id == f"wake-on:{request.id}"
    assert "says the problem is back" in message.text
    assert "The deck reopened eerovil/agentdeck#41" in message.text
    assert "SENTRY-12 seen again" in message.text and "`failed`" in message.text
    assert service.registry.all() == ()


@pytest.mark.asyncio
async def test_a_watch_ends_quietly_and_only_errors_in_a_row_wake_it(root, scheduled, clock):
    """#4466: the deadline with the problem never back sends nothing; three failed runs in a
    row wake the chat without a reopen; a renewal keeps a reopen owed; with no way to reopen,
    the chat is told to do it; a run started just before the deadline still counts."""
    from agentdeck.wake_on import CHECK_NOT_YET, CHECK_TIMEOUT_S, WATCH_ERRORS_TO_WAKE

    def schedule(message):
        scheduled.append(message)
        return message

    service = WakeOnService(
        root,
        schedule=schedule,
        message_exists=lambda message_id: any(m.id == message_id for m in scheduled),
        clock=clock,
    )
    quiet = _watch(service, after_minutes=150)
    for _ in range(2):
        clock.now += 3600
        (run,) = service.due_checks()
        service.check_done(run, CHECK_NOT_YET, "", "")
    clock.now += 30 * 60
    service.expire_due()  # at its deadline: held while a last run could still be finishing
    assert service.registry.all() and service.due_checks() == []
    assert service.next_due() == quiet.deadline + CHECK_TIMEOUT_S
    clock.now += CHECK_TIMEOUT_S
    service.expire_due()
    assert service.registry.all() == () and scheduled == []

    broken = _watch(service)
    for n in range(WATCH_ERRORS_TO_WAKE):
        clock.now += 3600
        (run,) = service.due_checks()
        service.check_done(run, 2, f"mcp-call: refused {n}", "exit 2")
        assert len(scheduled) == (1 if n == WATCH_ERRORS_TO_WAKE - 1 else 0)
    assert scheduled[0].id == f"wake-on:{broken.id}" != f"wake-on:{quiet.id}"
    assert "failed 3 runs in a row" in scheduled[0].text
    assert "did not reopen" in scheduled[0].text and "refused 2" in scheduled[0].text
    assert service.registry.all() == ()

    found = _watch(service)
    clock.now += 3600
    (run,) = service.due_checks()
    service.check_done(run, 0, "back", "")
    renewed = _watch(service)  # the chat registers it again meanwhile
    assert renewed.id == found.id and renewed.reopen_owed and renewed.check_gen == 1
    clock.now += 14 * 24 * 3600  # past its deadline: still owed, never ended quietly
    service.expire_due()
    assert service.registry.all()[0].reopen_owed
    await service._reopen_watch(renewed)  # this service has no way to reopen
    assert "could not reopen eerovil/agentdeck#41" in scheduled[-1].text
    assert service.registry.all() == ()

    late = _watch(service, after_minutes=61)
    clock.now += 3600
    (run,) = service.due_checks()
    clock.now += 2 * 60  # the run ends past the deadline, and finds it back
    service.expire_due()
    service.check_done(run, 0, "back late", "")
    (held,) = service.registry.all()
    assert held.id == late.id and held.reopen_owed and held.fired_output == "back late"
    clock.now += 4 * 60  # a run that ends after the grace changes nothing
    service.registry.remove([late.id])
    late = _watch(service, after_minutes=61)
    clock.now += 3600
    (run,) = service.due_checks()
    clock.now += 60 + CHECK_TIMEOUT_S
    service.check_done(run, 0, "too late", "")
    assert not service.registry.all()[0].reopen_owed


def test_a_watch_needs_a_deadline_a_check_and_no_worktree():
    """#4466: what a watch body must hold, and that its check stays out of `.worktrees/`,
    which is removed a few days after its Work's issue closes."""
    check = {"command": "sentry-search"}
    fields = parse_request(watch=" the  error ", after_minutes=4320, check=check)
    assert fields["kind"] == "watch" and fields["note"] == "the error"
    for body, words in [
        ({"after_minutes": 4320}, "needs a check"),
        ({"check": check}, "needs its deadline"),
        ({"after_minutes": 60, "at": "11:30", "check": check}, "needs its deadline"),
        ({"after_minutes": 4320, "check": check, "waiting_for": "x"}, "takes only"),
        ({"after_minutes": 4320, "check": check, "commit": f"a/b@{SHA}"}, "takes only"),
        (
            {"after_minutes": 4320, "check": {"command": "cd ~/x/.worktrees/issue-4 && t"}},
            "worktree",
        ),
    ]:
        with pytest.raises(WakeRequestError, match=words):
            parse_request(watch="the error", **body)
    with pytest.raises(WakeRequestError, match="few words"):
        parse_request(watch="  ", after_minutes=4320, check=check)
    with pytest.raises(WakeRequestError, match="watch is at most"):
        parse_request(watch="x" * 81, after_minutes=4320, check=check)


@pytest.mark.asyncio
async def test_a_check_runs_its_shell_command_and_is_stopped_at_its_time_limit(
    tmp_path, monkeypatch
):
    """#4256: the real run, in its cwd, with its exit code and output; one that hangs is
    killed and reported, as is one whose directory has gone."""
    from agentdeck.wake_on import run_check

    code, output, why = await run_check("pwd; echo oops >&2; exit 3", str(tmp_path), 5)
    assert (code, why) == (3, "") and str(tmp_path) in output and "oops" in output
    assert (await run_check("exit 7", str(tmp_path), 5))[::2] == (7, "exit 7")
    code, output, why = await run_check("echo before the hang; sleep 5", str(tmp_path), 0.25)
    assert code is None and "was stopped" in why and "before the hang" in output
    # The guide's pattern: a failed read wakes (exit 1), a clean empty read is not yet.
    pattern = 'out=$({read}) || exit 1; grep -q SENTRY-12 <<<"$out" || exit 3; echo "$out"'
    assert (await run_check(pattern.format(read="false"), "", 5))[0] == 1
    assert (await run_check(pattern.format(read="echo none"), "", 5))[0] == 3
    code, output, _ = await run_check(pattern.format(read="echo SENTRY-12 is back"), "", 5)
    assert code == 0 and "SENTRY-12 is back" in output
    # A noisy command: only the tail is held, and its last line survives.
    code, output, _ = await run_check("yes x | head -c 3000000; echo; echo END", "", 5)
    assert code == 0 and output.rstrip().endswith("END") and len(output) <= 3001
    code, _, why = await run_check("true", str(tmp_path / "gone"), 5)
    assert code is None and "could not start" in why
    # It runs as its chat's account: the web's own CLAUDE_CONFIG_DIR is not passed on.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/web/own")
    _, output, _ = await run_check('echo "[$CLAUDE_CONFIG_DIR]"', "", 5)
    assert output.strip() == "[]"
    account = {"CLAUDE_CONFIG_DIR": "/acct"}
    _, output, _ = await run_check('echo "$CLAUDE_CONFIG_DIR"', "", 5, env=account)
    assert output.strip() == "/acct"
    # #4662: it reaches GitHub as a job does, the caching ``gh`` first on PATH and git
    # through AgentDeck's credential helper.
    monkeypatch.setenv("AGENTDECK_GH_SHIM_DIR", str(tmp_path / "shim"))
    _, output, _ = await run_check('echo "$PATH"; echo "[$GIT_CONFIG_COUNT]"', "", 5)
    first, count = output.splitlines()[:2]
    assert first.split(":")[0] == str(tmp_path / "shim") and count not in {"[]", "[0]"}


ACCOUNT_SERVER = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {}}
    else:
        result = {"content": [{"type": "text", "text": "data of " + sys.argv[1]}]}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


@pytest.mark.asyncio
async def test_a_check_reads_only_its_own_chat_claude_account_mcp_servers(tmp_path):
    """#4256: two Claude accounts each name a ``sentry``; a chat's check, through the real
    ``agentdeck mcp-call``, reads its own account's, and a Codex chat's or an unknown
    account's check is refused rather than reading the web user's default Claude config."""
    import sys

    from agentdeck.wake_on import check_account_env, run_check

    script = tmp_path / "server.py"
    script.write_text(ACCOUNT_SERVER)
    accounts = []
    for provider, label in (("claude_code", "main"), ("claude_code", "alt"), ("codex", "cx")):
        root = tmp_path / label
        root.mkdir()
        server = {"command": sys.executable, "args": [str(script), label]}
        (root / ".claude.json").write_text(json.dumps({"mcpServers": {"sentry": server}}))
        accounts.append(SimpleNamespace(provider=provider, label=label, root=root))
    home = tmp_path / "home"
    home.mkdir()
    default = {"command": sys.executable, "args": [str(script), "default"]}
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"sentry": default}}))
    config = SimpleNamespace(accounts=accounts)
    # The real ``agentdeck mcp-call`` entry, without the CLI's slower start-up.
    call = "import sys; from agentdeck.mcp_call import main; raise SystemExit(main(sys.argv[1:]))"
    command = f"HOME={home} {sys.executable} -c '{call}' sentry search"

    run = await run_check(command, "", 20, env=check_account_env(config, "claude_code:alt:a1"))
    assert run[0] == 0 and run[1].strip() == "data of alt"
    codex = check_account_env(config, "codex:cx:t1")
    code, output, why = await run_check(command, "", 20, env=codex)
    assert (code, why) == (2, "exit 2") and "no Claude account" in output
    assert "data of" not in output
    main = check_account_env(config, "claude_code:main:a2")
    assert main["AGENTDECK_MCP_CONFIG"] == str(tmp_path / "main" / ".claude.json")
    run = await run_check(command, "", 20, env=main)
    assert run[0] == 0 and run[1].strip() == "data of main"
    # An account the config no longer names, a malformed key, and no account at all are
    # refused like the Codex chat: nothing falls back to the web user's own config.
    for key in ("claude_code:gone:a3", "bogus"):
        assert check_account_env(config, key)["AGENTDECK_MCP_CONFIG"] == ""
    code, output, _ = await run_check(command, "", 20)
    assert code == 2 and "no Claude account" in output

def test_a_time_wait_at_a_clock_time_takes_its_next_occurrence():
    tz = timezone(timedelta(hours=3))
    now = datetime(2026, 10, 13, 10, 0, tzinfo=tz)
    assert parse_request(at="11:30", now=now)["timeout_s"] == 90 * 60
    # A time already past today is tomorrow's.
    assert parse_request(at="9.30", now=now)["timeout_s"] == (23 * 60 + 30) * 60
    assert parse_request(at="2026-10-13T12:00:00+03:00", now=now)["timeout_s"] == 2 * 3600
    # An ISO time without an offset is on this host's clock, like a bare 11:30.
    assert parse_request(at="2026-10-13T10:30", now=now)["timeout_s"] == 30 * 60
    with pytest.raises(WakeRequestError):
        parse_request(at="2026-10-13T09:00:00+03:00", now=now)
    plain = parse_request(after_minutes=5, now=now)
    assert plain["note"] == "" and plain["kind"] == "time"


def test_two_time_waits_for_different_work_are_kept_apart(service):
    first = register(service, after_minutes=30, waiting_for="the render")
    second = register(service, after_minutes=60, waiting_for="the build")
    again = register(service, after_minutes=45, waiting_for="The Render")
    assert again.id == first.id
    assert {item.id for item in service.registry.all()} == {first.id, second.id}


def test_the_next_step_request_names_the_time_wait():
    from agentdeck.work_endings import NUDGE_TEXT

    text = NUDGE_TEXT.format(ref="eerovil/agentdeck#2968")
    assert "agentdeck job" in text
    assert '{"at": "11:30", "waiting_for": "the render"}' in text


def test_the_session_guide_offers_the_wait_next_to_the_no_polling_rule():
    for role in ("primary", "delegated", "work"):
        flat = " ".join(agentdeck_session_instructions(provider="claude_code", role=role).split())
        assert "`POST $AGENTDECK_URL/api/sessions/<session-key>/wake-on`" in flat
        assert flat.index("Never wait by polling") < flat.index("/wake-on")
        # The bodies are served on request since #4459.
        assert "`GET $AGENTDECK_URL/api/guide/waits`" in flat
    waits = " ".join(guide_topics()["waits"].split())
    assert "`POST $AGENTDECK_URL/api/sessions/<session-key>/wake-on`" in waits
    assert '{"pull_request": "<owner/repo#number>"}' in waits
    assert '{"after_minutes": 90}' in waits
    assert '{"needs_you": "all"}' in waits
    assert '{"deck_deploy": "<sha>"}' in waits and "restart-nudge" in waits
    # #4256: a time wait's check, its "not yet" exit, and the MCP tool command.
    assert '"check": {"command": "<shell>", "every_minutes": 60}' in waits
    assert "agentdeck mcp-call <server> <tool>" in waits
    guide_pattern = "out=$(<read>) || exit 1; grep -q '<what you wait for>' <<<\"$out\""
    assert guide_pattern + " || exit 3" in waits
    # #4466: the watch body, and that its check stays out of a Work's worktree.
    assert '{"watch": "<what would be back, in a few words>", "after_minutes": 4320,' in waits
    assert "record the live check `verified` and close the issue" in waits
    assert "Keep the command and its `\"cwd\"` out of a Work's worktree" in waits


def test_a_finished_check_is_seen_in_each_ci_shape_until_it_runs_again(root, monkeypatch):
    """#3863: a commit wait registered after its check finished is answered at once, for a
    check suite, a check run and a workflow run; a re-run since, an unfinished and a finished
    state at one time, or a delivery with no time keeps the wait."""
    from agentdeck import github_webhook_log
    from agentdeck.wake_on import KIND_COMMIT, WakeRequest

    # Durability is not under test, and an fsync per delivery is most of the test's time.
    monkeypatch.setattr(github_webhook_log.os, "fsync", lambda _fd: None)

    def run(event, n, *, done, at, name="ci", **extra):
        body = {"head_sha": SHA, "status": "completed" if done else "in_progress", **extra}
        if done:
            body["conclusion"] = "success"
        if event == "check_suite":
            body.update(app={"name": name}, updated_at=at)
        elif event == "check_run":
            body.update(name=name, **({"completed_at": at} if done else {"started_at": at}))
        else:
            body.update(name=name, updated_at=at)
        action = "completed" if done else ("rerequested" if event == "check_suite" else "requested")
        deliver(root, event=event, delivery=f"{event}{n}", action=action, **{event: body})

    def seen(name):
        wait = WakeRequest(
            id="",
            session_key="s",
            chat_id="c",
            kind=KIND_COMMIT,
            repository="eerovil/agentdeck",
            sha=SHA[:12],
            name=name,
        )
        return seen_result(root, wait)

    t = "2026-10-29T10:0{}:00Z".format
    for event in ("check_suite", "check_run", "workflow_run"):
        run(event, 1, done=True, at=t(1), name=f"{event} green")
        # Re-run at 10:05, its old success delivered after it.
        run(event, 2, done=False, at=t(5), name=f"{event} again")
        run(event, 3, done=True, at=t(1), name=f"{event} again")
        found = seen(f"{event} green")
        assert found is not None and found.summary.endswith("finished: success"), event
        assert seen(f"{event} again") is None, event
    run("check_run", 4, done=True, at=t(1), name="tie")
    run("check_run", 5, done=False, at=t(1), name="tie")
    run("check_run", 6, done=True, at=t(1), name="untimed")
    run("check_run", 7, done=False, at="", name="untimed")
    assert seen("tie") is None and seen("untimed") is None


@pytest.mark.anyio
async def test_the_api_tells_a_named_deploy_check_run_already_ended(tmp_path):
    """#4662: a named deploy on a commit that already ended as a check run is told at
    registration: a success keeps no wait, a failure is said and the wait is kept for a deploy
    run again, and a wait with no commit is about later deploys only."""
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        restart_nudge=RestartNudgeConfig(file=str(tmp_path / "restart-nudge.json")),
        github_webhook=GitHubWebhookConfig(state_dir=str(tmp_path / "github-webhooks")),
        accounts=[
            AccountConfig(provider="claude_code", label="alt", config_dir=str(tmp_path / "alt"))
        ],
    )
    app = create_app(config)
    key = "claude_code:alt:fresh"
    streams = tmp_path / "github-webhooks"
    name = "deploy-cloudflare"
    deploy_run(streams, "d1", "success", "2026-11-25T10:00:00Z", name=name)
    deploy_run(streams, "d2", "failure", "2026-11-25T10:00:00Z", sha="b" * 40, name=name)
    app.state.app_state.session_key_has_token = lambda candidate: candidate == key
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/sessions/{key}/wake-on"
        done = await client.post(url, json={"deployment": f"eerovil/agentdeck@{SHA}", "name": name})
        failed = await client.post(
            url, json={"deployment": f"eerovil/agentdeck@{'b' * 40}", "name": name}
        )
        later = await client.post(url, json={"deployment": "eerovil/agentdeck", "name": name})
        held = (await client.get(url)).json()
    assert done.json()["already_happened"] is True
    assert "finished: success" in done.json()["result"]
    assert "finished: failure" in failed.json()["already_failed"]
    assert not failed.json().get("already_happened")
    assert not later.json().get("already_happened") and "already_failed" not in later.json()
    ids = {wait["id"] for wait in held["waits"]}
    assert ids == {failed.json()["registered"]["id"], later.json()["registered"]["id"]}


@pytest.mark.anyio
async def test_the_api_registers_lists_and_cancels_a_wait(tmp_path, monkeypatch):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        restart_nudge=RestartNudgeConfig(file=str(tmp_path / "restart-nudge.json")),
        github_webhook=GitHubWebhookConfig(state_dir=str(tmp_path / "github-webhooks")),
        accounts=[
            AccountConfig(provider="claude_code", label="alt", config_dir=str(tmp_path / "alt"))
        ],
    )
    app = create_app(config)
    key = "claude_code:alt:fresh"
    # #3584: the deploys already in a repository's stream, filed under GitHub's spelling.
    abacus = Repository(owner="Northwind", name="abacus")
    for n, (environment, state, sha, at) in enumerate(
        [
            ("prod", "success", SHA, "2026-10-26T10:00:00Z"),
            ("staging", "success", "f" * 40, "2026-10-26T11:00:00Z"),
            ("prod", "in_progress", "e" * 40, "2026-10-26T12:00:00Z"),
        ]
    ):
        deliver(
            tmp_path / "github-webhooks",
            event="deployment_status",
            delivery=f"dep{n}",
            repository=abacus,
            deployment_status={"state": state, "environment": environment, "updated_at": at},
            deployment={"sha": sha, "environment": environment},
        )
    # #3863: drone went green on SHA, and a second CI wait on it was kept open. A check
    # re-run since (``pending`` again) is still waited on, even when GitHub delivered the
    # older success after it: the status's own time orders them, not arrival.
    for n, (context, state, at) in enumerate(
        [
            ("drone/push", "pending", "10:00"),
            ("drone/push", "success", "10:05"),
            ("drone/pr", "pending", "10:09"),
            ("drone/pr", "success", "10:05"),
        ]
    ):
        deliver(
            tmp_path / "github-webhooks",
            event="status",
            delivery=f"ci{n}",
            repository=abacus,
            sha=SHA,
            context=context,
            state=state,
            updated_at=f"2026-10-29T{at}:00Z",
        )
    # A worker registers from its token before the deck has scanned its transcript.
    app.state.app_state.session_key_has_token = lambda candidate: candidate == key
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(f"/api/sessions/{key}/wake-on", json={"commit": "x"})
        deployed = await client.post(
            f"/api/sessions/{key}/wake-on",
            json={"deployment": f"northwind/abacus@{SHA[:10]}", "name": "prod"},
        )
        not_there = await client.post(
            f"/api/sessions/{key}/wake-on",
            json={"deployment": f"Northwind/abacus@{SHA[:10]}", "name": "staging"},
        )
        green = await client.post(
            f"/api/sessions/{key}/wake-on",
            json={"commit": f"northwind/abacus@{SHA}", "name": "drone/push"},
        )
        rerun = await client.post(
            f"/api/sessions/{key}/wake-on",
            json={"commit": f"northwind/abacus@{SHA}", "name": "drone/pr"},
        )
        await client.delete(
            f"/api/sessions/{key}/wake-on", params={"id": rerun.json()["registered"]["id"]}
        )
        await client.delete(
            f"/api/sessions/{key}/wake-on", params={"id": not_there.json()["registered"]["id"]}
        )
        made = await client.post(
            f"/api/sessions/{key}/wake-on",
            json={"pull_request": "eerovil/agentdeck#7", "timeout_minutes": 30},
        )
        # #3849: on a deck with no Work reviewer a review wait could only time out.
        app.state.work_router.reviewer_off = True
        no_reviewer = await client.post(
            f"/api/sessions/{key}/wake-on", json={"review": "eerovil/agentdeck#7"}
        )
        app.state.work_router.reviewer_off = False
        assert no_reviewer.status_code == 409 and "no Work reviewer" in no_reviewer.text
        timed = await client.post(
            f"/api/sessions/{key}/wake-on",
            json={
                "after_minutes": 45,
                "waiting_for": "the render",
                "work_id": "eerovil/agentdeck#7",
                "check": {"command": "test -f done || exit 3", "every_minutes": 10},
            },
        )
        # #4272: the Work the wait is for, in one spelling.
        assert timed.json()["registered"]["work_id"] == "github_issue:eerovil/agentdeck#7"
        # #4256: the check rides the time wait and comes back in the reply.
        assert timed.json()["registered"]["check"]["every_minutes"] == 10
        # #4466: a watch reopens one Work's issue; this chat is in none, so it must name it.
        unnamed = await client.post(
            f"/api/sessions/{key}/wake-on",
            json={"watch": "the error", "after_minutes": 4320, "check": {"command": "true"}},
        )
        assert unnamed.status_code == 422 and "name it with work_id" in unnamed.text
        watched = await client.post(
            f"/api/sessions/{key}/wake-on",
            json={
                "watch": "the error",
                "after_minutes": 4320,
                "work_id": "eerovil/agentdeck#7",
                "check": {"command": "true"},
            },
        )
        assert watched.json()["registered"]["kind"] == "watch"
        assert watched.json()["registered"]["work_id"] == "github_issue:eerovil/agentdeck#7"
        await client.delete(
            f"/api/sessions/{key}/wake-on", params={"id": watched.json()["registered"]["id"]}
        )
        # A chat directly in one Work watches that one; in two, it must say which.
        works = {"one": ["github_issue:eerovil/agentdeck#9"]}
        works["two"] = [*works["one"], "github_issue:eerovil/agentdeck#10"]
        state = app.state.app_state
        for shape, status in (("one", 200), ("two", 422)):
            state.work_projection = lambda shape=shape: SimpleNamespace(
                direct_works_for_chat=lambda chat: (
                    works[shape] if chat == state.chat_identity_key(key) else ()
                )
            )
            own = await client.post(
                f"/api/sessions/{key}/wake-on",
                json={"watch": "the error", "after_minutes": 4320, "check": {"command": "true"}},
            )
            assert own.status_code == status, own.text
        assert "directly in 2 Works" in own.text
        state.work_projection = lambda: SimpleNamespace(
            direct_works_for_chat=lambda chat: works["one"]
        )
        own = await client.post(
            f"/api/sessions/{key}/wake-on",
            json={"watch": "the error", "after_minutes": 4320, "check": {"command": "true"}},
        )
        assert own.json()["registered"]["work_id"] == "github_issue:eerovil/agentdeck#9"
        del state.work_projection
        await client.delete(
            f"/api/sessions/{key}/wake-on", params={"id": own.json()["registered"]["id"]}
        )
        bad_check = await client.post(
            f"/api/sessions/{key}/wake-on",
            json={"after_minutes": 45, "check": {"command": "true", "every_minutes": 50}},
        )
        assert bad_check.status_code == 422 and "shorter than the wait" in bad_check.text
        await client.delete(
            f"/api/sessions/{key}/wake-on", params={"id": timed.json()["registered"]["id"]}
        )
        # #3183: a fresh review of a pull request this chat registered, and its wait.
        unregistered = await client.post(
            f"/api/sessions/{key}/review-request", json={"pull_request": "eerovil/agentdeck#9"}
        )
        asks = []

        async def ask_again(repository, number, note="", *, no_verdict=False):
            asks.append((repository, number, note, no_verdict))
            return {"pull_request": f"{repository}#{number}", "outcome": "asked"}

        async def refuse(repository, number, note="", *, no_verdict=False):
            raise AgainRefused("already reviewed again twice")

        app.state.work_router = SimpleNamespace(enabled=True, ask_again=refuse)
        app.state.app_state.add_pull_request(key, PullRequestRef.parse("eerovil/agentdeck#9"))
        # #3344: a blank note is refused before a wait is registered.
        blank = await client.post(
            f"/api/sessions/{key}/review-request",
            json={"pull_request": "eerovil/agentdeck#9", "note": "  "},
        )
        after_blank = await client.get(f"/api/sessions/{key}/wake-on")
        # #3849: with no reviewer, said before the note is even read.
        app.state.work_router = SimpleNamespace(enabled=True, ask_again=refuse, reviewer_off=True)
        no_reviewer = await client.post(
            f"/api/sessions/{key}/review-request", json={"pull_request": "eerovil/agentdeck#9"}
        )
        assert no_reviewer.status_code == 409 and "no Work reviewer" in no_reviewer.text
        app.state.work_router = SimpleNamespace(enabled=True, ask_again=refuse)
        capped = await client.post(
            f"/api/sessions/{key}/review-request",
            json={"pull_request": "eerovil/agentdeck#9", "note": "once more"},
        )
        after_cap = await client.get(f"/api/sessions/{key}/wake-on")

        async def queued(repository, number, note="", *, no_verdict=False):
            raise AgainBusy("a review is already asked for; keep waiting")

        # #3453 review: a review already queued keeps the wait, and is not a refusal.
        app.state.work_router = SimpleNamespace(enabled=True, ask_again=queued)
        busy = await client.post(
            f"/api/sessions/{key}/review-request",
            json={"pull_request": "eerovil/agentdeck#9", "note": "no verdict", "no_verdict": True},
        )
        after_busy = await client.get(f"/api/sessions/{key}/wake-on")

        async def stopped(repository, number, note="", *, no_verdict=False):
            # #4658: a start that stopped on a carried pass ended the wait meanwhile; the
            # request's review needs one, so the reply names a wait that is live.
            registry = app.state.wake_on.registry
            registry.remove([item.id for item in registry.all() if item.kind == "review"])
            return {"pull_request": f"{repository}#{number}", "outcome": "pending"}

        app.state.work_router = SimpleNamespace(enabled=True, ask_again=stopped)
        renewed = await client.post(
            f"/api/sessions/{key}/review-request",
            json={"pull_request": "eerovil/agentdeck#9", "note": "the owner decided"},
        )
        after_renewed = await client.get(f"/api/sessions/{key}/wake-on")
        accepts = []

        async def accept(repository, number, note):
            accepts.append((repository, number, note))
            if number != 9:
                raise AgainRefused("never sent for review")
            return {"pull_request": f"{repository}#{number}", "outcome": "review pass recorded"}

        async def pre_review_done(repository, number):
            if number != 9:
                raise AgainRefused("no pre-review checklist waits for it")
            return {"pull_request": f"{repository}#{number}", "outcome": "asked"}

        from agentdeck.services_link import ServicesUncertain

        ships = []

        async def ship_now(repository, number, head=""):
            ships.append((number, head))
            if number == 10:
                return {"pull_request": f"{repository}#10", "head": head, "state": "running"}
            if number == 11:
                raise ServicesUncertain()
            if number != 9:
                raise AgainRefused("its review has not come back with findings")
            return {"pull_request": f"{repository}#{number}", "head": head, "outcome": "shipped"}

        app.state.work_router = SimpleNamespace(
            enabled=True,
            ask_again=ask_again,
            accept=accept,
            pre_review_done=pre_review_done,
            ship_now=ship_now,
            note_registered=lambda repository, number: None,
        )
        app.state.app_state.add_pull_request(key, PullRequestRef.parse("eerovil/agentdeck#9"))
        # #3828: the owner's acceptance passes the head with no review and no wait.
        accepted = await client.post(
            f"/api/sessions/{key}/review-accepted",
            json={"pull_request": "eerovil/agentdeck#9", "note": " accept as is "},
        )
        # #3880: the agent went through its pre-review checklist; an unregistered one is refused.
        checked = await client.post(
            f"/api/sessions/{key}/pre-review-done", json={"pull_request": "eerovil/agentdeck#9"}
        )
        checked_other = await client.post(
            f"/api/sessions/{key}/pre-review-done", json={"pull_request": "eerovil/agentdeck#8"}
        )
        assert checked.status_code == 200 and checked.json()["outcome"] == "asked"
        assert checked_other.status_code == 409 and "register it first" in checked_other.text
        # #4579: the agent reports a deck bug; the deck names its Work, chat and the instance.
        from agentdeck import issue_post
        from agentdeck.poller_engine import bug_reports
        from agentdeck.work import WorkRef

        report = {
            "topic": "pre_review_wrong",
            "expected": "the minor verdict carries the pass to this head",
            "observed": "the deck sent a pre-review checklist and held the merge",
            "pull_request": "eerovil/agentdeck#9",
        }
        monkeypatch.setattr(issue_post, "issue_post_target", lambda state, chat: None)
        no_work = await client.post(f"/api/sessions/{key}/deck-bug", json=report)
        monkeypatch.setattr(
            issue_post, "issue_post_target", lambda state, chat: WorkRef("eerovil/agentdeck", 7)
        )
        state_path = tmp_path / "poller" / "state.json"
        scope = SimpleNamespace(org="eerovil", state_path=str(state_path))
        app.state.issues = SimpleNamespace(
            read_board=lambda: SimpleNamespace(managing=lambda repository: (scope,))
        )
        unregistered = await client.post(
            f"/api/sessions/{key}/deck-bug", json={**report, "pull_request": "eerovil/x#1"}
        )
        terse = await client.post(f"/api/sessions/{key}/deck-bug", json={**report, "observed": "x"})
        guessed = await client.post(f"/api/sessions/{key}/deck-bug", json={"bug": "it broke"})
        # Every malformed report says the whole body (#4579 review): a bad pull request, a
        # field that is not text, and a body that is no JSON object.
        path = f"/api/sessions/{key}/deck-bug"
        malformed = [
            await client.post(path, json={**report, "pull_request": "x"}),
            await client.post(path, json={**report, "topic": 5}),
            await client.post(path, json=["not", "an", "object"]),
            await client.post(path, content=b"{broken"),
        ]
        for answer in malformed:
            assert answer.status_code == 422 and "GET this path" in answer.json()["detail"]
        bare = {name: value for name, value in report.items() if name != "pull_request"}
        unnamed = await client.post(f"/api/sessions/{key}/deck-bug", json=bare)
        filed = await client.post(f"/api/sessions/{key}/deck-bug", json=report)
        listed = await client.get(f"/api/sessions/{key}/deck-bug")
        assert no_work.status_code == 409 and unregistered.status_code == 409
        assert terse.status_code == 422 and "at least 20" in terse.text
        # A guessed body learns the whole one, and the GET to try first (#4579 helper checks).
        told = guessed.json()["detail"]
        assert guessed.status_code == 422 and '"observed"' in told and "GET this path" in told
        assert "usage" in listed.json()
        # A chat with a pull request names it or its head: the report's evidence.
        assert unnamed.status_code == 422 and "`pull_request` or `head`" in unnamed.text
        assert filed.status_code == 200 and filed.json()["state"] == "filed"
        waiting = json.loads(next(bug_reports.directory(state_path).glob("*.json")).read_text())
        assert (waiting["cause"], waiting["work_id"], waiting["session_key"]) == (
            "agent_report:pre_review_wrong", "github_issue:eerovil/agentdeck#7", key
        )
        assert waiting["pull_request"] == "eerovil/agentdeck#9"
        assert listed.json()["topics"] == [
            {"topic": "pre_review_wrong", "issue": None, "waiting": True}
        ]
        # #4212: the owner picked "Ship now, fix later" in the agent's question, at the head
        # whose findings it showed (review): a head that is not a full commit id ships nothing.
        # What may ship is the chat's Work's pull requests (`_work_pulls_of_chat`, tested in
        # test_work_router); here, the ones this chat registered.
        from agentdeck.web import routes_api

        monkeypatch.setattr(
            routes_api,
            "_work_pulls_of_chat",
            lambda state, chat: {
                f"{ref.repository}#{ref.number}".lower() for ref in state.pull_requests_for(chat)
            },
        )
        headless = await client.post(
            f"/api/sessions/{key}/ship-now", json={"pull_request": "eerovil/agentdeck#9"}
        )
        assert headless.status_code == 422 and "full commit id" in headless.text
        shipped = await client.post(
            f"/api/sessions/{key}/ship-now",
            json={"pull_request": "eerovil/agentdeck#9", "head": "C" * 40},
        )
        assert shipped.status_code == 200 and shipped.json()["outcome"] == "shipped"
        assert ships == [(9, "c" * 40)]
        not_mine = await client.post(
            f"/api/sessions/{key}/ship-now",
            json={"pull_request": "eerovil/agentdeck#8", "head": "c" * 40},
        )
        assert not_mine.status_code == 409 and "register it first" in not_mine.text
        # Every pull request the question showed ships, in order, as the button ships them
        # all (review); a refused one stops the rest and is named.
        app.state.app_state.add_pull_request(key, PullRequestRef.parse("eerovil/agentdeck#8"))
        both = await client.post(
            f"/api/sessions/{key}/ship-now",
            json={
                "pull_requests": [
                    {"pull_request": "eerovil/agentdeck#9", "head": "d" * 40},
                    {"pull_request": "eerovil/agentdeck#8", "head": "e" * 40},
                ]
            },
        )
        assert both.status_code == 200, both.text
        assert [item["head"] for item in both.json()["shipped"]] == ["d" * 40]
        assert both.json()["not_shipped"] == ["eerovil/agentdeck#8"]
        assert "findings" in both.json()["refused"]
        assert ships[-2:] == [(9, "d" * 40), (8, "e" * 40)]
        assert "same pull request and head is safe" in both.json()["next"]
        # #4558: a press still running stops the rest, named as not started, and the reply
        # says what the agent may do next; a merge service past the web's wait says a repeat
        # is safe. A recorded one says it is not merged yet.
        assert "not merged yet" in shipped.json()["next"]
        app.state.app_state.add_pull_request(key, PullRequestRef.parse("eerovil/agentdeck#10"))
        app.state.app_state.add_pull_request(key, PullRequestRef.parse("eerovil/agentdeck#11"))
        slow = await client.post(
            f"/api/sessions/{key}/ship-now",
            json={
                "pull_requests": [
                    {"pull_request": "eerovil/agentdeck#9", "head": "d" * 40},
                    {"pull_request": "eerovil/agentdeck#10", "head": "d" * 40},
                    {"pull_request": "eerovil/agentdeck#8", "head": "e" * 40},
                ]
            },
        )
        assert slow.status_code == 200, slow.text
        assert slow.json()["state"] == "running" and ships[-1] == (10, "d" * 40)
        # The top of the reply is the running one, not the first, already shipped (#4559).
        assert slow.json()["pull_request"] == "eerovil/agentdeck#10"
        assert len(slow.json()["shipped"]) == 2
        assert slow.json()["not_started"] == ["eerovil/agentdeck#8"]
        assert "Do not poll" in slow.json()["next"] and "not_started" in slow.json()["next"]
        stuck = await client.post(
            f"/api/sessions/{key}/ship-now",
            json={"pull_request": "eerovil/agentdeck#11", "head": "d" * 40},
        )
        assert stuck.status_code == 504 and "same pull request and head is safe" in stuck.text
        # Stuck after one shipped: the shipped one is still named (#4559 helper checks).
        late = await client.post(
            f"/api/sessions/{key}/ship-now",
            json={
                "pull_requests": [
                    {"pull_request": "eerovil/agentdeck#9", "head": "d" * 40},
                    {"pull_request": "eerovil/agentdeck#11", "head": "d" * 40},
                    {"pull_request": "eerovil/agentdeck#8", "head": "e" * 40},
                ]
            },
        )
        assert late.status_code == 200, late.text
        assert late.json()["pull_request"] == "eerovil/agentdeck#9"
        assert late.json()["uncertain"] == "eerovil/agentdeck#11"
        assert late.json()["not_shipped"] == ["eerovil/agentdeck#11", "eerovil/agentdeck#8"]
        assert "same pull request and head is safe" in late.json()["next"]
        for number in (8, 10, 11):
            app.state.app_state.remove_pull_request(
                key, PullRequestRef.parse(f"eerovil/agentdeck#{number}")
            )
        # The fix of a shipped head is named when it is registered (#4212 review), and only for
        # a pull request that shipped for the Work the fix is for.
        monkeypatch.setattr(
            routes_api, "_pull_work", lambda *args: "github_issue:eerovil/agentdeck#7"
        )
        pull_states = {"eerovil/agentdeck#11": "OPEN", "eerovil/agentdeck#12": "MERGED"}

        async def pull_state(ref):
            return pull_states.get(ref, "")

        monkeypatch.setattr(routes_api, "_pull_state", pull_state)
        stranger = await client.post(
            f"/api/sessions/{key}/pull-requests",
            json={"pull_request": "eerovil/agentdeck#11", "fixes": "eerovil/agentdeck#9"},
        )
        assert stranger.status_code == 409 and "did not ship" in stranger.text
        assert "eerovil/agentdeck#11" not in {
            f"{ref.repository}#{ref.number}" for ref in app.state.app_state.pull_requests_for(key)
        }
        state = app.state.app_state
        assert state.claim_shipped("eerovil/agentdeck#9", "c" * 40, "eerovil/agentdeck#7")
        assert state.note_shipped("eerovil/agentdeck#9", "c" * 40)
        fixing = await client.post(
            f"/api/sessions/{key}/pull-requests",
            json={"pull_request": "eerovil/agentdeck#11", "fixes": "EEROVIL/agentdeck#9"},
        )
        assert fixing.status_code == 200, fixing.text
        [record] = state.shipped_by_work("eerovil/agentdeck#7")
        assert record.fix == "eerovil/agentdeck#11"
        # Neither the shipped pull request itself nor one already merged can be its fix.
        own = await client.post(
            f"/api/sessions/{key}/pull-requests",
            json={"pull_request": "eerovil/agentdeck#9", "fixes": "eerovil/agentdeck#9"},
        )
        assert own.status_code == 409 and "its own findings" in own.text
        merged = await client.post(
            f"/api/sessions/{key}/pull-requests",
            json={"pull_request": "eerovil/agentdeck#12", "fixes": "eerovil/agentdeck#9"},
        )
        assert merged.status_code == 409 and "MERGED" in merged.text
        [record] = state.shipped_by_work("eerovil/agentdeck#7")
        assert record.fix == "eerovil/agentdeck#11"
        app.state.app_state.remove_pull_request(key, PullRequestRef.parse("eerovil/agentdeck#11"))
        blank_accept = await client.post(
            f"/api/sessions/{key}/review-accepted",
            json={"pull_request": "eerovil/agentdeck#9", "note": " "},
        )
        # #4797: the owner left #9's Ship now findings as shipped. Once it merged, its acceptance
        # records that no fix follows and asks the router for no pass: it has one.
        # Registered for no Work by name, it is the chat's own Work's (``_pull_work``).
        assert not next(
            ref for ref in state.pull_requests_for(key) if ref.number == 9
        ).work_id
        # Still open, it is the usual acceptance: findings at a newer head need their pass.
        open_accept = await client.post(
            f"/api/sessions/{key}/review-accepted",
            json={"pull_request": "eerovil/agentdeck#9", "note": "accept the new findings"},
        )
        assert open_accept.status_code == 200 and "declined" not in open_accept.text
        assert state.shipped_by_work("eerovil/agentdeck#7")[0].fix == "eerovil/agentdeck#11"
        accepts.pop()
        pull_states["eerovil/agentdeck#9"] = "MERGED"
        from agentdeck.state import REVIEWS_TOPIC

        drawn = state.bus.revision(REVIEWS_TOPIC)
        declined = await client.post(
            f"/api/sessions/{key}/review-accepted",
            json={"pull_request": "eerovil/agentdeck#9", "note": "leave it as shipped"},
        )
        assert declined.status_code == 200, declined.text
        # The Work page redraws: its Shipped line for #9 goes.
        assert state.bus.revision(REVIEWS_TOPIC) > drawn
        assert declined.json()["outcome"].startswith("fix declined")
        [record] = state.shipped_by_work("eerovil/agentdeck#7")
        assert record.fix == "declined"
        after_accept = await client.get(f"/api/sessions/{key}/wake-on")
        # #4016: the request crosses the services socket to the router and back, as it does
        # once the router runs in agentdeck-services.service.
        import httpx

        from agentdeck import services_link
        from agentdeck.services_process import create_services_app

        services_app = create_services_app(app.state.config, watchdog=False)
        crossed = []

        class Services:
            async def call(self, target, method, args, kwargs):
                crossed.append((target, method))
                if method == "recover_review":  # #4478: a timed-out wait's read-back
                    return f"https://x/{args[1]}"
                return await ask_again(*args, **kwargs)

        services_app.state.services = Services()
        app.state.work_router = services_link.RouterLink(
            app.state.config,
            services_link.SocketTransport(
                lambda: tmp_path / "services.sock", transport=httpx.ASGITransport(services_app)
            ),
            app.state.app_state,
        )
        app.state.work_router.reviewer_off = False
        again = await client.post(
            f"/api/sessions/{key}/review-request",
            json={
                "pull_request": "eerovil/agentdeck#9",
                "note": " no verdict ",
                "no_verdict": True,
            },
        )
        await client.delete(
            f"/api/sessions/{key}/wake-on", params={"id": again.json()["registered"]["id"]}
        )
        recovered = await app.state.wake_on._recover_review("eerovil/agentdeck", 9)
        listed = await client.get(f"/api/sessions/{key}/wake-on")
        cancelled = await client.delete(
            f"/api/sessions/{key}/wake-on", params={"id": made.json()["registered"]["id"]}
        )
    assert refused.status_code == 422
    latest = [
        {"environment": "prod", "state": "in_progress", "sha": "e" * 40},
        {"environment": "staging", "state": "success", "sha": "f" * 40},
    ]
    assert deployed.status_code == 200, deployed.text
    assert deployed.json()["already_happened"] is True and "registered" not in deployed.json()
    assert deployed.json()["deployment"]["sha"] == SHA
    for reply in (deployed, not_there):
        assert [
            {k: v for k, v in item.items() if k != "time"} for item in reply.json()["latest"]
        ] == latest
    assert "already_happened" not in not_there.json()
    assert green.json()["already_happened"] is True and "registered" not in green.json()
    assert green.json()["result"].startswith("commit status drone/push on northwind/abacus@")
    assert green.json()["result"].endswith("finished: success")
    assert rerun.json()["registered"]["kind"] == "commit"
    assert not_there.json()["registered"]["kind"] == "deployment"
    assert unregistered.status_code == 409
    assert blank.status_code == 422 and "what changed" in blank.text
    assert [item["kind"] for item in after_blank.json()["waits"]] == ["pull_request"]
    assert capped.status_code == 409 and "twice" in capped.text
    assert [item["kind"] for item in after_cap.json()["waits"]] == ["pull_request"]
    assert busy.status_code == 200 and busy.json()["outcome"] == "already asked", busy.text
    assert [item["kind"] for item in after_busy.json()["waits"]] == ["pull_request", "review"]
    assert busy.json()["registered"]["id"] == after_busy.json()["waits"][-1]["id"]
    assert renewed.json()["registered"]["id"] == after_renewed.json()["waits"][-1]["id"]
    assert renewed.json()["registered"]["id"] != busy.json()["registered"]["id"]
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["outcome"] == "review pass recorded"
    assert blank_accept.status_code == 422 and "decision" in blank_accept.text
    assert accepts == [("eerovil/agentdeck", 9, "accept as is")]
    assert [item["kind"] for item in after_accept.json()["waits"]] == ["pull_request", "review"]
    assert again.status_code == 200, again.text
    assert asks == [("eerovil/agentdeck", 9, "no verdict", True)]
    assert crossed == [("router", "ask_again"), ("router", "recover_review")]
    assert recovered == "https://x/9"
    assert again.json()["registered"]["kind"] == "review"
    assert made.status_code == 200, made.text
    wait = made.json()["registered"]
    assert wait["waiting_for"] == "pull request eerovil/agentdeck#7"
    assert timed.status_code == 200, timed.text
    assert timed.json()["registered"]["waiting_for"] == "the render"
    assert timed.json()["registered"]["kind"] == "time"
    assert wait["deadline"] - wait["registered_at"] == pytest.approx(30 * 60)
    assert listed.json()["waits"] == [wait]
    assert cancelled.json()["waits"] == []
    assert (tmp_path / "wake-on.json").exists()


@pytest.mark.asyncio
async def test_a_failing_deadline_pass_backs_off_instead_of_spinning(service, monkeypatch):
    """#2865: a pass that keeps raising leaves ``next_due`` in the past; the loop waits out a
    back-off rather than running again at once on the event loop."""
    import asyncio

    from agentdeck import watch_retry

    monkeypatch.setattr(watch_retry, "RETRY_MIN_S", 0.1)
    calls = {"n": 0}

    def failing():
        calls["n"] += 1
        raise OSError("registry not writable")

    monkeypatch.setattr(service, "expire_due", failing)
    monkeypatch.setattr(service, "next_due", lambda: 0.0)
    task = asyncio.create_task(service._deadlines())
    try:
        await asyncio.sleep(0.25)
        assert 1 <= calls["n"] <= 3
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# A deployment wait follows its commit (#4445) -------------------------------------------

LATER = "1234567890abcdef1234567890abcdef12345678"
PROMOTE = "continuous-integration/drone/promote/production"


def _at(clock, offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(clock.now + offset_s, UTC).isoformat()


def drone_status(root, delivery, sha, context, state, at):
    deliver(root, event="status", delivery=delivery, sha=sha, context=context, state=state,
            updated_at=at)


@pytest.fixture
def compared():
    """What the fake compare answers, by deployed sha (an exception raises), and its calls."""
    return {"answers": {}, "calls": []}


@pytest.fixture
def following(root, scheduled, clock, compared):
    async def compare(repository, base, head):
        compared["calls"].append((repository, base, head))
        answer = compared["answers"].get(head, False)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def schedule(message):
        scheduled.append(message)
        return message

    return WakeOnService(
        root,
        schedule=schedule,
        message_exists=lambda message_id: any(m.id == message_id for m in scheduled),
        clock=clock,
        compare=compare,
    )


def _wait(service, **extra):
    return register(
        service,
        deployment=f"eerovil/agentdeck@{SHA}",
        name="promote/production",
        timeout_minutes=120,
        **extra,
    )


def test_a_later_deploy_that_contains_a_killed_merge_wakes_its_wait(
    root, following, scheduled, clock, compared
):
    request = _wait(following)
    drone_status(root, "d1", SHA, "continuous-integration/drone/push", "error", _at(clock))
    drain(following)
    assert scheduled == [] and following.registry.all()[0].failed
    compared["answers"][LATER] = True
    drone_status(root, "d2", LATER, PROMOTE, "success", _at(clock, 60))
    drain(following)
    assert compared["calls"] == [("eerovil/agentdeck", SHA, LATER)]
    assert [m.id for m in scheduled] == [f"wake-on:{request.id}"]
    assert f"contains {SHA[:12]}" in scheduled[0].text and "success" in scheduled[0].text
    assert following.registry.all() == ()


def test_a_later_commit_whose_deploy_check_run_succeeded_wakes_its_wait(
    root, following, scheduled, clock, compared
):
    """#4662 D6: a later commit's deploy that reports as a check run counts as #4445's later
    deploy for a named wait; a skipped run on that commit does not."""
    request = register(
        following, deployment=f"eerovil/agentdeck@{SHA}", name="deploy", timeout_minutes=120
    )
    compared["answers"][LATER] = True
    deploy_run(root, "s1", "skipped", _at(clock, 30), sha=LATER, action="created")
    drain(following)
    assert scheduled == []
    deploy_run(root, "d1", "success", _at(clock, 60), sha=LATER)
    drain(following)
    assert [m.id for m in scheduled] == [f"wake-on:{request.id}"]
    assert f"contains {SHA[:12]}" in scheduled[0].text


def test_a_deploy_from_before_the_wait_is_found_by_its_first_scan(
    root, following, scheduled, clock, compared
):
    drone_status(root, "d1", LATER, PROMOTE, "success", _at(clock, -600))
    drain(following)
    compared["answers"][LATER] = True
    request = _wait(following)
    following.expire_due()
    assert [m.id for m in scheduled] == [f"wake-on:{request.id}"]


def test_a_later_deploy_without_the_commit_and_an_unnamed_wait_do_not_count(
    root, following, scheduled, clock, compared
):
    named = _wait(following)
    unnamed = register(following, deployment=f"eerovil/agentdeck@{SHA}", timeout_minutes=120)
    drone_status(root, "d1", LATER, PROMOTE, "success", _at(clock, 60))
    deploy_state(root, "d2", "success", _at(clock, 60), sha=LATER)
    drain(following)
    # Only the named wait asked, and the "no" is kept: a second deploy asks nothing.
    assert compared["calls"] == [("eerovil/agentdeck", SHA, LATER)]
    drone_status(root, "d3", LATER, PROMOTE, "success", _at(clock, 90))
    drain(following)
    assert len(compared["calls"]) == 1
    assert scheduled == [] and {r.id for r in following.registry.all()} == {named.id, unnamed.id}


def test_a_failed_check_is_told_once_after_the_grace_and_the_wait_goes_on(
    root, following, scheduled, clock
):
    request = _wait(following)
    drone_status(root, "d1", SHA, "continuous-integration/drone/push", "error", _at(clock))
    drain(following)
    held = following.registry.all()[0]
    assert following.next_due() == pytest.approx(held.failed_at + 30 * 60)
    clock.now += 29 * 60
    following.expire_due()
    assert scheduled == []
    clock.now += 2 * 60
    following.expire_due()
    following.expire_due()
    assert [m.id.rsplit(":", 1)[0] for m in scheduled] == [f"wake-on:build-failed:{request.id}"]
    assert "continuous-integration/drone/push" in scheduled[0].text
    assert "finished: error" in scheduled[0].text
    assert following.registry.all()[0].failed_told
    # At the timeout the wait has its one more round, then names the failure.
    clock.now = request.deadline + 1
    following.expire_due()
    assert len(scheduled) == 1
    clock.now = following.registry.all()[0].deadline + 1
    following.expire_due()
    assert scheduled[-1].id == f"wake-on:{request.id}"
    assert "A check on that commit failed" in scheduled[-1].text
    assert following.registry.all() == ()


def test_a_check_that_passed_on_a_rerun_is_no_failure(root, following, scheduled, clock):
    _wait(following)
    push = "continuous-integration/drone/push"
    drone_status(root, "d1", SHA, push, "error", _at(clock))
    drain(following)
    drone_status(root, "d2", SHA, push, "success", _at(clock, 60))
    drain(following)
    assert not following.registry.all()[0].failed
    clock.now += 31 * 60
    following.expire_due()
    assert scheduled == []


def test_an_old_failure_is_found_by_the_first_scan(root, following, scheduled, clock):
    push = "continuous-integration/drone/push"
    drone_status(root, "d1", SHA, push, "error", _at(clock, -3600))
    drain(following)
    request = _wait(following)
    following.expire_due()
    following.expire_due()
    assert [m.id.rsplit(":", 1)[0] for m in scheduled] == [f"wake-on:build-failed:{request.id}"]


def test_an_unread_compare_is_tried_again_and_then_given_up_at_the_deadline(
    root, following, scheduled, clock, compared
):
    request = _wait(following)
    compared["answers"][LATER] = RuntimeError("HTTP 502")
    drone_status(root, "d1", LATER, PROMOTE, "success", _at(clock, 60))
    drain(following)
    assert len(compared["calls"]) == 1 and scheduled == []
    # Paced: not again before a minute is up.
    following.expire_due()
    assert len(compared["calls"]) == 1
    assert following.next_due() == pytest.approx(clock.now + 60)
    clock.now += 61
    following.expire_due()
    assert len(compared["calls"]) == 2
    # The compare answers at last: woken.
    compared["answers"][LATER] = True
    clock.now += 61
    following.expire_due()
    assert [m.id for m in scheduled] == [f"wake-on:{request.id}"]


def test_a_deadline_whose_reads_keep_failing_says_so(root, following, scheduled, clock, compared):
    request = _wait(following)
    following.expire_due()  # its first scan, with nothing to compare
    compared["answers"][LATER] = RuntimeError("HTTP 502")
    drone_status(root, "d1", LATER, PROMOTE, "success", _at(clock, 60))
    clock.now = request.deadline + 1
    held = following.registry.all()[0]
    following.registry.update(replace(held, extra_s=0.0))
    drain(following)
    for _ in range(3):
        clock.now += 61
        following.expire_due()
    assert [m.id for m in scheduled] == [f"wake-on:{request.id}"]
    assert "could not" in scheduled[0].text and "HTTP 502" in scheduled[0].text


def test_a_refused_failure_message_is_retried(root, clock, compared):
    stored = []
    refuse = [True]

    def schedule(message):
        if refuse[0]:
            return None
        stored.append(message)
        return message

    service = WakeOnService(
        root,
        schedule=schedule,
        message_exists=lambda message_id: any(m.id == message_id for m in stored),
        clock=clock,
    )
    request = _wait(service)
    drone_status(root, "d1", SHA, "continuous-integration/drone/push", "error", _at(clock, -3600))
    drain(service)
    service.expire_due()
    assert stored == [] and not service.registry.all()[0].failed_told
    assert service.next_due() == pytest.approx(clock.now + 60)
    refuse[0] = False
    clock.now += 61
    service.expire_due()
    assert [m.id.rsplit(":", 1)[0] for m in stored] == [f"wake-on:build-failed:{request.id}"]


def test_a_deploy_that_lands_while_a_compare_runs_is_read_by_the_same_run(
    root, scheduled, clock
):
    other = "fedcba0987654321fedcba0987654321fedcba09"
    calls = []

    async def scenario():
        gate = asyncio.Event()

        async def compare(repository, base, head):
            calls.append(head)
            if head == LATER:
                await gate.wait()
                return False
            return True

        service = WakeOnService(
            root,
            schedule=lambda message: scheduled.append(message) or message,
            message_exists=lambda message_id: any(m.id == message_id for m in scheduled),
            clock=clock,
            compare=compare,
        )
        request = _wait(service)
        service.expire_due()
        await asyncio.gather(*service._side_tasks)
        drone_status(root, "d1", LATER, PROMOTE, "success", _at(clock, 60))
        drain(service)
        await asyncio.sleep(0)
        assert calls == [LATER] and request.id in service._settling
        drone_status(root, "d2", other, PROMOTE, "success", _at(clock, 90))
        drain(service)  # busy: the sha waits in the slot for the running run
        gate.set()
        await asyncio.gather(*service._side_tasks)
        return request

    request = asyncio.run(scenario())
    assert calls == [LATER, other]
    assert [m.id for m in scheduled] == [f"wake-on:{request.id}"]


def test_a_rerun_that_fails_again_and_a_second_failing_check_are_told(
    root, following, scheduled, clock
):
    """Helper checks on #4446: one message per recorded failure, and a failure the first
    one kept out is found once the first passes; an older pass clears nothing."""
    request = _wait(following)
    push, lint = "continuous-integration/drone/push", "lint"
    drone_status(root, "d1", SHA, push, "error", _at(clock))
    drone_status(root, "d2", SHA, lint, "failure", _at(clock, 10))
    drone_status(root, "d3", SHA, push, "success", _at(clock, -10))  # late, and older
    drain(following)
    assert following.registry.all()[0].failed_check == f"status:{push}"
    clock.now += 31 * 60
    following.expire_due()
    following.expire_due()
    assert len(scheduled) == 1 and push in scheduled[0].text
    # The push passes on its re-run: the lint failure, kept out until now, is recorded.
    drone_status(root, "d4", SHA, push, "success", _at(clock))
    drain(following)
    # It failed over 30 minutes ago, so it is told at once.
    assert following.registry.all()[0].failed_check == f"status:{lint}"
    assert len(scheduled) == 2 and "lint" in scheduled[1].text
    # Lint passes, then the push fails again: told again, under its own id.
    drone_status(root, "d5", SHA, lint, "success", _at(clock, 60))
    drone_status(root, "d6", SHA, push, "error", _at(clock, 120))
    drain(following)
    clock.now += 33 * 60
    following.expire_due()
    assert len(scheduled) == 3 and len({m.id for m in scheduled}) == 3
    assert all(m.id.startswith(f"wake-on:build-failed:{request.id}:") for m in scheduled)


def test_a_merged_wake_of_the_works_own_chat_is_held_for_the_decks_merged_message(
    root, scheduled, clock
):
    """#4753: the release tells the chat that owns a Work of the merge itself, usually seconds
    after the chat's own merge wait would: that wake is held three minutes, and ended without
    a message by whichever of the two reaches the wait first."""
    owned: list[bool] = [True]
    notices: list[str] = []
    service = settling_service(
        root,
        scheduled,
        clock,
        owns_pull=lambda request: owned[0],
        release_notices=lambda chat_id, since: list(notices),
    )
    registry = service.registry
    chat = "claude_code:alt:one"
    clock.now -= 2 * MERGE_TOLD_S  # each delivery below is received after its wait began

    def merged(delivery, number=12, was_merged=True):
        deliver(
            root,
            event="pull_request",
            delivery=delivery,
            action="closed",
            number=number,
            pull_request={"number": number, "merged": was_merged},
        )
        drain(service)

    def held():
        return [(item.number, bool(item.held_text), item.deadline) for item in registry.all()]

    # Held, not sent; registered again or delivered again, it keeps its short deadline.
    first = register(service, pull_request="eerovil/agentdeck#12")
    merged("h1")
    due = clock.now + MERGE_HOLD_S
    assert scheduled == [] and held() == [(12, True, due)]
    assert service.next_due() == due
    assert register(service, pull_request="eerovil/agentdeck#12").id == first.id
    merged("h1b")
    service.expire_due()
    assert scheduled == [] and held() == [(12, True, due)]
    # The release tells the chat first: the wait ends, and no wake told the chat.
    register(service, "claude_code:alt:two", pull_request="eerovil/agentdeck#12")
    register(service, pull_request="eerovil/agentdeck#13")
    assert registry.end_merge_waits(chat, ["EeroVil/AgentDeck#12"]) == 1
    assert [(item.chat_id, item.number) for item in registry.all()] == [
        ("claude_code:alt:two", 12),
        (chat, 13),
    ], "only that chat's wait on that merge"
    registry.remove(item.id for item in registry.all())
    assert scheduled == [] and registry.end_merge_waits(chat, []) == 0
    # The deck's message reached the chat before the hold ran out: gone quietly.
    register(service, pull_request="eerovil/agentdeck#12")
    merged("h2")
    notices.append("work-release:eerovil/agentdeck#7:live-check:eerovil/agentdeck#12")
    clock.now += MERGE_HOLD_S
    service.expire_due()
    assert scheduled == [] and registry.all() == ()
    # No message from the deck (another pull request of the Work is still open, or the
    # release is late): the wake is sent once, and says the deck's message is still to come.
    notices.clear()
    register(service, pull_request="eerovil/agentdeck#12")
    merged("h3")
    clock.now += MERGE_HOLD_S - 1
    service.expire_due()
    assert scheduled == []
    clock.now += 1
    service.expire_due()
    service.expire_due()
    assert len(scheduled) == 1 and registry.all() == ()
    assert "agentdeck#12 was merged" in scheduled[0].text
    assert scheduled[0].text.endswith(MERGE_HOLD_SUFFIX)
    # Claimed for sending, then the release, which reads the file from its own process: the
    # wake the wait owes stays, and is sent once.
    again = register(service, pull_request="eerovil/agentdeck#12")
    merged("h4")
    claimed = registry.claim_held(again.id)
    assert claimed is not None and claimed.pending_text.endswith(MERGE_HOLD_SUFFIX)
    assert registry.claim_held(again.id) is None, "claimed once"
    assert WakeOnRegistry(registry.path).end_merge_waits(chat, ["eerovil/agentdeck#12"]) == 0
    assert [item.id for item in registry.all()] == [again.id]
    service.expire_due()
    assert len(scheduled) == 2 and registry.all() == ()
    # The release first, then the hold's end: nothing left to claim, nothing sent.
    register(service, pull_request="eerovil/agentdeck#13")
    merged("h5", 13)
    waiting = registry.all()[0]
    registry.end_merge_waits(chat, ["eerovil/agentdeck#13"])
    service._end_hold(waiting)
    assert len(scheduled) == 2 and registry.all() == ()
    # Closed without a merge, or a chat that does not own the Work: woken at once, as before.
    register(service, pull_request="eerovil/agentdeck#14")
    merged("h6", 14, was_merged=False)
    assert len(scheduled) == 3 and MERGE_HOLD_SUFFIX not in scheduled[2].text
    owned[0] = False
    register(service, pull_request="eerovil/agentdeck#15")
    merged("h7", 15)
    assert len(scheduled) == 4 and MERGE_HOLD_SUFFIX not in scheduled[3].text
    assert registry.all() == ()
