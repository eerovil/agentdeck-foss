"""A Work hears its own GitHub events (#2895, ADR 0008).

Fixtures go through `WebhookLog`, the same appender the live ingress route uses, so the
filter is asserted over real stream bytes. Who owns an issue or a pull request, the
scheduled-message store and the review start are stubbed: what is under test is which
deliveries reach a chat, which chat, under which id, and how often a review is asked for.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from public_copy import private_feature

from agentdeck import chat_review as _chat_review
from agentdeck import review_history
from agentdeck.db import Db
from agentdeck.github_webhook_log import Repository, WebhookLog
from agentdeck.models import PullRequestRef
from agentdeck.poller_ops import work_repositories
from agentdeck.webhook_tailer import WebhookTailer
from agentdeck.work_release import MINOR_LINES, OWNER_CALL_ASK, SIBLING_SWEEP_ASK
from agentdeck.work_router import (
    ESCALATED_ASK,
    HANDED_OVER,
    MESSAGE,
    RETRY_FIRST_S,
    REVIEW,
    AgainRefused,
    SwitchedRepositories,
    WorkRouterService,
    _grown,
    classify,
    issue_owner_of_pull,
    owner_of_issue,
    owner_of_pull,
    pulls_by_work,
    pulls_lookup,
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
            "default_branch": "master",
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
            **({"created_at": kw.pop("created_at")} if "created_at" in kw else {}),
            **({"performed_via_github_app": {"slug": kw.pop("via")}} if "via" in kw else {}),
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
    def __init__(self, root, history: list | None = None) -> None:
        self.root = root
        self.scheduled: list = []
        self.reviews: list[tuple[str, str]] = []
        self.switched = {"eerovil/agentdeck"}
        self.issues = {("eerovil/agentdeck", 7): ("chat-a", "eerovil/agentdeck#7")}
        self.pulls = {("eerovil/agentdeck", 9): ("chat-a", "eerovil/agentdeck#7")}
        self.fail_review = False
        self.refuse = None
        self.heads: dict[tuple[str, int], str] = {}
        # Heads of open drafts, which ``ready_head`` does not read (#4352).
        self.drafts: dict[tuple[str, int], str] = {}
        # Changed lines of the pull request's diff at each head (#4220); a head left out is
        # unread. ``size_read`` replaces the read when set (it may raise).
        self.sizes: dict[str, int] = {}
        self.size_read = None
        self.size_reads: list[tuple[str, ...]] = []
        self.now = 1000.0
        self.store_refuses = False
        self.stored: set[str] = set()
        self.strangers: set[str] = set()
        self.authorized_asked: list[tuple[str, str, str]] = []
        self.rereads: list[tuple[str, int]] = []
        self.asks: list[dict] = []
        self.verdicts: list[tuple] = []
        # The deck's database outlives a restart, so a restarted harness may share it.
        self.history: list[review_history.ReviewEvent] = [] if history is None else history

        async def start_review(pull: str, work: str, **asks) -> None:
            self.reviews.append((pull, work))
            self.asks.append(asks)
            if self.fail_review:
                raise RuntimeError("gh is down")

        async def ready_head(repository: str, number: int) -> str:
            return self.heads.get((repository, number), "")

        async def open_head(repository: str, number: int) -> str:
            key = (repository, number)
            return self.heads.get(key) or self.drafts.get(key, "")

        async def diff_sizes(repository: str, number: int, heads: tuple[str, ...]):
            self.size_reads.append(heads)
            if self.size_read is not None:
                self.size_read()
            return {head: self.sizes[head] for head in heads if head in self.sizes}

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
            open_head=open_head,
            exists=lambda message_id: any(item.id == message_id for item in self.scheduled),
            clock=lambda: self.now,
            authorized=self._authorized,
            reread_issue=self._reread,
            acknowledge=self._acknowledge,
            spec_only_label=lambda repository: "Spec-Only",
            on_verdict=lambda chat, pulls: self.verdicts.append((chat, pulls)),
            review_event=self.note,
            reviews=self.reviews_of,
            diff_sizes=diff_sizes,
        )
        self.acknowledged: list[tuple[str, int]] = []

    def note(self, pull, head, kind, key, at=None, data=None) -> bool:
        """The deck's review history (``state.record_review_event``), in memory."""
        if all(event.key != key for event in self.history):
            when = self.now if at is None else at
            event = review_history.ReviewEvent(
                pull.lower(), head.lower(), when, kind, key, dict(data or {}), len(self.history)
            )
            self.history.append(event)
        return True

    def reviews_of(self, pull: str) -> list:
        return [event for event in self.history if event.pull == pull.lower()]

    def asked(self, pull: str, head: str, **data) -> None:
        """A review of ``pull`` at ``head`` was asked for before."""
        again = data.get("again", "")
        self.note(pull, head, review_history.ASKED, f"asked:{pull}@{head}+{again}", data=data)

    def answered(self, pull: str, head: str, kind: str = review_history.CHANGES) -> None:
        self.note(pull, head, kind, f"{kind}:{pull}@{head}@{len(self.history)}")

    def agains(self, pull: str, head: str) -> int:
        """How many times ``pull`` at ``head`` was asked about again."""
        events = review_history.at_head(self.reviews_of(pull), head)
        return sum(1 for e in events if e.kind == review_history.ASKED and e.data.get("again"))

    def kinds(self, pull: str, head: str) -> list[str]:
        return [e.kind for e in review_history.at_head(self.reviews_of(pull), head)]

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
    # #3828: an owner's acceptance needs no fresh review.
    assert "/review-accepted" in ESCALATED_ASK and "/review-accepted" in OWNER_CALL_ASK
    # #4212: the escalation's question also offers "Ship now, fix later", the Work page's press.
    assert '"Ship now, fix later"' in ESCALATED_ASK and "/ship-now" in ESCALATED_ASK
    assert '"Ship now, fix later"' in OWNER_CALL_ASK and "/ship-now" in OWNER_CALL_ASK
    assert '"head": "<full commit id>"' in OWNER_CALL_ASK and "ships them all" in OWNER_CALL_ASK


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
    # The sweep and the shared-cause ask too: this is how such a reviewer's findings reach
    # the agent (#4497 review), and in the link form a refused text falls back to.
    assert f"{SIBLING_SWEEP_ASK} {OWNER_CALL_ASK}" in events[1].text
    assert SIBLING_SWEEP_ASK in events[1].link and SIBLING_SWEEP_ASK not in events[0].text


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
    assert SIBLING_SWEEP_ASK in events[0].text  # #4497 review


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
    assert "Mechanism check" not in harness.scheduled[0].text
    # #4596 (owner: Simplify): a third round on the Work's pull requests citing the file two
    # earlier rounds cited asks nothing with the findings, even with a ``changes-needed``
    # verdict already at the head (an earlier review of the same commit): only a verdict's
    # message asks.
    pull, second, third = "eerovil/agentdeck#9", "eerovil/agentdeck#10", "c" * 40
    harness.pulls[("eerovil/agentdeck", 10)] = ("chat-a", "eerovil/agentdeck#7")
    for ref in (pull, second):
        for n, head in enumerate(("a" * 40, "b" * 40)):
            data = {"text": f"Review findings for PR {ref} at {head[:9]}: src/x/work_start.py:{n}"}
            harness.note(ref, head, review_history.FINDINGS, f"f{ref}{n}", data=data)
        harness.asked(ref, third)
        harness.note(ref, third, review_history.CHANGES, f"v{ref}", data={})
    body = (
        f"/claude\nReview findings for PR #9 at {third[:9]} and #10 at {third[:9]}:\n\n"
        "1. `work_start.py:7` races"
    )
    comment(root, "d2", sender=DECK_BOT, body=body, comment_id=5)
    await harness.drain()
    assert "Mechanism check" not in harness.scheduled[-1].text
    assert not [e for e in harness.history if e.kind == review_history.MECHANISM]


async def test_another_reviewers_findings_never_ask(root, harness):
    """#4596: the ChatGPT reviewer's findings come before its verdict too, so its third round
    citing a file asks nothing with them, with or without a ``changes-needed`` verdict at the
    head; a pull request whose verdicts the release refuses gets no ask at all (accepted)."""
    reviewer = {"login": "review-bot[bot]", "type": "Bot"}
    pull, third = "eerovil/agentdeck#9", "c" * 40
    for n, head in enumerate(("a" * 40, "b" * 40)):
        data = {"text": f"Review findings for PR {pull} at {head[:9]}: src/x/work_start.py:{n}"}
        harness.note(pull, head, review_history.FINDINGS, f"f{n}", data=data)
    harness.asked(pull, third)
    body = f"Review findings for PR #9 at {third[:9]}\n/claude\n\n1. `work_start.py:7` races"
    comment(root, "r7", sender=reviewer, body=body, comment_id=7)
    harness.note(pull, third, review_history.CHANGES, "v", data={})
    comment(root, "r8", sender=reviewer, body=body, comment_id=8)
    await harness.drain()
    assert all("Mechanism check" not in m.text for m in harness.scheduled)
    assert not [e for e in harness.history if e.kind == review_history.MECHANISM]


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

        async def stale_findings(self, work_id, text, repository, source=""):
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


def _dep(
    number, state="open", labels=("wayfinder:research",), repository="eerovil/agentdeck",
    assignee="",
):
    return {
        "repository": repository, "number": number, "state": state, "labels": list(labels),
        "assignee": assignee,
    }


def _unblock_harness(harness, blocking, blockers, *, fail=None):
    """The router with the dependency reads, the Start write and the Work start faked
    (#3764, #4216): ``started`` holds ``("label", n)`` for the Start write, ``("work", n)``
    for a labelled issue's Work started."""
    asked, started = [], []

    async def read_dependencies(repository, number, kind):
        asked.append((kind, number))
        if fail == kind:
            raise RuntimeError("gh is down")
        return blocking.get(number, []) if kind == "blocking" else blockers.get(number, [])

    async def start_ticket(repository, number):
        started.append(("label", number))
        return "done", f"Started {repository}#{number}"

    async def start_work(repository, number, **kwargs):
        started.append(("work", number))
        return "started"

    harness.service._read_dependencies = read_dependencies
    harness.service._start_ticket = start_ticket
    harness.service._start_work = start_work
    harness.service._start_labels = lambda repository: ("agent",)
    harness.service._hold_labels = lambda repository: ("Paused",)
    harness.service._mine = lambda repository, assignee: assignee != "coworker"
    return asked, started


@pytest.mark.parametrize(
    ("blocked", "blockers", "closed_labels", "fail", "started", "why"),
    [
        # #3705 on map #3697: both blockers closed, the second by the deck's own login.
        (_dep(30), [_dep(21, "closed"), _dep(22, "closed")], ["wayfinder:research"], None,
         "label", "eerovil/agentdeck#30 has no open blocker: done"),
        (_dep(30), [_dep(21, "closed"), _dep(22)], ["wayfinder:research"], None,
         None, "eerovil/agentdeck#30 waits on #22"),
        # #4216: a store repository's issue #1134, a map's build issue labelled only with the
        # start label, closed as the last blocker of #1149, another build issue: no wayfinder
        # label on either, and it starts all the same.
        (_dep(30, labels=["Agent"]), [_dep(22, "closed")], ["agent"], None,
         "work", "eerovil/agentdeck#30 has no open blocker: starting its Work"),
        (_dep(30, labels=["bug"]), [], ["agent"], None,
         "label", "eerovil/agentdeck#30 has no open blocker: done"),
        (_dep(30, labels=["wayfinder:task", "paused"]), [], ["wayfinder:research"], None,
         None, "it is paused"),
        (_dep(30, "closed"), [], ["wayfinder:research"], None, None, "it is closed"),
        (_dep(30, labels=["wayfinder:map"]), [], ["wayfinder:research"], None,
         None, "it is a wayfinder map"),
        # #4216 review: assigned to another deck's person, labelled or not, it is theirs.
        (_dep(30, assignee="coworker"), [], ["agent"], None, None, "another deck works it"),
        (_dep(30, labels=["agent"], assignee="coworker"), [], ["agent"], None,
         None, "another deck works it"),
        (_dep(30), [], ["wayfinder:research"], "blocking", None, "blocks unread"),
        (_dep(30), [], ["wayfinder:research"], "blocked_by", None, "blockers of"),
    ],
)
async def test_an_issues_last_blocker_closing_starts_it(
    root, harness, caplog, blocked, blockers, closed_labels, fail, started, why
):
    """#3764, #4216: an issue waits for every blocker, then starts, whoever closed the last
    one: an unlabelled one gets the Start button's write, a labelled one, whose label's start
    waited, starts its Work. A paused, closed or map one is left alone, and a failed read
    starts nothing and stops nothing else."""
    caplog.set_level("INFO", logger="agentdeck.work_router")
    asked, starts = _unblock_harness(harness, {22: [blocked]}, {30: blockers}, fail=fail)
    deliver(
        root,
        event="issues",
        delivery="d1",
        action="closed",
        sender={"login": "agentdeck-eerovil[bot]", "type": "Bot"},
        issue={"number": 22, "state": "closed", "labels": [{"name": n} for n in closed_labels]},
    )
    await harness.drain()
    assert starts == ([(started, 30)] if started else [])
    logged = [r.getMessage() for r in caplog.records if r.name == "agentdeck.work_router"]
    assert any(why in line for line in logged), logged
    # A replay of the delivery reads the issue again, now labelled and owned by its Work's
    # chat, and starts nothing more.
    if started:
        blocked["labels"].append("agent")
        harness.issues[("eerovil/agentdeck", 30)] = ("chat-t", "eerovil/agentdeck#30")
        await harness.service.apply(read(root))
        assert len(starts) == 1
        logged = [r.getMessage() for r in caplog.records if r.name == "agentdeck.work_router"]
        assert any("already has its start label and a chat" in line for line in logged)


async def test_a_pull_requests_close_reads_no_blockers(root, harness):
    asked, starts = _unblock_harness(harness, {22: [_dep(30)]}, {30: []})
    deliver(root, event="issues", delivery="d1", action="closed", sender=PERSON,
            issue={"number": 22, "state": "closed", "pull_request": {"url": "x"}})
    await harness.drain()
    assert asked == [] and starts == []


async def test_a_blocker_removed_by_hand_starts_a_ticket_left_with_none(root, harness):
    """#3764: removing a ticket's last open blocker starts it; one with another open does not."""
    asked, starts = _unblock_harness(harness, {}, {30: [], 31: [_dep(23)]})
    for delivery, number in (("d1", 30), ("d2", 31)):
        deliver(
            root,
            event="issue_dependencies",
            delivery=delivery,
            action="blocked_by_removed",
            sender=PERSON,
            blocked_issue={"number": number, "state": "open",
                           "labels": [{"name": "wayfinder:research"}]},
            blocked_issue_repo={"full_name": "eerovil/agentdeck"},
            blocking_issue={"number": 22, "state": "open"},
        )
    await harness.drain()
    assert asked == [("blocked_by", 30), ("blocked_by", 31)]
    assert starts == [("label", 30)]


@pytest.mark.parametrize(
    ("deployed", "blockers", "started", "answer"),
    [
        # #4384: #22 deployed, waiting for a try; #30's other blocker closed: it starts.
        ({"github_issue:eerovil/agentdeck#22"}, [_dep(21, "closed"), _dep(22)], [("label", 30)],
         True),
        # Another blocker still open: nothing starts.
        ({"github_issue:eerovil/agentdeck#22"}, [_dep(22), _dep(23)], [], True),
        # #22 not in the saved set (an open pull request, a newer merge with its check
        # pending, a pending question, a reopen, or the set not yet saved): nothing is read.
        (set(), [_dep(22)], [], False),
    ],
)
async def test_a_deployed_blocker_starts_what_it_blocks(
    root, harness, deployed, blockers, started, answer
):
    asked, starts = _unblock_harness(harness, {22: [_dep(30)]}, {30: blockers})
    harness.service._deployed = lambda: deployed
    reply = await harness.service.note_deployed("Eerovil/Agentdeck", 22)
    assert reply == {"in_set": answer}
    assert starts == started
    assert asked == ([("blocking", 22), ("blocked_by", 30)] if answer else [])


async def test_an_unread_deployed_set_keeps_a_close_from_starting_past_it(root, harness):
    """#4384: the close of #21 starts #30 only past a deployed #22; unread, #22 blocks."""
    asked, starts = _unblock_harness(
        harness, {21: [_dep(30)]}, {30: [_dep(21, "closed"), _dep(22)]}
    )

    def unread():
        raise OSError("database is locked")

    harness.service._deployed = unread
    deliver(root, event="issues", delivery="d1", action="closed", sender=PERSON,
            issue={"number": 21, "state": "closed", "labels": []})
    await harness.drain()
    assert starts == []
    harness.service._deployed = lambda: frozenset({"github_issue:eerovil/agentdeck#22"})
    deliver(root, event="issues", delivery="d2", action="reopened", sender=PERSON,
            issue={"number": 21, "state": "open", "labels": []})
    deliver(root, event="issues", delivery="d3", action="closed", sender=PERSON,
            issue={"number": 21, "state": "closed", "labels": []})
    await harness.drain()
    assert starts == [("label", 30)]


@pytest.mark.parametrize(
    ("base", "fail", "blockers", "started", "recorded"),
    [
        # #4398: #7's pull request #9 merged into the default branch; #30's only open blocker
        # is #7: it counts as done, and #30 starts.
        ("master", False, [_dep(7)], [("label", 30)], [("eerovil/agentdeck#9", "master")]),
        # Another blocker still open: recorded, nothing starts.
        ("master", False, [_dep(7), _dep(23)], [], [("eerovil/agentdeck#9", "master")]),
        # A stacked pull request merged into a feature branch: not the code #30 builds on.
        ("feature-x", False, [_dep(7)], [], []),
        # The row could not be written: nothing starts on it; #7's close still counts.
        ("master", True, [_dep(7)], [], []),
    ],
)
async def test_a_blockers_merged_pull_request_starts_what_it_blocks(
    root, harness, tmp_path, base, fail, blockers, started, recorded
):
    db = Db(tmp_path / "control.db")
    asked, starts = _unblock_harness(harness, {7: [_dep(30)]}, {30: blockers})

    def note(work_id, pull, branch):
        return False if fail else db.note_merged_pull(work_id, pull, branch)

    harness.service._note_merged = note
    harness.service._forget_merged = db.forget_merged_pulls
    harness.service._deployed = db.load_done_blockers
    merged_pull(root, "d1", base=base)
    await harness.drain()
    assert starts == started
    assert db.load_merged_pulls().get("github_issue:eerovil/agentdeck#7", []) == recorded
    assert asked == ([("blocking", 7), ("blocked_by", 30)] if recorded else [])


async def test_a_reopen_starts_a_round_and_an_earlier_merge_unblocks_nothing(
    root, harness, tmp_path
):
    """#4585: a reopen of #7 starts its next round with the merged #9 earlier, once; a late
    delivery of #9's merge is not recorded again, so #30 stays blocked by the round."""
    db = Db(tmp_path / "control.db")
    asked, starts = _unblock_harness(harness, {7: [_dep(30)]}, {30: [_dep(7)]})
    work = "github_issue:eerovil/agentdeck#7"
    rounds = []

    def start_round(work_id):
        rounds.append(work_id)
        return db.start_work_round(work_id, ["eerovil/agentdeck#9"], "reopened")

    harness.service._note_merged = db.note_merged_pull
    harness.service._forget_merged = db.forget_merged_pulls
    harness.service._deployed = db.load_done_blockers
    harness.service._start_round = start_round
    harness.service._earlier_pulls = lambda work_id: next(
        (round_.earlier for round_ in db.load_work_rounds([work_id]).values()), ()
    )
    db.note_merged_pull(work, "eerovil/agentdeck#9", "master")
    for delivery in ("d1", "d2"):
        deliver(root, event="issues", delivery=delivery, action="reopened", sender=PERSON,
                issue={"number": 7, "state": "open", "labels": []})
        await harness.drain()
    started = db.load_work_rounds()[work].started_at
    assert rounds == [work, work]
    assert db.load_work_rounds()[work].earlier == {"eerovil/agentdeck#9"}
    assert db.load_work_rounds()[work].started_at == started
    merged_pull(root, "d3", base="master")
    await harness.drain()
    assert db.load_merged_pulls().get(work, []) == []
    assert work not in db.load_done_blockers()
    assert starts == []


async def test_a_reopen_round_that_cannot_start_never_stops_the_router(root, harness):
    """#4585: a round write that raises is logged; the reopen still drops the merges."""
    forgot = []

    def broken(work_id):
        raise OSError("database is locked")

    harness.service._forget_merged = lambda work_id: forgot.append(work_id) or True
    harness.service._note_merged = lambda *args: True
    harness.service._start_round = broken
    deliver(root, event="issues", delivery="d1", action="reopened", sender=PERSON,
            issue={"number": 7, "state": "open", "labels": []})
    await harness.drain()
    assert forgot == ["github_issue:eerovil/agentdeck#7"]


async def test_a_reopened_blocker_with_no_chat_blocks_again(root, harness, tmp_path):
    """#4398: a reopen drops the Work's merges by its own id, with no chat of it loaded."""
    db = Db(tmp_path / "control.db")
    asked, starts = _unblock_harness(harness, {}, {30: [_dep(7)]})
    harness.service._note_merged = db.note_merged_pull
    harness.service._forget_merged = db.forget_merged_pulls
    harness.service._deployed = db.load_done_blockers
    assert db.note_merged_pull("github_issue:eerovil/agentdeck#7", "acme/robot#68", "main")
    assert db.load_done_blockers() == {"github_issue:eerovil/agentdeck#7"}
    harness.issues.clear()
    deliver(root, event="issues", delivery="d1", action="reopened", sender=PERSON,
            issue={"number": 7, "state": "open", "labels": []})
    deliver(root, event="issue_dependencies", delivery="d2", action="blocked_by_removed",
            sender=PERSON, blocked_issue={"number": 30, "state": "open", "labels": []})
    await harness.drain()
    assert db.load_merged_pulls() == {} and db.load_done_blockers() == frozenset()
    assert starts == []


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
    assert harness.service._spent("eerovil/agentdeck#9", "1" * 40)


async def test_a_reviewed_head_survives_a_restart(root, harness):
    pull_event(root, "d1")
    await harness.drain()
    again = Harness(root, harness.history)
    pull_event(root, "d2", action="synchronize")
    await again.drain()
    assert again.reviews == [], "the head is remembered in its history"


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


def test_the_routers_old_review_files_are_deleted_at_start(root):
    """#3955: each head's review history replaced them, with nothing copied over."""
    root.parent.mkdir(parents=True, exist_ok=True)
    for name in ("work-router-reviews.json", "work-router-pre-review.json"):
        (root.parent / name).write_text("{}")
    (root.parent / "work-router-pending.json").write_text("{}")
    Harness(root)
    assert sorted(path.name for path in root.parent.iterdir()) == ["work-router-pending.json"]


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
    now = [0.0]
    switched = SwitchedRepositories(tmp_path, clock=lambda: now[0])
    assert switched("eerovil/agentdeck")
    assert not switched("eerovil/notes")
    _manifest(tmp_path, "agentdeck", body + '[[repos]]\nid = "notes"\n')
    assert not switched("eerovil/notes"), "within the TTL the manifests are not looked at"
    now[0] += switched.ttl_s
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

    def chat_identity_key(self, leg, chats=None):
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
    # The release tells the issue's owner (#4753), found from the Work ``owner_of_pull`` names
    # as ``owner/repo#n``: the registering chat, or another chat that owns the issue.
    assert issue_owner_of_pull(state, "eerovil/agentdeck", 9) is None, "no issue owner"
    state._work = FakeState(memberships=[("chat-a", True)], activity={"chat-a": 0})._work
    assert issue_owner_of_pull(state, "eerovil/agentdeck", 9) == ("chat-a", "eerovil/agentdeck#7")
    state._work = FakeState(memberships=[("chat-b", True)], activity={"chat-b": 0})._work
    state.sessions.update(FakeState(activity={"chat-b": 0}).sessions)
    assert issue_owner_of_pull(state, "eerovil/agentdeck", 9) == ("chat-b", "eerovil/agentdeck#7")
    assert issue_owner_of_pull(state, "eerovil/other", 9) is None


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


def test_a_registration_naming_a_work_outweighs_a_newer_chat_naming_none():
    """#4125: chats from a branch's first life, more recently active, do not take the pull
    request from the chat that registered it for the Work it was revived under."""
    old = SimpleNamespace(repository="eerovil/agentdeck", number=7)
    revived = SimpleNamespace(repository="eerovil/agentdeck", number=8)
    named = PullRequestRef("eerovil/other", 9, "github_issue:eerovil/agentdeck#8")
    state = FakeState(
        pulls={"chat-old": (PullRequestRef("eerovil/other", 9),), "chat-new": (named,)},
        joined={"chat-old": (old,), "chat-new": (revived, old)},
        activity={"chat-old": 0, "chat-new": 60},
    )
    assert owner_of_pull(state, "eerovil/other", 9) == ("chat-new", "eerovil/agentdeck#8")
    # A Work its chat has left names nothing: the newest chat decides, as before.
    state._joined["chat-new"] = (old,)
    assert owner_of_pull(state, "eerovil/other", 9) == ("chat-old", "eerovil/agentdeck#7")


def test_the_ship_now_api_ships_every_pull_request_of_the_chats_work():
    """#4212 review: the agent's question ships each of its Work's pull requests, whichever of
    the Work's chats registered it, as the Work page's button does; a pull request placed in
    another Work is not one of them, and a chat in no Work ships nothing."""
    from agentdeck.web.routes_api import _work_pulls_of_chat

    seven = SimpleNamespace(repository="eerovil/agentdeck", number=7)
    eight = SimpleNamespace(repository="eerovil/agentdeck", number=8)
    state = FakeState(
        pulls={
            "chat-a/leg1": (PullRequestRef("eerovil/agentdeck", 9),),
            "chat-e": (PullRequestRef("Eerovil/AgentDeck", 13),),
            "chat-b": (PullRequestRef("eerovil/agentdeck", 10),),
            "chat-d": (PullRequestRef("eerovil/agentdeck", 12),),
        },
        joined={"chat-a": (seven,), "chat-e": (seven,), "chat-b": (eight,)},
        activity={"chat-a": 1, "chat-e": 2, "chat-b": 0, "chat-d": 0},
    )
    every = {"eerovil/agentdeck#9", "eerovil/agentdeck#13"}
    assert _work_pulls_of_chat(state, "chat-a/leg2") == every
    assert _work_pulls_of_chat(state, "chat-e") == every
    assert _work_pulls_of_chat(state, "chat-b") == {"eerovil/agentdeck#10"}
    assert _work_pulls_of_chat(state, "chat-d") == set()


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
    # #4125: a Work page places only what it shows, by the same rule.
    assert pulls_by_work(state, only={("eerovil/other", 9)}) == {
        "eerovil/agentdeck#7": ("eerovil/other#9",)
    }
    # #3993: one walk answers every Work, the same as asking each.
    builds.clear()
    lookup = pulls_lookup(state)
    for work_id in ("github_issue:eerovil/agentdeck#7", "github_issue:EEROVIL/agentdeck#8"):
        assert lookup(work_id) == work_pulls(state, work_id)
    assert lookup("github_issue:eerovil/agentdeck#6") == ()
    assert len(builds) == 3, "one projection for the lookup, one per work_pulls"


