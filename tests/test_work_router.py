"""A Work hears its own GitHub events (#2895, ADR 0008).

Fixtures go through `WebhookLog`, the same appender the live ingress route uses, so the
filter is asserted over real stream bytes. Who owns an issue or a pull request, the
scheduled-message store and the review start are stubbed: what is under test is which
deliveries reach a chat, which chat, under which id, and how often a review is asked for.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from public_copy import private_feature

from agentdeck import chat_review as _chat_review
from agentdeck.github_webhook_log import Repository, WebhookLog
from agentdeck.models import PullRequestRef
from agentdeck.poller_ops import work_repositories
from agentdeck.webhook_tailer import WebhookTailer
from agentdeck.work_release import OWNER_CALL_ASK
from agentdeck.work_router import (
    ESCALATED_ASK,
    HANDED_OVER,
    MESSAGE,
    REVIEW,
    ReviewedHeads,
    SwitchedRepositories,
    WorkRouterService,
    classify,
    owner_of_issue,
    owner_of_pull,
    work_pulls,
)

REPO = Repository(owner="eerovil", name="agentdeck")
OTHER = Repository(owner="eerovil", name="trailhead")
SHA = "abcdef1234567890abcdef1234567890abcdef12"
OWN = frozenset({"eerovil-agent"})
PERSON = {"login": "eerovil", "type": "User"}


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


def comment(
    root, delivery: str, *, sender=PERSON, body="/claude please also fix X", number=7, **kw
):
    issue = {"number": number}
    labels = kw.pop("labels", None)
    if labels is not None:
        issue["labels"] = [{"name": name} for name in labels]
    if kw.pop("on_pull", False):
        issue["pull_request"] = {"url": "x"}
    deliver(
        root,
        event="issue_comment",
        delivery=delivery,
        action="created",
        sender=sender,
        issue=issue,
        comment={
            "body": body,
            "html_url": f"https://github.com/c/{delivery}",
            "id": kw.pop("comment_id", 100),
            "author_association": kw.pop("comment_association", "OWNER"),
        },
        **kw,
    )


def pull_event(root, delivery: str, *, action="ready_for_review", draft=False, sha=SHA, **kw):
    deliver(
        root,
        event="pull_request",
        delivery=delivery,
        action=action,
        sender=PERSON,
        pull_request={"number": 9, "draft": draft, "state": "open", "head": {"sha": sha}},
        **kw,
    )


def read(root):
    tailer = WebhookTailer(root, consumer="work-router-test")
    return list(tailer.deliveries())


@pytest.fixture
def root(tmp_path):
    return tmp_path / "github-webhooks"


class Harness:
    def __init__(self, root) -> None:
        self.root = root
        self.scheduled: list = []
        self.reviews: list[tuple[str, str]] = []
        self.switched = {"eerovil/agentdeck"}
        self.issues = {("eerovil/agentdeck", 7): ("chat-a", "eerovil/agentdeck#7")}
        self.pulls = {("eerovil/agentdeck", 9): ("chat-a", "eerovil/agentdeck#7")}
        self.fail_review = False
        self.refuse = None
        self.heads: dict[tuple[str, int], str] = {}
        self.now = 1000.0
        self.store_refuses = False
        self.stored: set[str] = set()
        self.strangers: set[str] = set()
        self.authorized_asked: list[tuple[str, str, str]] = []
        self.rereads: list[tuple[str, int]] = []
        self.asks: list[dict] = []
        self.verdicts: list[tuple] = []

        async def start_review(pull: str, work: str, **asks) -> None:
            self.reviews.append((pull, work))
            self.asks.append(asks)
            if self.fail_review:
                raise RuntimeError("gh is down")

        async def ready_head(repository: str, number: int) -> str:
            return self.heads.get((repository, number), "")

        def schedule(message):
            if any(item.id == message.id for item in self.scheduled):
                return None  # the store absorbs a replay of the same id
            if self.store_refuses:
                return None  # a refused row: also None, but nothing is stored
            self.scheduled.append(message)
            return message

        self.service = WorkRouterService(
            root,
            switched=lambda repository: repository.lower() in self.switched,
            owner_of_issue=lambda repository, number: self.issues.get((repository, number)),
            owner_of_pull=lambda repository, number: self.pulls.get((repository, number)),
            schedule=schedule,
            start_review=start_review,
            own_logins=OWN,
            refusal=lambda chat_id, text: self.refuse(text) if self.refuse else None,
            ready_head=ready_head,
            exists=lambda message_id: any(item.id == message_id for item in self.scheduled),
            clock=lambda: self.now,
            authorized=self._authorized,
            reread_issue=self._reread,
            acknowledge=self._acknowledge,
            spec_only_label=lambda repository: "Spec-Only",
            on_verdict=lambda chat, pulls: self.verdicts.append((chat, pulls)),
        )
        self.acknowledged: list[tuple[str, int]] = []

    async def _acknowledge(self, repository: str, comment_id: int) -> None:
        self.acknowledged.append((repository, comment_id))
        if comment_id == 13:
            raise RuntimeError("gh is down")

    async def _reread(self, repository: str, number: int) -> None:
        self.rereads.append((repository, number))
        if number == 13:
            raise RuntimeError("gh is down")

    async def _authorized(self, repository: str, login: str, association: str) -> bool:
        self.authorized_asked.append((repository, login, association))
        if login == "boom":
            raise RuntimeError("gh is down")
        return login not in self.strangers

    async def drain(self) -> None:
        tailer = WebhookTailer(self.root, consumer="work-router")
        await self.service.apply(list(tailer.deliveries()))
        tailer.commit()
        await asyncio.gather(*list(self.service._checks))


@pytest.fixture
def harness(root):
    return Harness(root)


# --- the filter ---


def test_a_persons_comment_on_the_issue_is_a_message(root):
    comment(root, "d1")
    [event] = [classify(item, own_logins=OWN) for item in read(root)]
    assert event.kind == MESSAGE and (event.repository, event.number) == ("eerovil/agentdeck", 7)
    assert not event.is_pull
    assert "eerovil commented on issue eerovil/agentdeck#7" in event.text
    assert "please also fix X" in event.text and "https://github.com/c/d1" in event.text
    # The agent is asked to answer where it was asked (#3118), in the link form too.
    reply = "answer on issue eerovil/agentdeck#7 in one short comment (`gh issue comment`)"
    assert reply in event.text and reply in event.link


@pytest.mark.parametrize("body", ["please also fix X", "/claudette", "Starting deployment…"])
def test_a_persons_comment_without_claude_is_not_sent(root, body):
    """CI posting under a person's token reached the chat as an ask (#3118)."""
    comment(root, "d1", body=body)
    assert [classify(item, own_logins=OWN) for item in read(root)] == [None]


@pytest.mark.parametrize(
    "sender",
    [
        {"login": "agentdeck-eerovil[bot]", "type": "Bot"},
        {"login": "Eerovil-Agent", "type": "User"},  # the deck's own account, any case
        {"login": "dependabot[bot]", "type": "Bot"},
        {"login": "someone[bot]", "type": "User"},
        {},
    ],
)
def test_the_decks_own_and_bot_comments_are_not_sent(root, sender):
    comment(root, "d1", sender=sender)
    assert [classify(item, own_logins=OWN) for item in read(root)] == [None]


async def test_a_verdict_handed_to_the_chat_ends_its_review_waits(root, harness):
    """#3183: the verdict is the wake, so the chat's review wait ends without a second one.

    Only for the pull requests it is about: the one it was posted on, else the Work's own
    pull requests its text names; one on the issue naming none is about the whole Work.
    """
    harness.pulls[("eerovil/agentdeck", 11)] = ("chat-b", "eerovil/agentdeck#7")
    reviewer = {"login": "review-bot[bot]", "type": "Bot"}
    comment(root, "d1", sender=reviewer, body="/claude fix the off-by-one", on_pull=True, number=9)
    comment(root, "d2", body="/claude please also fix X")
    comment(root, "d3", sender=reviewer, body="/claude PR #11 and eerovil/agentdeck#7 and #12")
    comment(root, "d4", sender=reviewer, body="/claude 1. `a.py:3` is off by one")
    # A release verdict is the release's to end the wait on, once it has taken it.
    comment(root, "d5", sender=reviewer, body="/reviewed pass " + SHA[:12] + " " + "f" * 64)
    # An escalation is neither, and ChatGPT posts it under the owner's login (#3216).
    comment(root, "d6", body=ESCALATION)
    await harness.drain()
    assert len(harness.scheduled) == 6
    assert harness.verdicts == [
        ("chat-a", ["eerovil/agentdeck#9"]),
        ("chat-a", ["eerovil/agentdeck#11"]),
        ("chat-a", None),
        ("chat-a", None),
    ]
    assert harness.authorized_asked[-1][1] == "eerovil"


# The reviewer's escalation on agentdeck#3200 (2026-10-16 20:19Z), posted under the owner's own
# login by ChatGPT's GitHub connector (#3216).
ESCALATION = (
    "Review loop escalated to the operator\n\nThe same identity defect recurs after the finding"
    " at `bf7e1ea6c1a1d4564abece11b64d96b7def4dd99`: \u201cif a person sends the same text as an"
    " earlier deck notice in that chat, ... their own message [is marked] as a deck notice too."
    "\u201d At `abd7c30e6f94797deb3b1fd372708e4c769f6436`, `src/agentdeck/deck_notices.py:78-85`"
    " still identifies a user turn by text and a six-hour window (`if -DELIVERY_BEFORE <= gap <="
    " DELIVERY_AFTER: return kind`), not by the delivered transcript event.\n\nOperator decision"
    " needed: is this collision acceptable by design, is the worker stuck on identity"
    " correlation, or should the remaining work be finished by hand? No further `/claude`"
    " review loop is being started."
)


