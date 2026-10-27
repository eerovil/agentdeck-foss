"""A chat registers a GitHub event and is woken once when it lands (#2775).

Fixtures go through `WebhookLog`, the same appender the live ingress route uses, so the
matching is asserted over real stream bytes. Scheduling is stubbed: what is under test is
which message each registration produces and how often, not the injection path.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from public_copy import private_feature

from agentdeck import chat_board as _chat_board
from agentdeck.app import create_app
from agentdeck.config import (
    AccountConfig,
    AppConfig,
    GitHubWebhookConfig,
    HistoryConfig,
    RestartNudgeConfig,
)
from agentdeck.github_webhook_log import Repository, WebhookLog
from agentdeck.models import PullRequestRef
from agentdeck.providers.instructions import agentdeck_session_instructions
from agentdeck.wake_on import (
    STOP_SETTLE_S,
    NeedsYouStop,
    WakeOnRegistry,
    WakeOnService,
    WakeRequestError,
    parse_request,
    registry_path,
    timeout_message,
)
from agentdeck.webhook_tailer import WebhookTailer
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
        "claude_code:alt:five", "claude_code:alt:five", asked=True,
        **parse_request(review="eerovil/agentdeck#15"),
    )
    merge = register(service, "claude_code:alt:six", pull_request="acmecorp/bazaar#351",
                     timeout_minutes=120)
    told = register(service, "claude_code:alt:two", pull_request="eerovil/agentdeck#12",
                    timeout_minutes=120)
    other = register(service, "claude_code:alt:three", pull_request="eerovil/agentdeck#13")
    # A clean pass usually reaches the chat before the reviewer's closing line: that line is
    # no news then, and a review chat opened after the pass still cuts.
    passed = register(service, "claude_code:alt:seven", pull_request="eerovil/agentdeck#17",
                      timeout_minutes=120)
    later = register(service, "claude_code:alt:eight", pull_request="eerovil/agentdeck#18",
                     timeout_minutes=120)
    service.registry.answer_reviews("claude_code:alt:seven", ["eerovil/agentdeck#17"])
    service.registry.answer_reviews("claude_code:alt:eight", ["eerovil/agentdeck#18"])
    # The pass came first and the merge wait after it: the closing line is no news either.
    service.registry.answer_reviews("claude_code:alt:nine", ["eerovil/agentdeck#19"])
    after = register(service, "claude_code:alt:nine", pull_request="eerovil/agentdeck#19",
                     timeout_minutes=120)

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
    deliver(root, event="issue_comment", delivery="c1", repository=chats, action="created",
            issue=bazaar, comment={"body": why})
    deliver(root, event="issues", delivery="l1", repository=chats, action="labeled",
            issue=bazaar, label={"name": "blocked"})
    closing = f"Reviewed agentdeck#12 at `{SHA[:12]}`: 1 outstanding finding (sent)."
    two = chat(6226, "eerovil/agentdeck#11", "eerovil/agentdeck#12")
    two["body"] += f"\n<!-- agentdeck: review-head {SHA} eerovil/agentdeck#14 -->"
    deliver(root, event="issue_comment", delivery="c2", repository=chats, action="created",
            issue=two, comment={"body": closing})
    # The older chat closing on its own summary does not end the wait on the review asked for.
    opened = datetime.fromtimestamp(clock.now - 600, UTC).isoformat()
    old = {**chat(6230, "eerovil/agentdeck#16", "eerovil/agentdeck#15"), "created_at": opened}
    deliver(root, event="issue_comment", delivery="c4", repository=chats, action="created",
            issue=old, comment={"body": f"Reviewed agentdeck#15 at `{SHA[:12]}`: pass."})
    for number, pull, opened_at in (
        (6240, "eerovil/agentdeck#17", clock.now - 600),
        (6241, "eerovil/agentdeck#18", clock.now + 0.5),
        (6242, "eerovil/agentdeck#19", clock.now - 600),
    ):
        created = datetime.fromtimestamp(opened_at, UTC).isoformat()
        deliver(root, event="issue_comment", delivery=f"c{number}", repository=chats,
                action="created", issue={**chat(number, pull, pull), "created_at": created},
                comment={"body": f"Reviewed {pull.partition('/')[2]} at `{SHA[:12]}`: pass."})
    # Not a review chat, and not a closing line: neither cuts anything short.
    deliver(root, event="issues", delivery="l2", repository=chats, action="labeled",
            issue={**chat(7, "x", "eerovil/agentdeck#13"), "title": "Something else"},
            label={"name": "blocked"})
    deliver(root, event="issue_comment", delivery="c3", repository=chats, action="created",
            issue=chat(8, "eerovil/agentdeck#11", "eerovil/agentdeck#13"),
            comment={"body": "Reviewed the protocol; reading the diff next."})
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
    assert set(held) == {
        untouched.id, still.id, asked.id, told.id, other.id, passed.id, after.id
    }
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
    assert scheduled[0].text.startswith(
        "AgentDeck wake-on: the web worker serving this deck now runs 222222222222"
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


def test_a_wait_that_times_out_still_wakes_the_chat_once(root, service, scheduled, clock):
    request = register(service, pull_request="eerovil/agentdeck#12", timeout_minutes=5)
    service.expire_due()
    assert scheduled == []
    clock.now += 5 * 60
    service.expire_due()
    service.expire_due()
    assert [message.id for message in scheduled] == [f"wake-on:{request.id}"]
    assert "nothing arrived for pull request eerovil/agentdeck#12 within 5 minutes" in (
        scheduled[0].text
    )


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
        fields["timeout_s"] = 0.3
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
    clock.now += 120
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
        {"after_minutes": 24 * 60 + 1},
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
        parse_request(**body)


def test_a_time_wait_wakes_the_chat_at_its_time_naming_what_it_waits_on(
    root, service, scheduled, clock
):
    """#2968: a render on another machine reports nothing to GitHub; the time is the wake."""
    request = register(service, after_minutes=90, waiting_for="the  render on the GTX 970")
    assert request.target == "the render on the GTX 970"
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
    assert service.registry.all() == ()


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
    for role in ("primary", "delegated", "kanban"):
        flat = " ".join(agentdeck_session_instructions(provider="claude_code", role=role).split())
        assert "`POST $AGENTDECK_URL/api/sessions/<session-key>/wake-on`" in flat
        assert '{"pull_request": "<owner/repo#number>"}' in flat
        assert flat.index("Never wait by polling") < flat.index("/wake-on")
        assert '{"after_minutes": 90}' in flat
        assert '{"needs_you": "all"}' in flat