async def test_a_busy_reviewer_leaves_the_head_for_the_next_event(root, harness):
    from agentdeck.chat_review import ReviewBusy

    async def busy(pull, work):
        harness.reviews.append((pull, work))
        raise ReviewBusy("someone is mid-review")

    harness.service._start_review = busy
    pull_event(root, "d1")
    await harness.drain()
    assert not harness.service._spent("eerovil/agentdeck#9", SHA)
    pull_event(root, "d2", action="synchronize")
    await harness.drain()
    assert len(harness.reviews) == 2, "asked again on the next event"


async def test_an_ask_again_that_cannot_start_now_keeps_its_place_and_note(root, harness):
    """#3183 review: an old review chat that would not close leaves the ask pending, and its
    retry carries the agent's note into the new chat. Only a started ask counts against the
    cap (#3955); one still pending turns the next ask away as busy."""
    from agentdeck.chat_review import ReviewBusy
    from agentdeck.work_router import AgainBusy, AgainRefused

    pull = "eerovil/agentdeck#9"
    harness.asked(pull, OLD)
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    calls = []

    async def busy_once(pull, work, **asks):
        calls.append(asks)
        if len(calls) == 1:
            raise ReviewBusy("chat #12 is still open")

    harness.service._start_review = busy_once
    asked = await harness.service.ask_again("eerovil/agentdeck", 9, "owner keeps it")
    assert asked["outcome"] == "pending" and harness.agains(pull, OLD) == 0
    with pytest.raises(AgainBusy):
        await harness.service.ask_again("eerovil/agentdeck", 9, "again")
    harness.now += 3600
    await harness.service.retry_pending()
    assert calls[1] == {"note": "owner keeps it", "again": True}
    assert harness.agains(pull, OLD) == 1
    assert not harness.service.pending.entries

    # An ask whose retry is dropped (the head moved on) never counts.
    calls.clear()
    await harness.service.ask_again("eerovil/agentdeck", 9, "walkthrough posted")
    harness.heads[("eerovil/agentdeck", 9)] = ""
    harness.now += 3600
    await harness.service.retry_pending()
    assert harness.agains(pull, OLD) == 1 and not harness.service.pending.entries

    # #3324: kept while the running review of that head passes it, the ask is answered, not
    # reviewed: a new review re-opened a Work already waiting for its Merge.
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    calls.clear()
    harness.service._start_review = busy_once  # busy at first again
    await harness.service.ask_again("eerovil/agentdeck", 9, "owner keeps it")
    assert len(calls) == 1 and harness.service.pending.entries
    harness.now += 5
    harness.answered(pull, OLD, review_history.PASS)
    harness.now += 3600
    await harness.service.retry_pending()
    assert len(calls) == 1, "no review of a head that passed since the ask"
    assert not harness.service.pending.entries
    assert harness.agains(pull, OLD) == 1

    # #4089: an ask for want of a verdict has no reason left once one stands. Made after it, it
    # is refused; kept pending while it landed, it drops. Its row would void metso#77's pass.
    with pytest.raises(AgainRefused, match="has its verdict"):
        await harness.service.ask_again("eerovil/agentdeck", 9, "no verdict", no_verdict=True)
    harness.asked(pull, NEW)
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    calls.clear()
    await harness.service.ask_again("eerovil/agentdeck", 9, "no verdict", no_verdict=True)
    assert len(calls) == 1 and harness.service.pending.entries
    harness.answered(pull, NEW, review_history.PASS)
    harness.now += 3600
    await harness.service.retry_pending()
    assert len(calls) == 1 and not harness.service.pending.entries
    assert harness.kinds(pull, NEW) == [review_history.ASKED, review_history.PASS]


async def test_a_refused_review_leaves_the_head_for_the_next_event(root, harness):
    from agentdeck.chat_review import ReviewError

    async def refused(pull, work):
        harness.reviews.append((pull, work))
        raise ReviewError("this deck has no [chat_board] repository configured")

    harness.service._start_review = refused
    pull_event(root, "d1")
    await harness.drain()
    assert not harness.service._spent("eerovil/agentdeck#9", SHA)
    pull_event(root, "d2", action="synchronize")
    await harness.drain()
    assert len(harness.reviews) == 2, "a start that wrote nothing is asked again"


async def test_a_deck_with_no_reviewer_keeps_nothing_pending_and_says_so_once(root, harness):
    # #3849: the public copy's reviewer is off. Kept pending, the refusal was retried for a day
    # and every later ask heard "already asked, keep waiting".
    from agentdeck.review_refs import ReviewOff

    harness.service.reviewer_off = True
    harness.heads[("eerovil/agentdeck", 9)] = SHA
    pull_event(root, "d1")
    pull_event(root, "d2", action="synchronize")  # the same head again
    await harness.drain()
    assert harness.reviews == [] and not harness.service.pending.entries
    assert not harness.service._spent("eerovil/agentdeck#9", SHA)
    assert [message.chat_id for message in harness.scheduled] == ["chat-a"]
    assert "no Work reviewer" in harness.scheduled[0].text
    with pytest.raises(AgainRefused, match="no Work reviewer"):
        await harness.service.ask_again("eerovil/agentdeck", 9, "walkthrough posted")

    # An ask kept pending before the upgrade, even one past a day, is answered once the
    # router starts again with no reviewer, rather than waiting out its back-off.
    harness.service.pending.put(
        f"review:eerovil/agentdeck#9@{SHA}+ab12",
        {
            "kind": REVIEW,
            "repository": "eerovil/agentdeck",
            "number": 9,
            "head": SHA,
            "again": "ab12",
            "note": "walkthrough posted",
        },
        harness.now - 2 * 86400,
    )
    # Tried again since, so its next attempt is still to come: the loop alone would wait.
    harness.service.pending.put(f"review:eerovil/agentdeck#9@{SHA}+ab12", {}, harness.now)
    restarted = Harness(root)  # read back from disk, as after the upgrade's restart
    restarted.heads[("eerovil/agentdeck", 9)] = SHA
    restarted.service.reviewer_off = True
    assert restarted.service.pending.wait(restarted.now) > 0, "not due by its back-off"
    loop = asyncio.create_task(restarted.service._retry_loop())
    for _ in range(20):
        await asyncio.sleep(0)
    loop.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop
    assert not restarted.service.pending.entries and restarted.reviews == []
    assert restarted.agains("eerovil/agentdeck#9", SHA) == 0
    assert [message.chat_id for message in restarted.scheduled] == ["chat-a"]
    assert "no Work reviewer" in restarted.scheduled[0].text

    # A start that says so itself (the flag unset) is not kept pending either.
    async def off(pull, work, **asks):
        harness.reviews.append((pull, work))
        raise ReviewOff("no Work reviewer here")

    harness.service.reviewer_off = False
    harness.service._start_review = off
    harness.heads[("eerovil/agentdeck", 9)] = "b" * 40
    pull_event(root, "d3", action="synchronize", sha="b" * 40)
    await harness.drain()
    assert len(harness.reviews) == 1 and not harness.service.pending.entries
    assert len(harness.scheduled) == 2 and "no Work reviewer" in harness.scheduled[-1].text


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


#: The escalation on eerovil/agentdeck#4064 (issuecomment-6624116226), its heads replaced:
#: it retells the round at the older head before asking about the live one (#4069).
LOOP_ESCALATION = (
    "Review loop escalated to the operator\n"
    "\n"
    "The earlier review of PR #9 at `{old}` "
    "(https://github.com/eerovil/agentdeck/issues/4064#issuecomment-6623823598) found that"
    " `reviewer_down` falsely called a Work's reviewer down when a reviewer was still "
    "active. The subsequent fix added `src/agentdeck/review_fallback.py:1421-1422`:\n"
    "\n"
    "```python\n"
    '    if any(row.group == "working" for row in chats.rows.values()):\n'
    '        return ""\n'
    "```\n"
    "\n"
    "At `{new}`, the opposite failure remains: the predicate ranges over **every** review "
    "chat, not the Work whose wait expired, and `chat_board.py:180-188` defines `working` "
    "from the `in-progress` label alone. For example, a Claude fallback can still be "
    "working on an *unrelated* claimed chat while its account has reached its usage limit;"
    " a second Work's chat is untouched, ChatGPT's latest start has failed and no eligible"
    " account can start its reviewer, yet the global guard suppresses the outage signal. "
    "An abandoned `in-progress` claim silent past the worker protocol's 30-minute recovery"
    " limit also suppresses it indefinitely. The Work is sent to check/retry instead of "
    "being told why no new review can start, recreating the review-wait loop this issue "
    "set out to stop.\n"
    "\n"
    "This is another case of the earlier slow-versus-down classification problem, not a "
    "request for another unbounded patch round. Please choose one:\n"
    "\n"
    "1. **Accept the limit** and document that another claimed (or abandoned) chat can "
    "mask a down reviewer on this Work.\n"
    "2. **Ask for a different approach** based on the actual Work/review chat and its live"
    " reviewer, rather than a board-wide label.\n"
    "3. **Allow one more narrow fix** to scope the check to the waiting review and "
    "disregard claims silent past the worker's 30-minute threshold, with regression tests "
    "for unrelated and abandoned claims.\n"
    "\n"
    "<!-- chatgpt-review -->"
)


OLD = "c" * 40
MID = "b" * 40
NEW = "a" * 40


async def test_review_fixes_that_grew_the_diff_add_a_design_check(root, harness):
    """#4220: a round whose diff grew over 15% (and 100 lines) of the previous round's gets a
    checklist point asking whether the design still holds; smaller growth or an unread size
    gets the plain checklist. The previous round is the newest head asked about, whoever
    asked, and both sizes are read from GitHub when the checklist goes out."""
    harness.service.pre_review_on = True
    pull, key = "eerovil/agentdeck#9", ("eerovil/agentdeck", 9)

    async def round_at(head: str, size, delivery: str) -> str:
        harness.heads[key] = head
        if size is not None:
            harness.sizes[head] = size
        pull_event(root, delivery, sha=head)
        await harness.drain()
        checklist = harness.scheduled[-1]
        assert checklist.id == f"work-event:pre-review:{pull}@{head}"
        assert (await harness.service.pre_review_done("eerovil/agentdeck", 9))["outcome"]
        return checklist.text

    first = await round_at(OLD, 1000, "d1")
    assert "6. " not in first
    # #4437: the helper checks are asked for before the first review only.
    assert (
        f"agentdeck pre-review-helpers --cwd . --pull {pull} --issue eerovil/agentdeck#7`"
        in first
    ), "the Work's issue, where the comment is checked, not the body's first reference"
    # #4567: run at the first push, a comment for an earlier head stands if only its finds
    # were fixed since.
    assert "Already ran them on an earlier head" in " ".join(first.split())
    assert "otherwise, or when it says `did not run`, run them again on this head" in " ".join(
        first.split()
    )
    assert harness.size_reads == [], "no earlier round: nothing is read"
    # #4786: the deck's refusal names the big pull request's reading guide.
    assert "1,000 changed lines or more lacks a `Reading guide:` section" in " ".join(
        first.split()
    )
    second = await round_at(MID, 1100, "d2")
    assert "6. " not in second, "10% is not over"
    assert "pre-review-helpers --cwd" not in second
    grown = await round_at(NEW, 1300, "d3")
    assert "grew from 1100 to 1300 changed lines (+18%)" in grown
    assert "still the right design" in grown and grown.index("6. ") < grown.index("Fix and push")
    assert harness.size_reads[-1] == (MID, NEW)
    # A failed read, or a head GitHub would not size, sends the plain checklist.
    harness.size_read = lambda: (_ for _ in ()).throw(RuntimeError("gh is down"))
    assert "6. " not in await round_at(SHA, 9000, "d4")
    harness.size_read = None
    assert "6. " not in await round_at("d" * 40, None, "d5")
    # No writer stores a size: the rows say only what was asked.
    assert all("size" not in event.data for event in harness.history)
    # A restart keeps the rounds: they live in the review history, not in the router.
    first = harness.service
    harness.service = WorkRouterService(
        root,
        switched=first._switched,
        owner_of_issue=first._chat_of_issue,
        owner_of_pull=first._chat_of_pull,
        schedule=first._schedule,
        start_review=first._start_review,
        own_logins=OWN,
        ready_head=first._ready_head,
        exists=first._exists,
        clock=first._clock,
        pre_review=True,
        review_event=harness.note,
        reviews=harness.reviews_of,
        diff_sizes=first._diff_sizes,
    )
    harness.sizes["d" * 40] = 1300
    assert "grew from 1300 to 1600" in await round_at("e" * 40, 1600, "d6")
    # #4220 review: the head moves on between the checklist and pre-review-done, so the
    # review asks about another head. That head is the next round's baseline, not the
    # checklist's; a checklist still waiting is no round either.
    harness.heads[key], harness.sizes["a1" * 20] = "a1" * 20, 1900
    pull_event(root, "d6a", sha="a1" * 20)
    await harness.drain()
    assert "grew from 1600 to 1900" in harness.scheduled[-1].text
    harness.heads[key], harness.sizes["a5" * 20] = "a5" * 20, 1950
    pull_event(root, "d6c", sha="a5" * 20)
    await harness.drain()
    assert "grew from 1600 to 1950" in harness.scheduled[-1].text
    harness.heads[key], harness.sizes["a2" * 20] = "a2" * 20, 1650
    assert (await harness.service.pre_review_done("eerovil/agentdeck", 9))["head"] == "a2" * 20
    assert "grew from 1650 to 1900" in await round_at("a3" * 20, 1900, "d6b")
    # #4220 review: a person's press on the Review form writes an ``asked`` row with no data;
    # it is a round like any other.
    harness.note(pull, "9a" * 20, review_history.ASKED, "press", data={})
    harness.sizes["9a" * 20] = 1500
    assert "grew from 1500 to 1800" in await round_at("a4" * 20, 1800, "d6e")
    # Two at once: another head's checklist goes out while this one's sizes are read. The
    # read gave the router away, so this head is covered by that checklist, not sent another.
    sent = len(harness.scheduled)
    harness.heads[key], harness.sizes["f" * 40] = "f" * 40, 2500
    harness.size_read = lambda: harness.note(pull, "9" * 40, review_history.CHECKLIST, "other")
    pull_event(root, "d7", action="synchronize", sha="f" * 40)
    await harness.drain()
    assert len(harness.scheduled) == sent
    assert harness.history[-1].data == {"covered": True}


async def test_an_agents_gh_pr_ready_prints_its_checklist_once(root, harness):
    """#4253: the checklist a `gh pr ready` starts goes into that command's reply and is
    recorded as sent with no message; whichever of the ready event and the press comes
    first, it reaches the agent once. Too late for the reply, it is a routine message."""
    from agentdeck.deck_notices import ROUTINE_LINE

    harness.service.pre_review_on = True
    pull, key = "eerovil/agentdeck#9", ("eerovil/agentdeck", 9)
    pressed: list[dict] = []

    async def press(repository: str, number: int, budget: float = 4.0, caller="chat-a") -> str:
        answer = await harness.service.ready_press(repository, number, budget, caller=caller)
        pressed.append(answer)
        return answer["checklist"]

    def checklists(head: str) -> list:
        return [e for e in harness.history if e.kind == review_history.CHECKLIST and e.head == head]

    # The press first, then the ready event it raced: printed, and the event sends nothing.
    harness.heads[key] = OLD
    text = await press("EEROVIL/agentdeck", 9)
    assert text.startswith(f"AgentDeck: before {pull} at {OLD[:12]}") and ROUTINE_LINE not in text
    assert harness.scheduled == [] and harness.reviews == []
    assert [e.data for e in checklists(OLD)] == [{"printed": True}]
    assert harness.service.pre_review_held(pull)
    pull_event(root, "d1", sha=OLD)
    await harness.drain()
    assert harness.scheduled == [] and len(checklists(OLD)) == 1
    # Handed over is not delivered (#4253 review): its message waits a retry's length for
    # the agent's word that it printed it, which takes it back; nothing is sent then.
    # Only the Work's own chat, at the head the reply named, can say so.
    waiting = f"message:work-event:pre-review:{pull}@{OLD}"
    assert harness.service.pending.entries[waiting]["receipt"] is True
    assert pressed[-1]["heads"] == [OLD]
    received = harness.service.ready_received
    assert received("eerovil/agentdeck", 9, [OLD], caller="chat-b") == 0
    assert received("eerovil/agentdeck", 9, [MID], caller="chat-a") == 0
    assert received("eerovil/agentdeck", 9, [], caller="chat-a") == 0
    assert waiting in harness.service.pending
    assert received("EEROVIL/agentdeck", 9, [OLD.upper()], caller="chat-a") == 1
    assert waiting not in harness.service.pending
    harness.now += RETRY_FIRST_S + 1
    await harness.service.retry_pending()
    assert harness.scheduled == []
    assert received("eerovil/agentdeck", 9, [OLD], caller="chat-a") == 0
    # Another chat that registered the same pull request presses: the router places it with
    # chat-a's Work, so nothing is handed back to chat-b, and chat-a gets its message.
    harness.heads[key] = "a1" * 20
    pressing = asyncio.create_task(press("eerovil/agentdeck", 9, caller="chat-b"))
    await asyncio.sleep(0)
    pull_event(root, "d0", sha="a1" * 20)
    await harness.drain()
    assert await pressing == "" and pressed[-1]["heads"] == []
    [message] = harness.scheduled
    assert message.chat_id == "chat-a" and message.id.endswith("@" + "a1" * 20)
    harness.scheduled.clear()
    # The ready event first: sent as a routine message, and the press after it prints nothing.
    harness.heads[key] = MID
    pull_event(root, "d2", sha=MID)
    await harness.drain()
    [message] = harness.scheduled
    assert message.id == f"work-event:pre-review:{pull}@{MID}"
    assert message.text.startswith(ROUTINE_LINE + "\n\nAgentDeck: before")
    assert await press("eerovil/agentdeck", 9) == "" and len(harness.scheduled) == 1
    # Both at once: the press's wait is open while the event is acted on, so it prints.
    harness.heads[key] = NEW
    pressing = asyncio.create_task(press("eerovil/agentdeck", 9))
    await asyncio.sleep(0)  # the press is waiting
    pull_event(root, "d3", sha=NEW)
    await harness.drain()
    assert NEW[:12] in await pressing and len(harness.scheduled) == 1
    assert len(checklists(NEW)) == 1
    # No word back (the answer was lost on its way): after the wait it goes to the chat as
    # the same routine message, once.
    harness.now += RETRY_FIRST_S - 1
    await harness.service.retry_pending()
    assert len(harness.scheduled) == 1
    harness.now += 2
    await harness.service.retry_pending()
    await harness.service.retry_pending()
    assert harness.scheduled[-1].id == f"work-event:pre-review:{pull}@{NEW}"
    assert harness.scheduled[-1].text.startswith(ROUTINE_LINE) and len(harness.scheduled) == 2
    # Not made within the wait: the look goes on, and its checklist goes to the chat.
    harness.heads[key] = SHA
    gate, read = asyncio.Event(), harness.service._ready_head

    async def slow(repository: str, number: int) -> str:
        await gate.wait()
        return await read(repository, number)

    harness.service._ready_head = slow
    assert await press("eerovil/agentdeck", 9, budget=0.01) == ""
    gate.set()
    await asyncio.gather(*harness.service._checks)
    harness.service._ready_head = read
    assert harness.scheduled[-1].id == f"work-event:pre-review:{pull}@{SHA}"
    assert harness.scheduled[-1].text.startswith(ROUTINE_LINE)
    # A repository the router does not run is not looked at.
    assert await press("eerovil/trailhead", 9) == ""
    # Going through the checklist is word enough that the agent has it: a printed one whose
    # receipt never came is not sent after pre-review-done.
    late = "e" * 40
    harness.heads[key] = late
    pressing = asyncio.create_task(press("eerovil/agentdeck", 9))
    await asyncio.sleep(0)
    pull_event(root, "d4", sha=late)
    await harness.drain()
    assert late[:12] in await pressing
    sent = len(harness.scheduled)
    await harness.service.pre_review_done("eerovil/agentdeck", 9)
    harness.now += RETRY_FIRST_S + 1
    await harness.service.retry_pending()
    assert not [m for m in harness.scheduled[sent:] if m.id.startswith("work-event:pre-review:")]


async def test_pre_review_done_refuses_while_paperwork_is_missing(root, harness):
    """#4261: the deck's own paperwork check refuses pre-review-done with each gap, writes
    nothing, so the checklist still waits; once complete, or when the check fails, the review
    starts as before."""
    harness.service.pre_review_on = True
    found: list = [["the pull request body does not say `Refs #7`", "no walkthrough"]]
    calls: list = []

    async def paperwork(repository: str, number: int, work: str, first: bool) -> list[str]:
        calls.append((repository, number, work, first))
        if isinstance(found[0], Exception):
            raise found[0]
        return found[0]

    harness.service._paperwork = paperwork
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    pull_event(root, "d1", sha=OLD)
    await harness.drain()
    before = len(harness.history)
    with pytest.raises(AgainRefused) as refused:
        await harness.service.pre_review_done("EEROVIL/agentdeck", 9)
    text = str(refused.value)
    assert "1. the pull request body does not say `Refs #7`\n2. no walkthrough" in text
    assert "review did not start" in text
    # #4437: no review asked yet, so the helper checks' comment is asked for.
    assert calls == [("eerovil/agentdeck", 9, "eerovil/agentdeck#7", True)]
    assert harness.reviews == [] and len(harness.history) == before
    assert harness.service.pre_review_held("eerovil/agentdeck#9")
    # A check that raises holds nothing back.
    found[0] = RuntimeError("gh is down")
    assert (await harness.service.pre_review_done("eerovil/agentdeck", 9))["outcome"] == "asked"
    assert len(harness.reviews) == 1
    # A complete one goes to review too.
    found[0] = []
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    pull_event(root, "d2", sha=NEW)
    await harness.drain()
    assert (await harness.service.pre_review_done("eerovil/agentdeck", 9))["outcome"] == "asked"
    assert len(harness.reviews) == 2
    # A fix round (a review was asked at an earlier head) is never asked for the helper
    # checks' comment: a Work already past its first review when #4437 shipped included.
    assert calls[-1][3] is False

    # An unreadable history is not a first round: no extra requirement on a failed read.
    def unreadable(pull):
        raise OSError("disk")

    harness.service._reviews = unreadable
    assert harness.service._first_round("eerovil/agentdeck#10") is False


async def test_no_code_review_starts_before_the_design_passed(root, harness):
    """#4332 review: a marked design that has not passed holds every start of the code
    review: the checklist's pre-review-done and the agent's own review request refuse with
    it, and a start the deck makes itself is dropped and told, once per head and gap."""
    from agentdeck.work_router import AgainRefused

    class Design:
        gap = "the issue's newest `**Design:**` comment has no design review after it"
        read: list[str] = []

        async def missing(self, work, queued=None):
            self.read.append(work)
            return self.gap

    design = Design()
    harness.service._design = design
    pull = "eerovil/agentdeck#9"
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    harness.service.pre_review_on = True
    pull_event(root, "d1", sha=OLD)
    await harness.drain()
    with pytest.raises(AgainRefused, match="no design review after it"):
        await harness.service.pre_review_done("eerovil/agentdeck", 9)
    # Held only by the design review it already asked for: wait, with nothing to fix (#4781).
    from agentdeck.design_review import WAITING_GAP

    design.gap = WAITING_GAP
    with pytest.raises(AgainRefused) as waiting:
        await harness.service.pre_review_done("eerovil/agentdeck", 9)
    assert WAITING_GAP in str(waiting.value) and "Fix it" not in str(waiting.value)
    harness.service._tell_design_gap(("chat", "eerovil/agentdeck#7"), pull, NEW, WAITING_GAP)
    [wait_told] = [m for m in harness.scheduled if m.id.startswith("work-event:design-gap:")]
    assert "review-request" not in wait_told.text
    harness.scheduled.clear()
    design.gap = Design.gap
    with pytest.raises(AgainRefused, match="no code review before its design"):
        await harness.service.ask_again("eerovil/agentdeck", 9, "walkthrough posted")
    # With no checklist the ready event goes straight to the dispatch, which holds it too.
    harness.service.pre_review_on = False
    pull_event(root, "d2", sha=OLD, action="synchronize")
    await harness.drain()
    pull_event(root, "d3", sha=OLD, action="synchronize")
    await harness.drain()
    assert harness.reviews == [] and not harness.service._spent(pull, OLD)
    told = [m for m in harness.scheduled if m.id.startswith("work-event:design-gap:")]
    assert len(told) == 1 and "no design review after it" in told[0].text
    assert "review-request" in told[0].text
    assert not harness.service.pending.entries
    assert set(design.read) == {"eerovil/agentdeck#7"}
    # Once it passed, the agent's own request starts the review.
    design.gap = ""
    assert (await harness.service.ask_again("eerovil/agentdeck", 9, "design passed"))["outcome"]
    assert len(harness.reviews) == 1