def test_a_reviewers_escalation_reaches_the_chat_with_what_to_do(root):
    """#3216: the escalation matched neither findings nor a pass, so nobody heard it."""
    comment(root, "d1", body=ESCALATION, number=3200)
    comment(root, "d2", body="Is the review loop escalated? Asking for a friend.")
    events = [classify(item, own_logins=OWN) for item in read(root)]
    assert events[1] is None
    event = events[0]
    assert event.kind == MESSAGE and event.verdict and event.author == "eerovil"
    assert event.text.startswith(
        "AgentDeck event: the reviewer escalated the review on issue eerovil/agentdeck#3200"
    )
    assert "Operator decision needed" in event.text
    assert ESCALATED_ASK in event.text and ESCALATED_ASK in event.link


def test_the_reviewers_verdict_is_sent_but_its_chatter_is_not(root):
    reviewer = {"login": "review-bot[bot]", "type": "Bot"}
    comment(root, "d1", sender=reviewer, body="/reviewed pass " + SHA[:12] + " " + "f" * 64)
    comment(root, "d2", sender=reviewer, body="/claude fix the off-by-one")
    comment(root, "d3", sender=reviewer, body="Looking at this now.")
    events = [classify(item, own_logins=OWN) for item in read(root)]
    assert [event.kind if event else None for event in events] == [MESSAGE, MESSAGE, None]
    assert "the reviewer posted" in events[0].text
    # #3314: findings tell the agent to put a finding only the owner can settle to the owner.
    assert OWNER_CALL_ASK in events[1].text and OWNER_CALL_ASK not in events[0].text


# The live delivery from agentdeck#2940 (2026-10-12 21:54 Helsinki), trimmed to the fields read:
# the Claude fallback reviewer's findings, posted under the deck's own App account (#2943).
DECK_BOT = {"login": "agentdeck-eerovil[bot]", "type": "Bot", "id": 334402557}
DECK_FINDINGS = (
    "/claude\nReview findings for PR #2941 at 6937d8fb5ab9e64e608409eb0f1ffb4cd2a87a0f:\n\n"
    "1. `src/agentdeck/work_start.py:130` — The fix covers only new busy starts."
)


def test_the_decks_own_reviewer_findings_are_sent(root):
    own = OWN | {"agentdeck-eerovil[bot]"}
    comment(
        root, "d1", sender=DECK_BOT, body=DECK_FINDINGS, number=2940, comment_association="NONE"
    )
    comment(root, "d2", sender=DECK_BOT, body="/reviewed changes-needed 6937d8f", number=2940)
    # The same account's other comments stay echo: a worker's report, a bare `/claude`.
    comment(root, "d3", sender=DECK_BOT, body="More on this: removing #2492 did not help.")
    comment(root, "d4", sender=DECK_BOT, body="/claude carry on", number=2940)
    events = [classify(item, own_logins=own) for item in read(root)]
    assert [event.kind if event else None for event in events] == [MESSAGE, MESSAGE, None, None]
    assert events[0].number == 2940 and not events[0].author
    assert "the deck's reviewer posted on issue eerovil/agentdeck#2940" in events[0].text
    assert "Review findings for PR #2941" in events[0].text
    assert OWNER_CALL_ASK in events[0].text and OWNER_CALL_ASK not in events[1].text


def test_the_decks_reviewer_findings_in_their_other_shape_are_sent(root):
    """Northwind/issues#387, #661, #674 opened ``/claude Review of …`` (#3004)."""
    own = OWN | {"agentdeck-eerovil[bot]"}
    body = "/claude Review of store#3554 at `459576a`: please fix these.\n\n1. `a.py:84` — x"
    comment(root, "d1", sender=DECK_BOT, body=body, number=661)
    comment(root, "d2", sender=DECK_BOT, body="/claude Reviewing is done, carry on")
    events = [classify(item, own_logins=own) for item in read(root)]
    assert [event.kind if event else None for event in events] == [MESSAGE, None]
    assert "Review of store#3554" in events[0].text


async def test_the_decks_own_reviewer_findings_reach_the_works_chat(root, harness):
    harness.service._own_logins = OWN | {"agentdeck-eerovil[bot]"}
    comment(root, "d1", sender=DECK_BOT, body=DECK_FINDINGS)
    await harness.drain()
    assert [message.chat_id for message in harness.scheduled] == ["chat-a"]
    assert harness.authorized_asked == []


def test_a_comment_on_a_pull_request_is_marked_as_one(root):
    comment(root, "d1", on_pull=True)
    [event] = [classify(item, own_logins=OWN) for item in read(root)]
    assert event.is_pull and "pull request eerovil/agentdeck#7" in event.text
    assert "(`gh pr comment`)" in event.text


def test_an_edited_comment_is_not_sent_again(root):
    deliver(
        root,
        event="issue_comment",
        delivery="d1",
        action="edited",
        sender=PERSON,
        issue={"number": 7},
        comment={"body": "x"},
    )
    assert [classify(item, own_logins=OWN) for item in read(root)] == [None]


def test_a_persons_pull_request_review_is_a_message(root):
    deliver(
        root,
        event="pull_request_review",
        delivery="d1",
        action="submitted",
        sender=PERSON,
        pull_request={"number": 9},
        review={"state": "changes_requested", "body": "", "html_url": "https://r"},
    )
    [event] = [classify(item, own_logins=OWN) for item in read(root)]
    assert event.kind == MESSAGE and event.is_pull and event.number == 9
    assert "(changes requested)." in event.text


@pytest.mark.parametrize("action", ["ready_for_review", "synchronize", "opened", "reopened"])
def test_a_ready_pull_request_at_a_head_asks_for_a_review(root, action):
    pull_event(root, "d1", action=action)
    [event] = [classify(item, own_logins=OWN) for item in read(root)]
    assert event.kind == REVIEW and event.head == SHA and event.number == 9


def test_a_push_that_says_draft_asks_for_a_live_read(root):
    """#2983: a push landing just after the ready press can still carry ``draft: true``."""
    pull_event(root, "d1", action="synchronize", draft=True)
    [event] = [classify(item, own_logins=OWN) for item in read(root)]
    assert event.kind == REVIEW and event.head == SHA and event.confirm


@pytest.mark.parametrize(
    "fields",
    [
        {"draft": True},
        {"draft": True, "action": "opened"},
        {"action": "closed"},
        {"action": "converted_to_draft"},
        {"action": "edited"},
        {"sha": ""},
    ],
)
def test_a_draft_or_a_closed_pull_request_asks_for_nothing(root, fields):
    pull_event(root, "d1", **fields)
    assert [classify(item, own_logins=OWN) for item in read(root)] == [None]


@pytest.mark.parametrize("event", ["check_suite", "workflow_run", "status", "push", "issues"])
def test_ci_deploys_pushes_and_issue_changes_are_not_sent(root, event):
    deliver(root, event=event, delivery="d1", action="completed", sender=PERSON)
    assert [classify(item, own_logins=OWN) for item in read(root)] == [None]


# --- the service ---


async def test_a_message_goes_to_the_owning_chat_under_the_deliverys_id(root, harness):
    comment(root, "d1")
    await harness.drain()
    [message] = harness.scheduled
    assert (message.id, message.chat_id) == ("work-event:d1", "chat-a")


@pytest.mark.parametrize(
    "label, sender, sent",
    [
        ("spec-only", PERSON, True),
        ("spec-only", {"login": "eerovil-agent", "type": "User"}, False),
        ("bug", PERSON, False),
    ],
    ids=["a person approves", "the agent's own removal", "another label"],
)
async def test_a_person_removing_the_spec_only_label_tells_the_agent_to_build(
    root, harness, label, sender, sent
):
    """#3134: the removal is the approval of the spec the Work's agent was started to write."""
    deliver(
        root,
        event="issues",
        delivery="d1",
        action="unlabeled",
        sender=sender,
        issue={"number": 7, "state": "open"},
        label={"name": label},
    )
    await harness.drain()
    if not sent:
        assert harness.scheduled == []
        return
    [message] = harness.scheduled
    assert (message.id, message.chat_id) == ("work-event:d1", "chat-a")
    assert "eerovil removed the `Spec-Only` label" in message.text
    assert "Build the approved spec" in message.text


async def test_automatic_news_resumes_a_paused_work_and_is_sent(root, harness):
    """#2956, the owner's choice: a paused Work's own automatic news resumes it and goes on;
    a person's comment needs no resume. Paused is read for the owning Work, never from the
    delivery, so a pull request event is paused no less than an issue comment."""
    harness.service._own_logins = OWN | {"agentdeck-eerovil[bot]"}
    paused = {"github_issue:eerovil/agentdeck#7"}
    resumed = []

    async def resume(work_id):
        resumed.append(work_id)
        paused.discard(work_id)
        return True

    harness.service._paused = lambda work_id: work_id in paused
    harness.service._resume = resume
    comment(root, "d1")  # a person's ask: sent, and nothing resumed
    await harness.drain()
    assert [message.id for message in harness.scheduled] == ["work-event:d1"]
    assert resumed == []
    harness.heads[("eerovil/agentdeck", 9)] = SHA
    pull_event(root, "d2", action="ready_for_review")  # carries no issue label at all
    comment(root, "d3", sender=DECK_BOT, body=DECK_FINDINGS)
    await harness.drain()
    assert resumed == ["github_issue:eerovil/agentdeck#7"]
    assert len(harness.reviews) == 1
    assert [message.id for message in harness.scheduled] == ["work-event:d1", "work-event:d3"]