@pytest.mark.anyio
async def test_the_api_registers_lists_and_cancels_a_wait(tmp_path):
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
        await client.delete(
            f"/api/sessions/{key}/wake-on", params={"id": not_there.json()["registered"]["id"]}
        )
        made = await client.post(
            f"/api/sessions/{key}/wake-on",
            json={"pull_request": "eerovil/agentdeck#7", "timeout_minutes": 30},
        )
        timed = await client.post(
            f"/api/sessions/{key}/wake-on",
            json={"after_minutes": 45, "waiting_for": "the render"},
        )
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
        app.state.work_router = SimpleNamespace(enabled=True, ask_again=ask_again)
        app.state.app_state.add_pull_request(key, PullRequestRef.parse("eerovil/agentdeck#9"))
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
    assert not_there.json()["registered"]["kind"] == "deployment"
    assert unregistered.status_code == 409
    assert blank.status_code == 422 and "what changed" in blank.text
    assert [item["kind"] for item in after_blank.json()["waits"]] == ["pull_request"]
    assert capped.status_code == 409 and "twice" in capped.text
    assert [item["kind"] for item in after_cap.json()["waits"]] == ["pull_request"]
    assert busy.status_code == 200 and busy.json()["outcome"] == "already asked", busy.text
    assert [item["kind"] for item in after_busy.json()["waits"]] == ["pull_request", "review"]
    assert again.status_code == 200, again.text
    assert asks == [("eerovil/agentdeck", 9, "no verdict", True)]
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