async def test_pre_review_done_refuses_a_checked_repository_head_with_no_test_run(root, harness):
    """#4326: where the tests are checked, a head with no verification job queued, running or
    green refuses pre-review-done too, read for the Work's job id; an unread read, or a
    repository not checked, holds nothing back."""
    harness.service.pre_review_on = True
    rows: list = [[]]
    asked: list = []

    async def verification_rows(work: str) -> list[dict]:
        asked.append(work)
        if isinstance(rows[0], Exception):
            raise rows[0]
        return rows[0]

    harness.service._verification_rows = verification_rows
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    pull_event(root, "d1", sha=OLD)
    await harness.drain()
    # Not a checked repository: never read.
    checked: set = set()
    harness.service._tests_checked = lambda repository: repository in checked
    assert (await harness.service.pre_review_done("eerovil/agentdeck", 9))["outcome"] == "asked"
    assert asked == []
    checked.add("eerovil/agentdeck")
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    pull_event(root, "d2", sha=NEW)
    await harness.drain()
    with pytest.raises(AgainRefused) as refused:
        await harness.service.pre_review_done("eerovil/agentdeck", 9)
    assert "1. no verification job of this Work" in str(refused.value)
    assert asked == ["github_issue:eerovil/agentdeck#7"] and len(harness.reviews) == 1
    rows[0] = RuntimeError("runtime is down")
    assert (await harness.service.pre_review_done("eerovil/agentdeck", 9))["outcome"] == "asked"
    assert len(harness.reviews) == 2


@pytest.mark.parametrize(
    ("before", "after", "point"),
    [
        (1000, 1150, False),  # exactly 15% is not over it
        (1000, 1151, True),
        (300, 390, False),  # 30%, but under 100 lines
        (500, 600, True),
        (0, 800, False),  # nothing to take a percent of
        (1000, 400, False),  # it shrank
        (None, 1500, False),  # unread
        (1000, None, False),
        (True, 1500, False),
    ],
)
def test_growth_needs_both_the_percent_and_the_floor(before, after, point):
    assert bool(_grown(before, after)) is point


async def test_a_push_after_a_pass_at_the_checklists_head_gets_its_own_checklist(root, harness):
    """#4589: a minor fix's pass at the head a checklist was sent for settles that checklist,
    so the next push is not taken as covered by it (and left waiting on a call the agent
    never knew it owed): it gets a checklist of its own."""
    pull = "eerovil/agentdeck#9"
    harness.service.pre_review_on = True
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    pull_event(root, "p1", sha=OLD)
    await harness.drain()
    harness.note(pull, OLD, review_history.PASS, f"minor-fixed:c:{pull}@{OLD}")
    assert not harness.service.pre_review_held(pull)
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    pull_event(root, "p2", action="synchronize", sha=NEW)
    await harness.drain()
    assert harness.scheduled[-1].id == f"work-event:pre-review:{pull}@{NEW}"
    assert not [e for e in harness.history if e.data.get("covered")]
    assert harness.service.pre_review_held(pull) and harness.reviews == []


async def test_a_ready_head_waits_on_the_agents_pre_review(root, harness):
    """#3880: each ready head first sends the agent its checklist; the review starts only
    once it says it went through it, at the head current then, and only for that head."""
    harness.service.pre_review_on = True
    done = harness.service.pre_review_done
    with pytest.raises(AgainRefused, match="not open and ready"):
        await done("eerovil/agentdeck", 9)
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    pull_event(root, "d1", sha=OLD)
    await harness.drain()
    assert harness.reviews == []
    [checklist] = harness.scheduled
    assert checklist.id == f"work-event:pre-review:eerovil/agentdeck#9@{OLD}"
    # No diagram unless the owner asked (#4721).
    assert "1. No diagram work unless the owner asked for a diagram" in checklist.text
    assert "A diagram only for a new feature" not in checklist.text
    assert '{"pull_request": "eerovil/agentdeck#9"}' in checklist.text
    # ... and says how to do each point, moved there from the guide (#4252).
    assert "<!-- agentdeck-diagram -->" in checklist.text
    assert "--verification --session-token" in checklist.text
    assert harness.service.pre_review_held("EEROVIL/agentdeck#9")
    # The hold is in the history: after a restart a replayed ready event sends no second one.
    first = harness.service
    harness.service = WorkRouterService(
        root,
        switched=first._switched,
        owner_of_issue=first._chat_of_issue,
        owner_of_pull=first._chat_of_pull,
        schedule=first._schedule,
        start_review=first._start_review,
        own_logins=OWN,
        ready_head=first._ready_head,
        exists=first._exists,
        clock=first._clock,
        pre_review=True,
        review_policy="https://example.test/review-policy.md",
        review_event=harness.note,
        reviews=harness.reviews_of,
    )
    done = harness.service.pre_review_done
    pull_event(root, "d1b", sha=OLD)
    await harness.drain()
    assert harness.reviews == [] and len(harness.scheduled) == 1
    # A fix pushed while it waits sends nothing more, and a draft cannot be done.
    harness.heads[("eerovil/agentdeck", 9)] = ""
    with pytest.raises(AgainRefused, match="not open and ready"):
        await done("eerovil/agentdeck", 9)
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    pull_event(root, "d2", action="synchronize", sha=NEW)
    await harness.drain()
    assert harness.reviews == [] and len(harness.scheduled) == 1
    # #3896: turned back to draft, pushed and marked ready again while the older head's
    # checklist went unanswered: the new head gets its own, once.
    harness.heads[("eerovil/agentdeck", 9)] = ""
    pull_event(root, "d2a", action="synchronize", draft=True, sha=MID)
    await harness.drain()
    assert len(harness.scheduled) == 1, "a push while draft sends nothing"
    harness.heads[("eerovil/agentdeck", 9)] = MID
    # Pushed and marked ready at once, GitHub's ready event still names the old head, and the
    # push after it is a plain push: the live head decides (seen live on #3898).
    pull_event(root, "d2b", sha=OLD)
    pull_event(root, "d2c", action="synchronize", sha=MID)
    pull_event(root, "d2d", sha=OLD)  # its replay
    await harness.drain()
    assert harness.reviews == [] and len(harness.scheduled) == 2
    assert harness.scheduled[-1].id == f"work-event:pre-review:eerovil/agentdeck#9@{MID}"
    # Its ready event missed, registering the pull request again sends the new head's too.
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    assert await harness.service.check_registered("eerovil/agentdeck", 9)
    assert harness.reviews == [] and len(harness.scheduled) == 3
    assert harness.scheduled[-1].id == f"work-event:pre-review:eerovil/agentdeck#9@{NEW}"
    # #3893, #3896: with no checklist waiting (a reload cut a call off, or a ready event was
    # never acted on), the ready head nobody asked about is still asked about.
    harness.note("eerovil/agentdeck#9", NEW, review_history.CHECKLIST_DONE, "cut-off")
    assert (await done("EEROVIL/agentdeck", 9))["outcome"] == "asked"
    assert harness.service._spent("eerovil/agentdeck#9", NEW)
    with pytest.raises(AgainRefused, match="no pre-review checklist waits"):
        await done("eerovil/agentdeck", 9)
    assert not harness.service.pre_review_held("eerovil/agentdeck#9")
    assert len(harness.reviews) == 1
    # A replayed ready event of the reviewed head asks nothing; the next fix's head does.
    pull_event(root, "d3", action="ready_for_review", sha=NEW)
    await harness.drain()
    harness.heads[("eerovil/agentdeck", 9)] = SHA
    pull_event(root, "d4", sha=SHA)
    await harness.drain()
    assert len(harness.reviews) == 1
    assert harness.scheduled[-1].id == f"work-event:pre-review:eerovil/agentdeck#9@{SHA}"
    assert "full rules: https://example.test/review-policy.md" in harness.scheduled[-1].text
    assert "full rules" not in checklist.text, "no policy configured, no link"
    # #3933: the agent's own review request asked first, so pre-review-done is refused; the
    # checklist is done with, not left waiting on the Work page for good.
    assert harness.service.pre_review_held("eerovil/agentdeck#9")
    harness.asked("eerovil/agentdeck#9", SHA)
    with pytest.raises(AgainRefused, match="no pre-review checklist waits"):
        await done("eerovil/agentdeck", 9)
    assert not harness.service.pre_review_held("eerovil/agentdeck#9")
    # A review asked for again skips it, and clears a checklist still waiting.
    harness.note("eerovil/agentdeck#9", SHA, review_history.CHECKLIST, "sent-again")
    assert harness.service.pre_review_held("eerovil/agentdeck#9")
    assert (await harness.service.ask_again("eerovil/agentdeck", 9, "walkthrough posted"))["again"]
    assert len(harness.reviews) == 2
    assert not harness.service.pre_review_held("eerovil/agentdeck#9")
    # #3896: a ready head no checklist was ever sent for is asked about when the agent says so.
    harness.pulls[("eerovil/agentdeck", 10)] = ("chat-a", "eerovil/agentdeck#7")
    harness.heads[("eerovil/agentdeck", 10)] = OLD
    assert (await done("eerovil/agentdeck", 10))["outcome"] == "asked"
    assert harness.reviews[-1] == ("eerovil/agentdeck#10", "eerovil/agentdeck#7")
    # Each step is in its head's history, the router's only record of it (#3955).
    noted = [(event.head, event.kind, event.data) for event in harness.history]
    keys = [event.key for event in harness.history]
    kinds = [(head, kind) for head, kind, _data in noted]
    assert (NEW, "checklist", {"covered": True}) in noted, "a push while it waits"
    # (a replay writes the same key again, which the table absorbs)
    sent = [head for head, kind, data in noted if kind == "checklist" and not data]
    assert list(dict.fromkeys(sent)) == [OLD, MID, NEW, SHA]
    done_at = keys.index(f"checklist-done:eerovil/agentdeck#9@{NEW}")
    assert kinds[done_at + 1] == (NEW, "asked") and noted[done_at + 1][2]["again"] == ""
    # An ask is keyed by its head and ask-again id alone: its retry or replay is the same row.
    assert f"asked:eerovil/agentdeck#9@{NEW}+" in keys
    again = [data for head, kind, data in noted if (head, kind) == (SHA, "asked")]
    assert again[-1]["note"] == "walkthrough posted" and again[-1]["again"]
    # #4208: a review of the ready head already starting from another call (a ready event a
    # moment earlier) answers "pending", not "not reviewed", and starts no second one.
    harness.heads[("eerovil/agentdeck", 9)] = MID
    harness.service._in_flight.add(f"eerovil/agentdeck#9@{MID}")
    reviewed = len(harness.reviews)
    answer = await done("eerovil/agentdeck", 9)
    assert (answer["outcome"], answer["head"]) == ("pending", MID)
    assert len(harness.reviews) == reviewed


async def test_a_ready_head_nobody_started_is_caught_by_the_backstop(root, harness):
    """#3903: a ready head with no checklist, review or verdict past the grace gets its
    checklist with no event; one read per such pull request, none for drafts or moving heads;
    a restart in between only restarts the grace; one still not moving shows stuck."""
    reads: list[tuple[str, int]] = []
    cached: dict[tuple[str, int], str] = {}
    busy = [False]

    def stuck(head: str) -> bool:
        events = review_history.at_head(harness.reviews_of("eerovil/agentdeck#9"), head)
        return review_history.head_review(events).unstarted

    async def ready_head(repository: str, number: int) -> str:
        reads.append((repository, number))
        return harness.heads.get((repository, number), "")

    def fresh() -> WorkRouterService:
        harness.service = WorkRouterService(
            root,
            switched=lambda repository: repository.lower() in harness.switched,
            owner_of_issue=lambda repository, number: harness.issues.get((repository, number)),
            owner_of_pull=lambda repository, number: harness.pulls.get((repository, number)),
            schedule=harness.service._schedule,
            start_review=harness.service._start_review,
            own_logins=OWN,
            ready_head=ready_head,
            exists=harness.service._exists,
            clock=lambda: harness.now,
            pre_review=True,
            ready_pulls=lambda: [(*ref, head) for ref, head in cached.items()],
            busy=lambda work_id: busy[0],
            review_event=harness.note,
            reviews=harness.reviews_of,
        )
        return harness.service

    service = fresh()
    pull = "eerovil/agentdeck#9"
    # Marked ready at OLD and its ready event dropped: only the cached row says so. #10 is a
    # draft (no cached ready row) and #11 is no Work's.
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    cached[("EEROVIL/agentdeck", 9)] = OLD.upper()
    cached[("eerovil/agentdeck", 11)] = NEW
    harness.heads[("eerovil/agentdeck", 11)] = NEW
    # Two at once, the second in a project just added: switched is all it takes, with no
    # setting of its own. Its ready event was dropped too.
    harness.switched.add("eerovil/fresh")
    harness.pulls[("eerovil/fresh", 10)] = ("chat-b", "eerovil/fresh#1")
    cached[("eerovil/fresh", 10)] = MID
    harness.heads[("eerovil/fresh", 10)] = MID
    assert await service.sweep_ready() == harness.now + 300
    harness.now += 299
    assert await service.sweep_ready() == 1300.0
    assert reads == [] and harness.scheduled == []
    # A restart in between restarts the grace; nothing is lost.
    service = fresh()
    harness.now += 1
    assert await service.sweep_ready() == harness.now + 300
    harness.now += 300
    assert await service.sweep_ready() is None
    assert sorted(reads) == [("eerovil/agentdeck", 9), ("eerovil/fresh", 10)]
    assert sorted(item.id for item in harness.scheduled) == [
        f"work-event:pre-review:{pull}@{OLD}",
        f"work-event:pre-review:eerovil/fresh#10@{MID}",
    ]
    await service.sweep_ready()
    assert len(reads) == 2, "a head whose checklist waits costs no read"
    # The late ready event, after the backstop acted, sends nothing more.
    pull_event(root, "late", sha=OLD)
    await harness.drain()
    assert len(harness.scheduled) == 2 and harness.reviews == []
    # #3904 review: OLD's checklist still waits and a new head is ready, its event dropped.
    # While the agent is mid-turn it is working through that checklist: nothing, no read.
    # Once its chat is idle, the new head gets its own checklist.
    other = "d" * 40
    harness.heads[("eerovil/agentdeck", 9)] = other
    cached[("EEROVIL/agentdeck", 9)] = other
    busy[0] = True
    assert await service.sweep_ready() == harness.now + 300
    harness.now += 300
    await service.sweep_ready()
    assert len(reads) == 3 and len(harness.scheduled) == 2  # (+1: the late event's own read)
    busy[0] = False
    await service.sweep_ready()
    harness.now += 300
    await service.sweep_ready()
    assert harness.scheduled[-1].id == f"work-event:pre-review:{pull}@{other}"
    assert service.pre_review_held(pull) and len(reads) == 4
    # #3933: the agent's own review request asked for that head: its ask ends the checklist,
    # after a restart too, with no read.
    harness.asked(pull, other)
    service = fresh()
    assert not service.pre_review_held(pull)
    await service.sweep_ready()
    assert len(reads) == 4
    # The agent went through it and the review was asked; it pushed MID and marked it ready,
    # and the ready event named OLD (#3899's shape) and was not acted on: the cached row says
    # MID, and nothing moves it.
    harness.asked(pull, OLD)
    harness.heads[("eerovil/agentdeck", 9)] = MID
    cached[("EEROVIL/agentdeck", 9)] = MID
    await service.sweep_ready()
    harness.now += 300
    await service.sweep_ready()
    # (the late ready event read its live head itself: #3899)
    assert reads[-1] == ("eerovil/agentdeck", 9) and len(reads) == 5
    assert harness.scheduled[-1].id == f"work-event:pre-review:{pull}@{MID}"
    assert harness.reviews == []
    # A head nothing could start, even the backstop (the next cause nobody has seen yet):
    # stuck in its history, which the cards read (#3956); once it moves, no longer.
    harness.note(pull, MID, review_history.CHECKLIST_DONE, "done-mid")
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    cached[("EEROVIL/agentdeck", 9)] = NEW

    async def broken(*args, **kwargs):
        raise RuntimeError("an unseen cause")

    service._review = broken
    await service.sweep_ready()
    harness.now += 300
    await service.sweep_ready()
    assert stuck(NEW) and not stuck(MID)
    reads_before = len(reads)
    await service.sweep_ready()
    assert len(reads) == reads_before, "a stuck head is not read again"
    harness.asked(pull, NEW)
    await service.sweep_ready()
    assert not stuck(NEW)
    # A deck with no reviewer reads nothing.
    harness.now += 1000
    cached[("EEROVIL/agentdeck", 9)] = SHA
    service.reviewer_off = True
    await service.sweep_ready()
    harness.now += 300
    assert await service.sweep_ready() is None and len(reads) == reads_before


async def test_a_refreshed_cache_row_wakes_the_backstop_with_no_delivery(
    root, harness, monkeypatch
):
    """#3903 review: a dropped ready event leaves the cached row a draft; the refresh that
    rewrites it wakes the backstop, and the grace's own deadline does the rest."""
    from agentdeck import work_router

    monkeypatch.setattr(work_router, "SWEEP_GAP_S", 0.0)
    monkeypatch.setattr(work_router, "READY_GRACE_S", 0.0)
    cached: dict[tuple[str, int], str] = {}
    service = harness.service
    service._ready_pulls = lambda: [(*ref, head) for ref, head in cached.items()]
    service.pre_review_on = True
    loop = asyncio.create_task(service._sweep_loop())
    try:
        await asyncio.sleep(0.01)
        assert harness.scheduled == []  # the row says draft: nothing to look at
        harness.heads[("eerovil/agentdeck", 9)] = OLD
        cached[("eerovil/agentdeck", 9)] = OLD
        service.wake_sweep()  # what ``IssueResolver.refresh_pull_rows`` calls
        for _ in range(50):
            await asyncio.sleep(0.01)
            if harness.scheduled:
                break
        assert [item.id for item in harness.scheduled] == [
            f"work-event:pre-review:eerovil/agentdeck#9@{OLD}"
        ]
    finally:
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop

async def test_ready_then_push_reviews_the_new_head(root, harness):
    """#2941's order: ready at an already-reviewed head, then a push whose payload says draft.

    Marked ready at a reviewed head no longer asks for its review again (#3344): the chat is
    told to ask with a note instead, and the push still gets its own review.
    """
    harness.asked("eerovil/agentdeck#9", OLD)
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
    assert harness.service._spent("eerovil/agentdeck#9", NEW)


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
    harness.answered(pull, synced)  # the release took a verdict
    pull_event(root, "d2", action="synchronize", sha=NEW)
    await harness.drain()
    assert len(harness.asks) == 2 and compared == [(synced, NEW)]
    [told] = harness.scheduled
    assert told.id == f"work-unchanged:{pull}@{NEW}" and "verdict on that diff stands" in told.text
    assert harness.verdicts == [("chat-a", [pull])]
    # The new head's history has the old head's verdict, carried; the release credits the old
    # head's verdict to the new one, short shas too, never the other way round.
    assert harness.kinds(pull, NEW) == ["changes"]
    events = harness.reviews_of(pull)
    assert review_history.same_change(events, NEW, synced[:12])
    assert not review_history.same_change(events, synced, NEW)
    # A person settled a finding: the agent's ask reviews the head after all.
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    await harness.service.ask_again("eerovil/agentdeck", 9, "owner accepts the gap")
    assert harness.asks[2:] == [{"note": "owner accepts the gap", "again": True}]
    pull_event(root, "d3", action="synchronize", sha=third)
    pull_event(root, "d4", action="synchronize", sha=moved)  # same digest, files touched
    pull_event(root, "d5", action="synchronize", sha="f" * 40)  # its digest unread
    await harness.drain()
    events = harness.reviews_of(pull)
    assert len(harness.asks) == 6 and not review_history.same_change(events, moved, third)

    # A review event deferred by a busy reviewer is still skipped when its retry runs.
    async def busy(pull, work, **asks):
        raise ReviewBusy("someone is mid-review")

    harness.service._start_review = busy
    digests["1" * 40] = digests["2" * 40] = "f" * 64
    harness.asked(pull, "1" * 40, digest="f" * 64)
    harness.answered(pull, "1" * 40)
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

    # A pass, then a review asked for again, then a base merge before its verdict (#3972
    # review): that pass does not stand, so nothing is carried and the new head is reviewed.
    started = []

    async def start(pull, work, **asks):
        started.append(asks)

    harness.service._start_review = start
    passed, merged = "3" * 40, "4" * 40
    digests[passed] = digests[merged] = "c" * 64
    harness.asked(pull, passed, digest="c" * 64)
    harness.answered(pull, passed, review_history.PASS)
    harness.asked(pull, passed, again="x", digest="")
    pull_event(root, "d7", action="synchronize", sha=merged)
    await harness.drain()
    assert started == [{}] and harness.kinds(pull, merged) == ["asked"]

    # A standing pass a base merge carries over wakes nobody (#4253): the chat's verdict wait
    # still ends, quietly, and the new head keeps the pass.
    kept, synced_again = "5" * 40, "6" * 40
    digests[kept] = digests[synced_again] = "a" * 64
    harness.service._base_only = lambda *args: asyncio.sleep(0, result=True)
    harness.asked(pull, kept, digest="a" * 64)
    harness.answered(pull, kept, review_history.PASS)
    told, ended = len(harness.scheduled), len(harness.verdicts)
    notes: dict[str, tuple[str, str]] = {}
    harness.service._pending_note = lambda chat, note_id, text: (
        notes.setdefault(note_id, (chat, text)) is not None
    )
    pull_event(root, "d8", action="synchronize", sha=synced_again)
    await harness.drain()
    assert len(harness.scheduled) == told and started == [{}]
    assert len(harness.verdicts) == ended + 1
    assert harness.kinds(pull, synced_again) == ["pass"]
    # #4306: its chat hears it as a pending note, once per head.
    note_id = f"work-unchanged:{pull}@{synced_again}"
    assert list(notes) == [note_id] and "the pass carries over" in notes[note_id][1]
    assert notes[note_id][1].count(synced_again[:12]) == 1


async def test_a_ship_now_pass_carries_across_a_base_merge_that_touched_its_files(root, harness):
    """#4576: a pull request shipped with Ship now, then conflicted; the base merge that cleared it
    touched the pull request's own files and changed a doc's lines, so the pass was lost and
    a full review started. A Ship now pass carries to a head ``ship_merge`` proves only merged
    the base; a plain pass does not take that proof, and an unproven head is reviewed. A crash
    after the carried row is finished by the retry, and only while that head is the newest."""
    from agentdeck.review_history import SHIP_NOTE
    from agentdeck.work_router import REVIEW, Event

    pull, merged, other, plain = "eerovil/agentdeck#9", "a" * 40, "b" * 40, "c" * 40
    digests = {OLD: "d" * 64, merged: "e" * 64, other: "f" * 64, plain: "1" * 64}
    proofs, notes = [], {}

    async def digest(ref, head):
        return digests.get(head, "")

    async def ship_merge(repository, number, old, new):
        proofs.append((old, new))
        return new == merged

    harness.service._digest = digest
    harness.service._base_only = lambda *args: asyncio.sleep(0, result=False)
    harness.service._ship_merge = ship_merge
    harness.service._pending_note = lambda chat, note_id, text: (
        notes.setdefault(note_id, text) is not None
    )
    harness.asked(pull, OLD, digest="d" * 64)
    harness.note(pull, OLD, review_history.FINDINGS, "findings:1", data={"text": "1. off"})
    data = {"note": SHIP_NOTE, "digest": "d" * 64}
    harness.note(pull, OLD, review_history.ACCEPTED, "accepted:ship", data=data)
    pull_event(root, "d1", action="synchronize", sha=merged)
    await harness.drain()
    assert harness.asks == [] and proofs == [(OLD, merged)]
    assert harness.kinds(pull, merged) == ["accepted"]
    events = harness.reviews_of(pull)
    assert review_history.ship_pass_head(events, merged) == OLD
    assert harness.verdicts == [("chat-a", [pull])] and not harness.service.pending.entries
    [text] = notes.values()
    assert "the owner shipped with Ship now" in text and merged[:12] in text
    # A head with its own code changes is reviewed.
    pull_event(root, "d2", action="synchronize", sha=other)
    await harness.drain()
    assert len(harness.asks) == 1 and proofs[-1] == (merged, other)

    # A plain pass never takes the Ship now proof: its changed digest is reviewed.
    plain_pull = "eerovil/agentdeck#10"
    digests["2" * 40] = "2" * 64
    harness.asked(plain_pull, plain, digest="1" * 64)
    harness.answered(plain_pull, plain, review_history.PASS)
    calls = len(proofs)
    owner = ("chat-a", "eerovil/agentdeck#7")
    event = Event(REVIEW, "eerovil/agentdeck", 10, True, head="2" * 40)
    await harness.service._review(owner, event)
    assert len(proofs) == calls and len(harness.asks) == 2

    # The router stopped after writing a carried row: the start, still pending, finishes the
    # hand-off on retry, with the same ids, and starts no review.
    crashed = "3" * 40
    data = {"digest": "4" * 64, "carried_from": other}
    harness.note(pull, crashed, review_history.PASS, f"carried:{pull}@{crashed}", data=data)
    key = f"review:{pull}@{crashed}"
    entry = {"kind": REVIEW, "repository": "eerovil/agentdeck", "number": 9, "head": crashed}
    harness.service.pending.put(key, entry, harness.now)
    harness.heads[("eerovil/agentdeck", 9)] = crashed
    harness.now += 3600
    verdicts = len(harness.verdicts)
    await harness.service.retry_pending()
    assert len(harness.asks) == 2 and key not in harness.service.pending
    assert len(harness.verdicts) == verdicts + 1 and f"work-unchanged:{pull}@{crashed}" in notes
    # ... but not once a newer head has a wait of its own: ending the chat's waits on the
    # pull request (not kept per head) would end that one too (#4576 design review).
    stale, newer = "5" * 40, "6" * 40
    data = {"digest": "7" * 64, "carried_from": crashed}
    harness.note(pull, stale, review_history.PASS, f"carried:{pull}@{stale}", data=data)
    harness.service.pending.put(f"review:{pull}@{stale}", {**entry, "head": stale}, harness.now)
    harness.now += 1
    harness.asked(pull, newer)
    verdicts = len(harness.verdicts)
    await harness.service._review(owner, Event(REVIEW, "eerovil/agentdeck", 9, True, head=stale))
    assert len(harness.verdicts) == verdicts and f"work-unchanged:{pull}@{stale}" not in notes
    assert f"review:{pull}@{stale}" not in harness.service.pending