async def test_a_paused_work_that_does_not_resume_hears_no_automatic_news(root, harness):
    harness.heads[("eerovil/agentdeck", 9)] = SHA
    harness.service._paused = lambda work_id: True

    async def refuse(work_id):
        return False

    harness.service._resume = None  # nothing to resume it with
    pull_event(root, "d1", action="ready_for_review")
    await harness.drain()
    harness.service._resume = refuse
    pull_event(root, "d2", action="ready_for_review", sha="c" * 40)
    await harness.drain()

    async def broken(work_id):
        raise RuntimeError("gh is down")

    harness.service._resume = broken
    pull_event(root, "d3", action="ready_for_review", sha="d" * 40)
    await harness.drain()
    assert harness.reviews == []
    harness.service._paused = lambda work_id: 1 / 0  # unreadable: holds nothing back
    pull_event(root, "d4", action="ready_for_review", sha="b" * 40)
    await harness.drain()
    assert len(harness.reviews) == 1


async def test_a_comment_reaches_the_agent_only_from_a_work_tier_login(root, harness):
    """#2895 review: on a public repository anyone can comment; the Poller obeys only the
    Manifest's Work tier, and so does the router."""
    harness.strangers.add("drive-by")
    comment(root, "d1", sender={"login": "drive-by", "type": "User"}, comment_id=1)
    comment(root, "d2", sender={"login": "boom", "type": "User"}, comment_id=2)
    comment(root, "d3", comment_association="OWNER", comment_id=3)
    comment(root, "d4", comment_id=13)  # its 👀 fails: the message still went
    reviewer = {"login": "review-bot[bot]", "type": "Bot"}
    comment(root, "d5", sender=reviewer, body="/reviewed pass abc", comment_id=5)
    await harness.drain()
    assert [message.id for message in harness.scheduled] == [
        "work-event:d3", "work-event:d4", "work-event:d5"
    ]
    assert harness.authorized_asked[-1] == ("eerovil/agentdeck", "eerovil", "OWNER")
    # #3109: only a person's comment that was passed on gets the 👀, as a Poller command does.
    assert harness.acknowledged == [("eerovil/agentdeck", 3), ("eerovil/agentdeck", 13)]


async def test_the_reviewers_verdict_needs_no_permission_lookup(root, harness):
    comment(
        root,
        "d1",
        sender={"login": "review-bot[bot]", "type": "Bot"},
        body="/reviewed pass abc",
    )
    await harness.drain()
    assert len(harness.scheduled) == 1 and harness.authorized_asked == []


async def test_a_verdict_the_release_already_told_is_not_sent_again(root, harness):
    """#3192: the release's "passed clean" and the forwarded comment each woke the agent."""
    answers = iter(["review pass recorded for eerovil/agentdeck#9 at abc", "digest does not match"])

    class Release:
        async def on_verdict(self, *_args, **_kwargs):
            return next(answers)

        async def stale_findings(self, work_id, text, repository):
            # The branch has left bbbbbbb; the release reads that (#3192).
            assert repository == "eerovil/agentdeck" and not text.startswith("/claude")
            return "moved past bbbbbbb" if "at bbbbbbb" in text else ""

    harness.service._release = Release()
    reviewer = {"login": "review-bot[bot]", "type": "Bot"}
    verdict = "/reviewed pass " + "a" * 40 + " " + "d" * 64
    comment(root, "d1", sender=reviewer, body=verdict)
    comment(root, "d2", sender=reviewer, body=verdict)
    # Findings from a reviewer still on a replaced chat, about the commit the branch left.
    comment(root, "d3", sender=reviewer, body="/claude Review findings for PR #9 at bbbbbbb:\n1. x")
    comment(root, "d4", sender=reviewer, body="/claude Review findings for PR #9 at aaaaaaa:\n1. x")
    await harness.drain()
    # The told pass is not forwarded, nor stale findings; the rest still are.
    assert [message.id for message in harness.scheduled] == ["work-event:d2", "work-event:d4"]


async def test_without_an_authorization_port_no_persons_comment_is_forwarded(root, harness):
    harness.service._authorized = None
    comment(root, "d1")
    await harness.drain()
    assert harness.scheduled == []


async def test_a_replayed_delivery_is_one_message(root, harness):
    comment(root, "d1")
    tailer = WebhookTailer(root, consumer="work-router")
    batch = list(tailer.deliveries())
    await harness.service.apply(batch)
    await harness.service.apply(batch)  # a crash before the offset was committed
    assert [message.id for message in harness.scheduled] == ["work-event:d1"]


@pytest.mark.parametrize(
    "sender, finished, busy, then, sent",
    [
        (PERSON, False, False, False, True),
        (DECK_BOT, False, False, False, True),  # #753: another chat, as the deck's own login
        (DECK_BOT, False, True, False, False),  # the Work's own chat ending its Work
        (PERSON, False, True, False, True),  # a person, while the chat works: still sent
        (PERSON, True, False, False, False),
        (PERSON, False, False, True, False),  # the Work's own pull request merged and closed it
        (PERSON, False, False, "reopened", False),
    ],
    ids=[
        "a person",
        "another chat",
        "its own chat",
        "a person mid-turn",
        "a finished Work",
        "a merge",
        "reopened",
    ],
)
async def test_someone_else_closing_the_issue_tells_the_works_chat_once(
    root, harness, sender, finished, busy, then, sent
):
    """#3188: a Work's chat hears its issue closed under it, after a hold for a merge."""
    harness.service._own_logins = OWN | {"agentdeck-eerovil[bot]"}
    harness.service._finished = lambda work_id: finished
    harness.service._busy = lambda work_id: busy
    closed = "2026-10-16T09:51:48Z"
    said = {"body": "Filed by mistake. " + "x" * 400, "user": sender}
    said["created_at"] = "2026-10-16T09:51:47Z"  # just before the close: quoted
    asked = []

    async def latest_comment(repository, number, since, login):
        asked.append((repository, number, since, login))
        return said

    harness.service._latest_comment = latest_comment
    deliver(
        root,
        event="issues",
        delivery="d1",
        action="closed",
        sender=sender,
        issue={"number": 7, "state": "closed", "closed_at": closed, "html_url": "https://i/7"},
    )
    await harness.drain()
    # The closer's own comment is read from GitHub, from a minute before the close.
    assert asked == [("eerovil/agentdeck", 7, "2026-10-16T09:50:48Z", sender["login"])]
    if then == "reopened":
        deliver(root, event="issues", delivery="d2", action="reopened", issue={"number": 7})
        await harness.drain()
    elif then is True:
        merged_pull(root, "d2")
        await harness.drain()
    assert harness.scheduled == []  # held for a merge first
    harness.now += 61
    await harness.service.retry_pending()
    if not sent:
        assert harness.scheduled == [] and harness.service.pending.entries == {}
        return
    [message] = harness.scheduled
    assert (message.id, message.chat_id) == ("work-event:d1", "chat-a")
    assert f"{sender['login']}" in message.text
    assert "closed issue eerovil/agentdeck#7" in message.text
    assert "Filed by mistake. " + "x" * 282 + " …" in message.text
    assert "x" * 283 not in message.text
    assert "ask the person in the question box" in message.text
    harness.now += 3600
    await harness.service.retry_pending()
    assert len(harness.scheduled) == 1


def _ticket(number, state="closed", repository="eerovil/agentdeck"):
    closed_at = "2026-10-27T10:00:05Z" if state == "closed" else None
    return {"repository": repository, "number": number, "state": state, "closed_at": closed_at}


@pytest.mark.parametrize(
    ("labels", "done", "state", "tickets", "sent", "why"),
    [
        (["wayfinder:map", "parked"], 3, "open", None, True, "all 3 tickets closed"),
        (["Wayfinder:Map"], 3, "open", None, True, "all"),  # GitHub keeps a label's own case
        (["wayfinder:map"], 2, "open", None, False, "summary says 2 of 3"),
        # #3671: read right after the close, the summary still counts it open, but every
        # listed ticket is closed, the closing one whatever its lagging state says.
        (["wayfinder:map"], 2, "open", [_ticket(21), _ticket(22), _ticket(23, "open")],
         True, "all 3 tickets closed"),
        (["wayfinder:map"], 3, "open", [_ticket(21), _ticket(22, "open"), _ticket(23)],
         False, "tickets still open: [22]"),
        # Another repository's ticket that shares a closing ticket's number is not it.
        (["wayfinder:map"], 2, "open",
         [_ticket(21), _ticket(23), _ticket(21, "open", "eerovil/other")],
         False, "tickets still open: [21]"),
        # ... and minutes later still leaves out tickets filed since.
        (["wayfinder:map"], 3, "open", [_ticket(21), _ticket(22), _ticket(23), _ticket(24, "open")],
         False, "tickets still open: [24]"),
        (["agent"], 3, "open", None, False, "parent is not a wayfinder map"),
        (["wayfinder:map"], 3, "closed", None, False, "map is not open"),
        (None, 3, "open", None, False, None),  # the map could not be read
    ],
)
async def test_a_maps_last_ticket_closing_wakes_the_maps_chat(
    root, harness, caplog, labels, done, state, tickets, sent, why
):
    """#3627: when a wayfinder map's last open ticket closes, even under the deck's own login
    as a ticket's chat closes it, the map's own chat is told to reach the destination. Every
    outcome of the check is logged, so a miss can be explained (#3671)."""
    caplog.set_level("INFO", logger="agentdeck.work_router")
    harness.issues[("eerovil/agentdeck", 20)] = ("chat-m", "eerovil/agentdeck#20")
    asked = []

    async def read_issue(repository, number):
        asked.append((repository, number))
        if labels is None:
            raise RuntimeError("gh is down")
        return {"state": state, "labels": labels,
                "sub_issues_summary": {"total": 3, "completed": done},
                "last_closed": "2026-10-27T10:00:05Z", "sub_issues": tickets}

    harness.service._read_issue = read_issue
    parents = []

    async def read_parent(repository, number):
        parents.append(number)
        return ("eerovil/agentdeck", 20) if number in {21, 23} else None

    harness.service._read_parent = read_parent
    deliver(
        root,
        event="issues",
        delivery="d1",
        action="closed",
        sender={"login": "eerovil-agent", "type": "User"},
        # As GitHub sends it live: no ``parent_issue_url`` at all, so the router asks (#3627).
        issue={"number": 21, "state": "closed", "closed_at": "2026-10-27T10:00:00Z"},
    )
    deliver(root, event="issues", delivery="d2", action="closed",
            sender=PERSON, issue={"number": 22, "state": "closed", "parent_issue_url": None})
    # A second ticket closed seconds later: its delivery reads the same completed map.
    deliver(
        root,
        event="issues",
        delivery="d3",
        action="closed",
        sender={"login": "eerovil-agent", "type": "User"},
        issue={
            "number": 23, "state": "closed", "closed_at": "2026-10-27T10:00:05Z",
            "parent_issue_url": "https://api.github.com/repos/eerovil/agentdeck/issues/20",
        },
    )
    await harness.drain()
    # Only a sub-issue's close reads its parent; a close that names none asks GitHub.
    assert asked == [("eerovil/agentdeck", 20)] * 2
    assert parents == [21, 22]  # #23 named its parent in the payload
    logged = [r.getMessage() for r in caplog.records if r.name == "agentdeck.work_router"]
    assert "work-router: eerovil/agentdeck#22 closed, no parent map" in logged
    if why is None:
        assert any("map eerovil/agentdeck#20 unread" in line for line in logged)
    else:
        verdict = "ready" if sent else "not ready"
        assert any(
            line.startswith(f"work-router: eerovil/agentdeck#23 closed, map eerovil/agentdeck#20 "
                            f"{verdict}: {why}")
            for line in logged
        ), logged
    if not sent:
        assert harness.scheduled == []
        return
    [message] = harness.scheduled  # one wake for one completed map
    assert (message.id, message.chat_id) == (
        "work-event:map-ready:eerovil/agentdeck#20@2026-10-27T10:00:05Z", "chat-m"
    )
    # The first close to find the map complete names itself; with the list, that is #23's.
    closer = 21 if tickets is None else 23
    assert f"eerovil/agentdeck#{closer} closed" in message.text
    assert "all 3 are closed" in message.text
    assert "Reach the destination" in message.text