async def test_ship_merge_between_proves_only_a_base_merge_with_the_same_code(monkeypatch):
    """#4576: the proof a Ship now pass carries on. The first parents back to the shipped head
    are merge commits, and each code file contributes the same lines; a doc file, context and
    line numbers may differ. Anything else, or anything unread, is reviewed."""
    from agentdeck import gh_cli, work_router

    shipped, merged = "a" * 40, "b" * 40
    parents = {merged: [shipped, "c" * 40]}
    files = 2
    code = "diff --git a/src/x.py b/src/x.py\nindex 1..2 100644\n--- a/src/x.py\n+++ b/src/x.py\n"
    doc = "diff --git a/docs/x.md b/docs/x.md\n--- a/docs/x.md\n+++ b/docs/x.md\n"
    diffs = {
        shipped: code + "@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n" + doc + "@@ -1 +1 @@\n-a\n+b\n",
        merged: code + "@@ -9,2 +9,2 @@\n other\n-old\n+new\n" + doc + "@@ -1 +1 @@\n-a\n+c\n",
    }

    async def run_once(argv, *, timeout, repo=None, **kw):
        path = argv[-1] if argv[-2] != "--jq" else argv[-3]
        if "/commits/" in path:
            found = parents.get(path.rpartition("/")[2])
            return (0, "\n".join(found).encode(), b"") if found else (1, b"", b"")
        if "/pulls/" in path:
            return 0, b"master\n", b""
        diff = diffs.get(path.rpartition("...")[2])
        if argv[-1] == ".files | length":
            return (0, f"{files}\n".encode(), b"") if diff is not None else (1, b"", b"")
        return (0, diff.encode(), b"") if diff is not None else (1, b"", b"")

    monkeypatch.setattr(gh_cli, "run_once", run_once)
    between = work_router.ship_merge_between
    assert await between("o/r", 9, shipped, merged)
    assert not await between("o/r", 9, shipped, shipped)
    files = 300  # GitHub cuts a compare's files at 300: the diff may be cut too
    assert not await between("o/r", 9, shipped, merged)
    files = 2
    files = 3  # the diff lacks a file the comparison lists: unproven
    assert not await between("o/r", 9, shipped, merged)
    files = 2
    kept = dict(diffs)
    for sha in (shipped, merged):  # a code file left out of both diffs alike: still unproven
        diffs[sha] = diffs[sha].split("diff --git a/docs", 1)[0]
    assert not await between("o/r", 9, shipped, merged)
    diffs.update(kept)
    diffs[merged] = diffs[merged].replace("+new", "+newer")  # its code changed: reviewed
    assert not await between("o/r", 9, shipped, merged)
    diffs[merged] = diffs[shipped]
    parents[merged] = [shipped]  # a commit of its own, not a merge
    assert not await between("o/r", 9, shipped, merged)
    parents[merged] = ["d" * 40, "c" * 40]  # does not lead back to the shipped head
    assert not await between("o/r", 9, shipped, merged)
    parents[merged] = [shipped, "c" * 40]
    del diffs[shipped]  # unread
    assert not await between("o/r", 9, shipped, merged)


async def test_same_files_merge_proves_only_a_base_merge_that_undoes_nothing(monkeypatch):
    """#4635: the proof a Merge press stands on, with the reported shape: the branch was built
    on ``stacked``, which then merged into the base. The same tree with the base's changes
    thrown away (``-s ours``), or a base revert undone, is no proof."""
    from agentdeck import gh_cli, work_router

    old, new, base, inner = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
    earlier, stacked = "e" * 40, "f" * 40  # both already in ``old``
    good = {
        f"git/commits/{new}": f"tree\n{old}\n{base}",
        f"git/commits/{old}": f"tree\n{stacked}",
        "pulls/9": "master",
        f"compare/master...{base}": "0",
        f"compare/{base}...{new}": "ahead 2\nsrc/x.py\nsrc/y.py",
        f"compare/{old}...{base}": f"2\n{inner} {earlier}\n{base} {inner} {stacked}",
        f"compare/{earlier}...{inner}": "ahead 1\nsrc/other.py",
        f"compare/{inner}...{base}": "ahead 1\nsrc/x.py",  # the stacked work
        f"compare/{stacked}...{base}": "ahead 1\nsrc/other.py",
    }
    answers = dict(good)

    async def run_once(argv, *, timeout, repo=None, **kw):
        found = answers.get(argv[2].removeprefix("repos/o/r/"))
        return (1, b"", b"") if found is None else (0, found.encode(), b"")

    monkeypatch.setattr(gh_cli, "run_once", run_once)
    assert await work_router.same_files_merge("o/r", 9, old, new)
    for path, answer in (
        (f"git/commits/{new}", f"other\n{old}\n{base}"),  # a file changed
        (f"git/commits/{new}", f"tree\n{old}"),  # a commit of its own
        (f"git/commits/{new}", f"tree\n{base}\n{old}"),  # the base took the branch in
        (f"git/commits/{new}", f"tree\n{old}\n{base}\n{inner}"),  # three parents
        (f"compare/master...{base}", "1"),  # not on the base branch
        (f"compare/{earlier}...{inner}", "ahead 1\nsrc/y.py"),  # a base change undone
        (f"compare/{stacked}...{base}", "ahead 1\nsrc/x.py"),  # changed by the merge itself
        (f"compare/{stacked}...{base}", "diverged 1\nsrc/other.py"),
        (f"compare/{base}...{new}", "ahead 300\nsrc/x.py"),  # cut short
        (f"compare/{old}...{base}", f"3\n{inner} {earlier}\n{base} {inner} {stacked}"),
        (f"compare/{old}...{base}", "0"),
        (f"compare/{old}...{base}", f"1\n{inner}"),  # a root commit
    ):
        answers = {**good, path: answer}
        assert not await work_router.same_files_merge("o/r", 9, old, new), (path, answer)
    for path in good:  # unread
        answers = {key: value for key, value in good.items() if key != path}
        assert not await work_router.same_files_merge("o/r", 9, old, new), path


async def test_behind_base_reads_how_far_the_head_lags_its_base(monkeypatch):
    """#4576: the press warns of a conflict when its head lacks base commits; unread is no
    warning."""
    from agentdeck import gh_cli, work_router

    answers = {".base.ref": (0, b"master\n"), ".behind_by": (0, b"3\n")}

    async def run_once(argv, *, timeout, repo=None, **kw):
        if argv[-1] == ".behind_by":
            assert argv[-2:] == ["--jq", ".behind_by"] and "/compare/master..." in argv[-3]
        code, out = answers[argv[-1]]
        return code, out, b""

    monkeypatch.setattr(gh_cli, "run_once", run_once)
    assert await work_router.behind_base("o/r", 9, "a" * 40)
    answers[".behind_by"] = (0, b"0\n")
    assert not await work_router.behind_base("o/r", 9, "a" * 40)
    answers[".base.ref"] = (1, b"")
    assert not await work_router.behind_base("o/r", 9, "a" * 40)


@pytest.mark.parametrize("refuse", [False, True])
async def test_a_review_start_waits_for_a_running_ship_now_press(
    root, harness, monkeypatch, refuse
):
    """#4576 design: a base merge pushed while a Ship now press runs, before its pass is
    written, is held pending and goes once the press ends: carried when the press passed the
    head, reviewed as usual when it was refused."""
    from agentdeck import work_router
    from agentdeck.work_router import AgainRefused, ShipPorts

    monkeypatch.setattr(work_router, "SHIP_WAIT_S", 0.01)
    pull, merged = "eerovil/agentdeck#9", "a" * 40
    gate = asyncio.Event()
    records: dict[tuple[str, str], review_history.ShippedHead] = {}

    class Release:
        ports = SimpleNamespace(pulls=lambda work: [pull])

        async def on_pull_event(self, *args):
            return None

        def no_code_head(self, work, ref):
            return None

        async def owner_accepted(self, work, ref, head, note):
            await gate.wait()
            if refuse:
                return "refused: tests are red"
            data = {"note": note, "digest": "d" * 64}
            harness.note(ref, head, review_history.ACCEPTED, f"accepted:{head}", data=data)
            return f"review pass recorded for {ref} at {head[:12]}"

        def approval(self, work):
            return ""

        async def evaluate(self, work):
            return ""

    def claim(ref, head, work):
        records.setdefault((ref, head), review_history.ShippedHead(ref, head, work=work))
        return True

    def note_shipped(ref, head):
        records[(ref, head)] = replace(records[(ref, head)], shipped=True)
        return True

    async def open_head(repository, number):
        return OLD, False

    async def digest(ref, head):
        return {OLD: "d" * 64, merged: "e" * 64}.get(head, "")

    harness.service._release = Release()
    harness.service._ship = ShipPorts(
        open_head=open_head,
        mark_ready=lambda repository, number: None,
        claim=claim,
        note_shipped=note_shipped,
        shipped=lambda ref, head: records.get((ref, head)),
    )
    harness.service._digest = digest
    harness.service._ship_merge = lambda *args: asyncio.sleep(0, result=True)
    harness.asked(pull, OLD, digest="d" * 64)
    harness.note(pull, OLD, review_history.FINDINGS, "findings:1", data={"text": "1. off"})
    first = await harness.service.ship_now("eerovil/agentdeck", 9, head=OLD)
    assert first["state"] == "running"
    harness.heads[("eerovil/agentdeck", 9)] = merged
    pull_event(root, "d1", action="synchronize", sha=merged)
    await harness.drain()
    key = f"review:{pull}@{merged}"
    assert harness.asks == [] and key in harness.service.pending
    assert harness.kinds(pull, merged) == [] and harness.service.pending.entries[key]["next_at"] > (
        harness.now
    )
    gate.set()
    press = next(iter(harness.service.ship_runs.tasks.values()))
    if refuse:
        with pytest.raises(AgainRefused, match="tests are red"):
            await press
    else:
        await press
    assert harness.service.pending.entries[key]["next_at"] <= harness.now, "due at once"
    await harness.service.retry_pending()
    assert key not in harness.service.pending
    if refuse:
        # Its pass was never written: the held start is reviewed as any other.
        assert harness.asks == [{}] and harness.kinds(pull, merged) == ["asked"]
    else:
        assert harness.asks == [] and harness.kinds(pull, merged) == ["accepted"]


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
            harness.answered(ref, head, review_history.PASS)
            return f"review pass recorded for {ref} at {head[:12]}"

    harness.service._release = Release()
    harness.service._no_code = lambda repo, n, old, new, before, **_: asyncio.sleep(
        0, result=new == NEW and before == frozenset({"src/a.py"})
    )
    harness.asked(pull, OLD)
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
    # Nothing is left to do, so the chat is not woken to read so (#4253).
    assert harness.scheduled == []
    harness.service._release.held = (OLD, frozenset({"src/a.py"}))
    pull_event(root, "d3", action="synchronize", sha=NEW)
    await harness.drain()
    assert harness.asks == [] and fixed[-1][2] == NEW
    assert harness.service._spent(pull, NEW)
    pull_event(root, "d4", action="synchronize", sha=third)
    await harness.drain()
    assert harness.asks == [{}] and len(fixed) == 2

    # The proof: doc files, or files of neither diff (a base merge); never a file of the diff
    # then, which a revert drops from the diff now.
    touched: set[str] | None = set()

    async def compared(repository, old, new):
        return touched

    async def files(repository, number):
        return {"src/a.py", "src/new.py", "README.md"} | (sizes or {}).keys() - base_merged

    sizes: dict[str, int] | None = None
    base_merged = {"src/base_moved.py", "src/b.py", "src/moved.py", "src/z.py"}
    base_merged |= {"games/v8/index.html", "src/elsewhere.py"}
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
        why = []
        assert await between("eerovil/agentdeck", 9, OLD, NEW, before, why=why) is proven, changed
        assert bool(why) is not proven, (changed, why)

    # #4474: after a minor verdict, test files any size and up to MINOR_LINES lines of the
    # files the diff had; never a code file the fix adds, and nothing unread.
    renamed: frozenset[str] = frozenset()

    async def changes(repository, old, new):
        return None if sizes is None else (sizes, renamed)

    # #4743: over the limit, the count is taken again from the pull request's own diff.
    own: int | None = None
    recounted: list[list[str]] = []

    async def own_changes(repository, number, old, new, names):
        recounted.append(sorted(names))
        return own

    monkeypatch.setattr(work_router, "compared_changes", changes)
    monkeypatch.setattr(work_router, "own_changes", own_changes)
    before = {"src/a.py", "src/test_timings.py"}
    for changed, proven in (
        ({"src/a.py": 3, "README.md": 900}, True),
        ({"src/a.py": MINOR_LINES, "tests/test_a.py": 500}, True),
        # #4575: store keeps its tests in ``<app>/testing/``; a new one there is a test file.
        ({"shop/products/testing/test_barcode_search.py": 120, "src/a.py": 31}, True),
        ({"app/specs/test_a.py": 120, "web/__tests__/a.test.js": 120}, True),
        # #4575 review: a helper beside the tests is free too, as the wording says.
        ({"shop/products/testing/factories.py": 80, "app/specs/user_spec.rb": 50}, True),
        # #4604: a JavaScript test beside its code is a test file by its name.
        ({"frontend/admin/columnFilter.test.ts": 666, "src/a.py": 31}, True),
        ({"web/A.Test.TSX": 120, "web/b.test.mjs": 120, "c.test.js": 120}, True),
        ({"frontend/admin/latest.ts": 1}, False),
        ({"src/openapi.spec.ts": 1}, False),
        ({"frontend/a.test.ts/code.ts": 1}, False),
        ({"frontend/a.test.ts.py": 1}, False),
        ({"frontend/.test.ts": 1}, False),
        ({"src/a.py": MINOR_LINES + 1}, False),
        # A module named test_* outside a test directory is code.
        ({"src/test_timings.py": MINOR_LINES + 1}, False),
        ({"src/a.py": 2, "src/new.py": 1}, False),
        ({"src/a.py": 2, "src/base_moved.py": 300}, True),
        ({"README.md": 3}, True),
        (None, False),
    ):
        sizes = changed
        why: list[str] = []
        assert (
            await between("eerovil/agentdeck", 9, OLD, NEW, before, minor=True, why=why)
            is proven
        ), changed
        # #4575: every refusal gives the agent its reason, and a pass gives none.
        assert bool(why) is not proven, (changed, why)
    sizes = {"src/a.py": MINOR_LINES + 1}
    why = []
    await between("eerovil/agentdeck", 9, OLD, NEW, before, minor=True, why=why)
    assert why == [
        f"it changes {MINOR_LINES + 1} lines of code (added plus removed) in files the pull"
        f" request already had, over the limit of {MINOR_LINES}"
    ]
    # #4743: a base merge brought 182 lines into files the diff already had, and the fix is
    # 10 of the 192. Only the fix counts; a fix still too large says what was left out; an
    # unread recount, or one that is not smaller, keeps the plain count.
    del recounted[:]
    sizes = {"src/a.py": 182, "src/test_timings.py": 10, "tests/test_a.py": 40, "src/b.py": 9}
    for recount, proven, said in (
        (10, True, ""),
        (MINOR_LINES, True, ""),
        (
            MINOR_LINES + 1,
            False,
            f"it changes {MINOR_LINES + 1} lines of code (added plus"
            " removed) in files the pull request already had, not counting lines merged in from"
            f" the base branch, over the limit of {MINOR_LINES}",
        ),
        (
            None,
            False,
            "it changes 192 lines of code (added plus removed) in files the pull"
            f" request already had, over the limit of {MINOR_LINES}",
        ),
        (
            500,
            False,
            "it changes 192 lines of code (added plus removed) in files the pull"
            f" request already had, over the limit of {MINOR_LINES}",
        ),
    ):
        own, why = recount, []
        assert (
            await between("eerovil/agentdeck", 9, OLD, NEW, before, minor=True, why=why)
            is proven
        ), recount
        assert why == ([said] if said else []), recount
    # Only the code files the diff had are counted again, and a fix within the limit is not.
    assert recounted == [["src/a.py", "src/test_timings.py"]] * 5
    own, sizes = 0, {"src/a.py": MINOR_LINES}
    assert await between("eerovil/agentdeck", 9, OLD, NEW, before, minor=True) is True
    assert len(recounted) == 5
    own = None
    sizes = {"src/a.py": 2, "src/new.py": 1}
    why = []
    await between("eerovil/agentdeck", 9, OLD, NEW, before, minor=True, why=why)
    assert why[0].startswith("it adds the code file `src/new.py` to the pull request")
    # A code file renamed, even with no line changed, is reviewed; a test file renamed is not.
    sizes, renamed = {"docs/a.md": 0}, frozenset({"src/a.py"})
    assert await between("eerovil/agentdeck", 9, OLD, NEW, before, minor=True) is False
    sizes, renamed = {"tests/test_b.py": 0}, frozenset({"tests/test_a.py"})
    assert await between("eerovil/agentdeck", 9, OLD, NEW, before, minor=True) is True
    # #4604: code renamed to a test name is still code moved.
    sizes, renamed = {"src/a.test.ts": 0}, frozenset({"src/a.py", "src/a.test.ts"})
    assert await between("eerovil/agentdeck", 9, OLD, NEW, before, minor=True) is False
    # #4593: a file of the diff now moved is reviewed; one of neither diff, renamed by a base
    # merge, is not.
    sizes, renamed = {"src/moved.py": 0}, frozenset({"src/new.py"})
    why = []
    assert await between("eerovil/agentdeck", 9, OLD, NEW, before, minor=True, why=why) is False
    assert why == ["it renames or moves the code file `src/new.py`"]
    sizes = {"games/v8/index.html": 58}
    renamed = frozenset({"games/v7/index.html", "games/v8/index.html"})
    assert await between("eerovil/agentdeck", 9, OLD, NEW, before, minor=True) is True
    # #4594 review: a file of neither diff moved onto a path of the diff at the reviewed head
    # replaces that code, even as a pure rename: reviewed. A test file moved off it is free.
    sizes, renamed = {"src/a.py": 0}, frozenset({"src/elsewhere.py", "src/a.py"})
    why = []
    assert await between("eerovil/agentdeck", 9, OLD, NEW, before, minor=True, why=why) is False
    assert why == ["it renames or moves the code file `src/a.py`"]
    sizes, renamed = {"src/z.py": 0}, frozenset({"tests/test_z.py", "src/z.py"})
    assert await between("eerovil/agentdeck", 9, OLD, NEW, before, minor=True) is True
    renamed = frozenset()
    # Without the diff's files a minor fix is held to doc files, as a no-code one is.
    touched = {"src/a.py"}
    assert await between("eerovil/agentdeck", 9, OLD, NEW, None, minor=True) is False


class LateRelease:
    """#4474: a ``minor`` verdict about OLD came after the fix push to NEW (the release kept
    it at OLD); its fix check records a ``minor-fixed`` pass, as the release does."""

    def __init__(self, harness) -> None:
        self.harness = harness
        self.held: tuple | None = (OLD, frozenset({"src/a.py"}), True)
        self.fixed: list[str] = []

    async def on_pull_event(self, *args):
        return None

    def no_code_head(self, work, ref):
        return self.held

    async def no_code_fixed(self, work, ref, head):
        from agentdeck.work_release import PASS_RECORDED

        self.fixed.append(head)
        self.held = None
        self.harness.note(ref, head, review_history.PASS, f"minor-fixed:c:{ref}@{head}")
        return f"{PASS_RECORDED} for {ref} at {head[:12]}"


def late_router(harness):
    closes: list[tuple] = []

    async def close_review(work, pull, head, settled, reason):
        closes.append((work, pull, head, settled(pull, head), settled(pull, OLD), reason))
        return [5]

    release = LateRelease(harness)
    harness.service._release = release
    harness.service._close_review = close_review
    harness.service._no_code = lambda repo, n, old, new, before, minor=False, **_: asyncio.sleep(
        0, result=old == OLD and new == NEW and minor is True
    )
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    return release, closes


async def test_a_late_minor_verdicts_fix_passes_and_closes_its_review_chat(harness):
    """#4474: the release hands the router a late verdict's Work and pull request; the fix at
    the live head passes with no second review and the chat its push opened is closed, unless
    that start is still running (its own end closes it)."""
    pull, work = "eerovil/agentdeck#9", "github_issue:eerovil/agentdeck#7"
    release, closes = late_router(harness)
    # Another Work's verdict, or a pull request it does not own, checks nothing.
    await harness.service.late_fixed("github_issue:eerovil/agentdeck#8", pull)
    await harness.service.late_fixed(work, "eerovil/agentdeck#99")
    assert release.fixed == [] and closes == []
    harness.service._in_flight.add(f"{pull}@{NEW}")
    await harness.service.late_fixed(work.upper(), pull.upper())
    assert release.fixed == [NEW] and closes == []
    harness.service._in_flight.clear()
    release.held = (OLD, frozenset({"src/a.py"}), True)
    await harness.service.late_fixed(work, pull)
    [(chat_work, chat_pull, head, now, then, reason)] = closes
    assert (chat_work, chat_pull, head) == ("eerovil/agentdeck#7", pull, NEW)
    assert now and not then
    assert "minor fixes" in reason and NEW[:12] in reason
    # A fix too big for the minor limit is reviewed: nothing passes, nothing closes.
    release.held = (OLD, frozenset({"src/a.py"}), True)
    harness.service._no_code = lambda *a, **k: asyncio.sleep(0, result=False)
    await harness.service.late_fixed(work, pull)
    assert len(closes) == 1
    # #4575 review: its checklist went out before the verdict, so the reason goes apart.
    release.held = (OLD, frozenset({"src/a.py"}), True)

    def too_big(*a, why, **k):
        why.append("it changes 300 lines of code")
        return asyncio.sleep(0, result=False)

    harness.service._no_code = too_big
    harness.note(pull, NEW, review_history.CHECKLIST, f"work-event:pre-review:{pull}@{NEW}")
    await harness.service.late_fixed(work, pull)
    [said] = [m for m in harness.scheduled if m.id == f"work-event:not-small:{pull}@{NEW}"]
    assert "it changes 300 lines of code" in said.text and "already sent" in said.text
    assert not harness.service._not_small


@pytest.mark.parametrize("start", ["covered", "uncertain", "again", "passed-before"])
async def test_a_review_start_a_late_minor_pass_overtook_writes_no_ask(harness, monkeypatch, start):
    """#4474: the fix push's review start was running when the late verdict's pass landed at
    its head: no ``asked`` row goes over that pass, and the start's own end closes the chat it
    opened. A review asked again, or one started after the pass, keeps today's rules."""
    from agentdeck import work_router

    pull = "eerovil/agentdeck#9"
    release, closes = late_router(harness)
    release.held = None  # the verdict is not in yet as the push is seen
    passing = start != "again"

    async def start_review(pull_ref, work, **asks):
        harness.asks.append(asks)
        if passing:
            harness.note(pull, NEW, review_history.PASS, f"minor-fixed:c:{pull}@{NEW}")
        if start == "uncertain":
            raise RuntimeError("gh timed out after it pressed")
        return SimpleNamespace(pulls=[(pull, NEW)])

    harness.service._start_review = start_review
    monkeypatch.setattr(work_router, "covers", lambda outcome, ref, head: True)
    if start == "passed-before":
        harness.note(pull, NEW, review_history.PASS, f"minor-fixed:c:{pull}@{NEW}")
    again = "r1" if start == "again" else ""
    event = work_router.Event(
        work_router.REVIEW, "eerovil/agentdeck", 9, True, head=NEW, again=again
    )
    await harness.service._review(("chat-a", "eerovil/agentdeck#7"), event, asked=True)
    asked = [e for e in harness.reviews_of(pull) if e.kind == review_history.ASKED]
    if start in ("covered", "uncertain"):
        assert harness.asks and asked == [] and [c[2] for c in closes] == [NEW]
    elif start == "again":
        assert len(asked) == 1 and closes == []
    else:
        assert closes == []


def carry_to(harness, head=NEW):
    """The release's own carry: OLD's pass copied to ``head``, which only merged its base."""
    pull = "eerovil/agentdeck#9"
    data = {"digest": "d" * 64, "carried_from": OLD}
    harness.note(pull, head, review_history.PASS, f"carried:{pull}@{head}", data=data)


@pytest.mark.parametrize(
    "when", ["digest", "growth", "design", "covered", "uncertain", "replay", "asked"]
)
async def test_a_carried_pass_stops_a_running_automatic_start(harness, monkeypatch, when):
    """#4658: the release carried a pass to the head while its review start ran, and the
    start's ``asked`` row then voided it. The start stops, whichever wait it was in, ends the
    chat's own wait and sends no checklist; the agent's own request is still asked."""
    from agentdeck import work_router

    pull = "eerovil/agentdeck#9"
    service = harness.service
    _release, closes = late_router(harness)
    service._release = None
    service.pre_review_on = when in ("digest", "growth")
    owner = ("chat-a", "eerovil/agentdeck#7")
    event = work_router.Event(work_router.REVIEW, "eerovil/agentdeck", 9, True, head=NEW)

    async def digest(ref, head):
        if when in ("digest", "asked"):
            carry_to(harness)
        return "d" * 64

    async def growth(ref, event):
        if when == "growth":
            carry_to(harness)
        return ""

    async def design_gap(owner):
        if when == "design":
            carry_to(harness)
            return "no design review passed"
        return ""

    async def start_review(pull_ref, work, **asks):
        harness.asks.append(asks)
        if when in ("covered", "uncertain"):
            carry_to(harness)
        if when == "uncertain":
            raise RuntimeError("gh timed out after it pressed")
        return SimpleNamespace(pulls=[(pull, NEW)])

    service._digest, service._design_gap, service._growth = digest, design_gap, growth
    service._start_review = start_review
    monkeypatch.setattr(work_router, "covers", lambda outcome, ref, head: True)
    if when == "replay":
        carry_to(harness)
    await service._review(owner, event, asked=when == "asked")
    asked = review_history.ASKED in harness.kinds(pull, NEW)
    if when == "asked":
        assert asked and harness.verdicts == []
        return
    assert not asked and not harness.scheduled
    assert review_history.passed(
        review_history.standing_verdict(review_history.at_head(harness.reviews_of(pull), NEW))
    )
    assert harness.verdicts == [("chat-a", [pull])] and not service.pending.entries
    opened = when in ("covered", "uncertain")
    assert bool(harness.asks) is opened and [c[2] for c in closes] == [NEW] * opened
    assert not opened or "only its base branch changed" in closes[0][5]
    # A repeated call is told no review is coming, and registers no wait.
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    waits = []
    service._wait_review = lambda *args: waits.append(args)
    assert (await service.pre_review_done("eerovil/agentdeck", 9))["outcome"] == "not reviewed"
    assert waits == []
    # An older head's late stop ends no newer head's wait.
    harness.now += 1
    harness.asked(pull, "b" * 40)
    service._stop_on_carried(owner, pull, NEW)
    assert harness.verdicts == [("chat-a", [pull])]


async def test_a_pending_start_a_carried_pass_met_ends_its_wait_on_retry(harness):
    """#4658: the release carried the pass while the start waited to be retried, or the deck
    stopped between ending the wait and dropping the row."""
    pull = "eerovil/agentdeck#9"
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    row = {"kind": "review", "repository": "eerovil/agentdeck", "number": 9, "head": NEW}
    harness.service.pending.put(f"review:{pull}@{NEW}", row, harness.now)
    harness.now += 1
    carry_to(harness)
    harness.now += 4_000
    await harness.service.retry_pending()
    assert harness.verdicts == [("chat-a", [pull])]
    assert harness.asks == [] and not harness.service.pending.entries


@pytest.mark.parametrize("ends", ["carried", "nothing", "asked", "capped", "no note"])
async def test_an_own_request_made_while_an_automatic_start_runs_is_not_lost(harness, ends):
    """#4658: ``review-request`` met a start in flight, which may end without asking. The
    request is kept with its note until a review of the head is asked for; past the cap the
    chat is told and its wait ends."""
    from agentdeck.work_router import MAX_AGAIN, AgainBusy

    pull, service = "eerovil/agentdeck#9", harness.service
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    waits = []
    service._wait_review = lambda *args: waits.append(args)
    service._in_flight.add(f"{pull}@{NEW}")
    for note in ("first", "the owner decided", "") if ends != "no note" else ("",):
        with pytest.raises(AgainBusy):
            await service.ask_again("eerovil/agentdeck", 9, note)
    [held] = service._requests(pull, NEW)
    harness.now += 100
    if ends == "nothing":  # its start ended and only the kept request is left
        service._in_flight.clear()
        with pytest.raises(AgainBusy):
            await service.ask_again("eerovil/agentdeck", 9, "a newer note")
        assert service.pending.entries[held]["note"] == "a newer note"
        service._in_flight.add(f"{pull}@{NEW}")
    if ends in ("carried", "capped", "no note"):
        carry_to(harness)
        service._stop_on_carried(("chat-a", "eerovil/agentdeck#7"), pull, NEW)
        assert harness.verdicts == []  # the request still waits on its review
        done = await service.pre_review_done("eerovil/agentdeck", 9)
        assert done["outcome"] == "pending" and done["waiting"]
        waits.clear()
    harness.now += 4_000
    await service.retry_pending()
    assert service._requests(pull, NEW) == [held] and harness.asks == []  # still running
    service._in_flight.clear()
    if ends == "asked":
        from agentdeck import work_router

        event = work_router.Event(work_router.REVIEW, "eerovil/agentdeck", 9, True, head=NEW)
        service._note_asked(pull, event, "", uncertain=True)
        assert not service.pending.entries
        return
    if ends == "capped":
        for n in range(MAX_AGAIN):
            harness.note(pull, NEW, review_history.ASKED, f"old{n}", at=1.0, data={"again": f"{n}"})
    harness.now += 4_000
    await service.retry_pending()
    assert not service.pending.entries
    if ends in ("capped", "no note"):
        assert harness.asks == [] and harness.verdicts == [("chat-a", [pull])]
        assert "the review you asked for did not start" in harness.scheduled[-1].text
    else:
        again = ends == "carried"
        assert harness.asks == ([{"note": "the owner decided", "again": True}] if again else [{}])
        assert waits == [("chat-a", pull, True)] and harness.verdicts == []


@pytest.mark.parametrize("met", ["retry", "ready press", "replay"])
async def test_an_own_first_request_a_carried_pass_met_is_asked_again(harness, met):
    """#4658: the request's own pending row keeps its note, and becomes an ask-again row when
    a pass was carried to its head before it could start."""
    from agentdeck import work_router
    from agentdeck.chat_review import ReviewBusy

    service = harness.service
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    start = service._start_review

    async def busy(pull, work, **asks):
        raise ReviewBusy("someone is mid-review")

    service._start_review = busy
    asked = await service.ask_again("eerovil/agentdeck", 9, "the owner decided")
    assert asked["outcome"] == "pending"
    harness.now += 1
    carry_to(harness)
    service._start_review = start
    owner = ("chat-a", "eerovil/agentdeck#7")
    if met == "ready press":
        await service._ready_again(None, owner, "eerovil/agentdeck", 9, NEW)
    elif met == "replay":
        event = work_router.Event(REVIEW, "eerovil/agentdeck", 9, True, head=NEW)
        await service._review(owner, event)
    for _ in range(2):
        harness.now += 4_000
        await service.retry_pending()
    assert harness.asks == [{"note": "the owner decided", "again": True}]
    assert not service.pending.entries and harness.verdicts == []


async def test_a_compare_reports_a_pure_rename_apart(monkeypatch):
    """#4474 review: GitHub reports a content-identical rename with 0 additions and 0
    deletions, so its old name comes back as renamed, not as a size of 0."""
    import json
    import shutil
    import subprocess

    from agentdeck import work_router

    payload = {
        "status": "ahead",
        "files": [
            {
                "filename": "docs/a.md",
                "previous_filename": "src/a.py",
                "status": "renamed",
                "additions": 0,
                "deletions": 0,
            },
            {"filename": "src/b.py", "status": "modified", "additions": 2, "deletions": 1},
        ],
    }
    seen = []

    async def lines(repository, command):
        seen.append(command)
        if shutil.which("jq") is None:  # the lines jq prints for the payload
            return [
                "ahead 2",
                "0 docs/a.md",
                "renamed src/a.py",
                "renamed docs/a.md",
                "3 src/b.py",
            ]
        out = subprocess.run(
            ["jq", "-r", command[-1]], input=json.dumps(payload), capture_output=True, text=True
        )
        return out.stdout.splitlines()

    monkeypatch.setattr(work_router, "_gh_lines", lines)
    assert await work_router.compared_changes("eerovil/agentdeck", OLD, NEW) == (
        {"docs/a.md": 0, "src/b.py": 3},
        frozenset({"src/a.py", "docs/a.md"}),
    )
    assert seen[0][2] == f"repos/eerovil/agentdeck/compare/{OLD}...{NEW}"


async def test_a_minor_fix_is_counted_in_the_pull_requests_own_diff(monkeypatch):
    """#4743: ``own_changes`` compares the pull request's diff against its base at the
    reviewed head and at the new one, from GitHub's compare payload: lines a base merge
    brought into a file of the diff count nothing; a line the fix adds, drops, rewrites or
    moves does, a block moved past unchanged code included (#4744 review); anything unread
    is ``None``, so the caller keeps its plain count."""
    import base64
    import shutil
    import subprocess

    from agentdeck import work_router

    def patch(*rows: str) -> str:
        return "@@ -1,4 +1,6 @@ def f():\n context\n" + "\n".join(rows)

    left_was, left_now = "a" * 40, "b" * 40

    def payload(files: dict[str, str | None], status: str = "ahead", left: str = "") -> dict:
        return {
            "status": status,
            "merge_base_commit": {"sha": left},
            "files": [
                {"filename": name, "additions": 1, "deletions": 0}
                | ({} if rows is None else {"patch": rows})
                for name, rows in files.items()
            ],
        }

    def sized(files: dict[str, int], status: str = "ahead") -> dict:
        return {
            "status": status,
            "files": [
                {"filename": name, "additions": size, "deletions": 0}
                for name, size in files.items()
            ],
        }

    block = [f"+line {n}" for n in range(101)]
    was = {
        "src/merged.py": patch("+a = 1", "-b = 2", " shared", "+c = 3"),
        "src/added.py": patch("+a = 1"),
        "src/rewritten.py": patch("+a = 1", "+b = 2"),
        "src/moved.py": patch("+a = 1", "+b = 2", "+c = 3"),
        "src/dropped.py": patch("+a = 1", "-b = 2"),
        "src/big.py": patch("+a = 1"),
        "src/relocated.py": patch(*block),
    }
    now = {
        # The base merge moved the hunk and its context; the pull request's own lines stand.
        "src/merged.py": "@@ -90,7 +190,9 @@ def g():\n other\n+a = 1\n-b = 2\n+c = 3",
        # A character Python's line split cuts at must not leave the patch unread.
        "src/added.py": patch("+a = 1", "+b = '\u2028\u0085' # \r"),
        "src/rewritten.py": patch("+a = 1", "+b = 3"),
        "src/moved.py": patch("+b = 2", "+c = 3", "+a = 1"),
        "src/big.py": None,
        # #4744 review: the fix moved the pull request's own block past unchanged code, so
        # its added lines read the same, in the same order, from another place.
        "src/relocated.py": "@@ -400,3 +400,104 @@ class Other:\n elsewhere\n" + "\n".join(block),
    }
    base_range = f"compare/{left_was}...{left_now}"
    answers: dict[str, dict | None] = {
        "pulls/9": {"base": {"ref": "release/next"}},
        f"compare/release/next...{OLD}": payload(was, left=left_was),
        f"compare/release/next...{NEW}": payload(now, left=left_now),
        base_range: sized({"src/merged.py": 180, "src/relocated.py": 50, "src/added.py": 9}),
    }
    asked: list[str] = []

    async def lines(repository, command):
        path = command[2].removeprefix(f"repos/{repository}/")
        asked.append(path)
        answer = answers.get(path)
        if answer is None:
            return None
        if shutil.which("jq") is None:  # the lines jq prints for the payload
            if "files" not in answer:
                return [answer["base"]["ref"]]
            head = f"{answer['status']} {len(answer['files'])}"
            if "merge_base_commit" not in command[-1]:
                return [head] + [
                    f"{row['additions'] + row['deletions']} {row['filename']}"
                    for row in answer["files"]
                ]
            return [f"{head} {answer['merge_base_commit']['sha']}"] + [
                base64.b64encode(json.dumps([row["filename"], row.get("patch")]).encode()).decode()
                for row in answer["files"]
            ]
        out = subprocess.run(
            ["jq", "-r", command[-1]], input=json.dumps(answer), capture_output=True, text=True
        )
        return out.stdout.splitlines()

    monkeypatch.setattr(work_router, "_gh_lines", lines)

    async def count(sizes: dict[str, int]) -> int | None:
        return await work_router.own_changes("eerovil/agentdeck", 9, OLD, NEW, sizes)

    # Each file with its plain count between the two heads: the base merge's 180 lines in a
    # file of the diff are not the fix.
    assert await count({"src/merged.py": 180}) == 0
    assert await count({"src/merged.py": 185}) == 5
    assert await count({"src/added.py": 10}) == 1
    assert await count({"src/added.py": 3}) == 1  # the base changed more than the plain count
    assert await count({"src/rewritten.py": 2}) == 2
    assert await count({"src/moved.py": 2}) == 2
    assert await count({"src/dropped.py": 2}) == 2  # the fix took the file out of the diff
    assert await count({"src/merged.py": 180, "src/added.py": 10, "src/rewritten.py": 2}) == 3
    assert await count({"src/nowhere.py": 0}) == 0 and await count({}) == 0
    # #4744 review: a 101-line block moved inside its file leaves the own diff's lines as
    # they were, and is still the block removed and added, with a base merge or without.
    assert await count({"src/relocated.py": 252}) == 202
    answers[f"compare/release/next...{NEW}"] = payload(now, left=left_was)
    del asked[:]
    assert await count({"src/relocated.py": 202}) == 202
    assert base_range not in asked  # no base merge between the heads: nothing to take off
    assert await count({"src/merged.py": 180}) == 180
    answers[f"compare/release/next...{NEW}"] = payload(now, left=left_now)
    # A patch GitHub left out is unread, and so is the whole count.
    assert await count({"src/added.py": 10, "src/big.py": 1}) is None
    # Unread: the base, any compare, 300 files, a head the base already holds, or a base
    # range that does not run forward.
    for path, answer in (
        ("pulls/9", None),
        ("pulls/9", {"base": {"ref": ""}}),
        (f"compare/release/next...{OLD}", None),
        (f"compare/release/next...{NEW}", None),
        (f"compare/release/next...{NEW}", payload(now, "behind", left_now)),
        (f"compare/release/next...{OLD}", payload(was, "identical", left_was)),
        (f"compare/release/next...{OLD}", payload(was, left="")),
        (f"compare/release/next...{NEW}", payload(now, left="not a sha")),
        (
            f"compare/release/next...{NEW}",
            payload({f"f{n}.py": "+a" for n in range(300)}, left=left_now),
        ),
        (base_range, None),
        (base_range, sized({"src/added.py": 9}, "diverged")),
        (base_range, sized({f"f{n}.py": 1 for n in range(300)})),
    ):
        kept, answers[path] = answers[path], answer
        assert await count({"src/added.py": 10}) is None, (path, answer)
        answers[path] = kept
    answers[f"compare/release/next...{NEW}"] = payload(now, "diverged", left_now)
    assert await count({"src/added.py": 10}) == 1


async def test_a_late_verdict_reads_its_heads_files_from_the_base(monkeypatch):
    """#4474: a late verdict's files are the pull request's at the head it names, read from
    the base to that head, renames under both names; the live head must descend from it, and
    a base or compare that cannot be read leaves the files unread."""
    from agentdeck import work_router

    base = "d" * 40
    answers: dict[str, list[str] | None] = {
        f"compare/{OLD}...{NEW}": ["ahead 1", "src/a.py"],
        "pulls/9": [base],
        f"compare/{base}...{OLD}": ["2", "src/a.py", "docs/b.md", "src/old_b.py"],
    }

    async def lines(repository, command):
        path = command[2].removeprefix(f"repos/{repository}/")
        return answers.get(path)

    monkeypatch.setattr(work_router, "_gh_lines", lines)
    read = work_router.late_read
    assert await read("eerovil/agentdeck", 9, OLD, NEW) == (
        True,
        {"src/a.py", "docs/b.md", "src/old_b.py"},
    )
    answers[f"compare/{base}...{OLD}"] = ["300"]
    assert await read("eerovil/agentdeck", 9, OLD, NEW) == (True, None)
    answers["pulls/9"] = ["not a sha"]
    assert await read("eerovil/agentdeck", 9, OLD, NEW) == (True, None)
    answers[f"compare/{OLD}...{NEW}"] = ["diverged 1", "src/a.py"]
    assert await read("eerovil/agentdeck", 9, OLD, NEW) == (False, None)


async def test_a_minor_verdict_hands_its_flag_to_the_size_check(root, harness):
    """#4474: a release holding a minor verdict says so, and the router asks the size check
    with ``minor``; a no-code one asks without it."""
    pull = "eerovil/agentdeck#9"
    calls = []

    class Release:
        held = (OLD, frozenset({"src/a.py"}), True)

        async def on_pull_event(self, *args):
            return None

        def no_code_head(self, work, ref):
            return self.held if ref == pull else None

        async def no_code_fixed(self, work, ref, head):
            self.held = None
            harness.answered(ref, head, review_history.PASS)
            return f"review pass recorded for {ref} at {head[:12]}"

    small = True

    async def no_code(repo, n, old, new, before, why, **flags):
        calls.append(flags)
        if not small:
            why.append("it adds the code file `src/new.py` to the pull request")
        return small

    harness.service._release = Release()
    harness.service._no_code = no_code
    harness.asked(pull, OLD)
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    pull_event(root, "m1", action="synchronize", sha=NEW)
    await harness.drain()
    assert calls == [{"minor": True}] and harness.asks == []

    # #4575: a push the size check refuses says so, with its reason, above that head's
    # checklist: store#4161's agent read the generic checklist as the pass carried over.
    small, newest = False, "d" * 40
    harness.service.pre_review_on = True
    harness.service._release.held = (NEW, frozenset({"src/a.py"}), True)
    harness.heads[("eerovil/agentdeck", 9)] = newest
    pull_event(root, "m2", action="synchronize", sha=newest)
    await harness.drain()
    checklist = harness.scheduled[-1]
    assert checklist.id == f"work-event:pre-review:{pull}@{newest}" and harness.asks == []
    text = " ".join(checklist.text.split())
    assert text.split(" AgentDeck: ", 1)[1].startswith(
        f"your push {newest[:12]} to {pull} is not the small fix the minor-fixes"
        f" review at {NEW[:12]} allows: it adds the code file `src/new.py` to the pull request."
    )
    assert "call `pre-review-done`" in text and "will not merge on it" in text
    assert not harness.service._not_small, "said once, then forgotten"
    # #4575 review: a second push while that checklist waits is covered by it, and its own
    # reason comes as a message of its own.
    covered = "9" * 40
    harness.heads[("eerovil/agentdeck", 9)] = covered
    pull_event(root, "m2b", action="synchronize", sha=covered)
    await harness.drain()
    said_apart = harness.scheduled[-1]
    assert said_apart.id == f"work-event:not-small:{pull}@{covered}" and harness.asks == []
    assert "the pre-review checklist the deck already sent" in said_apart.text
    assert not harness.service._not_small
    # A code push after a standing pass (no third item, no no-code row) is the next round,
    # and says nothing of a small fix: the review is asked as usual.
    said = []

    class Watched(dict):
        def __setitem__(self, key, value):
            said.append(key)
            super().__setitem__(key, value)

    harness.service._not_small = Watched()
    later = "e" * 40
    harness.service._release.held = (newest, None)
    harness.heads[("eerovil/agentdeck", 9)] = later
    harness.answered(pull, newest, review_history.PASS)
    pull_event(root, "m3", action="synchronize", sha=later)
    await harness.drain()
    assert calls[-1] == {} and said == []
    # A plain no-code verdict refused for size is said, as a minor one is.
    last = "f" * 40
    harness.service._release.held = (later, None)
    harness.answered(pull, later, review_history.NO_CODE)
    harness.heads[("eerovil/agentdeck", 9)] = last
    pull_event(root, "m4", action="synchronize", sha=last)
    await harness.drain()
    assert said == [f"{pull}@{last}"]


async def test_a_push_to_any_branch_reaches_the_release_by_its_branch(root, harness):
    """#4217 review: a push no Work owns (another Work's merge, a hand push) still tells the
    release which branch moved; a deleted branch and a tag are no move."""
    moved = []

    class Release:
        def on_base_moved(self, repository, branch):
            moved.append((repository, branch))
            return []

    harness.service._release = Release()
    deliver(root, event="push", delivery="p1", ref="refs/heads/master", sender=PERSON)
    deliver(root, event="push", delivery="p2", ref="refs/heads/gone", deleted=True)
    deliver(root, event="push", delivery="p3", ref="refs/tags/v1", sender=PERSON)
    await harness.drain()
    assert moved == [("eerovil/agentdeck", "master")]


async def test_an_owner_accepting_findings_passes_the_reviewed_head_with_no_review(root, harness):
    """#3828: escalation, the owner accepts as is, and the head counts as reviewed with no
    second review round; never a head the reviewer was not sent, nor a draft."""
    from agentdeck.work_router import AgainRefused

    pull = "eerovil/agentdeck#9"
    accepted = []

    class Release:
        ports = SimpleNamespace(pulls=lambda work: [pull])

        async def on_pull_event(self, *args):
            return None

        def no_code_head(self, work, ref):
            return None

        async def owner_accepted(self, work, ref, head, note):
            accepted.append((work, ref, head, note))
            return f"review pass recorded for {ref} at {head[:12]}"

    harness.service._release = Release()
    with pytest.raises(AgainRefused, match="not open and ready"):
        await harness.service.accept("eerovil/agentdeck", 9, "accept as is")
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    with pytest.raises(AgainRefused, match="nothing at it to accept"):
        await harness.service.accept("eerovil/agentdeck", 9, "accept as is")
    pull_event(root, "d1", action="ready_for_review", sha=OLD)
    await harness.drain()
    assert len(harness.asks) == 1
    # Asked for is not answered (#3828 review): the first review is still running.
    with pytest.raises(AgainRefused, match="nothing at it to accept"):
        await harness.service.accept("eerovil/agentdeck", 9, "accept as is")
    assert accepted == []
    # An escalation naming other commits, or none, marks nothing (owner's decision on #3828).
    comment(root, "d2", body=ESCALATION, number=7)
    await harness.drain()
    with pytest.raises(AgainRefused, match="nothing at it to accept"):
        await harness.service.accept("eerovil/agentdeck", 9, "accept as is")
    # A push to NEW, reviewed; then a late escalation about OLD must not pass NEW.
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    pull_event(root, "d3", action="synchronize", sha=NEW)
    await harness.drain()
    assert len(harness.asks) == 2
    comment(root, "d4", body=f"Review loop escalated to the operator\n\nAt `{OLD[:12]}`: X?")
    await harness.drain()
    with pytest.raises(AgainRefused, match="nothing at it to accept"):
        await harness.service.accept("eerovil/agentdeck", 9, "accept as is")
    # One about OLD that mentions NEW, whose own review is pending, names two: nothing.
    both = f"At `{OLD[:12]}` X stands; `{NEW[:7]}` was pushed, its review is pending."
    comment(root, "d4b", body=f"Review loop escalated to the operator\n\n{both}")
    await harness.drain()
    with pytest.raises(AgainRefused, match="nothing at it to accept"):
        await harness.service.accept("eerovil/agentdeck", 9, "accept as is")
    assert accepted == []
    # Dated by the comment, not its late delivery: an ask made after it still stands (#3972
    # review); told, as its message was stored.
    harness.now = datetime(2026, 11, 1, 12, tzinfo=UTC).timestamp()
    harness.asked(pull, NEW, again="later")
    body = f"Review loop escalated to the operator\n\nAt `{NEW[:12]}`: X?"
    comment(root, "d5", body=body, created_at="2026-11-01T11:00:00Z")
    harness.now += 600
    await harness.drain()
    answer_row = review_history.head_review(review_history.at_head(harness.reviews_of(pull), NEW))
    assert answer_row.verdict == "escalated" and answer_row.findings_told
    assert answer_row.verdict_at == datetime(2026, 11, 1, 11, tzinfo=UTC).timestamp()
    assert answer_row.review_since == harness.now - 600, "the newer ask still waits"
    answer = await harness.service.accept("EerOvil/AgentDeck", 9, "accept as is")
    assert answer == {
        "pull_request": pull,
        "head": NEW,
        "outcome": f"review pass recorded for {pull} at {NEW[:12]}",
    }
    assert accepted == [("github_issue:eerovil/agentdeck#7", pull, NEW, "accept as is")]
    assert len(harness.asks) == 2 and not harness.service.pending.entries
    with pytest.raises(AgainRefused, match="no Work's chat registered"):
        await harness.service.accept("eerovil/agentdeck", 10, "accept as is")


async def test_ship_now_passes_the_reviewed_head_and_leaves_its_findings_to_the_work(
    root, harness
):
    """#4212: Ship now, fix later at a head whose review came back with findings. A draft the
    agent made of it is readied, the review counts as a pass, the findings stay with the same
    Work (no new issue, the owner's choice) and the agent hears it; never a head the reviewer
    did not answer, a moved head, or a head shipped once already."""
    from agentdeck.work_router import SHIP_NOTE, AgainRefused, ShipPorts

    pull = "eerovil/agentdeck#9"
    accepted, readied, merges = [], [], []
    open_heads = {("eerovil/agentdeck", 9): (OLD, True)}
    records: dict[tuple[str, str], review_history.ShippedHead] = {}

    class Release:
        ports = SimpleNamespace(pulls=lambda work: [pull])

        async def on_pull_event(self, *args):
            return None

        def no_code_head(self, work, ref):
            return None

        async def owner_accepted(self, work, ref, head, note):
            accepted.append((work, ref, head, note))
            harness.now += 1
            data = {"note": note}
            harness.note(ref, head, review_history.ACCEPTED, f"accepted:{head}", data=data)
            return f"review pass recorded for {ref} at {head[:12]}"

        def approval(self, work):
            return "offered"

        async def request_merge(self, work):
            merges.append(work)
            return "waits for its tests"

        async def evaluate(self, work):
            return "waits for its tests"

        async def look_after_ship(self, work):
            # Each press's one look comes after it left the running list (#4558 redesign).
            looks.append(harness.service.ship_runs.head_of(pull))
            return "waits for its tests"

    looks: list[str] = []

    async def open_head(repository, number):
        return open_heads.get((repository, number), ("", False))

    async def mark_ready(repository, number):
        readied.append(number)
        harness.heads[(repository, number)] = open_heads[(repository, number)][0]
        return True

    def claim(ref, head, work):
        if (ref, head) in records:
            return False
        records[(ref, head)] = review_history.ShippedHead(ref, head, work=work)
        return True

    def note_shipped(ref, head):
        records[(ref, head)] = replace(records[(ref, head)], shipped=1.0)
        return True

    harness.service._release = Release()
    harness.service._ship = ShipPorts(
        open_head=open_head,
        mark_ready=mark_ready,
        claim=claim,
        note_shipped=note_shipped,
        shipped=lambda ref, head: records.get((ref, head)),
    )
    # Asked for, not answered: the reviewer has not finished this diff.
    harness.asked(pull, OLD)
    with pytest.raises(AgainRefused, match="nothing to ship past"):
        await harness.service.ship_now("eerovil/agentdeck", 9)
    harness.note(
        pull, OLD, review_history.FINDINGS, "findings:1", data={"text": "1. <b> `a.py:3` is off"}
    )
    # A newer review of the same head is out again: it may pass, so nothing ships yet.
    harness.now += 10
    harness.asked(pull, OLD, again="later")
    with pytest.raises(AgainRefused, match="nothing to ship past"):
        await harness.service.ship_now("eerovil/agentdeck", 9)
    harness.now += 10
    harness.note(pull, OLD, review_history.ESCALATED, "escalated:1", data={"url": "https://c/1"})
    behind = []

    async def lags(repository, number, head):
        behind.append((repository, number, head))
        return True

    harness.service._ship = replace(harness.service._ship, behind=lags)
    answer = await harness.service.ship_now("EerOvil/AgentDeck", 9)
    assert readied == [9]
    # Behind its base: the answer warns a conflict may come, and how to keep the pass (#4576).
    assert behind == [("eerovil/agentdeck", 9, OLD)]
    assert "is behind its base branch" in answer["outcome"]
    assert accepted == [("github_issue:eerovil/agentdeck#7", pull, OLD, SHIP_NOTE)]
    # The findings stay with the Work's own issue: no new one.
    assert answer["head"] == OLD and merges == ["github_issue:eerovil/agentdeck#7"]
    assert records[(pull, OLD)].shipped  # only a finished press says Shipped (review)
    assert looks == ["", "", ""], "one look per press, refused or shipped, never under way"
    assert records[(pull, OLD)].work == "eerovil/agentdeck#7"
    [told] = [item for item in harness.scheduled if "ship-now" in item.id]
    assert told.chat_id == "chat-a" and "push nothing more" in told.text
    assert "merge the base in with a merge commit and push only that" in told.text
    assert "is behind its base branch" in told.text, "a running press shows no outcome"
    assert "new pull request for this same issue" in told.text
    assert "keep the issue open" in told.text and "(eerovil/agentdeck#7)" in told.text
    # A second press of the same head ships nothing again, and answers recorded (#4558).
    again = await harness.service.ship_now("eerovil/agentdeck", 9)
    assert again["state"] == "recorded" and "already shipped: its findings are left to" in (
        again["outcome"]
    )
    assert len(accepted) == 1
    # A draft whose branch moved while it was readied is not shipped.
    open_heads[("eerovil/agentdeck", 9)] = (NEW, True)
    harness.answered(pull, NEW, review_history.FINDINGS)
    harness.service._ship = replace(
        harness.service._ship, mark_ready=lambda repository, number: _true_moved(harness)
    )
    with pytest.raises(AgainRefused, match="moved past .* a fix is already in review"):
        await harness.service.ship_now("eerovil/agentdeck", 9)
    assert len(accepted) == 1
    with pytest.raises(AgainRefused, match="no Work's chat registered"):
        await harness.service.ship_now("eerovil/agentdeck", 10)