async def test_a_handed_over_maps_last_ticket_wakes_no_chat_here(root, harness):
    """#3731 review: the map went to another deck, whose agent reaches the destination."""
    harness.issues[("eerovil/agentdeck", 20)] = ("chat-m", "eerovil/agentdeck#20")
    harness.service.handed_over.add(f"{HANDED_OVER}github_issue:eerovil/agentdeck#20")

    async def read_issue(repository, number):
        return {"state": "open", "labels": ["wayfinder:map"],
                "sub_issues_summary": {"total": 3, "completed": 3},
                "last_closed": "2026-10-27T10:00:05Z", "sub_issues": None}

    harness.service._read_issue = read_issue
    deliver(root, event="issues", delivery="d1", action="closed", sender=PERSON, issue={
        "number": 23, "state": "closed", "closed_at": "2026-10-27T10:00:05Z",
        "parent_issue_url": "https://api.github.com/repos/eerovil/agentdeck/issues/20",
    })
    await harness.drain()
    assert harness.scheduled == []


async def test_a_project_that_has_not_switched_is_left_to_its_poller(root, harness):
    comment(root, "d1", repository=OTHER)
    harness.issues[("eerovil/trailhead", 7)] = ("chat-b", "eerovil/trailhead#7")
    pull_event(root, "d2", repository=OTHER)
    await harness.drain()
    assert harness.scheduled == [] and harness.reviews == []


async def test_an_issue_no_chat_owns_is_dropped(root, harness):
    comment(root, "d1", number=8)
    await harness.drain()
    assert harness.scheduled == []
    # #3405: a finished Work's chats are let go from memory, so its issue had no owner and a
    # person's comment on it was dropped. The router reads them back first, then routes.
    loads = []

    async def load_work(repository, number):
        loads.append((repository, number))
        harness.issues[(repository, number)] = ("chat-done", f"{repository}#{number}")

    harness.service._load_work = load_work
    comment(root, "d2", number=8)
    comment(root, "d3", number=7)  # an owned issue loads nothing
    comment(root, "d4", number=9, on_pull=True)  # nor does a pull request's comment
    await harness.drain()
    assert loads == [("eerovil/agentdeck", 8)]
    assert [message.chat_id for message in harness.scheduled] == ["chat-done", "chat-a", "chat-a"]


async def test_a_comment_on_a_pull_request_goes_to_the_chat_that_registered_it(root, harness):
    harness.pulls[("eerovil/agentdeck", 7)] = ("chat-pr", "eerovil/agentdeck#3")
    comment(root, "d1", on_pull=True)
    await harness.drain()
    assert [message.chat_id for message in harness.scheduled] == ["chat-pr"]


async def test_each_head_is_sent_to_the_reviewer_once(root, harness):
    pull_event(root, "d1", action="ready_for_review")
    pull_event(root, "d2", action="synchronize")  # the same head again
    pull_event(root, "d3", action="synchronize", sha="1" * 40)
    await harness.drain()
    assert harness.reviews == [
        ("eerovil/agentdeck#9", "eerovil/agentdeck#7"),
        ("eerovil/agentdeck#9", "eerovil/agentdeck#7"),
    ]
    assert "eerovil/agentdeck#9@" + "1" * 40 in harness.service.reviewed


async def test_a_reviewed_head_survives_a_restart(root, harness):
    pull_event(root, "d1")
    await harness.drain()
    again = Harness(root)
    pull_event(root, "d2", action="synchronize")
    await again.drain()
    assert again.reviews == [], "the head is remembered on disk"


async def test_an_unregistered_pull_request_is_not_reviewed(root, harness):
    harness.pulls.clear()
    pull_event(root, "d1")
    await harness.drain()
    assert harness.reviews == []


async def test_a_review_that_cannot_start_does_not_stop_the_batch(root, harness):
    harness.fail_review = True
    pull_event(root, "d1")
    comment(root, "d2")
    await harness.drain()
    assert len(harness.reviews) == 1
    assert [message.id for message in harness.scheduled] == ["work-event:d2"]


async def test_a_first_start_skips_the_streams_history(root, harness):
    comment(root, "d1")
    assert harness.service.skip_history() is True
    comment(root, "d2")
    tailer = WebhookTailer(root, consumer="work-router")
    assert [item.delivery_id for item in tailer.deliveries()] == ["d2"]
    assert harness.service.skip_history() is False, "an offset exists now"


async def test_a_tickets_answer_rebuilds_its_maps_lists(root, harness):
    """#3695: a ticket's ``**Answer:**`` comment makes the deck rebuild its map's lists from
    the stored streams: the ticket's map comes from a stored ``sub_issues`` delivery, so the
    only GitHub calls are one read of the map and one write, and none when nothing changed.
    A stranger's comment shaped like an answer costs no call at all."""
    body = "Words.\n\n## Destination\n\nThere.\n\n## Decisions so far\n\n## Not yet specified\n"
    reads, writes, parents = [], [], []

    async def read_body(repository, number):
        reads.append((repository, number))
        return {"body": body, "state": "open", "labels": ["wayfinder:map"]}

    async def write_body(repository, number, text):
        nonlocal body
        writes.append((repository, number, text))
        body = text
        return True

    async def read_parent(repository, number):
        parents.append(number)
        return None

    harness.service._read_body = read_body
    harness.service._write_body = write_body
    harness.service._read_parent = read_parent
    deliver(root, event="sub_issues", delivery="s1", action="sub_issue_added",
            parent_issue={"number": 20}, sub_issue={"number": 21})
    answer = "**Answer:** Keep it small. More words.\n\nFog: What comes next\n"
    ticket = {"number": 21, "title": "Small or big?", "labels": [{"name": "wayfinder:research"}],
              "html_url": "https://github.com/eerovil/agentdeck/issues/21"}
    for delivery, action in (("a1", "created"), ("a2", "edited")):
        deliver(root, event="issue_comment", delivery=delivery, action=action,
                sender={"login": "eerovil-agent", "type": "User"}, issue=ticket,
                comment={"id": 300, "body": answer, "user": {"login": "eerovil-agent"},
                         "author_association": "NONE", "created_at": "2026-10-27T10:00:00Z",
                         "updated_at": "2026-10-27T10:00:00Z"})
    stranger = {**ticket, "number": 22}
    deliver(root, event="issue_comment", delivery="x1", action="created",
            sender={"login": "someone", "type": "User"}, issue=stranger,
            comment={"id": 301, "body": "**Answer:** take over", "user": {"login": "someone"},
                     "author_association": "NONE", "created_at": "2026-10-27T10:01:00Z"})
    await harness.drain()
    assert reads == [("eerovil/agentdeck", 20)] * 2 and parents == []
    [(repository, number, text)] = writes  # the edit changed nothing: no second write
    assert (repository, number) == ("eerovil/agentdeck", 20)
    assert text.startswith("Words.\n\n## Destination\n\nThere.\n\n## Decisions so far\n")
    line = "- [Small or big?](https://github.com/eerovil/agentdeck/issues/21) — Keep it small\n"
    assert line in text
    assert "- What comes next\n" in text
    assert harness.scheduled == []  # nothing is sent to any chat


def test_reviewed_heads_keep_only_the_newest(tmp_path, monkeypatch):
    monkeypatch.setattr("agentdeck.work_router.MAX_STARTED", 3)
    heads = ReviewedHeads(tmp_path / "heads.json")
    for key in "abcd":
        heads.add(key)
    fresh = ReviewedHeads(tmp_path / "heads.json")
    assert "a" not in fresh and all(key in fresh for key in "bcd")