@pytest.mark.parametrize("fix", ["in review", "findings"])
async def test_ship_now_of_a_head_a_fix_was_pushed_on_ships_it_in_its_own_pull_request(
    root, harness, fix
):
    """#4212 review: the owner was shown the findings at OLD, but the agent had pushed NEW on
    top, still in review or answered with findings of its own. No force-push (the owner's
    choice): the deck opens a draft pull request at OLD, registers it to the Work, records its
    pass, readies it only then and ships it; the original stays a draft holding the fix. A
    second press finds what the first made and ships nothing twice. A shown head whose review
    has no findings standing is refused."""
    from agentdeck.work_router import SHIP_NOTE, AgainRefused, ShipPorts

    pull, split_pull = "eerovil/agentdeck#9", "eerovil/agentdeck#20"
    accepted, readied, splits, registered = [], [], [], []
    open_heads = {("eerovil/agentdeck", 9): (NEW, False)}
    records: dict[tuple[str, str], review_history.ShippedHead] = {}

    class Release:
        ports = SimpleNamespace(pulls=lambda work: [pull, split_pull])

        async def on_pull_event(self, *args):
            return None

        def no_code_head(self, work, ref):
            return None

        async def owner_accepted(self, work, ref, head, note):
            # The draft is readied only once its pass is recorded: no review starts on it.
            assert readied == []
            accepted.append((work, ref, head, note))
            harness.now += 1
            harness.note(ref, head, review_history.ACCEPTED, f"accepted:{ref}", data={"note": note})
            return f"review pass recorded for {ref} at {head[:12]}"

        def approval(self, work):
            return "offered"

        async def request_merge(self, work):
            return "waits for its tests"

        async def evaluate(self, work):
            return "waits for its tests"

    async def open_head(repository, number):
        return open_heads.get((repository, number), ("", False))

    async def mark_ready(repository, number):
        readied.append(number)
        harness.heads[(repository, number)] = open_heads[(repository, number)][0]
        return True

    async def split(repository, number, head, issue):
        splits.append((repository, number, head, issue))
        open_heads.setdefault((repository, 20), (head, True))
        return 20

    register_ok = [False]

    async def register(repository, number, new):
        registered.append((repository, number, new))
        return register_ok[0]

    def claim(ref, head, work, fix=""):
        if (ref, head) in records:
            return False
        records[(ref, head)] = review_history.ShippedHead(ref, head, work=work, fix=fix)
        return True

    def note_shipped(ref, head):
        records[(ref, head)] = replace(records[(ref, head)], shipped=1.0)
        return True

    harness.service._release = Release()
    harness.service._ship = ShipPorts(
        open_head=open_head,
        mark_ready=mark_ready,
        claim=claim,
        note_shipped=note_shipped,
        shipped=lambda ref, head: records.get((ref, head)),
        split=split,
        register=register,
    )
    harness.answered(pull, OLD, review_history.FINDINGS)
    harness.now += 10
    if fix == "in review":
        harness.asked(pull, NEW)
    else:
        harness.answered(pull, NEW, review_history.FINDINGS)
    # Cut off after the pull request was opened but before it joined the Work: nothing is
    # accepted or readied, and the next press (a restart between is the same) finishes it.
    with pytest.raises(AgainRefused):
        await harness.service.ship_now("eerovil/agentdeck", 9, head=OLD)
    assert accepted == [] and readied == [] and records == {}
    register_ok[0] = True
    answer = await harness.service.ship_now("eerovil/agentdeck", 9, head=OLD)
    assert splits == [("eerovil/agentdeck", 9, OLD, 7)] * 2
    assert registered == [("eerovil/agentdeck", 9, 20)] * 2
    assert accepted == [("github_issue:eerovil/agentdeck#7", split_pull, OLD, SHIP_NOTE)]
    assert readied == [20]
    assert answer["pull_request"] == split_pull and answer["head"] == OLD
    assert records[(split_pull, OLD)].shipped and (pull, OLD) not in records
    # Its fix is the pull request it was split from: the issue stays open until that merges.
    assert records[(split_pull, OLD)].fix == pull
    [told] = [item for item in harness.scheduled if "ship-now" in item.id]
    assert told.id == f"work-event:ship-now:{split_pull}@{OLD}"
    assert f"{pull} is now a draft holding your fix" in told.text
    assert f"does not cover in {pull}. {SIBLING_SWEEP_ASK}" in told.text  # #4497 helper checks
    assert f"until {split_pull} merges, then merge its base into it" in told.text
    assert "the issue stays open" in told.text
    # A second press finds the same pull request and ships nothing twice (#4558: recorded).
    again = await harness.service.ship_now("eerovil/agentdeck", 9, head=OLD)
    assert again["state"] == "recorded" and again["pull_request"] == split_pull
    assert len(accepted) == 1 and len(splits) == 3
    # A shown head whose review no longer stands at findings ships nothing.
    with pytest.raises(AgainRefused, match="holds no findings standing at that commit"):
        await harness.service.ship_now("eerovil/agentdeck", 9, head="b" * 40)
    assert len(splits) == 3


async def test_ship_now_cut_off_anywhere_is_finished_by_the_next_press(root, harness):
    """#4212 review: the review counts as passed only once the head is recorded for its
    Work, so a lost record or a refused acceptance leaves nothing to merge, and the next
    press (after a restart too: every step is in the table) finishes the same head. Two
    presses at once ship it once."""
    import asyncio

    from agentdeck.work_router import AgainRefused, ShipPorts

    pull = "eerovil/agentdeck#9"
    accepted = []
    records: dict[tuple[str, str], review_history.ShippedHead] = {}
    fail = {"claim": True, "accept": "refused: tests are red"}
    gate = asyncio.Event()
    gate.set()

    class Release:
        ports = SimpleNamespace(pulls=lambda work: [pull])

        async def on_pull_event(self, *args):
            return None

        def no_code_head(self, work, ref):
            return None

        async def owner_accepted(self, work, ref, head, note):
            await gate.wait()
            if fail["accept"]:
                return fail["accept"]
            accepted.append(head)
            return f"review pass recorded for {ref} at {head[:12]}"

        def approval(self, work):
            return ""

        async def evaluate(self, work):
            raise RuntimeError("the release looks again at its next event")

    async def open_head(repository, number):
        return OLD, False

    def claim(ref, head, work):
        if not fail["claim"] or (ref, head) in records:
            return False
        records[(ref, head)] = review_history.ShippedHead(ref, head, work=work, at=500.0)
        return True

    def note_shipped(ref, head):
        records[(ref, head)] = replace(records[(ref, head)], shipped=1.0)
        return True

    harness.service._release = Release()
    harness.service._ship = ShipPorts(
        open_head=open_head,
        mark_ready=lambda repository, number: None,
        claim=claim,
        note_shipped=note_shipped,
        shipped=lambda ref, head: records.get((ref, head)),
    )
    harness.note(pull, OLD, review_history.FINDINGS, "findings:1", data={"text": "1. off"})
    # A head that could not be recorded at all ships nothing.
    fail["claim"] = False
    with pytest.raises(AgainRefused, match="could not be recorded, so nothing shipped"):
        await harness.service.ship_now("eerovil/agentdeck", 9)
    assert accepted == [] and records == {}
    assert not [item for item in harness.scheduled if "ship-now" in item.id]
    # The next press records it; the acceptance is refused, so nothing ships.
    fail["claim"] = True
    with pytest.raises(AgainRefused, match="tests are red"):
        await harness.service.ship_now("eerovil/agentdeck", 9)
    assert records[(pull, OLD)].work == "eerovil/agentdeck#7"
    assert accepted == [] and not records[(pull, OLD)].shipped
    # A press made from a confirm that showed another head's findings ships nothing.
    with pytest.raises(AgainRefused, match="moved past dddddddddddd, whose findings"):
        await harness.service.ship_now("eerovil/agentdeck", 9, head="d" * 40)
    # Two presses at once: the second joins the first (#4558), which ships it once.
    fail["accept"] = ""
    gate.clear()
    first = asyncio.ensure_future(harness.service.ship_now("eerovil/agentdeck", 9))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(harness.service.ship_now("eerovil/agentdeck", 9))
    await asyncio.sleep(0)
    assert len(harness.service.ship_runs.tasks) == 1
    gate.set()
    answer, again = await first, await second
    assert answer == again and answer["head"] == OLD and answer["state"] == "recorded"
    assert accepted == [OLD] and records[(pull, OLD)].shipped
    assert harness.service.ship_runs.tasks == {} and harness.service.ship_runs.resolved == {}


async def test_a_press_refused_after_its_pass_is_finished_by_the_next_press(root, harness):
    """#4212 review: once a press recorded the pass, a refused Merge press or a failed mark
    leaves the head unfinished, so its merge holds (``ship_unfinished``) and the next press
    finishes it, at the head a base update carried the pass to too, with no second
    acceptance or Merge press. Only a finished press tells the agent."""
    from agentdeck.work_release import PressRefused
    from agentdeck.work_router import SHIP_NOTE, AgainRefused, ShipPorts

    pull = "eerovil/agentdeck#9"
    accepted, merges, looked = [], [], []
    records: dict[tuple[str, str], review_history.ShippedHead] = {}
    live = {"head": OLD, "merge": "refuse", "mark": "fail", "approval": "offered"}

    class Release:
        ports = SimpleNamespace(pulls=lambda work: [pull])

        async def on_pull_event(self, *args):
            return None

        def no_code_head(self, work, ref):
            return None

        async def owner_accepted(self, work, ref, head, note):
            accepted.append(head)
            data = {"note": note}
            harness.note(ref, head, review_history.ACCEPTED, f"accepted:{head}", data=data)
            return f"review pass recorded for {ref} at {head[:12]}"

        def approval(self, work):
            return live["approval"]

        async def request_merge(self, work):
            if live["merge"] == "refuse":
                raise PressRefused(f"{pull} could not be read, so no Merge was recorded")
            merges.append(work)
            live["approval"] = "approved"
            return "waits for its tests"

        async def evaluate(self, work):
            looked.append(work)
            return "waits for its tests"

    async def open_head(repository, number):
        return live["head"], False

    def claim(ref, head, work):
        if (ref, head) in records:
            return False
        records[(ref, head)] = review_history.ShippedHead(ref, head, work=work)
        return True

    def note_shipped(ref, head):
        if live["mark"] == "fail":
            return False
        if live["mark"] == "raise":
            raise OSError("database is locked")
        records[(ref, head)] = replace(records[(ref, head)], shipped=1.0)
        return True

    harness.service._release = Release()
    harness.service._ship = ShipPorts(
        open_head=open_head,
        mark_ready=lambda repository, number: None,
        claim=claim,
        note_shipped=note_shipped,
        shipped=lambda ref, head: records.get((ref, head)),
    )
    harness.note(pull, OLD, review_history.FINDINGS, "findings:1", data={"text": "1. off"})
    told = lambda: [item for item in harness.scheduled if "ship-now" in item.id]  # noqa: E731
    # The pass is recorded, but the Merge press this project waits for is refused.
    with pytest.raises(AgainRefused, match=r"Merge was not recorded \(.*could not be read"):
        await harness.service.ship_now("eerovil/agentdeck", 9)
    assert accepted == [OLD] and not records[(pull, OLD)].shipped
    # A stopped press makes the release look again (#4558), so no line says it is under way.
    assert told() == [] and looked == ["github_issue:eerovil/agentdeck#7"]
    # The next press records the Merge press, but the head could not be marked shipped.
    live["merge"] = ""
    with pytest.raises(AgainRefused, match="could not be marked shipped, so it does not merge"):
        await harness.service.ship_now("eerovil/agentdeck", 9)
    assert accepted == [OLD] and len(merges) == 1
    live["mark"] = "raise"
    with pytest.raises(AgainRefused, match="press Ship now again to finish it"):
        await harness.service.ship_now("eerovil/agentdeck", 9)
    # Base updates carried the pass to a new head: the press still finishes the head it
    # passed, with no second acceptance or Merge press, and the release looks again.
    live["head"], live["mark"] = NEW, ""
    # Two base updates carry it OLD -> MID -> NEW; each row names only the head before.
    for before, after in ((OLD, MID), (MID, NEW)):
        carried = {"note": SHIP_NOTE, "carried_from": before}
        harness.note(pull, after, review_history.PASS, f"carried:{pull}@{after}", data=carried)
    answer = await harness.service.ship_now("eerovil/agentdeck", 9, head=NEW)
    assert answer["head"] == OLD
    assert "finished" in answer["outcome"]
    assert accepted == [OLD] and len(merges) == 1 and looked
    assert records[(pull, OLD)].shipped and len(told()) == 1 and list(records) == [(pull, OLD)]
    # A repeat of a finished press is an answer, not a refusal (#4558).
    again = await harness.service.ship_now("eerovil/agentdeck", 9)
    assert again["state"] == "recorded" and again["head"] == OLD
    assert "already shipped: its findings are left to this Work" in again["outcome"]


async def test_a_slow_ship_now_answers_running_and_tells_the_chat_how_it_ended(
    root, harness, monkeypatch
):
    """#4558: a press longer than the call's wait answers ``running`` and goes on; a repeat
    joins it, the release reads it as under way, and a press that stops after its caller left
    tells the Work's chat, leaves the table first and makes the release look again. A head
    already marked shipped answers ``recorded`` before any GitHub read, merged or not."""
    import asyncio

    from agentdeck import work_router
    from agentdeck.work_router import AgainRefused, ShipPorts

    monkeypatch.setattr(work_router, "SHIP_WAIT_S", 0.01)
    pull = "eerovil/agentdeck#9"
    runs = harness.service.ship_runs
    gate = asyncio.Event()
    look_open = asyncio.Event()
    look_open.set()
    looked, reads = [], []
    records: dict[tuple[str, str], review_history.ShippedHead] = {}

    class Release:
        ports = SimpleNamespace(pulls=lambda work: [pull])

        async def on_pull_event(self, *args):
            return None

        def no_code_head(self, work, ref):
            return None

        async def owner_accepted(self, work, ref, head, note):
            await gate.wait()
            return "refused: tests are red"

        def approval(self, work):
            return ""

        async def evaluate(self, work):
            # Out of the table first, so the line it stores is not "under way".
            looked.append(runs.running(pull, OLD))
            await look_open.wait()
            return ""

    async def open_head(repository, number):
        reads.append(number)
        return OLD, False

    def claim(ref, head, work):
        records.setdefault((ref, head), review_history.ShippedHead(ref, head, work=work))
        return True

    harness.service._release = Release()
    harness.service._ship = ShipPorts(
        open_head=open_head,
        mark_ready=lambda repository, number: None,
        claim=claim,
        note_shipped=lambda ref, head: False,
        shipped=lambda ref, head: records.get((ref, head)),
        shipped_by_work=lambda work: [r for r in records.values() if r.shipped],
    )
    harness.note(pull, OLD, review_history.FINDINGS, "findings:1", data={"text": "1. off"})
    first = await harness.service.ship_now("eerovil/agentdeck", 9, head=OLD)
    assert first["state"] == "running" and first["head"] == OLD
    assert runs.running(pull, OLD) and not runs.running(pull, NEW)
    again = await harness.service.ship_now("eerovil/agentdeck", 9, head=OLD.upper())
    assert again["state"] == "running" and len(runs.tasks) == 1 and reads == [9]
    gate.set()
    task = next(iter(runs.tasks.values()))
    with pytest.raises(AgainRefused, match="tests are red"):
        await task
    assert runs.tasks == {} and runs.resolved == {} and runs.left == set()
    assert looked == [False]
    told = [item for item in harness.scheduled if "ship-stopped" in item.id]
    assert len(told) == 1 and "tests are red" in told[0].text
    assert "same pull request and head is safe" in told[0].text
    # A press its caller waited for to the end tells nothing more: the caller heard it.
    gate.clear()
    monkeypatch.setattr(work_router, "SHIP_WAIT_S", 5.0)
    waited = asyncio.ensure_future(harness.service.ship_now("eerovil/agentdeck", 9, head=OLD))
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(AgainRefused, match="tests are red"):
        await waited
    assert len([item for item in harness.scheduled if "ship-stopped" in item.id]) == 1
    # Refused before the wait ran out, but its closing look outlasts the wait: the caller
    # hears ``running``, so the chat must hear the refusal once the look ends (#4559 review).
    monkeypatch.setattr(work_router, "SHIP_WAIT_S", 0.01)
    look_open.clear()
    # Its id is stamped to the second, so the earlier message would absorb it.
    harness.scheduled[:] = [item for item in harness.scheduled if "ship-stopped" not in item.id]
    slow = asyncio.ensure_future(harness.service.ship_now("eerovil/agentdeck", 9, head=OLD))
    await asyncio.sleep(0)
    press = runs.tasks[f"{pull}@{OLD}"]  # off the list once refused, before its look
    await asyncio.sleep(0.05)
    assert slow.done() and slow.result()["state"] == "running"
    look_open.set()
    with pytest.raises(AgainRefused, match="tests are red"):
        await press
    told = [item for item in harness.scheduled if "ship-stopped" in item.id]
    assert len(told) == 1 and "tests are red" in told[0].text
    assert runs.left == set()
    # Marked shipped and merged since: the repeat answers recorded with no GitHub read.
    records[(pull, OLD)] = replace(records[(pull, OLD)], shipped=1.0)
    reads.clear()
    done = await harness.service.ship_now("eerovil/agentdeck", 9, head=OLD)
    assert done["state"] == "recorded" and done["pull_request"] == pull and reads == []
    # A split's source pull request at the shipped commit names the split one.
    records[(pull, OLD)] = replace(records[(pull, OLD)], pull="eerovil/agentdeck#30", fix=pull)
    split = await harness.service.ship_now("eerovil/agentdeck", 9, head=OLD)
    assert split["pull_request"] == "eerovil/agentdeck#30" and reads == []
    # Another caller's press already shipping the head this one resolves to (a carried pass,
    # or a split pull request pressed beside its source): this one waits for it and ships
    # nothing itself (#4559 helper checks).
    records.clear()
    other = asyncio.get_running_loop().create_future()
    runs.tasks["eerovil/agentdeck#9@carried"] = other
    runs.resolved["eerovil/agentdeck#9@carried"] = f"{pull}@{OLD}"
    joined = asyncio.ensure_future(harness.service.ship_now("eerovil/agentdeck", 9, head=OLD))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not joined.done() and reads == [9]
    other.set_result({"pull_request": pull, "head": OLD, "state": "recorded"})
    assert (await joined)["state"] == "recorded" and records == {}
    runs.tasks.clear()
    runs.resolved.clear()


async def _true_moved(harness) -> bool:
    harness.heads[("eerovil/agentdeck", 9)] = "e" * 40
    return True


async def test_an_escalation_retelling_older_heads_is_accepted_once_its_chat_line_lands(
    root, harness
):
    """#4069: an escalation names every head its loop reviewed, so its comment marks none,
    even when the older head was answered before it (#3828). The owner's acceptance is
    refused with a time wait, not a new review, and passes once the review chat's closing
    line writes the escalation at the head it reviewed."""
    from agentdeck.work_router import AgainRefused

    pull = "eerovil/agentdeck#9"
    accepted = []

    class Release:
        ports = SimpleNamespace(pulls=lambda work: [pull])

        async def on_pull_event(self, *args):
            return None

        def no_code_head(self, work, ref):
            return None

        async def owner_accepted(self, work, ref, head, note):
            accepted.append(head)
            return f"review pass recorded for {ref} at {head[:12]}"

    harness.service._release = Release()
    harness.now = datetime(2026, 11, 4, 8, tzinfo=UTC).timestamp()
    harness.asked(pull, OLD)
    harness.answered(pull, OLD, review_history.FINDINGS)
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    harness.now += 60
    harness.asked(pull, NEW)
    body = LOOP_ESCALATION.format(old=OLD[:8], new=NEW[:8])
    comment(root, "e1", body=body, created_at="2026-11-04T08:19:31Z")
    await harness.drain()
    assert harness.kinds(pull, NEW) == [review_history.ASKED]
    with pytest.raises(AgainRefused, match="time wait") as refused:
        await harness.service.accept("eerovil/agentdeck", 9, "accept the limit")
    assert "new review" in str(refused.value) and accepted == []
    # The chat board writes it from "Reviewed eerovil/agentdeck#9 at <NEW>: escalated".
    harness.now += 180
    harness.note(pull, NEW, review_history.ESCALATED, "escalated:chat", data={"chat": "c#1"})
    answer = await harness.service.accept("eerovil/agentdeck", 9, "accept the limit")
    assert answer["head"] == NEW and accepted == [NEW]


async def test_a_heading_naming_the_commit_decides_which_head_a_comment_marks(root, harness):
    """#4069: the same escalation with the commit in its first line marks the live head at
    once and the first acceptance passes; a heading for an older head marks nothing, whatever
    its body names (#3828); and a heading marks only the pull requests it names."""
    from agentdeck.work_router import AgainRefused

    pull = "eerovil/agentdeck#9"
    accepted = []
    listed = [pull]

    class Release:
        ports = SimpleNamespace(pulls=lambda work: listed)

        async def on_pull_event(self, *args):
            return None

        def no_code_head(self, work, ref):
            return None

        async def owner_accepted(self, work, ref, head, note):
            accepted.append(head)
            return f"review pass recorded for {ref} at {head[:12]}"

    def headed(line):
        return line + LOOP_ESCALATION.format(old=OLD[:8], new=NEW[:8]).partition("\n")[2]

    escalated = "Review loop escalated to the operator for PR "
    harness.service._release = Release()
    harness.now = datetime(2026, 11, 4, 8, tzinfo=UTC).timestamp()
    harness.asked(pull, OLD)
    harness.answered(pull, OLD, review_history.FINDINGS)
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    harness.now += 60
    harness.asked(pull, NEW)
    reviewer = {"login": "review-bot[bot]", "type": "Bot"}
    findings = f"/claude\nReview findings for PR #9 at {OLD}\n\n1. {NEW[:8]} was pushed since."
    comment(root, "h", sender=reviewer, body=findings, created_at="2026-11-04T08:19:30Z")
    late = [
        headed(f"{escalated}#9 at `{OLD[:8]}`"),
        headed(f"{escalated}#9 at {'1' * 12}"),  # no head sent for review
        headed(f"{escalated}#10 at {NEW[:8]}"),  # another pull request's heading
        headed(f"{escalated}other/repo#9 at {NEW[:8]}"),
    ]
    for index, body in enumerate(late):
        comment(root, f"h{index}", body=body, created_at="2026-11-04T08:19:31Z")
    await harness.drain()
    assert harness.kinds(pull, NEW) == [review_history.ASKED]
    with pytest.raises(AgainRefused, match="time wait"):
        await harness.service.accept("eerovil/agentdeck", 9, "accept the limit")
    # Upper case and the full reference read as the real thing does.
    body = headed(f"{escalated}EerOvil/AgentDeck#9 at `{NEW[:8].upper()}`")
    comment(root, "h9", body=body, created_at="2026-11-04T08:19:32Z")
    await harness.drain()
    assert harness.kinds(pull, NEW) == [review_history.ASKED, review_history.ESCALATED]
    answer = await harness.service.accept("eerovil/agentdeck", 9, "accept the limit")
    assert answer["head"] == NEW and accepted == [NEW]
    # Findings are read by the same rule.
    findings = f"/claude\nReview findings for PR #9 at {NEW}\n\n1. As at {OLD[:8]}."
    comment(root, "h10", sender=reviewer, body=findings, created_at="2026-11-04T08:19:33Z")
    await harness.drain()
    assert harness.kinds(pull, NEW)[-1] == review_history.FINDINGS
    # Two pull requests in the Work: a heading for #10 that quotes #9's live commit leaves #9
    # alone, and one review's heading naming both marks both.
    other = "eerovil/agentdeck#10"
    harness.pulls[("eerovil/agentdeck", 10)] = ("chat-a", "eerovil/agentdeck#7")
    harness.heads[("eerovil/agentdeck", 10)] = MID
    harness.asked(other, MID)
    rows = harness.kinds(pull, NEW)
    body = f"{escalated}#10 at {MID[:8]}\n\nAs on PR #9 at `{NEW[:8]}`: X?"
    comment(root, "h11", body=body, created_at="2026-11-04T08:19:34Z")
    await harness.drain()
    assert harness.kinds(pull, NEW) == rows
    assert harness.kinds(other, MID) == [review_history.ASKED, review_history.ESCALATED]
    body = f"/claude\nReview findings for #9 at {NEW} and #10 at {MID}:\n\n1. X"
    comment(root, "h12", sender=reviewer, body=body, created_at="2026-11-04T08:19:35Z")
    await harness.drain()
    assert harness.kinds(pull, NEW)[-1] == harness.kinds(other, MID)[-1] == review_history.FINDINGS
    # The Work's pull request in another repository (store#3943 under issues#1162, #4208): a
    # bare #3943 on the Work's issue is that repository's and marks nothing, whatever commit
    # it names; written out in full it marks the pull request.
    store = "other/store#3943"
    listed[:] = [pull, other, store]
    harness.pulls[("other/store", 3943)] = ("chat-a", "eerovil/agentdeck#7")
    harness.heads[("other/store", 3943)] = OLD
    harness.asked(store, OLD)
    body = f"/claude\nReview findings for PR #3943 at {OLD}:\n\n1. X"
    comment(root, "h13", sender=reviewer, body=body, created_at="2026-11-04T08:19:36Z")
    await harness.drain()
    assert harness.kinds(store, OLD) == [review_history.ASKED]
    body = body.replace("PR #3943", "PR Other/Store#3943")
    comment(root, "h14", sender=reviewer, body=body, created_at="2026-11-04T08:19:37Z")
    await harness.drain()
    assert harness.kinds(store, OLD) == [review_history.ASKED, review_history.FINDINGS]


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
    assert harness.service._spent(pull, OLD)
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

    # The caps count the head's asks in its history (#3955): each kind has two.
    assert harness.agains(pull, OLD) == 4

    harness.service._in_flight.add(f"{pull}@{NEW}")
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    with pytest.raises(AgainBusy, match="already asked"):
        await harness.service.ask_again("eerovil/agentdeck", 9)
    harness.heads[("eerovil/agentdeck", 9)] = ""
    with pytest.raises(AgainRefused, match="gh pr ready"):
        await harness.service.ask_again("eerovil/agentdeck", 9)


@private_feature(_chat_review)
async def test_a_verdict_on_a_head_turned_back_to_draft_still_stands_for_ship_now(root, harness):
    """#4352: the agent turned the pull request back to draft for a change of scope while
    OLD's review ran; the escalation about OLD came in then and was dropped, so the owner's
    Ship now of OLD was refused. A draft's head is read too: the verdict stands at OLD, and
    Ship now may ship it past a fix pushed on top. Findings about a head the branch has left
    still mark nothing (#3828)."""
    pull = "eerovil/agentdeck#9"
    reviewer = {"login": "review-bot[bot]", "type": "Bot"}
    key = ("eerovil/agentdeck", 9)
    harness.asked(pull, OLD)
    harness.drafts[key] = OLD
    escalation = (
        f"Review loop escalated to the operator for PR eerovil/agentdeck#9 at {OLD}\n\n"
        "Security choice needed: (A) accept, (B) block at connection?"
    )
    comment(root, "e1", sender=reviewer, body=escalation, comment_id=401)
    await harness.drain()
    assert harness.kinds(pull, OLD) == [review_history.ASKED, review_history.ESCALATED]
    # The fix pushed on top leaves OLD's verdict standing: Ship now's split ships OLD.
    harness.drafts[key] = NEW
    assert review_history.shippable(harness.reviews_of(pull), OLD) is not None
    assert review_history.split_head(harness.reviews_of(pull), NEW) == OLD
    # Findings about OLD that land after the draft moved to NEW mark neither head.
    late = escalation.replace("escalated to the operator", "findings").replace("e1", "e2")
    comment(root, "e2", sender=reviewer, body=late.replace("Review loop", "Review"))
    await harness.drain()
    assert harness.kinds(pull, NEW) == []
    assert harness.kinds(pull, OLD) == [review_history.ASKED, review_history.ESCALATED]


async def test_findings_headed_above_their_command_and_cited_later_are_taken_in(root, harness):
    """#4279: the reviewer opens findings with their heading (#4208) and puts ``/claude`` on
    the next line; that shape reached no chat and marked no head. The later reviews of the
    head closed citing those findings with no new verdict, so the owner's Ship now and
    acceptance were refused for good. The heading-first shape is findings now, and a review
    chat's closing line linking findings the deck missed has them read back and recorded at
    the head, past the cap on asks; the refusals say so instead of "wait"."""
    import dataclasses

    from agentdeck.chat_board import ChatRow
    from agentdeck.work_router import AgainRefused

    pull, chat = "eerovil/agentdeck#9", "eerovil/review-chats#51"
    reviewer = {"login": "review-bot[bot]", "type": "Bot"}

    def findings(head: str) -> str:
        # The live comment's shape: heading, then the command, then the findings.
        return (
            f"Review findings for PR EerOvil/AgentDeck#9 at {head}\n/claude\n\n"
            "1. **bug (95%)** — `a.py:164`: the fallback is still absent.\n\n"
            "<!-- chatgpt-review -->"
        )

    harness.heads[("eerovil/agentdeck", 9)] = OLD
    harness.asked(pull, OLD)
    comment(root, "f1", sender=reviewer, body=findings(OLD), comment_id=301)
    # The same shape from anyone else, or naming no head, is not findings.
    comment(root, "f2", sender=PERSON, body=findings(OLD).replace("/claude", "x"))
    comment(root, "f3", sender=reviewer, body="Review findings, see above\n/claude")
    await harness.drain()
    assert harness.kinds(pull, OLD) == [review_history.ASKED, review_history.FINDINGS]
    assert [item.id for item in harness.scheduled] == ["work-event:f1"]
    assert harness.verdicts == [("chat-a", [pull])]

    # The next head's findings were missed (posted while the deck was down, say), and two
    # reviews asked after it closed citing them with no new verdict.
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    harness.asked(pull, NEW)
    harness.now += 600
    harness.asked(pull, NEW, again="nv1", no_verdict=True)
    harness.now += 600
    harness.asked(pull, NEW, again="nv2", no_verdict=True)
    harness.now += 600

    class Release:
        async def owner_accepted(self, work, ref, head, note):
            raise AssertionError("nothing to accept yet")

    harness.service._release = Release()
    closed = datetime.fromtimestamp(harness.now - 600, UTC).isoformat()
    row = ChatRow(51, "Review eerovil/agentdeck#7", state="closed", closed_at=closed)
    harness.service._review_chat = lambda ref, head: (chat, row)
    with pytest.raises(AgainRefused, match="has ended with no verdict the deck took in"):
        await harness.service.accept("eerovil/agentdeck", 9, "ship it")
    # Still open: waiting is the answer, as before.
    harness.service._review_chat = lambda ref, head: (chat, dataclasses.replace(row, state="open"))
    with pytest.raises(AgainRefused, match="time wait"):
        await harness.service.accept("eerovil/agentdeck", 9, "ship it")
    harness.service._review_chat = lambda ref, head: (chat, row)

    reads: list[tuple[str, int]] = []
    comments = {
        301: {"id": 301, "body": findings(OLD), "user": reviewer},  # an older head's
        302: {"id": 302, "body": findings(NEW), "user": PERSON},  # not the reviewer's
        303: {"id": 303, "body": findings(NEW), "user": reviewer},
        304: {"id": 304, "body": findings(NEW), "user": reviewer},
    }

    async def read_comment(repository: str, ident: int):
        reads.append((repository, ident))
        return dict(comments[ident], created_at="2026-11-11T08:21:05Z")

    harness.service._read_comment = read_comment
    url = "https://github.com/EerOvil/AgentDeck/issues/{n}#issuecomment-{c}"

    def closing(*links: str) -> str:
        return f"Reviewed EerOvil/AgentDeck#9 at `{NEW}`: 1 existing finding, {', '.join(links)}."

    # Links on another Work's issue are not read; the rest name another head or another author.
    # While the review chat still runs, the spent asks are the answer.
    harness.service._review_chat = lambda ref, head: (chat, dataclasses.replace(row, state="open"))
    note = closing(url.format(n=8, c=304), url.format(n=7, c=301), url.format(n=7, c=302))
    with pytest.raises(AgainRefused, match="after a review with no verdict"):
        await harness.service.ask_again("eerovil/agentdeck", 9, note, no_verdict=True)
    assert reads == [("eerovil/agentdeck", 301), ("eerovil/agentdeck", 302)]
    assert harness.kinds(pull, NEW)[-1] == review_history.ASKED
    harness.service._review_chat = lambda ref, head: (chat, row)

    with pytest.raises(AgainRefused, match="findings already posted") as refused:
        await harness.service.ask_again(
            "eerovil/agentdeck", 9, closing(url.format(n=7, c=303)), no_verdict=True
        )
    assert url.format(n=7, c=303) in str(refused.value)
    assert harness.kinds(pull, NEW)[-1] == review_history.FINDINGS
    # Dated when read, after the asks it answers, so it stands and may be shipped past.
    assert review_history.shippable(harness.reviews_of(pull), NEW) is not None
    assert harness.scheduled[-1].id == "work-event:recovered-findings:303"
    assert "1. **bug (95%)**" in harness.scheduled[-1].text
    assert harness.verdicts[-1] == ("chat-a", [pull])
    assert len(harness.asks) == 0
    # Asked again, the head has its verdict: nothing is read or sent twice.
    told = len(harness.scheduled)
    with pytest.raises(AgainRefused, match="has its verdict"):
        await harness.service.ask_again(
            "eerovil/agentdeck", 9, closing(url.format(n=7, c=304)), no_verdict=True
        )
    assert len(harness.scheduled) == told and reads[-1] == ("eerovil/agentdeck", 303)

    # A later head whose reviews keep closing with no verdict and no findings to read back:
    # once the asks are spent and the chat has ended, the head goes to the owner, once.
    third = "b" * 40
    harness.heads[("eerovil/agentdeck", 9)] = third
    harness.asked(pull, third)
    harness.now += 600
    harness.asked(pull, third, again="nv3", no_verdict=True)
    harness.now += 600
    harness.asked(pull, third, again="nv4", no_verdict=True)
    harness.now += 600
    with pytest.raises(AgainRefused, match="handed this head to the owner"):
        await harness.service.ask_again("eerovil/agentdeck", 9, "No change.", no_verdict=True)
    assert harness.kinds(pull, third)[-1] == review_history.ESCALATED
    assert review_history.shippable(harness.reviews_of(pull), third) is not None
    assert len(harness.asks) == 0
    with pytest.raises(AgainRefused, match="has its verdict"):
        await harness.service.ask_again("eerovil/agentdeck", 9, "Reviewed.", no_verdict=True)
    assert harness.kinds(pull, third).count(review_history.ESCALATED) == 1


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
    harness.asked(pull, OLD)
    harness.heads[("eerovil/agentdeck", 9)] = OLD

    def stamp(ago: float) -> str:
        return datetime.fromtimestamp(harness.now - ago, UTC).isoformat()

    async def press(delivery: str) -> None:
        pull_event(root, delivery, action="ready_for_review", sha=OLD)
        await harness.drain()

    rows[OLD] = ChatRow(50, "Review eerovil/agentdeck#7", labels=("in-progress",))
    await press("r1")
    # The deck registered the wait itself, so the chat is not woken to read so (#4253).
    assert harness.scheduled == [] and waits == [("chat-a", pull, False)]
    # Closed a minute ago: its verdict may still be on its way, so it is waited on too.
    rows[OLD] = ChatRow(50, "t", state="closed", closed_at=stamp(60), updated_at=stamp(60))
    await press("r2")
    assert harness.scheduled == [] and harness.asks == [] and len(waits) == 2
    # Closed with no verdict long enough ago: asked again, and the new review is waited on.
    rows[OLD] = dataclasses.replace(rows[OLD], closed_at=stamp(600), updated_at=stamp(600))
    await press("r3")
    assert harness.asks == [{"note": "marked ready after the review of this head ended with no"
                             " verdict", "again": True}]
    assert "asked for a fresh review" in harness.scheduled[-1].text
    assert waits[-1] == ("chat-a", pull, True)
    # One already asked for and not started yet is the review this press waits on.
    harness.service.pending.put(f"review:{pull}@{OLD}+nvx", {}, harness.now)
    told = len(harness.scheduled)
    await press("r4")
    assert len(harness.scheduled) == told and len(harness.asks) == 1
    # A wait the deck could not register leaves the chat told where its review is.
    harness.service._wait_review = None
    await press("r4b")
    text = harness.scheduled[-1].text
    assert "is already running, and its verdict will come" in text and len(harness.asks) == 1
    harness.service._wait_review = lambda chat_id, ref, asked: waits.append((chat_id, ref, asked))
    harness.service.pending.discard(f"review:{pull}@{OLD}+nvx")
    await press("r5")  # the second place for a no-verdict ask
    await press("r6")  # past the cap, a person decides
    assert len(harness.asks) == 2 and "after a review with no verdict" in harness.scheduled[-1].text
    # A verdict with findings: fix them, nothing is asked and nothing waits.
    harness.answered(pull, OLD)
    count = len(waits)
    await press("r7")
    assert f"has findings: fix them. {SIBLING_SWEEP_ASK} Then push" in harness.scheduled[-1].text
    assert len(harness.asks) == 2 and len(waits) == count
    # #4658: a review asked for after that verdict is awaited; nothing says "has findings".
    harness.now += 1
    harness.asked(pull, OLD, again="own")
    rows[OLD] = ChatRow(51, "Review eerovil/agentdeck#7", labels=("in-progress",))
    told = len(harness.scheduled)
    await press("r8")
    assert len(harness.scheduled) == told and len(waits) == count + 1


async def test_a_ready_event_at_a_reviewed_head_reviews_the_live_head(root, harness):
    harness.asked("eerovil/agentdeck#9", OLD)
    harness.heads[("eerovil/agentdeck", 9)] = NEW  # the push landed before the read
    pull_event(root, "d1", action="ready_for_review", sha=OLD)
    await harness.drain()
    assert harness.reviews == [("eerovil/agentdeck#9", "eerovil/agentdeck#7")]
    assert harness.service._spent("eerovil/agentdeck#9", NEW)


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