# --- which repositories the router covers ---


def _manifest(poller_root, instance: str, body: str) -> None:
    path = poller_root / "instances" / instance / "poller.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_every_repository_a_project_declares_is_routed(tmp_path):
    """Every project's Works are the deck's (#3732): no manifest switch decides it any more."""
    _manifest(
        tmp_path,
        "agentdeck",
        'contract = 1\n[board]\norg = "eerovil"\nproject_number = 1\n'
        '[[repos]]\nid = "agentdeck"\nholds_issues = true\n'
        '[[repos]]\nid = "review-chats"\n',
    )
    _manifest(
        tmp_path,
        "trailhead",
        'contract = 1\n[board]\norg = "Northwind"\nproject_number = 2\n'
        '[[repos]]\nid = "issues"\nholds_issues = true\n',
    )
    assert work_repositories(tmp_path) == {
        "eerovil/agentdeck", "eerovil/review-chats", "northwind/issues",
    }


@pytest.mark.parametrize("value", ["true", "false", '"true"'])
def test_a_manifest_still_naming_the_old_switch_loads_and_is_routed(tmp_path, value):
    _manifest(
        tmp_path,
        "agentdeck",
        f'contract = 1\n[board]\norg = "eerovil"\nproject_number = 1\n'
        f"[capabilities]\nworks_router = {value}\n"
        '[[repos]]\nid = "agentdeck"\n',
    )
    assert work_repositories(tmp_path) == {"eerovil/agentdeck"}


def test_the_repositories_are_reread_when_a_manifest_changes(tmp_path):
    body = 'contract = 1\n[board]\norg = "Eerovil"\nproject_number = 1\n'
    body += '[[repos]]\nid = "agentdeck"\nholds_issues = true\n'
    _manifest(tmp_path, "agentdeck", body)
    switched = SwitchedRepositories(tmp_path)
    assert switched("eerovil/agentdeck")
    assert not switched("eerovil/notes")
    _manifest(tmp_path, "agentdeck", body + '[[repos]]\nid = "notes"\n')
    assert switched("EEROVIL/notes"), "a different size is a different manifest"


# --- who owns an issue or a pull request ---


class FakeState:
    def __init__(self, *, memberships=(), pulls=None, joined=None, activity=None) -> None:
        now = datetime.now(UTC)
        self.activity = activity or {}
        self.sessions = {
            chat: SimpleNamespace(last_activity=now - timedelta(minutes=age))
            for chat, age in self.activity.items()
        }
        self.session_pull_requests = pulls or {}
        self._joined = joined or {}
        self._work = (
            SimpleNamespace(
                ref=SimpleNamespace(repository="eerovil/agentdeck", number=7),
                memberships=tuple(
                    SimpleNamespace(chat_id=chat, direct=direct) for chat, direct in memberships
                ),
            )
            if memberships
            else None
        )

    def chat_by_id(self, chat_id):
        return None

    def current_session_for_chat(self, chat):
        return None

    def chat_identity_key(self, leg):
        return leg.split("/")[0]

    def work_projection(self):
        state = self

        class Projection:
            def get(self, ref):
                if state._work is None or (ref.repository, ref.number) != (
                    "eerovil/agentdeck",
                    7,
                ):
                    return None
                return state._work

            def works_chat_joined(self, chat):
                return state._joined.get(chat, ())

        return Projection()


def test_the_issue_owner_is_its_most_recent_direct_member():
    state = FakeState(
        memberships=[("old", True), ("new", True), ("delegate", False)],
        activity={"old": 30, "new": 1, "delegate": 0},
    )
    assert owner_of_issue(state, "EEROVIL/agentdeck", 7) == ("new", "eerovil/agentdeck#7")
    assert owner_of_issue(state, "eerovil/agentdeck", 8) is None


def test_a_pull_request_belongs_to_the_one_work_its_registering_chat_joined():
    ref = PullRequestRef("Eerovil/AgentDeck", 9)
    work = SimpleNamespace(repository="eerovil/agentdeck", number=7)
    state = FakeState(
        pulls={"chat-a/leg1": (ref,)},
        joined={"chat-a": (work,)},
        activity={"chat-a": 0},
    )
    assert owner_of_pull(state, "eerovil/agentdeck", 9) == ("chat-a", "eerovil/agentdeck#7")
    assert owner_of_pull(state, "eerovil/other", 9) is None, "same number, other repository"


def test_a_pull_request_whose_chat_joined_two_works_is_not_placed():
    ref = PullRequestRef("eerovil/agentdeck", 9)
    works = (
        SimpleNamespace(repository="eerovil/agentdeck", number=7),
        SimpleNamespace(repository="eerovil/agentdeck", number=8),
    )
    state = FakeState(pulls={"chat-a": (ref,)}, joined={"chat-a": works}, activity={"chat-a": 0})
    assert owner_of_pull(state, "eerovil/agentdeck", 9) is None
    # Registered for one of them, it is placed there (#3392); for a Work the chat left, not.
    for work_id, placed in (
        ("github_issue:EEROVIL/agentdeck#8", ("chat-a", "eerovil/agentdeck#8")),
        ("github_issue:eerovil/agentdeck#6", None),
    ):
        state.session_pull_requests = {"chat-a": (PullRequestRef("eerovil/agentdeck", 9, work_id),)}
        assert owner_of_pull(state, "eerovil/agentdeck", 9) == placed
        assert work_pulls(state, "github_issue:eerovil/agentdeck#8") == (
            ("eerovil/agentdeck#9",) if placed else ()
        )


def test_work_pulls_places_every_pull_with_one_projection():
    """#2954: one projection for the whole deck, and the same set ``owner_of_pull`` gives."""
    seven = SimpleNamespace(repository="eerovil/agentdeck", number=7)
    eight = SimpleNamespace(repository="eerovil/agentdeck", number=8)
    state = FakeState(
        pulls={
            "chat-a/leg1": (PullRequestRef("Eerovil/AgentDeck", 9),),
            "chat-a/leg2": (
                PullRequestRef("eerovil/agentdeck", 9),
                PullRequestRef("eerovil/other", 9),
            ),
            # Both register #10; chat-b, the more recently active, places it in Work 8.
            "chat-b": (PullRequestRef("eerovil/agentdeck", 10),),
            "chat-a/leg3": (PullRequestRef("eerovil/agentdeck", 10),),
            "chat-c": (PullRequestRef("eerovil/agentdeck", 11),),  # joined two Works
            "chat-d": (PullRequestRef("eerovil/agentdeck", 12),),  # joined none
        },
        joined={"chat-a": (seven,), "chat-b": (eight,), "chat-c": (seven, eight)},
        activity={"chat-a": 5, "chat-b": 1, "chat-c": 0, "chat-d": 0},
    )
    builds = []
    projection = state.work_projection
    state.work_projection = lambda: builds.append(1) or projection()

    every = {ref.identity for refs in state.session_pull_requests.values() for ref in refs}
    for work_id in ("github_issue:eerovil/agentdeck#7", "github_issue:EEROVIL/agentdeck#8"):
        builds.clear()
        expected = sorted(
            f"{repository}#{number}"
            for repository, number in every
            if (owner := owner_of_pull(state, repository, number))
            and owner[1].lower() == work_id.partition(":")[2].lower()
        )
        builds.clear()
        assert list(work_pulls(state, work_id)) == expected
        assert len(builds) == 1, "one projection, not one per registered pull request"
    assert work_pulls(state, "github_issue:eerovil/agentdeck#7") == (
        "eerovil/agentdeck#9",
        "eerovil/other#9",
    )
    assert work_pulls(state, "github_issue:eerovil/agentdeck#8") == ("eerovil/agentdeck#10",)


async def test_a_busy_reviewer_leaves_the_head_for_the_next_event(root, harness):
    from agentdeck.chat_review import ReviewBusy

    async def busy(pull, work):
        harness.reviews.append((pull, work))
        raise ReviewBusy("someone is mid-review")

    harness.service._start_review = busy
    pull_event(root, "d1")
    await harness.drain()
    assert "eerovil/agentdeck#9@" + SHA not in harness.service.reviewed
    pull_event(root, "d2", action="synchronize")
    await harness.drain()
    assert len(harness.reviews) == 2, "asked again on the next event"


async def test_an_ask_again_that_cannot_start_now_keeps_its_place_and_note(root, harness):
    """#3183 review: an old review chat that would not close leaves the ask pending, counted
    against the cap, and its retry carries the agent's note into the new chat."""
    from agentdeck.chat_review import ReviewBusy

    pull = "eerovil/agentdeck#9"
    harness.service.reviewed.add(f"{pull}@{OLD}")
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    calls = []

    async def busy_once(pull, work, **asks):
        calls.append(asks)
        if len(calls) == 1:
            raise ReviewBusy("chat #12 is still open")

    harness.service._start_review = busy_once
    asked = await harness.service.ask_again("eerovil/agentdeck", 9, "owner keeps it")
    assert asked["outcome"] == "pending"
    assert harness.service.reviewed.count(f"{pull}@{OLD}+") == 1
    harness.now += 3600
    await harness.service.retry_pending()
    assert calls[1] == {"note": "owner keeps it", "again": True}
    assert harness.service.reviewed.count(f"{pull}@{OLD}+") == 1
    assert not harness.service.pending.entries

    # An ask whose retry is dropped (the head moved on) frees its place under the cap.
    calls.clear()
    await harness.service.ask_again("eerovil/agentdeck", 9, "walkthrough posted")
    assert harness.service.reviewed.count(f"{pull}@{OLD}+") == 2
    harness.heads[("eerovil/agentdeck", 9)] = ""
    harness.now += 3600
    await harness.service.retry_pending()
    assert harness.service.reviewed.count(f"{pull}@{OLD}+") == 1

    # #3324: kept while the running review of that head passes it, the ask is answered, not
    # reviewed: a new review re-opened a Work already waiting for its Merge.
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    passes: list[float] = []

    pull_ = pull

    class Release:
        def passed_since(self, work, pull, head, since):
            assert (work, pull, head) == ("github_issue:eerovil/agentdeck#7", pull_, OLD)
            return any(at >= since for at in passes)

    harness.service._release = Release()
    calls.clear()
    harness.service._start_review = busy_once  # busy at first again
    await harness.service.ask_again("eerovil/agentdeck", 9, "owner keeps it")
    assert len(calls) == 1 and harness.service.pending.entries
    passes.append(harness.now + 5)
    harness.now += 3600
    await harness.service.retry_pending()
    assert len(calls) == 1, "no review of a head that passed since the ask"
    assert not harness.service.pending.entries
    assert harness.service.reviewed.count(f"{pull}@{OLD}+") == 1, "its place is freed"