def merged_pull(root, delivery: str, *, merged=True, number=9, action="closed", **kw):
    pull = {"number": number, "state": "closed", "merged": merged}
    if "sha" in kw:
        pull["head"] = {"sha": kw.pop("sha")}
    if "merge_sha" in kw:
        pull["merge_commit_sha"] = kw.pop("merge_sha")
    if "base" in kw:
        pull["base"] = {"ref": kw.pop("base")}
    deliver(
        root,
        event="pull_request",
        delivery=delivery,
        action=action,
        sender=PERSON,
        pull_request=pull,
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
        chat = "Eerovil/Review-Chats#5"
        return answers.pop(SHA, SimpleNamespace(head_sha=SHA, heads=((pull, SHA),), chat=chat))

    harness.service._start_review = held
    pull_event(root, "d1", action="synchronize")
    await harness.drain()
    assert not harness.service._spent("eerovil/agentdeck#9", SHA)
    pull_event(root, "d2", action="synchronize")  # the next event, once the chat is let go
    await harness.drain()
    assert len(harness.reviews) == 2
    assert harness.service._spent("eerovil/agentdeck#9", SHA)
    # #4665: the ask names the chat it got, so a replaced chat's pass of a sibling still counts.
    [ask] = [event for event in harness.history if event.kind == review_history.ASKED]
    assert ask.data["chat"] == "eerovil/review-chats#5"
    # Across the services socket too: the outcome carries its chat.
    from agentdeck import work_services

    plain = SimpleNamespace(heads=(("a/b#1", SHA),), summary="", chat="x/y#5")
    assert work_services.outcome_from(work_services.plain_outcome(plain)).chat == "x/y#5"


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
    assert harness.service._spent("eerovil/agentdeck#9", SHA)
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




async def test_diff_sizes_read_each_head_against_the_base_branch(monkeypatch):
    """#4220: a head's size is its compare against the pull request's base branch; a head
    GitHub cannot list whole (300 files) or a failed read is left out."""
    from agentdeck import review_refs

    reads: list[list[str]] = []

    async def run(argv, timeout=None, **kw):
        reads.append(argv)
        if argv[1:3] == ["pr", "view"]:
            return 0, b'{"baseRefName": "master"}'
        path = argv[2]
        if path.endswith("...aaa"):
            return 0, b'{"files": 3, "lines": 1200}'
        if path.endswith("...bbb"):
            return 0, b'{"files": 300, "lines": 9000}'
        if path.endswith("...ccc"):
            return 0, b'{"files": 0, "lines": null}'
        return 1, b""

    monkeypatch.setattr(review_refs.gh_cli, "run", run)
    sizes = await review_refs.diff_sizes("o/r", 9, ("aaa", "bbb", "ccc", "ddd", "aaa"))
    assert sizes == {"aaa": 1200, "ccc": 0}
    assert reads[1][:3] == ["gh", "api", "repos/o/r/compare/master...aaa"]
    assert len(reads) == 5, "each head is read once"


class _DesignDeck:
    """A design round over a fake reviewer with ``places`` free places (#4342)."""

    def __init__(self, tmp_path) -> None:
        from agentdeck import design_review
        from agentdeck.agent_review import Started
        from agentdeck.review_refs import ReviewBusy

        self.places = 0
        self.started: list[str] = []
        self.comments: dict[str, list | None] = {}

        async def start(issue, url, *, checkout=""):
            if url in self.started:
                return Started((), f"a reviewer already reviews the design {url}")
            if self.places < 1:
                raise ReviewBusy("2 agent reviews already run (the cap)")
            self.places -= 1
            self.started.append(url)
            return Started((), f"a Claude reviewer started on the design {url}")

        async def read(issue):
            return self.comments.get(issue)

        self.verdicts = design_review.Verdicts(tmp_path / "design-verdicts.json")
        self.closed = False

        async def closed(issue):
            return self.closed

        self.round = design_review.DesignRound(start, self.verdicts, read, closed=closed)

    def design(self, issue: str, number: int) -> None:
        url = f"https://github.com/{issue.replace('#', '/issues/')}#issuecomment-{number}"
        self.comments[issue] = [{"body": "**Design:**\n- one lock decides", "url": url}]


async def test_a_design_review_waits_for_a_reviewer_place(root, harness, tmp_path):
    """#4342: with every place taken the request is kept, like a code review that found every
    place taken; a repeat adds nothing, a restart keeps it, and it starts once a place frees."""
    from agentdeck import work_router as wr

    deck = _DesignDeck(tmp_path)
    issue, key = "eerovil/agentdeck#7", "design:eerovil/agentdeck#7"
    deck.design(issue, 70)
    harness.service._design = deck.round
    first = await harness.service.design_review(issue, "/w/7")
    assert first["queued"] and "time wait" not in first["next"]
    # The code review's gate now says to wait for it, not to ask again (#4781), and the
    # release's local read sees it waiting.
    from agentdeck.design_review import WAITING_GAP

    assert await harness.service._design_gap(("chat", issue)) == WAITING_GAP
    ledger = tmp_path / "ledger.json"
    assert wr.design_asked(root, ledger, issue, harness.now)
    entry = dict(harness.service.pending.entries[key])
    assert entry["checkout"] == "/w/7" and entry["next_at"] == harness.now + wr.RETRY_FIRST_S
    harness.now += 5
    assert (await harness.service.design_review(issue, "/w/7"))["queued"]
    assert harness.service.pending.entries[key] == entry
    assert wr.design_queue(wr.pending_path(root), issue)["next_try"]
    # Tried while every place is taken: the same growing wait as a code review.
    harness.now = entry["next_at"]
    await harness.service.retry_pending()
    assert harness.service.pending.entries[key]["delay"] == 2 * wr.RETRY_FIRST_S
    assert deck.started == []
    # A restart keeps it; a place frees, and its reviewer starts by itself.
    restarted = Harness(root, harness.history)
    restarted.issues = harness.issues
    restarted.now = restarted.service.pending.entries[key]["next_at"]
    restarted.service._design = deck.round
    deck.places = 1
    await restarted.service.retry_pending()
    assert deck.started == ["https://github.com/eerovil/agentdeck/issues/7#issuecomment-70"]
    assert key not in restarted.service.pending.entries
    assert wr.design_queue(wr.pending_path(root), issue) is None
    # Asked again once it started: answered, not kept again.
    answer = await restarted.service.design_review(issue)
    assert "already reviews" in answer["started"] and "queued" not in answer
    assert restarted.service.pending.entries == {} and restarted.scheduled == []


async def test_the_owners_answer_at_the_design_series_limit(harness, tmp_path):
    """#4765: past three series the router starts no review; the owner's "skip" is recorded
    and starts none, "more" starts it, and a bad answer is refused."""
    from agentdeck import design_review

    deck = _DesignDeck(tmp_path)
    issue = "eerovil/agentdeck#7"
    for number in ("10", "20", "30"):
        deck.verdicts.record(issue, number, design_review.PASS)
    deck.design(issue, 70)
    deck.places = 2
    service = harness.service
    service._design = deck.round
    stopped = await service.design_review(issue, "/w/7")
    assert stopped["status"] == 409 and "question box" in stopped["refused"]
    assert (await service.design_review(issue, owner="maybe"))["status"] == 400
    assert service.pending.entries == {} and deck.started == []
    more = await service.design_review(issue, "/w/7", owner=design_review.MORE)
    assert "started" in more and len(deck.started) == 1
    deck.verdicts.record(issue, "70", design_review.PASS)
    deck.design(issue, 80)
    skipped = await service.design_review(issue, owner=design_review.SKIP)
    assert skipped["recorded"] == design_review.OWNER and len(deck.started) == 1
    assert deck.verdicts.of(issue)["80"] == design_review.OWNER


async def test_a_waiting_design_review_is_dropped_only_for_a_reason(root, harness, tmp_path):
    """#4342: a passing fault keeps it; a verdict, no design, a Work gone, reviews off or a
    day of waiting drops it, as a code review's wait is dropped."""
    from agentdeck import design_review
    from agentdeck import work_router as wr

    deck = _DesignDeck(tmp_path)
    issue, key = "eerovil/agentdeck#7", "design:eerovil/agentdeck#7"
    harness.service._design = deck.round
    service = harness.service

    async def wait_then_try(change) -> None:
        deck.design(issue, 70)
        deck.places = 0
        await service.design_review(issue)
        assert key in service.pending.entries
        harness.now = service.pending.entries[key]["next_at"]
        change()
        deck.places = 1
        await service.retry_pending()

    def unread():
        deck.comments[issue] = None

    await wait_then_try(unread)
    assert service.pending.entries[key]["delay"] == 2 * wr.RETRY_FIRST_S and deck.started == []
    service.pending.discard(key)
    for change in (
        lambda: deck.verdicts.record(issue, "70", design_review.PASS),
        lambda: setattr(deck, "closed", True),  # #4675
        lambda: deck.comments.__setitem__(issue, []),
        lambda: harness.issues.pop(("eerovil/agentdeck", 7)),
    ):
        await wait_then_try(change)
        assert key not in service.pending.entries and deck.started == []
        harness.issues[("eerovil/agentdeck", 7)] = ("chat-a", issue)
        deck.closed = False
        deck.verdicts = design_review.Verdicts(tmp_path / f"v{harness.now}.json")
        deck.round.verdicts = deck.verdicts
    # Asked again after the close while it waits: refused, and no longer waiting (#4675).
    deck.design(issue, 70)
    deck.places, deck.closed = 0, False
    assert (await service.design_review(issue))["queued"] and key in service.pending.entries
    deck.closed = True
    assert (await service.design_review(issue))["status"] == 409
    assert key not in service.pending.entries
    deck.closed = False
    await wait_then_try(lambda: setattr(harness, "now", harness.now + wr.PENDING_MAX_AGE_S))
    assert key not in service.pending.entries and deck.started == []
    deck.design(issue, 70)
    await service.design_review(issue)
    service._design = None
    await service.settle_without_reviewer()
    assert key not in service.pending.entries and harness.scheduled == []


# --- the design review by ChatGPT (#4722) ---

BOT = {"login": "review-bot[bot]", "type": "Bot"}


class _DesignChats:
    """ChatGPT as the design reviewer: the chats opened and closed, and what was told."""

    def __init__(self, harness, tmp_path) -> None:
        from agentdeck import design_review

        self.deck = _DesignDeck(tmp_path)
        self.deck.design(ISSUE7, 70)
        self.wanted, self.pressed, self.fails = True, True, False
        self.opened: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.told: dict[str, dict] = {}
        self.hears = True
        self.gate = None  # an event the open waits for, to hold a chat mid-open
        chats = self

        async def open_chat(title, body):
            if chats.gate is not None:
                await chats.gate.wait()
            if chats.fails:
                raise RuntimeError("gh is down")
            chats.opened.append((title, body))
            return f"eerovil/chat#{len(chats.opened)}", chats.pressed

        async def close_chat(chat, comment):
            chats.closed.append(chat)
            return True

        class Release:
            async def on_design_review(self, work_id, key, text, url, **how):
                if chats.hears:
                    chats.told.setdefault(key, {"text": text, "url": url, **how})
                return chats.hears

        harness.service._design = self.deck.round
        harness.service._design_chat = design_review.ChatRoute(
            lambda: chats.wanted, open_chat, close_chat
        )
        harness.service._release = Release()


ISSUE7, KEY7 = "eerovil/agentdeck#7", "design:eerovil/agentdeck#7"


async def test_chatgpt_reviews_a_design_and_its_verdict_reaches_the_work_once(
    root, harness, tmp_path
):
    """#4722: one chat per design comment, however often it is asked; the verdict ChatGPT
    posts on the issue is recorded, told once and never forwarded as a comment; round 2
    names round 1's design, and its gap is notes."""
    from agentdeck import design_review
    from agentdeck import work_router as wr

    chats = _DesignChats(harness, tmp_path)
    service = harness.service
    first = await service.design_review(ISSUE7, "/w/7")
    assert "ChatGPT" in first["started"] and "queued" not in first
    [(title, body)] = chats.opened
    assert title == "Design review of eerovil/agentdeck#7 (70)" and "issuecomment-70" in body
    entry = dict(service.pending.entries[KEY7])
    assert (entry["comment"], entry["chat"]) == ("70", "eerovil/chat#1")
    assert entry["next_at"] == harness.now + design_review.CHAT_LIMIT_S
    assert wr.design_queue(wr.pending_path(root), ISSUE7)["chat"] == "eerovil/chat#1"
    # Asked again, and tried by the retry before the limit: nothing more is opened or asked.
    assert (await service.design_review(ISSUE7))["started"] == first["started"]
    service.pending.hurry([KEY7], harness.now)
    await service.retry_pending()
    assert len(chats.opened) == 1 and chats.deck.started == [] and chats.closed == []
    assert service.pending.entries[KEY7]["next_at"] == entry["next_at"]
    comment(root, "d1", sender=BOT, body=f"Design review of {ISSUE7}: gap 70\n1. two asks race")
    await harness.drain()
    assert chats.deck.verdicts.of(ISSUE7) == {"70": "gap"}
    assert list(chats.told) == ["comment:100"] and chats.told["comment:100"]["gap"]
    assert "two asks race" in chats.told["comment:100"]["text"]
    assert chats.closed == ["eerovil/chat#1"] and KEY7 not in service.pending.entries
    assert harness.scheduled == []  # not forwarded as somebody's comment
    # The fixed design: the last round, which names the first; its gap is recorded as notes.
    chats.deck.design(ISSUE7, 71)
    await service.design_review(ISSUE7)
    again = chats.opened[1][1]
    assert "This is the last round" in again and "issuecomment-70" in again
    late = f"Design review of {ISSUE7}: gap 71\nstill"
    comment(root, "d2", sender=BOT, body=late, comment_id=101)
    await harness.drain()
    assert chats.deck.verdicts.of(ISSUE7)["71"] == "notes"
    told = chats.told["comment:101"]
    assert told["notes"] and not told["gap"]
    assert told["text"].startswith(f"Design review of {ISSUE7}: notes 71")


@pytest.mark.parametrize(
    ("body", "how", "taken"),
    [
        (f"Design review of {ISSUE7}: pass 70", {"sender": BOT}, True),
        # ChatGPT's own GitHub connector speaks under the login that owns the chat (#3216).
        (f"Design review of {ISSUE7}: pass 70", {"via": "chatgpt-codex-connector"}, True),
        (
            f"Design review of {ISSUE7}: pass 70",
            {"via": "chatgpt-codex-connector", "sender": {"login": "mallory", "type": "User"}},
            False,
        ),
        (f"Design review of {ISSUE7}: pass 70", {}, False),  # a person, no connector
        (f"Design review of {ISSUE7}: pass 69", {"sender": BOT}, False),  # another design
        ("Design review of eerovil/agentdeck#8: pass 70", {"sender": BOT}, False),
    ],
)
async def test_only_the_asked_chats_reviewer_gives_a_design_verdict(
    root, harness, tmp_path, body, how, taken
):
    chats = _DesignChats(harness, tmp_path)
    await harness.service.design_review(ISSUE7)
    harness.service.pending.put(KEY7, {"chat": "eerovil/chat#1"}, harness.now)
    comment(root, "d1", body=body, **how)
    await harness.drain()
    assert bool(chats.deck.verdicts.of(ISSUE7)) is taken and bool(chats.told) is taken
    assert (KEY7 in harness.service.pending.entries) is not taken
    assert harness.scheduled == []


@pytest.mark.parametrize(
    ("pressed", "fails", "wanted", "wait"),
    [
        (True, False, True, 20 * 60.0),  # no verdict in time
        # No worker was started now: the start is owed to the run in flight, so the chat
        # still gets the whole limit.
        (False, False, True, 20 * 60.0),
        (True, True, True, 0.0),  # the chat could not be opened
        (True, False, False, 0.0),  # ChatGPT is not the reviewer now (the timed switch)
    ],
)
async def test_the_decks_reviewer_takes_a_design_chatgpt_does_not_answer(
    root, harness, tmp_path, pressed, fails, wanted, wait
):
    """#4722: at the limit, counted from the chat's opening, the entry stops naming the chat,
    the chat is closed and the deck's own reviewer starts; a verdict after that is dropped."""
    chats = _DesignChats(harness, tmp_path)
    chats.pressed, chats.fails, chats.wanted = pressed, fails, wanted
    chats.deck.places = 1
    service = harness.service
    url = "https://github.com/eerovil/agentdeck/issues/7#issuecomment-70"
    answer = await service.design_review(ISSUE7)
    if not wait:
        assert chats.deck.started == [url] and "reviewer started" in answer["started"]
        assert chats.opened == [] and KEY7 not in service.pending.entries
        return
    assert chats.deck.started == []
    harness.now += wait - 1
    service.pending.hurry([KEY7], harness.now)
    await service.retry_pending()
    assert chats.deck.started == [] and chats.closed == []
    harness.now += 1
    assert service.pending.entries[KEY7]["next_at"] == harness.now
    chats.wanted = True  # a fallen-back request is never handed to ChatGPT again
    chats.deck.places = 0
    await service.retry_pending()
    assert chats.closed == ["eerovil/chat#1"] and len(chats.opened) == 1
    assert not service.pending.entries[KEY7]["comment"]
    assert (await service.design_review(ISSUE7))["queued"] and len(chats.opened) == 1
    comment(root, "d1", sender=BOT, body=f"Design review of {ISSUE7}: pass 70")
    await harness.drain()
    assert chats.deck.verdicts.of(ISSUE7) == {} and chats.told == {}
    chats.deck.places = 1
    harness.now = service.pending.entries[KEY7]["next_at"]
    await service.retry_pending()
    assert chats.deck.started == [url] and KEY7 not in service.pending.entries


async def test_the_series_limit_note_follows_design_comments(root, harness, tmp_path):
    """#4800 review, the owner's narrow fix: the series-limit note is read with no GitHub
    call, so each design comment created, edited or deleted sets it by the local limit rule
    as it arrives: a withdrawal or delete of the stopped design clears it, a newer design is
    noted only while the limit holds for it; an edit of the stopped design itself, an older
    comment or another issue's leaves it."""
    from agentdeck import design_review as dr

    deck = _DesignDeck(tmp_path)
    harness.service._design = deck.round
    withdrawn = "**Design (withdrawn, the owner chose a narrow fix):** old plan"

    def edited(delivery, comment_id, body, number=7):
        deliver(
            root,
            event="issue_comment",
            delivery=delivery,
            action="edited",
            sender=PERSON,
            issue={"number": number},
            comment={
                "body": body,
                "html_url": f"https://github.com/c/{delivery}",
                "id": comment_id,
            },
        )

    deck.verdicts.mark_limited(ISSUE7, "70")
    edited("e1", 70, "**Design:**\nthe stopped design, reworded")
    edited("e2", 60, withdrawn)  # an older design
    edited("e3", 90, withdrawn, number=8)  # another issue
    await harness.drain()
    assert deck.verdicts.limited(ISSUE7) == {"70"}
    edited("e4", 70, withdrawn)
    await harness.drain()
    assert deck.verdicts.limited(ISSUE7) == set()
    # It records no verdict, so the comment edited back to a live design is gated again.
    assert deck.verdicts.of(ISSUE7) == {}
    deck.verdicts.record(ISSUE7, "30", dr.PASS)
    deck.comments[ISSUE7] = [
        {
            "body": "**Design:**\nrestored",
            "url": "https://github.com/eerovil/agentdeck/issues/7#issuecomment-70",
        }
    ]
    assert "no design review after it" in await deck.round.missing(ISSUE7)
    deck.verdicts.mark_limited(ISSUE7, "71")
    comment(root, "c1", body="a reply, no design", comment_id=72)
    await harness.drain()
    assert deck.verdicts.limited(ISSUE7) == {"71"}
    # Two series ended so far: a newer design is under the limit, so the note goes.
    deck.verdicts.record(ISSUE7, "20", dr.PASS)
    comment(root, "c2", body="**Design:**\na new plan", comment_id=80)
    await harness.drain()
    assert deck.verdicts.limited(ISSUE7) == set()
    # Three ended: a newer design is at the limit and is noted, created or edited.
    deck.verdicts.record(ISSUE7, "10", dr.PASS)
    comment(root, "c3", body="**Design:**\na fourth plan", comment_id=81)
    await harness.drain()
    assert deck.verdicts.limited(ISSUE7) == {"81"}
    edited("e5", 82, "**Design:**\nan edited-in plan")
    await harness.drain()
    assert deck.verdicts.limited(ISSUE7) == {"82"}
    # What the release and the turn-end nudge read follows each event (round 3).
    from agentdeck.work_router import design_limited

    def deleted(delivery, comment_id, body):
        deliver(
            root,
            event="issue_comment",
            delivery=delivery,
            action="deleted",
            sender=PERSON,
            issue={"number": 7},
            comment={"body": body, "html_url": "", "id": comment_id},
        )

    deleted("d0", 90, "a newer reply, no design")
    await harness.drain()
    assert design_limited(tmp_path / "ledger.json", ISSUE7)
    edited("e6", 82, "on second thought, no plan here")  # the noted design loses its mark
    await harness.drain()
    assert not design_limited(tmp_path / "ledger.json", ISSUE7)
    deck.verdicts.mark_limited(ISSUE7, "82")
    # Deleting an older comment leaves the note; deleting the stopped design clears it.
    for delivery, comment_id in (("d1", 81), ("d2", 82)):
        deleted(delivery, comment_id, "**Design:**\ngone")
        await harness.drain()
        assert deck.verdicts.limited(ISSUE7) == ({"82"} if delivery == "d1" else set())
    # A newer design the owner allowed one more series for is not at the limit.
    deck.verdicts.mark_limited(ISSUE7, "82")
    deck.verdicts.allow(ISSUE7, "84")
    comment(root, "c4", body="**Design:**\nthe allowed plan", comment_id=84)
    await harness.drain()
    assert deck.verdicts.limited(ISSUE7) == set()
    assert set(deck.verdicts.of(ISSUE7)) == {"10", "20", "30"}


async def test_two_asks_at_once_open_one_design_chat(root, harness, tmp_path):
    """#4722: the entry is reserved before the chat opens. A second ask and a retry try that
    run while it opens open nothing and start no fallback; a chat that opens after the entry
    went elsewhere is closed at once, and never named."""
    chats = _DesignChats(harness, tmp_path)
    service = harness.service
    chats.gate = asyncio.Event()
    first = asyncio.create_task(service.design_review(ISSUE7))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert service.pending.entries[KEY7]["comment"] == "70" and chats.opened == []
    second = await service.design_review(ISSUE7)
    assert "ChatGPT" in second["started"]
    harness.now += 61  # the reservation's first try, with the open still running
    chats.deck.places = 1
    await service.retry_pending()
    assert chats.deck.started == [] and service.pending.entries[KEY7]["comment"] == "70"
    chats.gate.set()
    await first
    assert len(chats.opened) == 1 and service.pending.entries[KEY7]["chat"] == "eerovil/chat#1"
    # A restart lost the open: the reservation falls back, and the late chat is closed.
    service.pending.discard(KEY7)
    chats.gate = asyncio.Event()
    late = asyncio.create_task(service.design_review(ISSUE7))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    service._design_opening.clear()
    harness.now += 61
    await service.retry_pending()
    assert chats.deck.started and KEY7 not in service.pending.entries
    chats.gate.set()
    await late
    assert chats.closed[-1] == "eerovil/chat#2" and KEY7 not in service.pending.entries


async def test_a_design_chat_ends_with_what_it_was_opened_for(root, harness, tmp_path):
    """#4722 helper checks: a newer design asked once ChatGPT stopped being the reviewer goes
    to the deck's own, and a closed issue ends the chat; both close the chat issue. A verdict
    comment the deck fails on is still not sent on as a person's comment."""
    chats = _DesignChats(harness, tmp_path)
    service = harness.service
    await service.design_review(ISSUE7)
    chats.wanted = False
    chats.deck.design(ISSUE7, 71)
    chats.deck.places = 1
    assert "reviewer started" in (await service.design_review(ISSUE7))["started"]
    assert chats.closed == ["eerovil/chat#1"] and len(chats.opened) == 1
    chats.wanted = True
    chats.deck.design(ISSUE7, 72)
    await service.design_review(ISSUE7)
    assert service.pending.entries[KEY7]["chat"] == "eerovil/chat#2"

    async def broken(key, entry):
        raise RuntimeError("the store is down")

    service._finish_design_verdict = broken
    comment(root, "d1", sender=BOT, body=f"Design review of {ISSUE7}: pass 72")
    await harness.drain()
    assert harness.scheduled == [] and service.pending.entries[KEY7]["verdict_comment"] == "100"
    del service._finish_design_verdict
    service.pending.put(KEY7, {"verdict_comment": "", "verdict": ""}, harness.now)
    chats.deck.closed = True
    assert (await service.design_review(ISSUE7))["status"] == 409
    assert chats.closed[-1] == "eerovil/chat#2" and KEY7 not in service.pending.entries


@pytest.mark.parametrize("then", ["retry", "ask", "newer", "closed", "gone"])
async def test_a_design_verdict_cut_off_before_the_work_heard_is_finished_by_the_retry(
    root, harness, tmp_path, then
):
    """#4722: the entry is kept until the Work was told. A retry, or the same delivery again,
    finishes the telling with the same id, and a recorded verdict is not recorded anew.

    Every later try asks again whether its design is still the newest (the review of
    77b61212d): a newer design, a closed issue or a Work gone drops it untold with its chat,
    and the recorded verdict stays for the round count."""
    chats = _DesignChats(harness, tmp_path)
    service = harness.service
    await service.design_review(ISSUE7)
    chats.hears = False
    comment(root, "d1", sender=BOT, body=f"Design review of {ISSUE7}: gap 70\n1. x")
    tailer = WebhookTailer(root, consumer="work-router")
    batch = list(tailer.deliveries())
    await service.apply(batch)
    entry = service.pending.entries[KEY7]
    assert entry["verdict_comment"] == "100" and chats.told == {} and chats.closed == []
    assert chats.deck.verdicts.of(ISSUE7) == {"70": "gap"}
    # A second verdict comment for the same design changes nothing.
    comment(root, "d2", sender=BOT, body=f"Design review of {ISSUE7}: pass 70", comment_id=101)
    await harness.drain()
    assert service.pending.entries[KEY7]["verdict_comment"] == "100"
    chats.hears = True
    if then == "ask":
        # The agent's next ask, with a newer design: the old verdict is dropped untold under
        # the entry's lock, and the same ask reserves the new design and returns.
        chats.deck.design(ISSUE7, 71)
        answer = await asyncio.wait_for(service.design_review(ISSUE7), 5)
        assert "ChatGPT" in answer["started"] and chats.told == {}
        assert chats.closed == ["eerovil/chat#1"] and len(chats.opened) == 2
        entry = service.pending.entries[KEY7]
        assert (entry["comment"], entry["chat"]) == ("71", "eerovil/chat#2")
        assert not entry.get("verdict_comment")
        assert chats.deck.verdicts.of(ISSUE7) == {"70": "gap"}
        return
    if then != "retry":
        if then == "newer":
            chats.deck.design(ISSUE7, 71)
        elif then == "closed":
            chats.deck.closed = True
        else:
            harness.issues.pop(("eerovil/agentdeck", 7))
        harness.now = service.pending.entries[KEY7]["next_at"]
        await service.retry_pending()
        assert chats.told == {} and chats.closed == ["eerovil/chat#1"]
        assert KEY7 not in service.pending.entries and chats.deck.started == []
        assert chats.deck.verdicts.of(ISSUE7) == {"70": "gap"}
        return
    harness.now = service.pending.entries[KEY7]["next_at"]
    await service.retry_pending()
    assert list(chats.told) == ["comment:100"] and chats.told["comment:100"]["gap"]
    assert chats.closed == ["eerovil/chat#1"] and KEY7 not in service.pending.entries
    await service.apply(batch)  # the delivery again, after all of it
    assert list(chats.told) == ["comment:100"] and chats.deck.verdicts.of(ISSUE7) == {"70": "gap"}


async def test_one_design_has_one_reviewer_across_the_switch(root, harness, tmp_path):
    """#4722 review: a design the deck's own reviewer is already on (asked while the switch
    was on) gets no ChatGPT chat once the switch is off, so ChatGPT's opposite verdict on it
    is dropped; and a chat's verdict never replaces one the deck's reviewer recorded."""
    from agentdeck.design_review import Verdict

    chats = _DesignChats(harness, tmp_path)
    service, deck = harness.service, chats.deck
    chats.wanted, deck.places = False, 1
    assert "reviewer started" in (await service.design_review(ISSUE7))["started"]
    deck.round.reviewing = lambda issue, number: (issue, number) == (ISSUE7, "70")
    chats.wanted = True
    assert "already reviews" in (await service.design_review(ISSUE7))["started"]
    assert chats.opened == [] and KEY7 not in service.pending.entries
    comment(root, "d0", sender=BOT, body=f"Design review of {ISSUE7}: pass 70", comment_id=99)
    await harness.drain()
    assert deck.verdicts.of(ISSUE7) == {} and chats.told == {}
    deck.round._reviews = lambda token, issue, number: True
    assert deck.round.record(ISSUE7, Verdict(ISSUE7, "gap", "70"), "tok") == "gap"
    assert deck.round.take(ISSUE7, Verdict(ISSUE7, "pass", "70")) == "gap"
    # The next design goes to ChatGPT, and its verdict is told once.
    deck.design(ISSUE7, 71)
    await service.design_review(ISSUE7)
    comment(root, "d1", sender=BOT, body=f"Design review of {ISSUE7}: notes 71\n1. x")
    await harness.drain()
    assert deck.verdicts.of(ISSUE7) == {"70": "gap", "71": "notes"} and len(chats.told) == 1
    # A design the deck's reviewer already decided takes no verdict from a chat still open.
    deck.design(ISSUE7, 72)
    await service.design_review(ISSUE7)
    deck.verdicts.record(ISSUE7, "72", "gap")
    comment(root, "d2", sender=BOT, body=f"Design review of {ISSUE7}: pass 72", comment_id=101)
    await harness.drain()
    assert deck.verdicts.of(ISSUE7)["72"] == "gap" and len(chats.told) == 1


@pytest.mark.parametrize("newer", [False, True])
async def test_a_design_verdict_waits_while_the_issue_cannot_be_read(
    root, harness, tmp_path, newer
):
    """#4722 review: a verdict that arrives while the newest design cannot be read is kept,
    neither recorded nor told, and checked again at the retry: taken when its design is
    still the newest, dropped with its chat when the Work posted a newer one meanwhile."""
    chats = _DesignChats(harness, tmp_path)
    service, deck = harness.service, chats.deck
    await service.design_review(ISSUE7)
    deck.comments[ISSUE7] = None
    comment(root, "d1", sender=BOT, body=f"Design review of {ISSUE7}: pass 70")
    await harness.drain()
    for _ in range(2):  # the retry while it is still unread keeps waiting
        assert service.pending.entries[KEY7]["verdict_comment"] == "100"
        assert deck.verdicts.of(ISSUE7) == {} and chats.told == {} and chats.closed == []
        harness.now = service.pending.entries[KEY7]["next_at"]
        await service.retry_pending()
    deck.design(ISSUE7, 71 if newer else 70)
    harness.now = service.pending.entries[KEY7]["next_at"]
    await service.retry_pending()
    assert chats.closed == ["eerovil/chat#1"] and KEY7 not in service.pending.entries
    assert deck.verdicts.of(ISSUE7) == ({} if newer else {"70": "pass"})
    assert list(chats.told) == ([] if newer else ["comment:100"])


@pytest.mark.parametrize("recorded", [False, True])
async def test_a_design_verdict_unread_for_the_whole_limit(root, harness, tmp_path, recorded):
    """#4722 design notes: a verdict whose issue stays unreadable for the whole limit falls
    back to the deck's own reviewer as if ChatGPT gave none. One already recorded for that
    design is never handed over: it waits, and is told once the issue reads again."""
    from agentdeck import design_review

    chats = _DesignChats(harness, tmp_path)
    service, deck = harness.service, chats.deck
    await service.design_review(ISSUE7)
    deck.comments[ISSUE7] = None
    comment(root, "d1", sender=BOT, body=f"Design review of {ISSUE7}: pass 70")
    await harness.drain()
    if recorded:
        deck.verdicts.record(ISSUE7, "70", "pass")
    deck.places = 1
    harness.now += design_review.CHAT_LIMIT_S - 1
    service.pending.hurry([KEY7], harness.now)
    await service.retry_pending()
    assert service.pending.entries[KEY7]["verdict_comment"] == "100" and chats.closed == []
    harness.now += 1
    service.pending.hurry([KEY7], harness.now)
    await service.retry_pending()
    entry = service.pending.entries[KEY7]
    assert chats.told == {} and deck.started == []
    if not recorded:
        assert not entry["comment"] and not entry["verdict_comment"]
        assert set(chats.closed) == {"eerovil/chat#1"} and deck.verdicts.of(ISSUE7) == {}
        deck.design(ISSUE7, 70)
        harness.now = entry["next_at"]
        await service.retry_pending()
        assert len(deck.started) == 1 and KEY7 not in service.pending.entries
        return
    assert entry["verdict_comment"] == "100" and chats.closed == []
    deck.design(ISSUE7, 70)
    harness.now = entry["next_at"]
    await service.retry_pending()
    assert list(chats.told) == ["comment:100"] and chats.closed == ["eerovil/chat#1"]
    assert KEY7 not in service.pending.entries and deck.started == []


# --- lighter review (#4399) ---


class _Light:
    """``light_review.LightReview`` as the router reads it."""

    def __init__(self, light: bool = True, error: str = "") -> None:
        self.light = light
        self.error = error
        self.checked: list[tuple[str, str, str]] = []

    async def checklist(self, pull, head, work_id):
        from agentdeck.light_review import Decision, LightError

        self.checked.append((pull, head, work_id))
        if self.error:
            raise LightError(self.error)
        return Decision(self.light, "outside guarded.json")

    def is_light(self, pull, head):
        return self.light

    def head_moving(self, pull, head):
        return self.light


async def test_pre_review_done_on_a_light_head_starts_no_review(root, harness):
    from agentdeck.work_router import LIGHT_NEXT_TEXT, AgainRefused

    light = _Light()
    harness.service._light = light
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    answer = await harness.service.pre_review_done("eerovil/agentdeck", 9)
    assert answer == {
        "pull_request": "eerovil/agentdeck#9",
        "head": NEW,
        "outcome": "light",
        "next": LIGHT_NEXT_TEXT.format(pull="eerovil/agentdeck#9", head=NEW[:12]),
    }
    assert light.checked == [("eerovil/agentdeck#9", NEW, "github_issue:eerovil/agentdeck#7")]
    assert harness.reviews == []
    # A light head's ready event starts no review either.
    assert harness.service._head_moving("eerovil/agentdeck#9", NEW)
    # A checklist that could not decide refuses, with its reason.
    light.error = "the diff could not be read"
    with pytest.raises(AgainRefused, match="the diff could not be read"):
        await harness.service.pre_review_done("eerovil/agentdeck", 9)
    assert harness.reviews == []
    # A full decision goes to review as before.
    light.error, light.light = "", False
    assert (await harness.service.pre_review_done("eerovil/agentdeck", 9))["outcome"] == "asked"
    assert len(harness.reviews) == 1


async def test_a_light_pass_overtaken_by_the_review_point_gets_its_full_review(
    root, harness, tmp_path
):
    """Two light pull requests with a pass and a limit of one, through the real light
    service: once the other merged light, this one's pre-review-done opens a full review."""
    from test_light_review import Harness

    from agentdeck import light_review as lr

    light = Harness(tmp_path / "light", limit=1)
    light.service.ports.policy = lambda repo: (
        ("guarded.json", 1, "~/robot") if repo == "eerovil/agentdeck" else None
    )
    light.db.put_light_review(
        "eerovil/agentdeck#9",
        "eerovil/agentdeck",
        {"heads": {NEW: {"seen_at": 1.0, "decision": lr.LIGHT, "verdict": lr.PASS}}},
    )
    light.db.put_light_review(
        "eerovil/agentdeck#8",
        "eerovil/agentdeck",
        {"heads": {OLD: {"seen_at": 1.0, "decision": lr.LIGHT, "verdict": lr.PASS}}},
        merged=True,
    )
    harness.service._light = light.service
    harness.heads[("eerovil/agentdeck", 9)] = NEW
    answer = await harness.service.pre_review_done("eerovil/agentdeck", 9)
    assert answer["outcome"] == "asked"
    assert len(harness.reviews) == 1
    record = light.db.light_review_row("eerovil/agentdeck#9")
    assert record["heads"][NEW]["decision"] == lr.LIGHT and "review point" in record["past_limit"]


async def test_ask_again_on_a_light_head_still_starts_its_review(root, harness):
    """Rule 2: the agent's own ask is a review; its ``asked`` event makes the pull request
    full for good, so a light head never skips it."""
    light = _Light(light=True)
    harness.service._light = light
    harness.heads[("eerovil/agentdeck", 9)] = OLD
    answer = await harness.service.ask_again("eerovil/agentdeck", 9, "please look")
    assert answer["outcome"] == "asked"
    assert len(harness.reviews) == 1


def test_design_asked_reads_a_review_asked_and_unanswered(tmp_path):
    """#4781: the release and the turn-end nudge read, with no GitHub call, whether a design
    review was asked for and has not answered: a request waiting for a place, a ChatGPT chat
    or a deck reviewer on a comment with no verdict; one that answered, or aged out, is not."""
    import json

    from agentdeck import design_review
    from agentdeck import work_router as wr
    from agentdeck.agent_review import MAX_AGE_S

    issue, root, ledger = "o/r#7", tmp_path / "streams", tmp_path / "ledger.json"
    root.mkdir()
    assert not wr.design_asked(root, ledger, issue, 1000.0)  # nothing on disk
    line = {"pull": "design:o/r#7", "head": "comment-5", "started": 1000.0}
    other = {"pull": "design:o/r#8", "head": "comment-9", "started": 1000.0}
    ledger.write_text(json.dumps([line, other]))
    assert wr.design_asked(root, ledger, "O/R#7", 1000.0)
    assert not wr.design_asked(root, ledger, issue, 1000.0 + MAX_AGE_S + 1)
    verdicts = design_review.Verdicts.beside(ledger)
    verdicts.record(issue, "5", design_review.PASS)
    assert not wr.design_asked(root, ledger, issue, 1000.0), "it answered"
    pending = wr.PendingActions(wr.pending_path(root))
    pending.put("design:o/r#7", {"kind": "design", "issue": issue}, 1000.0)
    assert wr.design_asked(root, ledger, issue, 1000.0), "it waits for a place"
    pending.put("design:o/r#7", {"comment": "5", "chat": "c"}, 1000.0)
    assert not wr.design_asked(root, ledger, issue, 1000.0), "ChatGPT's answer is recorded"
    pending.put("design:o/r#7", {"comment": "6", "chat": "c"}, 1000.0)
    assert wr.design_asked(root, ledger, issue, 1000.0)
    # #4800: the series limit is read the same way, until the owner answers.
    assert not wr.design_limited(ledger, issue)
    verdicts.mark_limited(issue, "8")
    assert wr.design_limited(ledger, "O/R#7") and not wr.design_limited(ledger, "o/r#8")
    verdicts.allow(issue, "8")
    assert not wr.design_limited(ledger, issue), "one more series"