async def test_a_refused_review_leaves_the_head_for_the_next_event(root, harness):
    from agentdeck.chat_review import ReviewError

    async def refused(pull, work):
        harness.reviews.append((pull, work))
        raise ReviewError("this deck has no [chat_board] repository configured")

    harness.service._start_review = refused
    pull_event(root, "d1")
    await harness.drain()
    assert "eerovil/agentdeck#9@" + SHA not in harness.service.reviewed
    pull_event(root, "d2", action="synchronize")
    await harness.drain()
    assert len(harness.reviews) == 2, "a start that wrote nothing is asked again"


async def test_an_uncertain_review_start_spends_the_head(root, harness):
    harness.fail_review = True
    pull_event(root, "d1")
    pull_event(root, "d2", action="synchronize")
    await harness.drain()
    assert len(harness.reviews) == 1, "it may have written, so it is not pressed again"


async def test_a_start_cut_off_by_a_crash_is_asked_again(root, harness):
    class Crash(BaseException):
        pass

    async def crash(pull, work):
        harness.reviews.append((pull, work))
        raise Crash

    harness.service._start_review = crash
    pull_event(root, "d1")
    with pytest.raises(Crash):
        await harness.drain()
    again = Harness(root)
    await again.drain()  # the uncommitted delivery is replayed after the restart
    assert again.reviews == [("eerovil/agentdeck#9", "eerovil/agentdeck#7")]


async def test_registering_a_ready_pull_request_starts_its_review(root, harness):
    harness.heads[("eerovil/agentdeck", 9)] = SHA.upper()
    await harness.service.check_registered("EEROVIL/agentdeck", 9)
    assert harness.reviews == [("eerovil/agentdeck#9", "eerovil/agentdeck#7")]
    pull_event(root, "d1", action="synchronize")  # the same head's event, late
    await harness.drain()
    assert len(harness.reviews) == 1, "registration and event share one start per head"


OLD = "c" * 40
NEW = "a" * 40


async def test_ready_then_push_reviews_the_new_head(root, harness):
    """#2941's order: ready at an already-reviewed head, then a push whose payload says draft.

    Marked ready at a reviewed head no longer asks for its review again (#3344): the chat is
    told to ask with a note instead, and the push still gets its own review.
    """
    harness.service.reviewed.add(f"eerovil/agentdeck#9@{OLD}")
    harness.heads[("eerovil/agentdeck", 9)] = OLD  # the push has not landed yet
    pull_event(root, "d1", action="ready_for_review", sha=OLD)
    await harness.drain()
    assert harness.asks == []
    assert [item.id for item in harness.scheduled] == ["work-event:d1"]
    harness.heads[("eerovil/agentdeck", 9)] = NEW.upper()
    pull_event(root, "d2", action="synchronize", sha=NEW, draft=True)
    await harness.drain()
    assert harness.reviews == [("eerovil/agentdeck#9", "eerovil/agentdeck#7")]
    assert harness.asks == [{}]
    assert f"eerovil/agentdeck#9@{NEW}" in harness.service.reviewed


async def test_a_head_that_only_merged_its_base_is_not_reviewed_again(root, harness):
    """#3314: a master merge touching no file of the change reopened a finding a person had
    to settle. Such a head keeps the verdict; a head whose review has not come back, a changed
    diff, the same lines moved inside the change's files (#3314 review), an unread digest and
    the agent's own ask are all reviewed, and a deferred event is still skipped on retry."""
    from agentdeck.chat_review import ReviewBusy

    pull, third, moved, synced = "eerovil/agentdeck#9", "b" * 40, "9" * 40, "7" * 40
    digests = {OLD: "d" * 64, synced: "d" * 64, NEW: "d" * 64, third: "e" * 64, moved: "e" * 64}
    compared = []

    async def digest(ref, head):
        assert ref == pull
        return digests.get(head, "")

    async def base_only(repository, number, old, new):
        compared.append((old, new))
        return (repository, number) == ("eerovil/agentdeck", 9) and new in {synced, NEW}

    harness.service._digest = digest
    harness.service._base_only = base_only
    pull_event(root, "d1", sha=OLD)
    # Its review has not come back (running, failed): no verdict stands, so it is reviewed.
    pull_event(root, "d1b", action="synchronize", sha=synced)
    await harness.drain()
    assert harness.asks == [{}, {}] and compared == [] and not harness.scheduled
    harness.service.reviewed.note_verdict(pull, synced[:12])  # the release took a verdict
    pull_event(root, "d2", action="synchronize", sha=NEW)
    await harness.drain()
    assert len(harness.asks) == 2 and compared == [(synced, NEW)]
    [told] = harness.scheduled
    assert told.id == f"work-unchanged:{pull}@{NEW}" and "verdict on that diff stands" in told.text
    assert harness.verdicts == [("chat-a", [pull])]
    # The release credits the old head's verdict to the new one, across a restart, short shas
    # too; never the other way round.
    reread = ReviewedHeads(harness.service.reviewed.path)
    assert reread.same_change(pull, NEW, synced[:12])
    assert not reread.same_change(pull, synced, NEW) and reread.answered(pull, NEW)
    # A person settled a finding: the agent's ask reviews the head after all.
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    await harness.service.ask_again("eerovil/agentdeck", 9, "owner accepts the gap")
    assert harness.asks[2:] == [{"note": "owner accepts the gap", "again": True}]
    pull_event(root, "d3", action="synchronize", sha=third)
    pull_event(root, "d4", action="synchronize", sha=moved)  # same digest, files touched
    pull_event(root, "d5", action="synchronize", sha="f" * 40)  # its digest unread
    await harness.drain()
    assert len(harness.asks) == 6 and not reread.same_change(pull, moved, third)

    # A review event deferred by a busy reviewer is still skipped when its retry runs.
    async def busy(pull, work, **asks):
        raise ReviewBusy("someone is mid-review")

    harness.service._start_review = busy
    digests["1" * 40] = digests["2" * 40] = "f" * 64
    harness.service.reviewed.note_digest(pull, "1" * 40, "f" * 64)
    harness.service.reviewed.note_verdict(pull, "1" * 40)
    pull_event(root, "d6", action="synchronize", sha="2" * 40)
    harness.service._base_only = lambda *args: asyncio.sleep(0, result=False)
    await harness.drain()
    assert harness.service.pending.entries
    harness.service._base_only = lambda *args: asyncio.sleep(0, result=True)
    harness.heads[("eerovil/agentdeck", 9)] = "2" * 40
    harness.now += 3600
    await harness.service.retry_pending()
    assert not harness.service.pending.entries
    assert harness.scheduled[-1].id == f"work-unchanged:{pull}@{'2' * 40}"


async def test_fixing_a_no_code_review_starts_no_new_review(root, harness, monkeypatch):
    """#3322: a ready press at the verdict's own head, or a push of doc files, answers a
    no-code review, which then counts as a pass; a push that changes code is reviewed."""
    from agentdeck import work_router

    pull, third = "eerovil/agentdeck#9", "b" * 40
    fixed = []

    class Release:
        held = (OLD, frozenset({"src/a.py"}))

        async def on_pull_event(self, *args):
            return None

        def no_code_head(self, work, ref):
            return self.held if ref == pull else None

        async def no_code_fixed(self, work, ref, head):
            fixed.append((work, ref, head))
            self.held = None
            return f"review pass recorded for {ref} at {head[:12]}"

        def passed_since(self, work, ref, head, since):
            return any(item[1:] == (ref, head) for item in fixed)

    harness.service._release = Release()
    harness.service._no_code = lambda repo, n, old, new, before: asyncio.sleep(
        0, result=new == NEW and before == frozenset({"src/a.py"})
    )
    harness.service.reviewed.add(f"{pull}@{OLD}")
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    pull_event(root, "d1", action="synchronize", sha=OLD)  # a push is not "fixed"
    pull_event(root, "d2", action="ready_for_review", sha=OLD)
    await harness.drain()
    assert harness.asks == [] and fixed == [("github_issue:eerovil/agentdeck#7", pull, OLD)]
    # A second draft-and-ready press at the passed head asks no review (#3341, review-chats
    # #6363 re-read docker#199 at the head its no-code review had just passed).
    pull_event(root, "d2b", action="ready_for_review", sha=OLD)
    await harness.drain()
    assert harness.asks == [] and not harness.service.pending.entries
    assert "already has a review pass" in harness.scheduled[-1].text
    harness.service._release.held = (OLD, frozenset({"src/a.py"}))
    pull_event(root, "d3", action="synchronize", sha=NEW)
    await harness.drain()
    assert harness.asks == [] and fixed[-1][2] == NEW
    assert f"{pull}@{NEW}" in harness.service.reviewed
    pull_event(root, "d4", action="synchronize", sha=third)
    await harness.drain()
    assert harness.asks == [{}] and len(fixed) == 2

    # The proof: doc files, or files of neither diff (a base merge); never a file of the diff
    # then, which a revert drops from the diff now.
    touched: set[str] | None = set()

    async def compared(repository, old, new):
        return touched

    async def files(repository, number):
        return {"src/a.py", "README.md"}

    monkeypatch.setattr(work_router, "compared_files", compared)
    monkeypatch.setattr(work_router, "pull_files", files)
    between = work_router.no_code_between
    for changed, before, proven in (
        ({"README.md", "docs/x.png", ".cursor/rules/a.mdc"}, None, True),
        ({"README.md", "src/b.py"}, {"src/a.py"}, True),
        ({"README.md", "src/b.py"}, None, False),
        ({"src/b.py"}, {"src/b.py"}, False),
        ({"src/a.py"}, set(), False),
        ({"requirements.txt"}, None, False),
        (None, None, False),
    ):
        touched = changed
        assert await between("eerovil/agentdeck", 9, OLD, NEW, before) is proven, changed


async def test_a_reviewed_head_is_reviewed_again_on_request_at_most_twice(root, harness):
    """#3183: a person settled a finding with no code change, so no push starts a review.

    The agent's request carries its note to the reviewer, a request while one runs is
    refused, and a third ask goes to a person. A request with no note, or a draft-and-ready,
    asks nothing at a reviewed head: neither tells the reviewer what changed (#3344). An ask
    after a review with no verdict is counted apart, two of its own (#3453).
    """
    from agentdeck.work_router import AgainBusy, AgainRefused

    pull = "eerovil/agentdeck#9"
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    # The first ready at a head is its ordinary review, under the plain key (#3183 review).
    pull_event(root, "d0", action="ready_for_review", sha=OLD)
    pull_event(root, "d0b", action="synchronize", sha=OLD)
    await harness.drain()
    assert harness.asks == [{}]
    assert f"{pull}@{OLD}" in harness.service.reviewed
    harness.asks.clear()
    harness.reviews.clear()
    asked = await harness.service.ask_again("eerovil/agentdeck", 9, "owner keeps it")
    assert asked == {"pull_request": pull, "head": OLD, "again": True, "outcome": "asked"}
    assert harness.asks == [{"note": "owner keeps it", "again": True}]

    pull_event(root, "d1", action="ready_for_review", sha=OLD)
    pull_event(root, "d2", action="synchronize", sha=OLD)  # a push is not a request
    await harness.drain()
    assert harness.asks[1:] == []
    assert [item.id for item in harness.scheduled] == ["work-event:d1"]
    # No verdict yet and no review chat known: that review is still taken as running (#3584).
    assert "is already running" in harness.scheduled[0].text

    with pytest.raises(AgainRefused, match="say in the note what changed"):
        await harness.service.ask_again("eerovil/agentdeck", 9, "  ")
    for _ in range(2):
        await harness.service.ask_again("eerovil/agentdeck", 9, "no verdict", no_verdict=True)
    with pytest.raises(AgainRefused, match="after a review with no verdict"):
        await harness.service.ask_again("eerovil/agentdeck", 9, "no verdict", no_verdict=True)
    await harness.service.ask_again("eerovil/agentdeck", 9, "walkthrough posted")
    assert harness.asks[1:] == [
        {"note": "no verdict", "again": True},
        {"note": "no verdict", "again": True},
        {"note": "walkthrough posted", "again": True},
    ]

    with pytest.raises(AgainRefused, match="how to settle the finding"):
        await harness.service.ask_again("eerovil/agentdeck", 9, "once more")
    assert len(harness.reviews) == 4

    # The cap is one locked step on the shared file: a second worker's view of the same
    # file cannot take a place the first already filled (#3183 review).
    other = ReviewedHeads(harness.service.reviewed.path)
    assert not other.reserve(f"{pull}@{OLD}+", 2, f"{pull}@{OLD}+x?")

    harness.service._in_flight.add(f"{pull}@{NEW}")
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    with pytest.raises(AgainBusy, match="already asked"):
        await harness.service.ask_again("eerovil/agentdeck", 9)
    harness.heads[("eerovil/agentdeck", 9)] = ""
    with pytest.raises(AgainRefused, match="gh pr ready"):
        await harness.service.ask_again("eerovil/agentdeck", 9)


@private_feature(_chat_review)
async def test_a_ready_press_at_a_head_under_review_says_where_its_review_is(root, harness):
    """#3584: a head a review was asked for is not a reviewed head. A verdict with findings
    says fix them; a review still open (or not known) waits on its verdict; one whose chat
    ended with no verdict is asked for again as ``no_verdict``, at most twice."""
    import dataclasses

    from agentdeck.chat_board import ChatRow

    pull, chat = "eerovil/agentdeck#9", "eerovil/review-chats#50"
    rows: dict[str, ChatRow | None] = {}
    waits: list[tuple[str, str, bool]] = []
    harness.service._review_chat = lambda ref, head: (chat, rows[head]) if head in rows else None
    harness.service._wait_review = lambda chat_id, ref, asked: waits.append((chat_id, ref, asked))
    harness.service.reviewed.add(f"{pull}@{OLD}")
    harness.heads[("eerovil/agentdeck", 9)] = OLD

    def stamp(ago: float) -> str:
        return datetime.fromtimestamp(harness.now - ago, UTC).isoformat()

    async def press(delivery: str) -> None:
        pull_event(root, delivery, action="ready_for_review", sha=OLD)
        await harness.drain()

    rows[OLD] = ChatRow(50, "Review eerovil/agentdeck#7", labels=("in-progress",))
    await press("r1")
    text = harness.scheduled[-1].text
    assert "already running in eerovil/review-chats#50 (in-progress)" in text
    assert "this chat now waits on it" in text and waits == [("chat-a", pull, False)]
    # Closed a minute ago: its verdict may still be on its way, so it is waited on too.
    rows[OLD] = ChatRow(50, "t", state="closed", closed_at=stamp(60), updated_at=stamp(60))
    await press("r2")
    assert "already running" in harness.scheduled[-1].text and harness.asks == []
    # Closed with no verdict long enough ago: asked again, and the new review is waited on.
    rows[OLD] = dataclasses.replace(rows[OLD], closed_at=stamp(600), updated_at=stamp(600))
    await press("r3")
    assert harness.asks == [{"note": "marked ready after the review of this head ended with no"
                             " verdict", "again": True}]
    assert "asked for a fresh review" in harness.scheduled[-1].text
    assert waits[-1] == ("chat-a", pull, True)
    # One already asked for and not started yet is the review this press waits on.
    harness.service.pending.put(f"review:{pull}@{OLD}+nvx", {}, harness.now)
    await press("r4")
    assert "already running" in harness.scheduled[-1].text and len(harness.asks) == 1
    harness.service.pending.discard(f"review:{pull}@{OLD}+nvx")
    await press("r5")  # the second place for a no-verdict ask
    await press("r6")  # past the cap, a person decides
    assert len(harness.asks) == 2 and "after a review with no verdict" in harness.scheduled[-1].text
    # A verdict with findings: fix them, nothing is asked and nothing waits.
    harness.service.reviewed.note_verdict(pull, OLD[:12])
    count = len(waits)
    await press("r7")
    assert "has findings: fix them" in harness.scheduled[-1].text
    assert len(harness.asks) == 2 and len(waits) == count


async def test_a_ready_event_at_a_reviewed_head_reviews_the_live_head(root, harness):
    harness.service.reviewed.add(f"eerovil/agentdeck#9@{OLD}")
    harness.heads[("eerovil/agentdeck", 9)] = NEW  # the push landed before the read
    pull_event(root, "d1", action="ready_for_review", sha=OLD)
    await harness.drain()
    assert harness.reviews == [("eerovil/agentdeck#9", "eerovil/agentdeck#7")]
    assert f"eerovil/agentdeck#9@{NEW}" in harness.service.reviewed


async def test_a_push_to_a_real_draft_starts_nothing(root, harness):
    harness.service._paused = lambda work_id: True
    resumed = []

    async def resume(work_id):
        resumed.append(work_id)
        return True

    harness.service._resume = resume
    pull_event(root, "d1", action="synchronize", sha=NEW, draft=True)  # live: still a draft
    await harness.drain()
    assert harness.reviews == [] and resumed == [], "a draft push resumes no paused Work"


async def test_registering_a_draft_or_unowned_pull_request_starts_nothing(root, harness):
    await harness.service.check_registered("eerovil/agentdeck", 9)  # draft: no ready head
    harness.heads[("eerovil/agentdeck", 5)] = SHA
    await harness.service.check_registered("eerovil/agentdeck", 5)  # no Work owns it
    assert harness.reviews == []


async def test_note_registered_runs_the_check_in_the_background(root, harness):
    import asyncio

    harness.heads[("eerovil/agentdeck", 9)] = SHA
    harness.service.note_registered("eerovil/trailhead", 9)  # not switched
    harness.service.note_registered("eerovil/agentdeck", 9)
    await asyncio.gather(*harness.service._checks)
    assert harness.reviews == [("eerovil/agentdeck#9", "eerovil/agentdeck#7")]


def merged_pull(root, delivery: str, *, merged=True, number=9, action="closed"):
    deliver(
        root,
        event="pull_request",
        delivery=delivery,
        action=action,
        sender=PERSON,
        pull_request={"number": number, "state": "closed", "merged": merged},
    )


async def test_a_merged_pull_request_rereads_its_works_issue(root, harness):
    """#2947: a dropped ``issues`` ``closed`` delivery must not hide the close."""
    import asyncio

    merged_pull(root, "d1")
    await harness.drain()
    await asyncio.gather(*harness.service._checks)
    assert harness.rereads == [("eerovil/agentdeck", 7)]


@pytest.mark.parametrize(
    "fields",
    [
        {"merged": False},  # closed without merging
        {"action": "opened"},
        {"number": 5},  # no Work owns it
    ],
)
async def test_only_a_merge_of_an_owned_pull_request_rereads(root, harness, fields):
    import asyncio

    merged_pull(root, "d1", **fields)
    await harness.drain()
    await asyncio.gather(*harness.service._checks)
    assert harness.rereads == []


async def test_an_unswitched_project_is_not_reread(root, harness):
    import asyncio

    harness.pulls[("eerovil/trailhead", 9)] = ("chat-b", "eerovil/trailhead#3")
    deliver(
        root,
        event="pull_request",
        delivery="d2",
        repository=OTHER,
        action="closed",
        pull_request={"number": 9, "merged": True},
    )
    await harness.drain()
    await asyncio.gather(*harness.service._checks)
    assert harness.rereads == []


async def test_a_failed_reread_does_not_stop_routing(root, harness):
    import asyncio

    harness.pulls[("eerovil/agentdeck", 9)] = ("chat-a", "eerovil/agentdeck#13")
    merged_pull(root, "d1")
    comment(root, "d2")
    await harness.drain()
    await asyncio.gather(*harness.service._checks)
    assert harness.rereads == [("eerovil/agentdeck", 13)]
    assert [message.id for message in harness.scheduled] == ["work-event:d2"]


async def test_a_comment_the_cli_would_drop_is_sent_as_a_link(root, harness):
    harness.refuse = lambda text: "dropped" if "Stuck?" in text else None
    comment(root, "d1", body="/claude Stuck? Try the other branch.")
    await harness.drain()
    [message] = harness.scheduled
    assert "Stuck?" not in message.text
    assert "eerovil commented on issue eerovil/agentdeck#7" in message.text
    assert "https://github.com/c/d1" in message.text


def test_own_logins_come_from_the_configured_authority(tmp_path, monkeypatch):
    from agentdeck.poller_engine import github_authority
    from agentdeck.work_router import configured_own_logins

    config = github_authority.AccountsConfig(owners={}, agent_login="Other-Agent")
    monkeypatch.setattr(github_authority, "load_accounts", lambda: config)
    assert configured_own_logins() == {"other-agent"}

    def broken():
        raise github_authority.ConfigError("unreadable")

    monkeypatch.setattr(github_authority, "load_accounts", broken)
    assert configured_own_logins() == frozenset()  # no built-in login since #3623


async def test_the_service_reads_its_own_logins_per_batch(root):
    logins = {"first-agent"}
    harness = Harness(root)
    harness.service._own_logins = lambda: logins
    comment(root, "d1", sender={"login": "Second-Agent", "type": "User"})
    logins = {"second-agent"}
    harness.service._own_logins = lambda: logins
    await harness.drain()
    assert harness.scheduled == [], "the configured agent's own comment is not echoed"


async def test_a_new_head_while_the_old_heads_chat_is_held_is_asked_again(root, harness):
    old = "1" * 40
    answers = {
        # The worker holds the old head's chat `in-progress`, so it is kept and answered.
        SHA: SimpleNamespace(head_sha=old, heads=(("eerovil/agentdeck#9", old),)),
    }

    async def held(pull, work):
        harness.reviews.append((pull, work))
        return answers.pop(SHA, SimpleNamespace(head_sha=SHA, heads=((pull, SHA),)))

    harness.service._start_review = held
    pull_event(root, "d1", action="synchronize")
    await harness.drain()
    assert "eerovil/agentdeck#9@" + SHA not in harness.service.reviewed
    pull_event(root, "d2", action="synchronize")  # the next event, once the chat is let go
    await harness.drain()
    assert len(harness.reviews) == 2
    assert "eerovil/agentdeck#9@" + SHA in harness.service.reviewed


def test_covers_reads_the_chats_own_commits():
    from agentdeck.work_router import covers

    pull = "eerovil/agentdeck#9"
    assert covers(None, pull, SHA), "an answer naming no commits is taken at its word"
    assert covers(SimpleNamespace(heads=(("Eerovil/AgentDeck#9", SHA.upper()),)), pull, SHA)
    assert not covers(SimpleNamespace(heads=(("eerovil/agentdeck#9", "1" * 40),)), pull, SHA)
    assert not covers(SimpleNamespace(heads=(("eerovil/other#9", SHA),)), pull, SHA)
    assert not covers(SimpleNamespace(heads=(("eerovil/agentdeck#9", ""),)), pull, SHA)
    assert not covers(SimpleNamespace(heads=()), pull, SHA), "a chat naming no commit"


# --- nothing is acknowledged that did not happen ---


async def test_a_refused_store_write_is_kept_and_retried_without_another_event(root, harness):
    harness.store_refuses = True
    comment(root, "d1")
    await harness.drain()  # the offset is committed: the action is written down instead
    assert harness.scheduled == [] and "message:work-event:d1" in harness.service.pending
    harness.store_refuses = False
    await harness.service.retry_pending()
    assert harness.scheduled == [], "not before its back-off runs out"
    harness.now += 61
    await harness.service.retry_pending()
    [message] = harness.scheduled
    assert (message.id, message.chat_id) == ("work-event:d1", "chat-a")
    assert "please also fix X" in message.text
    assert "message:work-event:d1" not in harness.service.pending


async def test_a_store_that_raises_keeps_the_message_pending(root, harness):
    def broken(message):
        raise OSError("database is locked")

    harness.service._schedule = broken
    comment(root, "d1")
    await harness.drain()
    assert "message:work-event:d1" in harness.service.pending


async def test_one_synchronize_while_the_old_chat_is_held_is_reviewed_later(root, harness):
    old = "1" * 40
    held = [True]

    async def start(pull, work):
        harness.reviews.append((pull, work))
        sha = old if held[0] else SHA
        return SimpleNamespace(head_sha=sha, heads=((pull, sha),))

    harness.service._start_review = start
    harness.heads[("eerovil/agentdeck", 9)] = SHA
    pull_event(root, "d1", action="synchronize")
    await harness.drain()  # one event, and no other ever comes
    assert "review:eerovil/agentdeck#9@" + SHA in harness.service.pending
    held[0] = False  # the worker lets the old chat go
    harness.now += 61
    await harness.service.retry_pending()
    assert len(harness.reviews) == 2
    assert "eerovil/agentdeck#9@" + SHA in harness.service.reviewed
    assert not harness.service.pending.entries


async def test_a_pending_review_follows_the_current_head_or_is_dropped(root, harness):
    from agentdeck.chat_review import ReviewBusy

    async def busy(pull, work):
        harness.reviews.append((pull, work))
        raise ReviewBusy("someone is mid-review")

    harness.service._start_review = busy
    pull_event(root, "d1")
    await harness.drain()
    newer = "2" * 40
    harness.heads[("eerovil/agentdeck", 9)] = newer
    harness.now += 61
    await harness.service.retry_pending()
    assert list(harness.service.pending.entries) == ["review:eerovil/agentdeck#9@" + newer]
    harness.heads.clear()  # turned back to draft
    harness.now += 3600
    await harness.service.retry_pending()
    assert not harness.service.pending.entries


async def test_a_pending_action_backs_off_and_is_dropped_after_a_day(root, harness):
    harness.store_refuses = True
    comment(root, "d1")
    await harness.drain()
    first = harness.service.pending.entries["message:work-event:d1"]["next_at"]
    harness.now = first
    await harness.service.retry_pending()
    second = harness.service.pending.entries["message:work-event:d1"]
    assert second["attempts"] == 2 and second["next_at"] - harness.now == 120
    harness.now += 25 * 3600
    await harness.service.retry_pending()
    assert not harness.service.pending.entries


async def test_a_pending_action_survives_a_restart(root, harness):
    harness.store_refuses = True
    comment(root, "d1")
    await harness.drain()
    again = Harness(root)
    again.now = harness.now + 61
    await again.service.retry_pending()
    assert [message.id for message in again.scheduled] == ["work-event:d1"]


def test_pending_wait_is_a_deadline_not_a_tick(tmp_path):
    from agentdeck.work_router import PendingActions

    pending = PendingActions(tmp_path / "pending.json")
    assert pending.wait(1000.0) is None, "nothing pending: no wake-up at all"
    pending.changed.clear()
    pending.put("message:a", {"kind": "message"}, 1000.0)
    assert pending.changed.is_set(), "a new action wakes the loop to re-plan"
    assert pending.wait(1000.0) == 60.0
    assert pending.wait(2000.0) == 0.0


async def test_the_retry_loop_idles_until_an_action_is_pending(root, harness):
    import asyncio

    calls = []

    async def counted():
        calls.append(harness.now)
        harness.service.pending.discard("message:a")

    harness.service.retry_pending = counted
    loop = asyncio.create_task(harness.service._retry_loop())
    await asyncio.sleep(0.05)
    assert calls == [], "idle with nothing pending"
    harness.service.pending.put("message:a", {"kind": "message"}, harness.now - 120)
    await asyncio.sleep(0.05)
    loop.cancel()
    assert calls, "a due action wakes it"


async def test_a_pending_review_already_in_flight_is_pushed_back(root, harness):
    harness.heads[("eerovil/agentdeck", 9)] = SHA
    key = "eerovil/agentdeck#9@" + SHA
    harness.service.pending.put(
        "review:" + key,
        {
            "kind": "review",
            "repository": "eerovil/agentdeck",
            "number": 9,
            "head": SHA,
        },
        harness.now - 120,
    )
    harness.service._in_flight.add(key)
    await harness.service.retry_pending()
    assert harness.service.pending.wait(harness.now) > 0, "no spin on an in-flight start"


