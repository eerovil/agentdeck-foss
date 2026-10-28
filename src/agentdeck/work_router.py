"""A Work hears its own GitHub events (#2895, ADR 0008).

Before this, every GitHub event reached a Work through its Poller: the engine read the
board on a 30-second timer, decided a status, and handed the worker an instruction — or
refused, or waited. This consumer of the raw webhook streams sends the event straight to
the chat that owns the Work instead, for every repository a poller manifest declares
(every project runs its Works this way since #3732).

What it acts on, and nothing else (the filter is the point: about 99% of agentdeck's
deliveries are the deck's own echo):

* **A person's ``/claude`` comment** on the Work's issue, or on a pull request the Work
  registered. Once passed on, it gets a 👀 reaction (``acknowledge``, #3109), as the Poller
  marks a command, and the message asks the agent to answer it there, so the answer is seen
  on GitHub and not only in the chat (#3118). A person's other comments are not sent: CI
  posting under a person's token ("Starting deployment to prod…") reached the chat as an ask.
* **A person's pull request review** on a registered pull request.
* **A person removing the spec-only label** from the Work's issue (#3134): the approval of the
  spec its agent was started to write, so the agent now builds it.
* **The Work's issue closed by someone else** (#3188), while the Work is not finished: who
  closed it and their closing comment, so the chat stops or asks instead of building on. Every
  chat writes as the deck's own login, so a close under that login is the Work's own chat's
  when the chat is mid-turn as it lands; a person's close is always sent. A close is held for
  ``CLOSE_HOLD_S`` first: a merge of the Work's own pull request closes its issue too, and its
  delivery may come either side of the close's.
* **A wayfinder map's last ticket closed** (#3627): a sub-issue's close, under any login, whose
  parent, read once from GitHub, is an open ``wayfinder:map`` with every sub-issue done. The
  map's own chat is told to reach the destination, so nobody has to wake it.
* **A wayfinder record** (#3695): a ticket's ``**Answer:**`` comment, or an ``**Ideas
  sorted:**`` or ``**Destination redrawn:**`` comment on the map, posted or edited by the deck
  or a person on the repository. Nothing is sent: the deck rebuilds the map's four lists from
  every record the stored streams hold (``wayfinder_lists``) and saves the map body, with one
  read of the map and one write, and no write when nothing changed.
* **The reviewer's verdict** — a ``/reviewed`` or ``/claude`` comment from the reviewer's
  account, or the deck's own reviewer's findings (``/claude Review ...``) or
  ``/reviewed`` posted under the deck's account (#2943).
* **A reviewer's escalation** (#3216) — a comment opening "Review loop escalated …" from the
  reviewer, the deck, or a person holding the Work tier (ChatGPT posts it under the owner's
  login). It ends the chat's review waits and asks the agent to put the reviewer's question
  in its question box.
* **A registered pull request that is ready for review at a head nobody reviewed yet** —
  marked ready, pushed while ready, or opened ready, or registered by its Work's chat while
  it already sits ready (the event may have come before the registration). That starts the
  reviewer through the same path ``agentdeck chat-review`` uses, once per head. A draft is
  never reviewed: the worker marks it ready when its changes are done (``gh pr ready``), and
  turns it back to draft when findings arrive. A new head whose diff has the review digest of
  the last head sent for review only merged its base, and is not reviewed (#3314): the chat is
  told the verdict on that diff stands, and the release keeps it for the new head.
* **A review asked for again at an unchanged head** (#3183) — the agent's
  ``POST /api/sessions/<key>/review-request`` (``ask_again``) with a note saying what changed.
  A person may have decided a finding with no code change, and nothing else could ever start
  the next review. Each asks once more, at most ``MAX_AGAIN`` times per head; past that a
  person decides, and the chat is told so. A pull request turned back to draft and marked
  ready again at a head already reviewed no longer asks (#3344): it carries no reason, and
  every chat it opened read to the reviewer as a plain duplicate. The chat is told what is
  going on instead (#3584): the head passed, its verdict has findings, its review is still
  running (and the chat now waits on it), or its review chat ended with no verdict, which
  asks again as ``no_verdict`` below. A request marked ``no_verdict`` (#3453) asks again because
  the last review ended without a verdict for the head; the agent makes it without asking the
  owner, and it is counted apart, at most ``MAX_NO_VERDICT`` times per head, so a reviewer that
  failed to post does not spend the places a person's decisions need.

The deck's own comments and pushes, bots, CI and deploy results and merges are not sent:
CI and deploys reach a chat only through a wake-on it registered (#2775), and a merge changes
the Work's status, not its agent's turn.

The rules this holds to:

* **At most one message per delivery.** Each message rides the scheduled-message path with
  ``work-event:<delivery id>`` as its id, so a replay after a crash is absorbed by the store.
  A comment the chat's provider would silently drop (``user_message_refusal``) is sent as a
  link to it instead of its text.
* **One review per head, and per request to ask again.** A review start is written down
  under ``<owner/repo#n>@<head>`` once the chat it answered with names that head (or it
  failed in a way that may have written), and is never asked for again. A request to ask
  again is its own key, ``<owner/repo#n>@<head>+<request id>`` (the delivery id of the
  ``ready_for_review``, so a replayed delivery asks nothing more), counted against the cap.
* **Nothing is acknowledged that did not happen.** The delivery offset moves on only once
  every action the batch asked for either happened — the message row exists, the head is
  covered — or is written down as pending in ``work-router-pending.json``. A pending action
  (a store that refused the row; a review that was busy, refused, or answered with a held
  chat still about an older commit) is retried on its own timer with a growing back-off, not
  left for another GitHub event that may never come. A pending review is re-read first and
  follows the pull request's current ready head, or is dropped once it has none. One that
  has not happened within a day is dropped with a warning.
* **A pull request belongs to the Work that registered it.** Its body's ``Closes #n`` is
  text anyone can write and is not read here.
* **Repositories are matched by full ``owner/repo``,** case-insensitively, so the same issue
  number in two repositories never crosses.
* **A missed delivery is caught by the committed offset** (ADR 0007): the next append
  re-reads anything a missed watch event left. A delivery GitHub never sent is caught by
  the wake-on timeout the agent registered for what it waits on.

These are best effort, not absolute (operator decision on #2895, ADR 0008). Accepted:

* **A host crash can lose an action.** The ledgers are written with an atomic replace but
  not fsynced, so a power loss can keep a committed offset and lose the pending entry
  written just before it. A process crash loses nothing.
* **A registration catch-up is not durable.** Registering a pull request through the
  session API checks for an already-ready head in the background; a restart or a failed
  read drops that check, and a Work side chat's own registration does not run it. The next
  event on that pull request, or the worker marking it ready again, asks.
* **A very old head can be reviewed twice.** ``ReviewedHeads`` keeps the newest
  ``MAX_STARTED`` heads, so a pull request left open past that many review starts and
  turned ready again at an unchanged head may be asked about once more.
* **An ask while a review of the head is already asked for keeps no note of its own**
  (owner's decision on #3453). It answers "already asked" and the agent waits on the review
  already coming; if that is the head's first review still starting, the new note is not
  queued. A decision note only follows a verdict, so by then the first review is done.
* **Any verdict on the pull request ends its review waits** (owner's decision on #3453),
  even one from an older review chat landing after a fresh ask. That verdict still reaches
  the agent as its own message, so the wait does not end silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import wayfinder_lists, wayfinder_map
from .poller_engine import github_authority
from .poller_engine.authorization import reviewer_logins
from .review_refs import ReviewBusy, ReviewError
from .scheduled_messages import ScheduledMessage
from .shared_file import SharedFile
from .wake_on import STOP_SETTLE_S
from .watch_retry import Backoff
from .webhook_tailer import Delivery, WebhookTailer, WebhookWatcher
from .work_claims import assignee_of
from .work_release import OWNER_CALL_ASK
from .work_signals import MARKER as START_MARKER

log = logging.getLogger(__name__)

CONSUMER = "work-router"
STARTED_FILENAME = "work-router-reviews.json"
PENDING_FILENAME = "work-router-pending.json"
#: The Works whose issue went to another deck, as ``handed-over:<work id>`` (#3731).
HANDED_OVER_FILENAME = "work-router-handed-over.json"
#: A pending action's back-off (``watch_retry.Backoff``): its first wait and its ceiling.
RETRY_FIRST_S = 60.0
RETRY_MAX_S = 30 * 60.0
#: A pending action that has not happened within this long is dropped with a warning: the
#: person or the worker will have asked again some other way by then.
PENDING_MAX_AGE_S = 24 * 3600.0
#: How many times one unchanged head may be reviewed again on request (#3183); past that, the
#: point is a person's to settle, not another reviewer run's.
MAX_AGAIN = 2
#: How many times one head may be reviewed again because its review ended with no verdict
#: (#3453), counted apart from ``MAX_AGAIN``; past that the owner is asked.
MAX_NO_VERDICT = 2
#: Starts the request id of an ask after no verdict. Request ids are otherwise hex, so this
#: prefix tells the two counts apart in the one ledger.
NO_VERDICT = "nv"
#: Marks an ask's place under the cap before its review started (``_reserve_again``).
RESERVED = "?"

#: How many reviewed heads are remembered. A head older than this many review starts is
#: long since reviewed or abandoned, so forgetting it costs at most one repeat press.
MAX_STARTED = 2000
#: How many pull requests' reviewed diff digests are remembered, and how many heads of each.
MAX_DIGESTS = 500
MAX_HEAD_DIGESTS = 8
#: A message is a nudge with a link, not a copy of the thread: a long comment is cut here.
MAX_BODY = 4000

MESSAGE = "message"
REVIEW = "review"
#: A Work start refused before anything ran (#2971), kept pending as ``start:<work id>``.
START = "start"
#: A Work's issue closed by someone else (#3188), held as ``close:<work id>`` for a merge.
CLOSE = "close"
#: How long a close waits for a merge of the Work's pull request, the usual closer, to show.
CLOSE_HOLD_S = RETRY_FIRST_S
#: A closing comment is quoted when its closer posted it this close to the close, cut here.
CLOSE_COMMENT_S = 60.0
CLOSE_QUOTE = 300

#: ``pull_request`` actions after which the pull request may sit ready at a new head.
_READY_ACTIONS = frozenset({"ready_for_review", "synchronize", "opened", "reopened"})
_VERDICT_RE = re.compile(r"^\s*/(?:reviewed|claude)\b", re.IGNORECASE)
#: A sub-issue's ``parent_issue_url`` (#3627).
_API_ISSUE_RE = re.compile(r"^https://api\.github\.com/repos/([^/\s]+)/([^/\s]+)/issues/(\d+)$")
#: The label of a wayfinder map, whose chat wakes when its last ticket closes (#3627).
MAP_LABEL = "wayfinder:map"
#: ``read_map`` reads one page of a map's sub-issues; a full page may not be all of them.
MAP_PAGE = 100
#: A person's instruction (#3118): only a comment opening with ``/claude`` is passed on.
_CLAUDE_RE = re.compile(r"^\s*/claude\b", re.IGNORECASE)
#: What the deck's own reviewer posts under the deck's account (#2943): its findings, or a
#: verdict. The Claude/Codex fallback reviewer posts these on GitHub itself when no Poller holds
#: the card, so they arrive as the deck's own comment and would otherwise be dropped as echo.
#: Its findings open ``/claude Review of <pr> at <head>`` as often as ``Review findings for PR``
#: (#3004), so any ``/claude Review`` counts.
#: A pull request a verdict's text names: ``owner/repo#12``, or ``#12`` in its own repository.
_REF_RE = re.compile(
    r"(?<![\w/#])(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#(?P<number>\d+)\b"
)
_RELEASE_VERDICT_RE = re.compile(r"^\s*/reviewed\b", re.IGNORECASE)
_DECK_REVIEW_RE = re.compile(r"^\s*/(?:claude\s+Review\b|reviewed\b)", re.IGNORECASE)
#: A reviewer handing the review to the owner instead of passing it or sending it back (#3216),
#: as its first line: "Review loop escalated to the operator". ChatGPT posts it through its own
#: GitHub connector, so under the owner's login rather than the reviewer's.
_ESCALATED_RE = re.compile(
    r"^\s*(?:/claude\s+)?review(?:\s+loop)?\s+(?:was\s+)?escalated\b", re.IGNORECASE
)
#: What the agent does with an escalation, appended to its message (#3216).
ESCALATED_ASK = (
    "The reviewer will not review this commit again by itself, so stop waiting on the review. "
    "Ask the owner in your question box: the reviewer's question, with its options as the "
    "choices. Once they answer, post their decision on the issue and act on it; when it "
    "settles the finding without a code change, ask for a fresh review of the same commit "
    "with your session's `review-request`, quoting the decision as its note."
)


@dataclass(frozen=True)
class Event:
    """What one delivery asks the router to do, before it knows which chat owns it."""

    kind: str  # MESSAGE, REVIEW or CLOSE
    repository: str  # lowercased owner/repo
    number: int
    is_pull: bool
    text: str = ""
    #: MESSAGE only: the same news without the comment's own words, for a chat whose
    #: provider would drop those words before its model saw them.
    link: str = ""
    head: str = ""  # REVIEW only: the head sha to review
    #: REVIEW only: the payload may be stale, so the live pull request decides (#2983).
    confirm: bool = False
    #: MESSAGE from a person: who wrote it and GitHub's ``author_association`` for them. The
    #: router forwards it only when that login holds the project's Work tier (#2895 review).
    author: str = ""
    association: str = ""
    #: MESSAGE from a person's issue comment: its id, for the 👀 that says it was passed on.
    comment_id: int = 0
    #: CLOSE only: closed under one of the deck's own logins, which every chat writes as.
    by_deck: bool = False
    #: MESSAGE only: the reviewer's findings, which end the chat's review waits. A ``/reviewed``
    #: verdict is not one: the release ends them once it has taken the verdict (#3183 review),
    #: so a stale head or a wrong digest leaves the wait to its timeout.
    verdict: bool = False
    #: REVIEW only: marked ready from draft, so an already-reviewed head is asked again (#3183).
    explicit: bool = False
    #: REVIEW only: the id of a request to review this head again, and the agent's reason.
    again: str = ""
    note: str = ""
    #: MESSAGE only: the message's own id, in place of the delivery's, for news several
    #: deliveries can carry and the chat must hear once (#3627).
    message_id: str = ""


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _login(payload: dict, key: str = "sender") -> tuple[str, str]:
    """``(login, type)`` of the payload's ``sender`` (or another user field)."""
    user = payload.get(key)
    if not isinstance(user, dict):
        return "", ""
    return _text(user.get("login")), _text(user.get("type"))


def is_person(login: str, kind: str, own_logins: frozenset[str]) -> bool:
    """A GitHub user who is not a bot and not one of the deck's own accounts."""
    return (
        bool(login)
        and kind == "User"
        and not login.endswith("[bot]")
        and login.casefold() not in own_logins
    )


def _clip(body: str) -> str:
    body = body.strip()
    return body if len(body) <= MAX_BODY else body[:MAX_BODY].rstrip() + " …"


def _link(headline: str, url: str) -> str:
    return (
        f"{headline}. Its text cannot be passed on here, because this chat's CLI would drop "
        f"it before the model saw it; read it on GitHub: {url}"
    ).rstrip()


def configured_own_logins() -> frozenset[str]:
    """The logins this host's GitHub authority config writes as (``agent.login`` and the
    App's bot), so a host whose agent account is not the default still knows its own echo."""
    try:
        config = github_authority.load_accounts()
    except github_authority.ConfigError:
        return frozenset(
            {github_authority.DEFAULT_AGENT_LOGIN.casefold()} - {""}
        )
    logins = {config.agent_login}
    if config.app is not None:
        logins.add(config.app.login)
    return frozenset(login.casefold() for login in logins if login)


def _when(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _api_issue(url: object) -> tuple[str, int] | None:
    """``(owner/repo, number)``, lowercased, from an issue's API URL (``parent_issue_url``)."""
    match = _API_ISSUE_RE.match(url) if isinstance(url, str) else None
    if match is None:
        return None
    return (f"{match.group(1)}/{match.group(2)}".lower(), int(match.group(3)))


def map_ready(
    delivery: Delivery, parent: tuple[str, int], record: object
) -> tuple[Event | None, str]:
    """The message that wakes a wayfinder map's chat, or None, and why, for the log (#3627).

    ``record`` is the map issue as GitHub returns it now, read after a ticket's close: an
    open issue labelled ``wayfinder:map`` whose sub-issues are all closed. ``last_closed``
    is when the last of them closed; it names the message, so every close delivery read
    after the map completed sends the same one, which the store keeps once, while a ticket
    reopened and closed again completes the map anew.

    ``sub_issues_summary`` lags: read within a second of a close it can still count the
    closing ticket open, and minutes later it still left out tickets filed since (#3671). So
    the sub-issues' own states decide whenever they were read, with this close's ticket
    counted closed whatever the list says; the summary decides only without a list."""
    if not isinstance(record, dict):
        return None, "map unread"
    if _text(record.get("state")).lower() != "open":
        return None, "map is not open"
    labels = record.get("labels")
    names = {
        _text(label.get("name") if isinstance(label, dict) else label).strip().casefold()
        for label in (labels if isinstance(labels, list) else ())
    }
    if MAP_LABEL not in names:
        return None, "parent is not a wayfinder map"
    summary = record.get("sub_issues_summary")
    summary = summary if isinstance(summary, dict) else {}
    total, completed = summary.get("total"), summary.get("completed")
    total = total if isinstance(total, int) else 0
    completed = completed if isinstance(completed, int) else 0
    issue = delivery.payload.get("issue")
    issue = issue if isinstance(issue, dict) else {}
    number = issue.get("number")
    last = _text(record.get("last_closed"))
    subs = record.get("sub_issues")
    listed = [sub for sub in subs if isinstance(sub, dict)] if isinstance(subs, list) else None
    if listed:
        stream = delivery.stream.lower()
        still_open = [
            sub.get("number")
            for sub in listed
            if _text(sub.get("state")).lower() != "closed"
            and (sub.get("number") != number or _text(sub.get("repository")).lower() != stream)
        ]
        if still_open:
            return None, f"tickets still open: {still_open}"
        if len(listed) >= MAP_PAGE:
            return None, f"{len(listed)} tickets listed, more may be unread"
        total = len(listed)
        last = max(
            [_text(sub.get("closed_at")) for sub in listed] + [last, _text(issue.get("closed_at"))]
        )
    elif total <= 0:
        return None, "map has no tickets"
    elif completed < total:
        return None, f"summary says {completed} of {total} closed, no ticket list"
    if not last:
        return None, "no close time"
    repository = delivery.stream.lower()
    text = (
        f"AgentDeck event: {repository}#{number} closed, and it was the last open ticket of "
        f"this wayfinder map, {parent[0]}#{parent[1]}: all {total} are closed. Follow the "
        "**Reach the destination** section of `WAYFINDER.md` in your installed `wayfinder` "
        "skill folder now."
    )
    return Event(
        MESSAGE,
        parent[0],
        parent[1],
        False,
        text=text,
        link=text,
        message_id=f"work-event:map-ready:{parent[0]}#{parent[1]}@{last}",
    ), f"all {total} tickets closed"


def classify(
    delivery: Delivery,
    *,
    own_logins: frozenset[str],
    spec_only_label: str = "",
    last_comment: dict | None = None,
) -> Event | None:
    """The one thing this delivery asks a Work's chat to hear, or ``None`` (D4, D8).

    ``spec_only_label`` is the repository's project's spec-only label, or empty.
    ``last_comment`` is the closer's newest comment on a closed issue, read from GitHub,
    quoted when posted just before closing.
    """
    payload = delivery.payload
    repository = delivery.stream.lower()
    action = _text(payload.get("action"))
    login, kind = _login(payload)
    if delivery.event == "issues" and action == "closed":
        issue = payload.get("issue")
        if (
            not login
            or not isinstance(issue, dict)
            or not isinstance(issue.get("number"), int)
            or isinstance(issue.get("pull_request"), dict)
        ):
            return None
        number = issue["number"]
        who = login
        by_deck = login.casefold() in own_logins
        if by_deck:
            who = f"{login} (the deck's own account, so another chat or the deck)"
        text = f"AgentDeck event: {who} closed issue {repository}#{number}"
        closed = _when(_text(issue.get("closed_at")))
        said = last_comment if isinstance(last_comment, dict) else {}
        author, _ = _login(said, "user")
        posted = _when(_text(said.get("created_at")))
        body = _text(said.get("body")).strip()
        if (
            body
            and closed is not None
            and posted is not None
            and author.casefold() == login.casefold()
            and abs((closed - posted).total_seconds()) <= CLOSE_COMMENT_S
        ):
            quote = body if len(body) <= CLOSE_QUOTE else body[:CLOSE_QUOTE].rstrip() + " …"
            text += f", saying:\n\n{quote}\n\n"
        else:
            text += ". "
        text += (
            "If your work on it is no longer wanted, stop and say so; if it still is, ask the "
            "person in the question box before going on."
        )
        url = _text(issue.get("html_url"))
        return Event(
            CLOSE,
            repository,
            number,
            False,
            text=f"{text}\n\n{url}".rstrip(),
            link=_link(f"AgentDeck event: {who} closed issue {repository}#{number}", url)
            + "\n\nIf your work on it is no longer wanted, stop and say so; if it still is, "
            "ask the person in the question box before going on.",
            by_deck=by_deck,
        )
    if delivery.event == "issues" and action == "unlabeled":
        issue = payload.get("issue")
        label = payload.get("label")
        wanted = spec_only_label.strip().casefold()
        if (
            not wanted
            or not isinstance(issue, dict)
            or not isinstance(label, dict)
            or _text(label.get("name")).strip().casefold() != wanted
            or not isinstance(issue.get("number"), int)
            or isinstance(issue.get("pull_request"), dict)
            or _text(issue.get("state")).lower() == "closed"
            or not is_person(login, kind, own_logins)
        ):
            return None
        number = issue["number"]
        text = (
            f"AgentDeck event: {login} removed the `{spec_only_label.strip()}` label from "
            f"issue {repository}#{number}, which approves its spec. Build the approved spec "
            "now — its newest version on the issue, with any decision the person overruled "
            "while approving — as an ordinary Work. If you are already building it, carry on."
        )
        return Event(MESSAGE, repository, number, False, text=text, link=text)
    if delivery.event == "issue_comment" and action == "created":
        issue = payload.get("issue")
        comment = payload.get("comment")
        if not isinstance(issue, dict) or not isinstance(comment, dict):
            return None
        number = issue.get("number")
        if not isinstance(number, int):
            return None
        body = _text(comment.get("body"))
        url = _text(comment.get("html_url"))
        is_pull = isinstance(issue.get("pull_request"), dict)
        author = association = reply = ""
        comment_id = 0
        verdict = False
        findings = not _RELEASE_VERDICT_RE.match(body)
        where = f"{'pull request' if is_pull else 'issue'} {repository}#{number}"
        person = is_person(login, kind, own_logins)
        if _ESCALATED_RE.match(body) and (
            person or login.casefold() in reviewer_logins() or login.casefold() in own_logins
        ):
            # Neither findings nor a pass, so it fitted no branch below and reached nobody: the
            # Work read "waiting for review" for good (#3216). It ends the chat's review waits.
            headline = f"AgentDeck event: the reviewer escalated the review on {where} to the owner"
            reply = ESCALATED_ASK
            verdict = True
            if person:
                # A person's login, so it reaches the chat only from one with the Work tier.
                author, association = login, _text(comment.get("author_association"))
        elif login.casefold() in reviewer_logins() and _VERDICT_RE.match(body):
            headline = f"AgentDeck event: the reviewer posted on {where}"
            verdict = findings
            reply = OWNER_CALL_ASK if findings else ""
        elif login.casefold() in own_logins and _DECK_REVIEW_RE.match(body):
            headline = f"AgentDeck event: the deck's reviewer posted on {where}"
            verdict = findings
            reply = OWNER_CALL_ASK if findings else ""
        elif person and _CLAUDE_RE.match(body):
            headline = f"AgentDeck event: {login} commented on {where}"
            gh = "gh pr comment" if is_pull else "gh issue comment"
            reply = (
                f"When you have acted on it, answer on {where} in one short comment "
                f"(`{gh}`): what you did or your answer, with a link to your chat if the "
                "detail is there."
            )
            author, association = login, _text(comment.get("author_association"))
            if isinstance(comment.get("id"), int):
                comment_id = comment["id"]
        else:
            return None
        text = f"{headline}:\n\n{_clip(body)}\n\n{url}".rstrip()
        link = _link(headline, url)
        if reply:
            text, link = f"{text}\n\n{reply}", f"{link}\n\n{reply}"
        return Event(
            MESSAGE,
            repository,
            number,
            is_pull,
            text=text,
            link=link,
            author=author,
            association=association,
            comment_id=comment_id,
            verdict=verdict,
        )
    if delivery.event == "pull_request_review" and action == "submitted":
        pull = payload.get("pull_request")
        review = payload.get("review")
        if not isinstance(pull, dict) or not isinstance(review, dict):
            return None
        number = pull.get("number")
        if not isinstance(number, int) or not is_person(login, kind, own_logins):
            return None
        state = _text(review.get("state")).lower().replace("_", " ") or "commented"
        body = _clip(_text(review.get("body")))
        headline = f"AgentDeck event: {login} reviewed pull request {repository}#{number} ({state})"
        text = headline + (f":\n\n{body}" if body else ".")
        url = _text(review.get("html_url"))
        return Event(
            MESSAGE,
            repository,
            number,
            True,
            text=f"{text}\n\n{url}".rstrip(),
            link=_link(headline, url),
            author=login,
            association=_text(review.get("author_association")),
        )
    if delivery.event == "pull_request" and action in _READY_ACTIONS:
        pull = payload.get("pull_request")
        if not isinstance(pull, dict) or pull.get("state") != "open":
            return None
        # A push that lands just after the pull request was marked ready can still carry
        # `draft: true` (#2983): that one is read live rather than trusted.
        confirm = bool(pull.get("draft"))
        if confirm and action != "synchronize":
            return None
        number = pull.get("number")
        head = pull.get("head")
        sha = _text(head.get("sha")).lower() if isinstance(head, dict) else ""
        if not isinstance(number, int) or not sha:
            return None
        return Event(
            REVIEW,
            repository,
            number,
            True,
            head=sha,
            confirm=confirm,
            explicit=action == "ready_for_review",
        )
    return None


def _pull_maps(raw: object, name: str) -> dict[str, dict[str, str]]:
    """``raw[name]`` as ``{pull: {head: text}}``, anything else left out."""
    value = raw.get(name) if isinstance(raw, dict) else None
    return {
        pull: {head: text for head, text in heads.items() if isinstance(text, str)}
        for pull, heads in (value.items() if isinstance(value, dict) else ())
        if isinstance(pull, str) and isinstance(heads, dict)
    }


async def _gh_lines(repository: str, argv: list[str]) -> list[str] | None:
    """A ``gh`` read's output lines, or ``None`` when it failed."""
    from . import gh_cli

    try:
        code, out, _err = await gh_cli.run_once(argv, timeout=60, repo=repository)
    except (OSError, TimeoutError):
        return None
    if code != 0:
        return None
    return (out or b"").decode("utf-8", "replace").splitlines()


async def compared_files(repository: str, old: str, new: str) -> set[str] | None:
    """The files the commits from ``old`` to ``new`` touch, renames under both names, or
    ``None`` when unread, cut short by GitHub (300 files) or ``new`` does not descend."""
    names = "[.files[] | .filename, (.previous_filename // empty)]"
    compared = await _gh_lines(
        repository,
        [
            "gh",
            "api",
            f"repos/{repository}/compare/{old}...{new}",
            "--jq",
            f'"\\(.status) \\(.files | length)", {names}[]',
        ],
    )
    status, _, count = (compared or [""])[0].partition(" ")
    if status != "ahead" or not count.isdigit() or int(count) >= 300:
        return None
    return set(compared[1:])


async def pull_files(repository: str, number: int) -> set[str] | None:
    """The files of a pull request's diff, renames under both names; ``None`` when unread."""
    files = await _gh_lines(
        repository,
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repository}/pulls/{number}/files?per_page=100",
            "--jq",
            ".[] | .filename, (.previous_filename // empty)",
        ],
    )
    return set(files) if files else None


async def base_only_between(repository: str, number: int, old: str, new: str) -> bool:
    """Whether ``new`` descends from ``old`` through commits that touch no file of the pull
    request (#3314 review): then every line it contributes is byte for byte what was reviewed.

    Two reads: GitHub's comparison of the two heads and the pull request's file list. Anything
    unread, a comparison GitHub cut short (300 files) or a head that does not descend is
    ``False``: unproven is reviewed.
    """
    touched = await compared_files(repository, old, new)
    if touched is None:
        return False
    files = await pull_files(repository, number)
    if not files:
        return False
    return not touched & files


async def no_code_between(
    repository: str, number: int, old: str, new: str, before: Iterable[str] | None
) -> bool:
    """Whether ``new`` descends from ``old`` through commits that change only doc files or
    files of neither diff, a base merge (#3322): the fix a ``no-code`` review asked for.

    ``before`` is the pull request's files at ``old``, kept with that verdict. Without them
    only doc files pass: a commit that reverts a file of the diff drops it from the file list
    now, and would read as a base merge. The same two reads as ``base_only_between``; anything
    unread is ``False``, so it is reviewed.
    """
    from .work_release import is_doc_path

    touched = await compared_files(repository, old, new)
    if touched is None:
        return False
    if all(is_doc_path(name) for name in touched):
        return True
    if before is None:
        return False
    files = await pull_files(repository, number)
    if not files:
        return False
    outside = set(files) | set(before)
    return all(is_doc_path(name) or name not in outside for name in touched)


class ReviewedHeads:
    """The heads a review was already asked for, in one small JSON file.

    Written once the review start answered with a chat that names the head (or failed in a
    way that may have written). A start cut off by a crash is asked for again when the
    uncommitted delivery is replayed; the review path reuses the open chat it may already
    have written. Re-read whenever the other web worker of a hot reload wrote it (#3070).

    Beside them, the review digest of each pull request's diff at the heads a review started
    for (#3314): a later head with the same digest only brought the branch up to its base, and
    is not reviewed again (``WorkRouterService._unchanged``).
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._file = SharedFile(self.path, prefix=".work-router.")
        self._keys: list[str] = []
        #: ``owner/repo#n`` -> {head: digest}, oldest first; the newest ``MAX_DIGESTS`` pulls.
        self._digests: dict[str, dict[str, str]] = {}
        #: ``owner/repo#n`` -> {head: the head whose verdict it keeps}, for skipped heads.
        self._carried: dict[str, dict[str, str]] = {}
        #: ``owner/repo#n`` -> {head (a verdict's short sha too): ""}: heads a verdict answered.
        self._verdicts: dict[str, dict[str, str]] = {}

    def _load(self) -> list[str]:
        try:
            raw = self._file.read()
        except (OSError, ValueError):
            log.warning("work-router: ignoring unreadable %s", self.path)
            return []
        self._digests = _pull_maps(raw, "digests")
        self._carried = _pull_maps(raw, "carried")
        self._verdicts = _pull_maps(raw, "verdicts")
        keys = raw.get("heads") if isinstance(raw, dict) else None
        return [key for key in keys if isinstance(key, str)] if isinstance(keys, list) else []

    def _fresh(self) -> None:
        if self._file.stale():
            self._keys = self._load()

    def __contains__(self, key: str) -> bool:
        self._fresh()
        return key in self._keys

    def last_digest(self, pull: str) -> tuple[str, str]:
        """``(head, digest)`` of the newest head of ``pull`` with a digest, or empty strings."""
        self._fresh()
        heads = self._digests.get(pull.lower()) or {}
        return next(reversed(heads.items()), ("", ""))

    def note_digest(self, pull: str, head: str, digest: str, *, carried_from: str = "") -> None:
        """Write ``pull``'s diff digest at ``head``, as its newest; ``carried_from`` is the head
        whose verdict it keeps, proven a base merge away (#3314)."""
        if not digest:
            return
        with self._file.locked():
            self._fresh()
            pull, head = pull.lower(), head.lower()
            heads = {key: value for key, value in (self._digests.get(pull) or {}).items()}
            heads.pop(head, None)
            heads[head] = digest.lower()
            digests = {key: value for key, value in self._digests.items() if key != pull}
            digests[pull] = dict(list(heads.items())[-MAX_HEAD_DIGESTS:])
            self._digests = dict(list(digests.items())[-MAX_DIGESTS:])
            carried = {key: value for key, value in (self._carried.get(pull) or {}).items()}
            carried.pop(head, None)
            if carried_from:
                carried[head] = carried_from.lower()
            others = {key: value for key, value in self._carried.items() if key != pull}
            if carried:
                others[pull] = dict(list(carried.items())[-MAX_HEAD_DIGESTS:])
            self._carried = {key: others[key] for key in list(others)[-MAX_DIGESTS:]}
            self._write(self._keys)

    def note_verdict(self, pull: str, head: str) -> None:
        """A verdict the release took answered ``pull`` at ``head`` (#3314 review)."""
        head = (head or "").lower()
        if len(head) < 7:
            return
        with self._file.locked():
            self._fresh()
            pull = pull.lower()
            heads = dict(self._verdicts.get(pull) or {})
            heads.pop(head, None)
            heads[head] = ""
            others = {key: value for key, value in self._verdicts.items() if key != pull}
            others[pull] = dict(list(heads.items())[-MAX_HEAD_DIGESTS:])
            self._verdicts = {key: others[key] for key in list(others)[-MAX_DIGESTS:]}
            self._write(self._keys)

    def answered(self, pull: str, head: str) -> bool:
        """Whether a verdict answered ``head``, or a head it keeps the verdict of."""
        self._fresh()
        given = self._verdicts.get(pull.lower()) or {}
        carried = self._carried.get(pull.lower()) or {}
        current = head.lower()
        for _ in range(MAX_HEAD_DIGESTS + 1):
            if any(current.startswith(verdict) for verdict in given):
                return True
            current = carried.get(current, "")
            if not current:
                return False
        return False

    def same_change(self, pull: str, head: str, older: str) -> bool:
        """Whether ``head`` keeps ``older``'s verdict: the same head, or carried from it through
        proven base merges (``older`` may be a verdict's short sha)."""
        self._fresh()
        older = older.lower()
        if len(older) < 7:
            return False
        carried = self._carried.get(pull.lower()) or {}
        current = head.lower()
        for _ in range(MAX_HEAD_DIGESTS + 1):
            if current.startswith(older):
                return True
            current = carried.get(current, "")
            if not current:
                return False
        return False

    def add(self, key: str) -> None:
        with self._file.locked():
            if key in self:
                return
            self._write([*self._keys, key][-MAX_STARTED:])

    def discard(self, key: str) -> None:
        with self._file.locked():
            if key in self:
                self._write([item for item in self._keys if item != key])

    def count(self, prefix: str, exclude: str = "") -> int:
        """How many remembered keys start with ``prefix`` but not ``exclude``: the asks
        again of one head."""
        self._fresh()
        return sum(
            1
            for key in self._keys
            if key.startswith(prefix) and not (exclude and key.startswith(exclude))
        )

    def reserve(self, prefix: str, limit: int, key: str, exclude: str = "") -> bool:
        """Add ``key`` unless ``limit`` keys already start with ``prefix``; whether it was.

        One locked read-modify-write, so two web workers asking at once cannot both take the
        last place under the cap (#3183 review).
        """
        with self._file.locked():
            if key in self:
                return True
            if self.count(prefix, exclude) >= limit:
                return False
            self._write([*self._keys, key][-MAX_STARTED:])
            return True

    def _write(self, keys: list[str]) -> None:
        self._file.write(
            {
                "heads": keys,
                "digests": self._digests,
                "carried": self._carried,
                "verdicts": self._verdicts,
            }
        )
        self._keys = keys


class PendingActions:
    """Actions a batch asked for that have not happened yet, in one small JSON file.

    Each entry is keyed ``message:<id>`` or ``review:<owner/repo#n>@<head>`` and carries
    what is needed to try again, its first sighting (``since``), the attempts so far, the
    last back-off wait (``delay``) and when the next attempt is due (``next_at``). Written
    before the offset is committed, so a crash loses neither the delivery nor the action.
    ``changed`` is set on every write, so the retry loop sleeps until the earliest
    ``next_at`` rather than looking on a clock, and not at all while nothing is pending.
    Re-read whenever the other web worker of a hot reload wrote it (#3070).
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._file = SharedFile(
            self.path, prefix=".work-router.", on_foreign_write=self._foreign_write
        )
        self._entries: dict[str, dict] = {}
        self.changed = asyncio.Event()

    def _foreign_write(self) -> None:
        self._entries = self._load()
        self.changed.set()  # the retry loop re-plans on what the other worker left

    def _load(self) -> dict[str, dict]:
        try:
            raw = self._file.read()
        except (OSError, ValueError):
            log.warning("work-router: ignoring unreadable %s", self.path)
            return {}
        entries = raw.get("pending") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            return {}
        return {k: v for k, v in entries.items() if isinstance(k, str) and isinstance(v, dict)}

    @property
    def entries(self) -> dict[str, dict]:
        if self._file.stale():
            self._entries = self._load()
        return self._entries

    def __contains__(self, key: str) -> bool:
        return key in self.entries

    def put(self, key: str, fields: dict, now: float) -> None:
        """Write an action down as pending, or push its next attempt back if it already is."""
        with self._file.locked():
            entry = dict(self.entries.get(key) or {"since": now, "attempts": 0})
            entry.update(fields)
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            backoff = Backoff(first=RETRY_FIRST_S, cap=RETRY_MAX_S)
            backoff.next_s = float(entry.get("delay", 0.0))
            entry["delay"] = backoff.failed()
            entry["next_at"] = now + entry["delay"]
            self._save({**self.entries, key: entry})

    def discard(self, key: str) -> None:
        with self._file.locked():
            if key in self.entries:
                self._save({k: v for k, v in self.entries.items() if k != key})

    def wait(self, now: float) -> float | None:
        """Seconds until the earliest pending attempt is due; ``None`` with nothing pending."""
        due = [float(v.get("next_at", 0)) for v in self.entries.values()]
        return max(min(due) - now, 0.0) if due else None

    def due(self, now: float) -> list[tuple[str, dict]]:
        return [(k, dict(v)) for k, v in self.entries.items() if v.get("next_at", 0) <= now]

    def _save(self, entries: dict[str, dict]) -> None:
        self._file.write({"pending": entries})
        self._entries = entries
        self.changed.set()


class SwitchedRepositories:
    """Which repositories' Works hear their own events, re-read when a manifest moves."""

    def __init__(self, poller_root: Path | str) -> None:
        self.root = Path(poller_root).expanduser()
        self._signature: tuple | None = None
        self._repositories: frozenset[str] = frozenset()

    def _current_signature(self) -> tuple:
        found = []
        for path in sorted(self.root.glob("instances/*/poller.toml")):
            with contextlib.suppress(OSError):
                stat = path.stat()
                found.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(found)

    def __call__(self, repository: str) -> bool:
        signature = self._current_signature()
        if signature != self._signature:
            from .poller_ops import work_repositories

            self._repositories = work_repositories(self.root)
            self._signature = signature
        return repository.lower() in self._repositories


#: ``(chat id, the Work's issue as owner/repo#n)`` of the chat that owns an issue or a
#: registered pull request, or ``None`` when no chat does.
Owner = tuple[str, str]

#: The key prefix of a Work whose issue went to another deck (#3731).
HANDED_OVER = "handed-over:"
#: What a Work's chat is told when its issue goes to another deck, or to nobody where this
#: deck does not take unassigned issues (#3731); it was interrupted just before.
HANDED_OVER_TEXT = (
    "AgentDeck: this issue was reassigned to {who}, so it is not this deck's any more: that "
    "person, or their own AgentDeck on the same branch, works it now. Stop: do not push, "
    "comment or open anything more. End your turn with one line saying where you left off."
)
#: ... and when it comes back to this deck's person.
TAKEN_BACK_TEXT = (
    "AgentDeck: this issue is assigned back to this deck's person, so it is yours again. "
    "Fetch the branch first, since another AgentDeck may have pushed to it, then carry on."
)
#: What a Work's chat is told when another deck's start comment lands on its issue (#3731).
FOREIGN_START_TEXT = (
    "AgentDeck: another AgentDeck (@{login}) says it started an agent on this issue, so two "
    "agents may now push the same branch. Do not push until a person has settled who works "
    "it: ask them in your question box whether to stop, naming @{login}, and suggest they "
    "assign the issue to the person whose deck should keep it."
)


class WorkRouterService:
    """Tail the streams and hand each switched Work's events to the chat that owns it.

    ``owner_of_issue(repository, number)`` and ``owner_of_pull(repository, number)`` name
    that chat. ``schedule``/``exists`` are the scheduled-message store's; ``exists`` tells a
    replay the store absorbed from a row it refused. ``start_review(pull, work)`` opens the
    review chat for ``owner/repo#n`` in the named Work and starts a reviewer on it.
    ``refusal(chat, text)`` is the chat's provider's ``user_message_refusal``.
    ``ready_head(repository, number)`` reads the head of an open, non-draft pull request
    (``""`` otherwise), for one registered after its ready event and for a pending review.
    ``own_logins`` is called per batch, so an edited authority config is picked up.
    ``digest(pull, head)`` is the review digest of the pull request's diff at that head
    (``""`` unread) and ``base_only(repository, number, old, new)`` whether the commits from
    ``old`` to ``new`` touch no file of the pull request: a new head both call the same is not
    reviewed (#3314).
    """

    def __init__(
        self,
        streams_root: Path | str,
        *,
        switched: Callable[[str], bool],
        owner_of_issue: Callable[[str, int], Owner | None],
        owner_of_pull: Callable[[str, int], Owner | None],
        schedule: Callable[[ScheduledMessage], ScheduledMessage | None],
        start_review: Callable[..., Awaitable[object]],
        own_logins: Callable[[], Iterable[str]] | Iterable[str] = configured_own_logins,
        refusal: Callable[[str, str], str | None] = lambda chat_id, text: None,
        ready_head: Callable[[str, int], Awaitable[str]] | None = None,
        enabled: bool = True,
        watcher: WebhookWatcher | None = None,
        reviewed: ReviewedHeads | None = None,
        exists: Callable[[str], bool] | None = None,
        pending: PendingActions | None = None,
        release: object | None = None,
        clock: Callable[[], float] = time.time,
        start_work: Callable[..., Awaitable[object]] | None = None,
        start_labels: Callable[[str], Iterable[str]] = lambda repository: (),
        spec_only_label: Callable[[str], str] = lambda repository: "",
        authorized: Callable[[str, str, str], Awaitable[bool]] | None = None,
        reread_issue: Callable[[str, int], Awaitable[object]] | None = None,
        paused: Callable[[str], bool] = lambda work_id: False,
        starred: Callable[[str], bool] = lambda work_id: False,
        resume: Callable[[str], Awaitable[bool]] | None = None,
        acknowledge: Callable[[str, int], Awaitable[object]] | None = None,
        finished: Callable[[str], bool] = lambda work_id: False,
        latest_comment: Callable[[str, int, str, str], Awaitable[dict | None]] | None = None,
        busy: Callable[[str], bool] = lambda work_id: False,
        on_verdict: Callable[[str, Iterable[str] | None], object] | None = None,
        digest: Callable[[str, str], Awaitable[str]] | None = None,
        base_only: Callable[[str, int, str, str], Awaitable[bool]] | None = None,
        no_code: Callable[..., Awaitable[bool]] | None = None,
        load_work: Callable[[str, int], Awaitable[object]] | None = None,
        read_issue: Callable[[str, int], Awaitable[dict | None]] | None = None,
        read_parent: Callable[[str, int], Awaitable[tuple[str, int] | None]] | None = None,
        read_body: Callable[[str, int], Awaitable[dict | None]] | None = None,
        write_body: Callable[[str, int, str], Awaitable[bool]] | None = None,
        review_chat: Callable[[str, str], tuple[str, object | None] | None] | None = None,
        wait_review: Callable[[str, str, bool], object] | None = None,
        mine: Callable[[str, str], bool] = lambda repository, assignee: True,
        interrupt: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._tailer = WebhookTailer(streams_root, consumer=CONSUMER)
        self._watcher = watcher
        self._switched = switched
        # Which chat holds an issue's or a pull request's Work at all, even one handed to
        # another deck; every other path reads ``_owner_of_issue``/``_owner_of_pull`` (#3731).
        self._chat_of_issue = owner_of_issue
        self._chat_of_pull = owner_of_pull
        self._schedule = schedule
        self._start_review = start_review
        self._start_work = start_work
        self._start_labels = start_labels
        self._spec_only_label = spec_only_label
        self._authorized = authorized
        self._reread_issue = reread_issue
        self._paused = paused
        self._starred = starred
        self._resume = resume
        self._acknowledge = acknowledge
        self._finished = finished
        self._latest_comment = latest_comment
        self._read_issue = read_issue
        self._read_parent = read_parent
        # A wayfinder map's body read and saved again when a record lands (#3695).
        self._read_body = read_body
        self._write_body = write_body
        self._streams_root = Path(streams_root).expanduser()
        self._busy = busy
        # When each Work's pull request last merged, so the close that merge made is not news
        # (#3188).
        self._merged: dict[str, float] = {}
        # The chat a verdict was just handed to, and the pull requests it is about (``None``:
        # the whole Work): those review waits end (#3183).
        self._on_verdict = on_verdict
        # The review digest of a pull request's diff at a head (``chat_review.target_digest``):
        # a head whose diff a review already read is not reviewed again (#3314).
        self._digest = digest
        # Whether the commits between two heads touch no file of the pull request
        # (``base_only_between``): the proof a same digest is a base merge, not moved code.
        self._base_only = base_only
        # Whether a push after a ``no-code`` verdict changed only doc files or merged its base
        # (``no_code_between``): then that review counts as a pass, with no new one (#3322).
        self._no_code = no_code
        # Reads back the chats of an issue's Work the deck let go from memory (#3405).
        self._load_work = load_work
        # The newest review chat about a pull request at a head, ``(owner/repo#n, its chat
        # board row or None)``, and the chat's wait on a review's verdict, ``(chat, pull,
        # asked)``: a ready press at a head under review says where that review is (#3584).
        self._review_chat = review_chat
        self._wait_review = wait_review
        # Whether an issue assigned to a login ("" for nobody) is this deck's to work, and how
        # a chat whose issue went to another deck is stopped (#3731).
        self._mine = mine
        self._interrupt = interrupt
        self._own_logins = own_logins if callable(own_logins) else frozenset(own_logins)
        self._refusal = refusal
        self._ready_head = ready_head
        self._in_flight: set[str] = set()
        # Starts whose issue closed or lost its label since: a late refusal does not revive
        # them (#3006).
        self._unwanted_starts: set[str] = set()
        # Repositories the deck is filing a New work in right now, and how many (#3590).
        self._filing: dict[str, int] = {}
        self._checks: set[asyncio.Task] = set()
        self.reviewed = reviewed or ReviewedHeads(
            Path(streams_root).expanduser().parent / STARTED_FILENAME
        )
        self.pending = pending or PendingActions(
            Path(streams_root).expanduser().parent / PENDING_FILENAME
        )
        # Kept apart from the reviewed heads, whose cap would drop them (#3731).
        self.handed_over = ReviewedHeads(
            Path(streams_root).expanduser().parent / HANDED_OVER_FILENAME
        )
        self._exists = exists
        self._clock = clock
        # The deck's own release (``work_release``, step 2): it hears verdicts, pull request
        # and check events for the Works it releases. ``None`` hears nothing.
        self._release = release
        self._task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="work-router")
        self._retry_task = asyncio.create_task(self._retry_loop(), name="work-router-retry")

    async def stop(self) -> None:
        for check in list(self._checks):
            check.cancel()
        retry, self._retry_task = self._retry_task, None
        if retry is not None:
            retry.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retry
        task, self._task = self._task, None
        if task is None:
            return
        self._stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def skip_history(self) -> bool:
        """A first start reads from the end: older deliveries are not this router's to send."""
        if self._tailer.state_path.exists():
            return False
        for _ in self._tailer.deliveries():
            pass
        self._tailer.commit()
        return True

    async def _run(self) -> None:
        self.skip_history()
        while True:
            try:
                async for batch in self._tailer.watch(stop_event=self._stop, watcher=self._watcher):
                    await self.apply(batch)
                    self._tailer.commit()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("work-router pass failed; retrying")
                await asyncio.sleep(5)

    async def _retry_loop(self) -> None:
        """Sleep until the earliest pending action is due, then try the due ones.

        A deadline, not a tick (ADR 0007): with nothing pending it waits only for
        ``PendingActions.changed``, and a new or rescheduled action wakes it to re-plan.
        """
        failures = Backoff()
        while True:
            self.pending.changed.clear()
            wait = self.pending.wait(self._clock())
            if wait is None or wait > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.pending.changed.wait(), timeout=wait)
                continue
            try:
                await self.retry_pending()
                failures.succeeded()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A pass that raised moved no ``next_at``: wait before trying it again.
                log.exception("work-router: retrying pending actions failed")
                await asyncio.sleep(failures.failed())

    async def retry_pending(self) -> None:
        """Try every pending action whose back-off has run out, each once.

        A starred Work's start is tried first (#3116), so it takes a worker that frees up.
        """
        now = self._clock()
        due = self.pending.due(now)
        if any(entry.get("kind") == START for _, entry in due):
            due.sort(
                key=lambda item: not (
                    item[1].get("kind") == START
                    and self._starred(item[0].removeprefix(f"{START}:"))
                )
            )
        for key, entry in due:
            if now - float(entry.get("since", now)) > PENDING_MAX_AGE_S:
                log.warning("work-router: dropping %s, still not done after a day", key)
                self._drop_pending(key)
            elif entry.get("kind") == MESSAGE:
                self._deliver(
                    ScheduledMessage(
                        id=str(entry.get("id") or ""),
                        chat_id=str(entry.get("chat_id") or ""),
                        text=str(entry.get("text") or ""),
                        due_at=datetime.now(UTC),
                        created_at=datetime.now(UTC),
                    )
                )
            elif entry.get("kind") == REVIEW:
                await self._retry_review(key, entry)
            elif entry.get("kind") == START:
                await self._retry_start(key, entry)
            elif entry.get("kind") == CLOSE:
                self._retry_close(key, entry)
            else:
                self.pending.discard(key)
            entry_now = self.pending.entries.get(key)
            if entry_now is not None and float(entry_now.get("next_at", 0)) <= now:
                # Tried and neither done nor rescheduled (a start already in flight): push
                # it back, so the deadline loop never spins on it.
                self.pending.put(key, {}, self._clock())

    async def _retry_start(self, key: str, entry: dict) -> None:
        repository = str(entry.get("repository") or "").lower()
        number = entry.get("number")
        if (
            self._start_work is None
            or not isinstance(number, int)
            or not self._switched(repository)
            or self._chat_of_issue(repository, number) is not None
            or not self._mine(repository, assignee := str(entry.get("assignee") or ""))
        ):
            self.pending.discard(key)
            return
        labels = entry.get("labels")
        await self._start(
            repository,
            number,
            [str(name) for name in labels] if isinstance(labels, list) else [],
            assignee,
        )

    async def _retry_review(self, key: str, entry: dict) -> None:
        repository = str(entry.get("repository") or "").lower()
        number = entry.get("number")
        head = str(entry.get("head") or "").lower()
        again = str(entry.get("again") or "")
        owner = self._owner_of_pull(repository, number) if isinstance(number, int) else None
        if owner is None or not head:
            self._drop_pending(key)
            return
        if self._ready_head is not None:
            try:
                current = (await self._ready_head(repository, number) or "").lower()
            except Exception:  # noqa: BLE001 - unreadable now: try again after the back-off
                log.warning("work-router: %s#%s unreadable", repository, number, exc_info=True)
                self.pending.put(key, {}, self._clock())
                return
            if current != head:
                # Pushed past, turned back to draft or closed: this head is nobody's to review
                # any more, and the current one (if ready) is asked for in its place. A request
                # to ask again was about the old head only: the new one gets its own review.
                self._drop_pending(key)
                if not current:
                    return
                head, again = current, ""
                entry = {}
        if entry and self._passed_since(owner, repository, number, head, entry.get("since")):
            # Kept while another review of this head ran, which then passed it (#3324): a
            # review now would only re-open a release that already waits for a person's Merge.
            log.info("work-router: %s passed since it was asked for; not reviewed", key)
            self._drop_pending(key)
            return
        note = str(entry.get("note") or "") if again else ""
        # Only the agent's own request reviews a diff an earlier head's review read (#3314).
        await self._review(
            owner,
            Event(REVIEW, repository, number, True, head=head, again=again, note=note),
            asked=bool(entry.get("asked")),
        )

    def _passed_since(
        self, owner: Owner, repository: str, number: int, head: str, since: object
    ) -> bool:
        """Whether the release recorded a pass of this head after ``since`` (#3324)."""
        read = getattr(self._release, "passed_since", None)
        if read is None or not isinstance(since, (int, float)):
            return False
        try:
            return bool(read(_work_id(owner), f"{repository}#{number}", head, float(since)))
        except Exception:  # noqa: BLE001 - unread: reviewed as asked
            log.warning(
                "work-router: could not read %s#%s's pass", repository, number, exc_info=True
            )
            return False

    def switched(self, repository: str) -> bool:
        """Whether ``repository``'s Works hear their own events: a poller manifest declares it."""
        return bool(self._switched(repository))

    def _owner_of_issue(self, repository: str, number: int) -> Owner | None:
        """The chat working an issue's Work, or ``None``, also for one handed to another deck."""
        return self._working(self._chat_of_issue(repository, number))

    def _owner_of_pull(self, repository: str, number: int) -> Owner | None:
        """The chat working a pull request's Work, as ``_owner_of_issue``."""
        return self._working(self._chat_of_pull(repository, number))

    def _working(self, owner: Owner | None) -> Owner | None:
        """``owner``, unless its Work's issue went to another deck (#3731): that deck's agent
        works it now, so this deck sends its stopped chat nothing and releases nothing until
        the issue is assigned back (``_maybe_reassigned``)."""
        if owner is None or f"{HANDED_OVER}{_work_id(owner)}" in self.handed_over:
            return None
        return owner

    def own_logins(self) -> frozenset[str]:
        logins = self._own_logins() if callable(self._own_logins) else self._own_logins
        return frozenset(login.casefold() for login in logins if login)

    async def apply(self, batch: Iterable[Delivery]) -> None:
        """Send every message and start every review this batch asks for, each once."""
        own = self.own_logins()
        for delivery in batch:
            if not self._switched(delivery.stream):
                continue
            await self._load_owner(delivery)
            settled = False
            if self._release is not None:
                try:
                    settled = await self._observe_release(delivery)
                except Exception:  # noqa: BLE001 - a release hiccup must not stop routing
                    log.warning(
                        "work-router: release of %s failed", delivery.delivery_id, exc_info=True
                    )
            if self._start_work is not None:
                await self._maybe_start(delivery)
            await self._maybe_reassigned(delivery)
            self._maybe_foreign_start(delivery)
            if self._reread_issue is not None:
                self._maybe_reread(delivery)
            self._note_merge_or_reopen(delivery)
            # Before the close that follows an answer wakes the map: its chat reads the lists.
            await self._maybe_keep_map_lists(delivery)
            await self._maybe_map_ready(delivery)
            if settled:
                # The release already told the chat what this verdict comment says, or dropped
                # it as about a commit the branch left: forwarded too, every pass woke the
                # agent twice for one status (#3192).
                continue
            event = classify(
                delivery,
                own_logins=own,
                spec_only_label=self._spec_only(delivery),
                last_comment=await self._closing_comment(delivery),
            )
            if event is None:
                continue
            owner = (self._owner_of_pull if event.is_pull else self._owner_of_issue)(
                event.repository, event.number
            )
            if owner is None:
                continue
            if event.kind == CLOSE:
                self._hold_close(delivery, owner, event)
                continue
            reviewed = (
                event.kind == REVIEW
                and f"{event.repository}#{event.number}@{event.head}" in self.reviewed
            )
            if event.kind == REVIEW and (event.confirm or reviewed):
                # A draft-looking push, or a ready event naming a head already reviewed while
                # a push may be landing behind it (#2983): the live head decides, before a
                # paused Work is resumed for it.
                head = await self._live_ready_head(event.repository, event.number)
                if not head:
                    continue
                again = ""
                if event.explicit and await self._no_code_fixed(owner, event, head):
                    # Marked ready once a no-code review's findings were fixed (#3322).
                    continue
                if event.explicit and reviewed and head == event.head:
                    # Turned back to draft and marked ready at a head a review was already
                    # asked for. That used to ask again (#3183), with no reason the reviewer
                    # could read, so each such chat looked like a duplicate (#3344). The chat
                    # is told what is actually going on with that review (#3584).
                    await self._ready_again(delivery, owner, event.repository, event.number, head)
                    continue
                event = replace(event, head=head, confirm=False, again=again)
            if not event.author and self._is_paused(owner) and not await self._unpause(owner):
                continue
            if event.kind == MESSAGE:
                if event.author and not await self._may_instruct(event):
                    continue
                if not event.author and await self._stale_findings(delivery, owner):
                    continue
                sent = self._send(delivery, owner, event)
                # Only once the verdict's message is stored: a pending one keeps the wait.
                if sent and event.verdict and self._on_verdict is not None:
                    try:
                        self._on_verdict(owner[0], self._verdict_pulls(event, owner))
                    except Exception:  # noqa: BLE001 - a stale wait times out on its own
                        log.warning("work-router: ending review waits failed", exc_info=True)
                if event.comment_id:
                    self._background(
                        self._ack(event.repository, event.comment_id), "work-router-ack"
                    )
            else:
                await self._review(owner, event)

    async def _load_owner(self, delivery: Delivery) -> None:
        """Bring back the chats of the Work an issue's event is about, if none is held (#3405).

        A finished Work's chats are let go from memory (#2841), and a Work no held chat joins
        has no owner: a person's ``/claude`` comment on its reopened issue was dropped without
        a word, and the Work then read its old ending as one without a next step. Only an
        issue's own events: a pull request's owner is read from its registration.
        """
        issue = delivery.payload.get("issue")
        if (
            self._load_work is None
            or delivery.event not in {"issues", "issue_comment"}
            or not isinstance(issue, dict)
            or not isinstance(issue.get("number"), int)
            or isinstance(issue.get("pull_request"), dict)
        ):
            return
        repository, number = delivery.stream.lower(), issue["number"]
        if self._chat_of_issue(repository, number) is not None:
            return
        try:
            await self._load_work(repository, number)
        except Exception:  # noqa: BLE001 - routed as before: no owner, nothing sent
            log.warning(
                "work-router: could not load the chats of %s#%s", repository, number,
                exc_info=True,
            )

    def _note_merge_or_reopen(self, delivery: Delivery) -> None:
        """Remember a Work's merge, and drop a close held for a merge or a reopen (#3188)."""
        payload = delivery.payload
        repository = delivery.stream.lower()
        action = _text(payload.get("action"))
        if delivery.event == "issues" and action == "reopened":
            issue = payload.get("issue")
            number = issue.get("number") if isinstance(issue, dict) else None
            owner = self._owner_of_issue(repository, number) if isinstance(number, int) else None
            if owner is not None:
                self.pending.discard(f"{CLOSE}:{_work_id(owner)}")  # undone within the hold
        elif delivery.event == "pull_request" and action == "closed":
            pull = payload.get("pull_request")
            if not isinstance(pull, dict) or not pull.get("merged"):
                return
            number = pull.get("number")
            owner = self._owner_of_pull(repository, number) if isinstance(number, int) else None
            if owner is None:
                return
            work = _work_id(owner)
            self._merged = {
                key: at
                for key, at in self._merged.items()
                if self._clock() - at <= 10 * CLOSE_HOLD_S
            }
            self._merged[work] = self._clock()
            # The issue's close came first and is held: it was this merge's.
            self.pending.discard(f"{CLOSE}:{work}")

    async def _maybe_map_ready(self, delivery: Delivery) -> None:
        """Wake a wayfinder map's chat when its last open ticket closes (#3627).

        A ticket is a sub-issue of the map, and its close names the map in
        ``parent_issue_url``. The map is read once from GitHub, for its label and its
        sub-issue counts: only then is it known that this close was the last. Sent even when
        the deck's own login closed the ticket, as a ticket's chat does: that is the point."""
        payload = delivery.payload
        issue = payload.get("issue")
        if (
            self._read_issue is None
            or delivery.event != "issues"
            or _text(payload.get("action")) != "closed"
            or not isinstance(issue, dict)
            or not isinstance(issue.get("number"), int)
        ):
            return
        parent = _api_issue(issue.get("parent_issue_url"))
        if (
            parent is None
            and self._read_parent is not None
            and not isinstance(issue.get("pull_request"), dict)
        ):
            # GitHub leaves ``parent_issue_url`` out of the issue, and so out of the close's
            # payload (live check, #3627): its own parent lookup says, one read per close.
            try:
                parent = await self._read_parent(delivery.stream.lower(), issue["number"])
            except Exception:  # noqa: BLE001 -- the map's chat is not woken; a person still can
                log.warning("work-router: parent of %s unread", issue["number"], exc_info=True)
                return
        if parent is None:
            log.info(
                "work-router: %s#%s closed, no parent map",
                delivery.stream.lower(),
                issue["number"],
            )
            return
        try:
            record = await self._read_issue(*parent)
        except Exception:  # noqa: BLE001 -- the map's chat is not woken; a person still can
            log.warning("work-router: map %s#%s unread", *parent, exc_info=True)
            return
        event, why = map_ready(delivery, parent, record)
        log.info(
            "work-router: %s#%s closed, map %s#%s %s: %s",
            delivery.stream.lower(),
            issue["number"],
            *parent,
            "ready" if event is not None else "not ready",
            why,
        )
        if event is None:
            return
        owner = self._owner_of_issue(event.repository, event.number)
        if owner is None and self._load_work is not None:
            try:
                await self._load_work(event.repository, event.number)
            except Exception:  # noqa: BLE001 -- no owner, nothing sent
                log.warning("work-router: map %s chats unloaded", parent, exc_info=True)
            owner = self._owner_of_issue(event.repository, event.number)
        if owner is None:
            log.info("work-router: map %s#%s is ready but has no chat", *parent)
            return
        if self._is_paused(owner) and not await self._unpause(owner):
            log.info("work-router: map %s#%s is ready but its chat stays paused", *parent)
            return
        log.info("work-router: map %s#%s is ready, waking chat %s", *parent, owner[0])
        self._send(delivery, owner, event)

    async def _maybe_keep_map_lists(self, delivery: Delivery) -> None:
        """Rebuild a wayfinder map's lists when one of its records lands (#3695).

        The map is the ticket's parent, which the stored ``sub_issues`` deliveries name; only
        a ticket they never linked asks GitHub. The records come from the stored streams too,
        so the only other calls are one live read of the map's body and, when the lists
        changed, one write of it."""
        payload = delivery.payload
        issue, comment = payload.get("issue"), payload.get("comment")
        if (
            self._read_body is None
            or self._write_body is None
            or delivery.event != "issue_comment"
            or _text(payload.get("action")) not in {"created", "edited"}
            or not isinstance(issue, dict)
            or not isinstance(comment, dict)
            or not isinstance(issue.get("number"), int)
            or isinstance(issue.get("pull_request"), dict)
        ):
            return
        kind = wayfinder_lists.record_kind(comment.get("body"))
        if not kind:
            return
        repository, number = delivery.stream.lower(), issue["number"]
        if not self._trusted_record(comment):
            log.info("work-router: %s on %s#%s not from a trusted author", kind, repository, number)
            return
        labels = [
            _text(label.get("name") if isinstance(label, dict) else label)
            for label in issue.get("labels") or ()
        ]
        try:
            # The stream's own case names its file; the facts' keys are lowercased.
            facts = await asyncio.to_thread(
                wayfinder_lists.scan, self._streams_root, delivery.stream
            )
        except Exception:  # noqa: BLE001 -- the lists wait for the next record
            log.warning("work-router: streams of %s unread", repository, exc_info=True)
            return
        wayfinder_lists.note(facts, delivery.event, payload, repository)
        if kind == wayfinder_lists.ANSWER:
            if not wayfinder_map.is_ticket(labels):
                return
            parent = facts.parents.get((repository, number))
            if (repository, number) not in facts.parents and self._read_parent is not None:
                try:
                    parent = await self._read_parent(repository, number)
                except Exception:  # noqa: BLE001 -- the lists wait for the next record
                    log.warning("work-router: parent of %s#%s unread", repository, number)
                    return
                if parent is not None:
                    facts.parents[(repository, number)] = parent
        else:
            parent = (repository, number) if wayfinder_map.is_map(labels) else None
        if parent is None:
            log.info("work-router: %s on %s#%s, no map", kind, repository, number)
            return
        records = []
        for held in sorted(
            (item for item in facts.comments.values() if item is not None),
            key=lambda item: item["created_at"],
        ):
            on_map = held["issue"] == parent
            if (held["kind"] == wayfinder_lists.ANSWER) == on_map:
                continue  # answers belong on tickets, the map's records on the map
            if not on_map and facts.parents.get(held["issue"]) != parent:
                continue
            if not self._trusted_record(held):
                continue
            record = wayfinder_lists.parse(
                held["body"],
                issue=held["issue"],
                title=held["title"],
                url=held["url"],
                created_at=held["created_at"],
            )
            if record is not None:
                records.append(record)
        try:
            found = await self._read_body(*parent)
        except Exception:  # noqa: BLE001 -- the lists wait for the next record
            log.warning("work-router: map %s#%s body unread", *parent, exc_info=True)
            return
        if not isinstance(found, dict) or not isinstance(found.get("body"), str):
            log.info("work-router: map %s#%s body unread", *parent)
            return
        if _text(found.get("state")).lower() != "open" or not wayfinder_map.is_map(
            [_text(label) for label in found.get("labels") or ()]
        ):
            log.info("work-router: %s#%s is not an open wayfinder map", *parent)
            return
        body = found["body"]
        rebuilt = wayfinder_lists.rebuild(body, records)
        if rebuilt == body:
            log.info("work-router: map %s#%s lists already hold %d records", *parent, len(records))
            return
        try:
            saved = await self._write_body(*parent, rebuilt)
        except Exception:  # noqa: BLE001 -- a write is never retried: it may have landed
            log.warning("work-router: map %s#%s body unsaved", *parent, exc_info=True)
            return
        log.info(
            "work-router: map %s#%s lists rebuilt from %d records%s",
            *parent,
            len(records),
            "" if saved else ", but the write failed",
        )

    def _trusted_record(self, comment: dict) -> bool:
        """A record the deck's own login or a person on the repository wrote: anyone else's
        comment shaped like an answer must not write the map."""
        user = comment.get("user")
        login = _text(user.get("login")) if isinstance(user, dict) else _text(comment.get("login"))
        association = _text(comment.get("author_association") or comment.get("association"))
        return (
            login.casefold() in {own.casefold() for own in self.own_logins()}
            or association.upper() in wayfinder_lists.TRUSTED_ASSOCIATIONS
        )

    async def _closing_comment(self, delivery: Delivery) -> dict | None:
        """The closer's own newest comment on a Work's issue around its close (#3188).

        Read from GitHub, not from the comment deliveries: a restart can fall between the two,
        and the comment's delivery can arrive after the close's. Only for an owned issue.
        """
        issue = delivery.payload.get("issue")
        if (
            self._latest_comment is None
            or delivery.event != "issues"
            or _text(delivery.payload.get("action")) != "closed"
            or not isinstance(issue, dict)
            or not isinstance(issue.get("number"), int)
        ):
            return None
        closed = _when(_text(issue.get("closed_at")))
        login, _kind = _login(delivery.payload)
        repository, number = delivery.stream.lower(), issue["number"]
        if closed is None or not login or self._owner_of_issue(repository, number) is None:
            return None
        since = (closed - timedelta(seconds=CLOSE_COMMENT_S)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            found = await self._latest_comment(repository, number, since, login)
        except Exception:  # noqa: BLE001 - the close is still told, without the quote
            log.warning("work-router: comments of %s#%s unread", repository, number, exc_info=True)
            return None
        return found if isinstance(found, dict) else None

    def _close_is_news(self, work: str) -> bool:
        """Would a close of this Work's issue tell its chat anything it does not know?"""
        merged = self._merged.get(work)
        if merged is not None and self._clock() - merged <= 10 * CLOSE_HOLD_S:
            return False  # the Work's own pull request merged and closed it
        try:
            # A paused Work is left paused: its chat is not building on anything.
            return not self._finished(work) and not self._paused(work)
        except Exception:  # noqa: BLE001 - an unread Work sends nothing extra
            log.warning("work-router: could not read %s", work, exc_info=True)
            return False

    def _hold_close(self, delivery: Delivery, owner: Owner, event: Event) -> None:
        """Hold a close of the Work's issue for a merge, unless it is not news (#3188).

        A close under the deck's own login while the chat is mid-turn is taken to be the chat
        closing it itself, the normal end of a Work: every chat writes as that login, so the
        login cannot say which. A person's close is never the chat's, so it is sent even while
        the chat works. Accepted gap (owner's decision on #3188): another chat closing it while
        this one is mid-turn goes unsaid.
        """
        chat_id, _ = owner
        work = _work_id(owner)
        busy = False
        if event.by_deck:
            try:
                busy = bool(self._busy(work))
            except Exception:  # noqa: BLE001 - unread is taken as busy: nothing extra is sent
                log.warning("work-router: could not tell whether %s is busy", work, exc_info=True)
                busy = True
        if busy or not self._close_is_news(work):
            return
        text = event.text
        refused = self._refusal(chat_id, text)
        if refused is not None:
            log.info("work-router: %s cannot carry the closing comment: %s", chat_id, refused)
            text = event.link
        key = f"{CLOSE}:{work}"
        self.pending.discard(key)  # a later close starts its own hold
        fields = {
            "kind": CLOSE,
            "id": f"work-event:{delivery.delivery_id}",
            "chat_id": chat_id,
            "text": text,
            "work": work,
        }
        self.pending.put(key, fields, self._clock())
        log.info("work-router: %s closed by someone else; held for a merge", work)

    def _retry_close(self, key: str, entry: dict) -> None:
        """The hold ran out with no merge: tell the chat, unless the Work finished since."""
        self.pending.discard(key)
        if not self._close_is_news(str(entry.get("work") or "")):
            return
        now = datetime.now(UTC)
        self._deliver(
            ScheduledMessage(
                id=str(entry.get("id") or ""),
                chat_id=str(entry.get("chat_id") or ""),
                text=str(entry.get("text") or ""),
                due_at=now,
                created_at=now,
            )
        )

    async def _stale_findings(self, delivery: Delivery, owner: Owner) -> bool:
        """Whether a reviewer's comment is findings about a commit the branch has left (#3192).

        A reviewer still running on a review chat that a newer head replaced posts about the old
        commit; the new head has its own review. Without a release nothing is read or dropped.
        """
        release = self._release
        comment = delivery.payload.get("comment")
        if release is None or not isinstance(comment, dict):
            return False
        body = re.sub(r"\A\s*/claude\b", "", _text(comment.get("body")), flags=re.IGNORECASE)
        try:
            why = await release.stale_findings(_work_id(owner), body, delivery.stream.lower())
        except Exception:  # noqa: BLE001 - unreadable: hand the findings on, as before
            log.warning(
                "work-router: could not check %s's head", delivery.delivery_id, exc_info=True
            )
            return False
        if why:
            log.info("work-router: findings in %s not sent: %s", delivery.delivery_id, why)
        return bool(why)

    def _spec_only(self, delivery: Delivery) -> str:
        """The delivery's project's spec-only label, read only for an issue's label removal."""
        if delivery.event != "issues" or _text(delivery.payload.get("action")) != "unlabeled":
            return ""
        try:
            return str(self._spec_only_label(delivery.stream.lower()) or "")
        except Exception:  # noqa: BLE001 - an unread manifest passes no approval on
            log.warning("work-router: could not read the spec-only label", exc_info=True)
            return ""

    async def _observe_release(self, delivery: Delivery) -> bool:
        """Hand the release what it acts on: a verdict, a pull request or a check event.

        ``True`` when the delivery is a verdict comment the release settled whole, every line
        of it (``verdict_settled``), so the comment has nothing left to tell the Work's chat.
        """
        from .poller_engine.intake import CommandKind, parse_verdicts
        from .work_release import verdict_settled

        payload = delivery.payload
        repository = delivery.stream.lower()
        action = _text(payload.get("action"))
        release = self._release
        if delivery.event == "issue_comment" and action == "created":
            login, _kind = _login(payload)
            issue = payload.get("issue")
            comment = payload.get("comment")
            if not login or not isinstance(issue, dict):
                return False
            body = _text(comment.get("body")) if isinstance(comment, dict) else ""
            commands = parse_verdicts(body)
            number = issue.get("number")
            if not commands or not isinstance(number, int):
                return False
            is_pull = isinstance(issue.get("pull_request"), dict)
            owner = (self._owner_of_pull if is_pull else self._owner_of_issue)(repository, number)
            if owner is None:
                return False
            if commands[0].kind != CommandKind.REVIEWED:
                # Dropping it quietly left a Work waiting on a pass its reviewer had sent (#3161).
                url = _text(comment.get("html_url")) if isinstance(comment, dict) else ""
                answer = await release.on_unreadable_verdict(
                    _work_id(owner), url or f"{repository}#{number}", by=login
                )
                log.info("work-router: unreadable verdict on %s#%s: %s", repository, number, answer)
                return False
            # One verdict line per pull request, in one comment, each taken on its own (#3161).
            settled = True
            for command in commands:
                targets = command.targets or ((f"{repository}#{number}",) if is_pull else ())
                # Who may send a verdict is the release's call (the Poller's tier for it), not
                # this filter's: Release where a pass merges by itself, Work where a person
                # still merges.
                work_id = _work_id(owner)
                answer = await release.on_verdict(
                    work_id, command.verdict, command.head, command.digest, targets, by=login
                )
                log.info("work-router: verdict on %s#%s: %s", repository, number, answer)
                settled = settled and verdict_settled(str(answer or ""))
            return settled
        if delivery.event == "pull_request":
            pull = payload.get("pull_request")
            number = pull.get("number") if isinstance(pull, dict) else None
            if not isinstance(number, int):
                return
            owner = self._owner_of_pull(repository, number)
            if owner is not None:
                await release.on_pull_event(
                    _work_id(owner), f"{repository}#{number}", action, bool(pull.get("merged"))
                )
        elif delivery.event == "issues" and action in {"labeled", "unlabeled"}:
            issue = payload.get("issue")
            label = payload.get("label")
            number = issue.get("number") if isinstance(issue, dict) else None
            name = _text(label.get("name")) if isinstance(label, dict) else ""
            owner = self._owner_of_issue(repository, number) if isinstance(number, int) else None
            if owner is not None and name:
                await release.on_label(_work_id(owner), name, action == "labeled")
        elif delivery.event == "status" and _text(payload.get("state")).lower() != "pending":
            # Drone's results arrive as commit statuses, which name a commit (#3019).
            await release.on_status(repository, _text(payload.get("sha")).lower())
        elif delivery.event == "check_suite" and action == "completed":
            suite = payload.get("check_suite")
            pulls = suite.get("pull_requests") if isinstance(suite, dict) else None
            for item in pulls if isinstance(pulls, list) else ():
                number = item.get("number") if isinstance(item, dict) else None
                owner = self._owner_of_pull(repository, number) if isinstance(number, int) else None
                if owner is not None:
                    await release.evaluate(_work_id(owner))
        return False

    async def _maybe_start(self, delivery: Delivery) -> None:
        """Start the Work's agent when its ownership label lands on an issue no chat owns.

        The deck's start of a switched project's Work (#2895 step 5b, D2): the labelled issue
        is the Work, and ``start_work`` starts its agent at most once per Work, so a replayed
        delivery or a second label event is harmless.
        """
        payload = delivery.payload
        action = _text(payload.get("action"))
        if delivery.event != "issues" or action not in {"labeled", "unlabeled", "closed"}:
            return
        issue = payload.get("issue")
        label = payload.get("label")
        number = issue.get("number") if isinstance(issue, dict) else None
        repository = delivery.stream.lower()
        if action != "labeled":
            # A refused start waiting for its retry is not wanted any more (#2971).
            starts = {item.lower() for item in self._start_labels(repository)}
            name = _text(label.get("name")).lower() if isinstance(label, dict) else ""
            if isinstance(number, int) and (action == "closed" or name in starts):
                key = f"{START}:github_issue:{repository}#{number}"
                self.pending.discard(key)
                self._unwanted_starts.add(key)
            return
        if not isinstance(issue, dict) or not isinstance(label, dict):
            return
        name = _text(label.get("name")).lower()
        if (
            not isinstance(number, int)
            or isinstance(issue.get("pull_request"), dict)
            or _text(issue.get("state")).lower() == "closed"
            or name not in {item.lower() for item in self._start_labels(repository)}
            or self._chat_of_issue(repository, number) is not None
        ):
            return
        assignee = assignee_of(issue)
        if not self._mine(repository, assignee):
            log.info(
                "work-router: %s#%s is assigned to %s, whose deck works it",
                repository, number, assignee or "nobody",
            )
            return
        labels = issue.get("labels")
        names = (
            [_text(item.get("name")) for item in labels or () if isinstance(item, dict)]
            if isinstance(labels, list)
            else []
        )
        names = [name for name in names if name]
        await self._start_on_event(delivery, repository, number, names, assignee)

    async def _start_on_event(
        self, delivery: Delivery, repository: str, number: int, names: list[str], assignee: str
    ) -> None:
        """Start a Work a label or an assignment delivery asks for, unless a filing holds it."""
        if self._filing.get(repository) and not delivery.delivery_id.startswith("deck-filed:"):
            # The deck may be filing this very issue with its agent already running (#3590):
            # tried again after the back-off, by when that start has spent the Work's. A New
            # work filed with its assignee (#3731) sends an assignment delivery too.
            key = f"{START}:github_issue:{repository}#{number}"
            fields = {"kind": START, "repository": repository, "number": number}
            self.pending.put(
                key, {**fields, "labels": names, "assignee": assignee}, self._clock()
            )
            log.info("work-router: start of %s#%s waits for a filing", repository, number)
            return
        await self._start(repository, number, names, assignee)

    async def _maybe_reassigned(self, delivery: Delivery) -> None:
        """Start, stop or resume a Work whose issue changed hands between decks (#3731).

        An issue assigned to this deck's person, or left to nobody where this deck takes those,
        and carrying a start label, starts here when no chat owns it. One this deck works that
        is now another deck's stops its agent: interrupted, then told who has it, so two agents
        never push one branch. Given back, the same chat is told to carry on.
        """
        payload = delivery.payload
        if delivery.event != "issues" or _text(payload.get("action")) not in {
            "assigned",
            "unassigned",
        }:
            return
        issue = payload.get("issue")
        if not isinstance(issue, dict) or isinstance(issue.get("pull_request"), dict):
            return
        number = issue.get("number")
        if not isinstance(number, int) or _text(issue.get("state")).lower() == "closed":
            return
        repository = delivery.stream.lower()
        assignee = assignee_of(issue)
        mine = self._mine(repository, assignee)
        key = f"{START}:github_issue:{repository}#{number}"
        owner = self._chat_of_issue(repository, number)
        if owner is None:
            if not mine:
                # A start waiting for its retry is now the other deck's.
                self.pending.discard(key)
                self._unwanted_starts.add(key)
                return
            starts = {item.lower() for item in self._start_labels(repository)}
            names = [
                _text(item.get("name"))
                for item in issue.get("labels") or ()
                if isinstance(item, dict) and _text(item.get("name"))
            ]
            if self._start_work is not None and any(name.lower() in starts for name in names):
                await self._start_on_event(delivery, repository, number, names, assignee)
            return
        chat_id, _ = owner
        work = _work_id(owner)
        handed = f"{HANDED_OVER}{work}"
        if self._finished(work):
            return
        if not mine and handed not in self.handed_over:
            self.handed_over.add(handed)
            stopped = False
            if self._interrupt is not None:
                try:
                    stopped = await self._interrupt(chat_id)
                except Exception:  # noqa: BLE001 - the message still says to stop
                    log.warning("work-router: interrupting %s failed", chat_id, exc_info=True)
            log.info(
                "work-router: %s#%s went to %s; %s %s",
                repository, number, assignee or "nobody",
                "interrupted" if stopped else "could not interrupt", chat_id,
            )
            self._tell(delivery, owner, HANDED_OVER_TEXT.format(
                who=f"@{assignee}" if assignee else "nobody"
            ))
        elif mine and handed in self.handed_over:
            self.handed_over.discard(handed)
            self._tell(delivery, owner, TAKEN_BACK_TEXT)

    def _maybe_foreign_start(self, delivery: Delivery) -> None:
        """Warn a Work's chat once another deck says it started an agent on its issue (#3731)."""
        payload = delivery.payload
        if delivery.event != "issue_comment" or _text(payload.get("action")) != "created":
            return
        comment, issue = payload.get("comment"), payload.get("issue")
        if not isinstance(comment, dict) or not isinstance(issue, dict):
            return
        number = issue.get("number")
        login, _kind = _login(comment, "user")
        if (
            START_MARKER not in _text(comment.get("body"))
            or not isinstance(number, int)
            or not login
            or login.casefold() in self.own_logins()
        ):
            return
        owner = self._owner_of_issue(delivery.stream.lower(), number)
        if owner is None or self._finished(_work_id(owner)):
            return
        self._tell(delivery, owner, FOREIGN_START_TEXT.format(login=login))

    async def _start(
        self, repository: str, number: int, labels: list[str], assignee: str = ""
    ) -> None:
        """Start the Work's agent; keep a start refused before anything ran for a retry.

        ``labels`` are the issue's labels as the delivery showed them, kept with a retry: a
        spec-only issue's agent is told to write the spec and not build it (#3134).
        """
        key = f"{START}:github_issue:{repository}#{number}"
        self._unwanted_starts.discard(key)
        fields = {
            "kind": START,
            "repository": repository,
            "number": number,
            "labels": labels,
            "assignee": assignee,
        }
        # A later refusal keeps the back-off this start reached, so a full account is not
        # tried every minute (#3006).
        prior = self.pending.entries.get(key) or {}
        carried = {name: prior[name] for name in ("since", "attempts", "delay") if name in prior}

        def refused_later() -> None:
            if key in self._unwanted_starts:
                return
            log.info(
                "work-router: start of %s#%s refused by the runtime; retrying", repository, number
            )
            self.pending.put(key, {**carried, **fields}, self._clock())

        try:
            outcome = await self._start_work(
                repository, number, on_refused=refused_later, labels=labels
            )
        except Exception:  # noqa: BLE001 - one failed start must not stop routing
            log.warning("work-router: start of %s#%s failed", repository, number, exc_info=True)
            self.pending.discard(key)
            return
        if _start_is_retryable(outcome):
            log.info("work-router: start of %s#%s: %s; retrying", repository, number, outcome)
            self.pending.put(key, fields, self._clock())
        else:
            self.pending.discard(key)

    def starts_on(self, repository: str, labels: Iterable[str]) -> str:
        """The label that starts a Work here when an issue is filed with ``labels``, or ""
        when the router would start none (not switched, off, or no start label)."""
        repository = repository.lower()
        names = [str(name).strip() for name in labels if str(name).strip()]
        starts = {item.lower() for item in self._start_labels(repository)}
        label = next((name for name in names if name.lower() in starts), "")
        if not self._enabled or self._start_work is None or not self._switched(repository):
            return ""
        return label

    @contextlib.contextmanager
    def filing(self, repository: str) -> Iterator[None]:
        """The deck is filing a New work in ``repository``: hold the router's starts there.

        A "label added" delivery that arrives meanwhile may be for this very issue, whose
        agent the deck started before GitHub answered (#3590); it is retried after the
        back-off instead, by when the deck's start has spent the Work's one start.
        """
        repository = repository.lower()
        self._filing[repository] = self._filing.get(repository, 0) + 1
        try:
            yield
        finally:
            left = self._filing.get(repository, 1) - 1
            if left > 0:
                self._filing[repository] = left
            else:
                self._filing.pop(repository, None)

    def note_filed(self, repository: str, number: int, labels: Iterable[str]) -> bool:
        """The deck just filed this issue with these labels: start its Work now (#3371).

        GitHub can hold its webhook deliveries back for many minutes, and the backstop only
        looks every 15. This feeds ``_maybe_start`` the "label added" delivery GitHub would
        have sent, in the background so the filer is not held up by the start. When GitHub's
        own delivery arrives later, ``start_work`` has already spent the Work's one start.
        ``False``: nothing to start here (not switched, or no start label).
        """
        repository = repository.lower()
        names = [str(name).strip() for name in labels if str(name).strip()]
        label = self.starts_on(repository, names)
        if not label:
            return False
        payload = {
            "action": "labeled",
            "issue": {
                "number": number,
                "state": "open",
                "labels": [{"name": name} for name in names],
            },
            "label": {"name": label},
        }
        delivery = Delivery(
            stream=repository,
            delivery_id=f"deck-filed:{repository}#{number}",
            event="issues",
            received_at=datetime.now(UTC).isoformat(),
            headers={},
            raw_body=b"",
            offset=0,
            _payload=payload,
        )
        self._background(self._maybe_start(delivery), "work-router-filed")
        return True

    def _maybe_reread(self, delivery: Delivery) -> None:
        """Re-read a Work's issue once when its registered pull request merges (#2947).

        The deck learns a close from the ``issues`` ``closed`` delivery alone, and GitHub can
        drop it: the merge that closed the issue is then the only sign left. In the background,
        so routing is not held up by a GitHub read.
        """
        payload = delivery.payload
        pull = payload.get("pull_request")
        if (
            delivery.event != "pull_request"
            or _text(payload.get("action")) != "closed"
            or not isinstance(pull, dict)
            or not pull.get("merged")
            or not isinstance(pull.get("number"), int)
        ):
            return
        owner = self._owner_of_pull(delivery.stream.lower(), pull["number"])
        if owner is None:
            return
        repository, _, number = owner[1].rpartition("#")
        if not repository or not number.isdigit():
            return
        check = asyncio.create_task(
            self._reread(repository, int(number)), name="work-router-reread"
        )
        self._checks.add(check)
        check.add_done_callback(self._checks.discard)

    def _background(self, work: Coroutine[object, object, object], name: str) -> None:
        check = asyncio.create_task(work, name=name)
        self._checks.add(check)
        check.add_done_callback(self._checks.discard)

    async def _ack(self, repository: str, comment_id: int) -> None:
        """👀 the comment just passed on, off the delivery loop; a miss changes nothing."""
        if self._acknowledge is None:
            return
        try:
            await self._acknowledge(repository, comment_id)
        except Exception:  # noqa: BLE001 - the sign is best effort; the message went
            log.warning("work-router: could not mark comment %s", comment_id, exc_info=True)

    async def _reread(self, repository: str, number: int) -> None:
        assert self._reread_issue is not None
        try:
            await self._reread_issue(repository, number)
        except Exception:  # noqa: BLE001 - the Work page's own re-read is the backstop
            log.warning("work-router: re-read of %s#%s failed", repository, number, exc_info=True)

    def note_registered(self, repository: str, number: int) -> None:
        """A chat registered this pull request: review it if it already sits ready (D8).

        Its ``opened`` or ``ready_for_review`` event may have arrived before the
        registration, when no Work owned it yet, so registering is itself a reason to look.
        Runs in the background so the registering call is not held up by a GitHub read.
        """
        if not self._enabled or self._ready_head is None or not self._switched(repository):
            return
        check = asyncio.create_task(
            self.check_registered(repository, number), name="work-router-registered"
        )
        self._checks.add(check)
        check.add_done_callback(self._checks.discard)

    async def _live_ready_head(self, repository: str, number: int) -> str:
        """The open, non-draft pull request's head read live; ``""`` if none or unreadable."""
        if self._ready_head is None:
            return ""
        try:
            return (await self._ready_head(repository, number) or "").lower()
        except Exception:  # noqa: BLE001 - the next pull request event looks again
            log.warning("work-router: %s#%s unreadable", repository, number, exc_info=True)
            return ""

    async def check_registered(self, repository: str, number: int) -> bool:
        repository = repository.lower()
        owner = self._owner_of_pull(repository, number)
        if owner is None or self._ready_head is None:
            return False
        try:
            head = (await self._ready_head(repository, number) or "").lower()
        except Exception:  # noqa: BLE001 - the next pull request event looks again
            log.warning("work-router: %s#%s unreadable", repository, number, exc_info=True)
            return False
        if not head:
            return True
        await self._review(owner, Event(REVIEW, repository, number, True, head=head))
        key = f"{repository}#{number}@{head}"
        return key in self.reviewed or f"review:{key}" in self.pending

    def _verdict_pulls(self, event: Event, owner: Owner) -> list[str] | None:
        """The pull requests a verdict is about: the one it was posted on, else the Work's own
        pull requests its text names (``owner/repo#n``, or ``#n`` in its own repository).

        ``None`` when posted on the issue and naming none of them: the Work's one review chat
        covers all its pull requests, so the verdict is about the whole Work. A number that is
        not one of this Work's pull requests (the issue itself, an old one) is not counted.
        """
        if event.is_pull:
            return [f"{event.repository}#{event.number}"]
        named = []
        for match in _REF_RE.finditer(event.text):
            repository = (match["repository"] or event.repository).lower()
            number = int(match["number"])
            placed = self._owner_of_pull(repository, number)
            # The Work decides, not the chat: an issue's owner is its most recent member,
            # a pull request's the chat that registered it.
            if placed is not None and placed[1].casefold() == owner[1].casefold():
                named.append(f"{repository}#{number}")
        return sorted(set(named)) or None

    def _again_refusal(
        self, repository: str, number: int, head: str, *, no_verdict: bool = False
    ) -> str:
        """Why this head may not be reviewed again now, or ``""`` (#3183); raises
        ``AgainBusy`` when a review of it is already asked for."""
        key = f"{repository}#{number}@{head}"
        busy = key in self._in_flight or f"review:{key}" in self.pending
        busy = (
            busy
            or any(item.startswith(f"{key}+") for item in self._in_flight)
            or any(item.startswith(f"review:{key}+") for item in self.pending.entries)
        )
        if busy:
            # Not a refusal to escalate: that review starts on its own (#3453 review).
            raise AgainBusy(
                f"a review of {repository}#{number} at {head[:12]} is already asked for and"
                " starts on its own once the open review chat closes; keep waiting for its"
                " verdict"
            )
        prefix, exclude, limit = _cap(key, no_verdict)
        if self.reviewed.count(prefix, exclude) >= limit:
            return _cap_refusal(repository, number, head, no_verdict)
        return ""

    def _reserve_again(self, event: Event, head: str, again: str) -> str:
        """Take one of the head's ``MAX_AGAIN`` places for this ask, or say why not (#3183).

        The place is held as ``<key>+<id>?`` from now on and written down as pending, so it is
        counted against the cap even by another web worker, and the ask is retried rather than
        lost if its start does not happen now. ``_review`` turns it into the started key.
        """
        key = f"{event.repository}#{event.number}@{head}"
        # The retry first, then the place (operator's choice on #3183): a crash between the two
        # leaves a retry that may run one review past the cap, never a place with no review.
        self.pending.put(
            f"review:{key}+{again}",
            {
                "kind": REVIEW,
                "repository": event.repository,
                "number": event.number,
                "head": head,
                "again": again,
                "note": event.note if again else "",
            },
            self._clock(),
        )
        no_verdict = again.startswith(NO_VERDICT)
        prefix, exclude, limit = _cap(key, no_verdict)
        if not self.reviewed.reserve(prefix, limit, f"{key}+{again}{RESERVED}", exclude):
            self.pending.discard(f"review:{key}+{again}")
            return _cap_refusal(event.repository, event.number, head, no_verdict)
        return ""

    def _drop_pending(self, key: str) -> None:
        """Give up a pending action; an ask's place under the cap goes with it (#3183).

        Its review never started, so the place it held is freed, and a later ask can use it.
        """
        self.pending.discard(key)
        if key.startswith("review:") and "+" in key:
            self.reviewed.discard(key.removeprefix("review:") + RESERVED)

    async def _ready_again(
        self, delivery: Delivery, owner: Owner, repository: str, number: int, head: str
    ) -> None:
        """Answer a ready press at a head a review was already asked for, in four ways (#3584).

        ``reviewed`` only says a review was asked for, not that a verdict came: told "already
        reviewed" while its review still ran or had ended with nothing, workers waited for a
        review that never came, or toggled draft and ready again. In order: the head passed;
        a verdict with findings answered it; its review chat is still open (or unknown: a
        duplicate review costs more than a wait), and the chat waits on its verdict; or that
        chat ended with no verdict, and the review is asked for again as ``no_verdict``. The
        message id is the delivery's, so a replay is absorbed; a replayed ask finds the chat
        it opened open, and waits.
        """
        pull = f"{repository}#{number}"
        at = f"{pull} at {head[:12]}"
        if self._passed_since(owner, repository, number, head, 0.0):
            # The head already passed (#3341): there is nothing to ask again.
            self._send_refusal(delivery, owner, f"{at} already has a review pass")
            return
        if self.reviewed.answered(pull, head):
            self._send_refusal(
                delivery,
                owner,
                f"{at} was reviewed and has findings: fix them and push a new commit (or, if a"
                " person settled them on the issue, POST"
                " $AGENTDECK_URL/api/sessions/<session-key>/review-request with a note saying"
                " what they decided)",
            )
            return
        chat, row = self._review_chat_of(pull, head)
        if row is not None and _review_ended(row, self._clock()):
            try:
                asked = await self.ask_again(
                    repository,
                    number,
                    note="marked ready after the review of this head ended with no verdict",
                    no_verdict=True,
                )
            except AgainBusy:
                # Asked for already and still to start: that review is the one waited on.
                chat, row = "", None
            except AgainRefused as exc:
                self._send_refusal(delivery, owner, str(exc))
                return
            else:
                waits = self._wait_on_review(owner, pull, asked=True)
                self._tell(
                    delivery,
                    owner,
                    f"AgentDeck: marking it ready again asked for a fresh review of {pull} at"
                    f" {asked['head'][:12]}: its review chat {chat} ended with no verdict."
                    f" Its verdict will come to this chat{waits}.",
                )
                return
        waits = self._wait_on_review(owner, pull, asked=False)
        where = f" in {chat}" + (f" ({row.state_label})" if row is not None else "") if chat else ""
        self._send_refusal(
            delivery,
            owner,
            f"a review of {at} is already running{where}, and its verdict will come to this"
            f" chat{waits}",
        )

    def _review_chat_of(self, pull: str, head: str) -> tuple[str, object | None]:
        """The newest review chat about ``pull`` at ``head`` and its board row, if known."""
        if self._review_chat is None:
            return "", None
        try:
            found = self._review_chat(pull, head)
        except Exception:  # noqa: BLE001 - unknown: the review is taken as still running
            log.warning("work-router: could not read %s's review chat", pull, exc_info=True)
            return "", None
        return found if found else ("", None)

    def _wait_on_review(self, owner: Owner, pull: str, *, asked: bool) -> str:
        """Register the chat's wait on ``pull``'s verdict, as ``review-request`` does; the
        words saying so, or ``""`` when none was registered."""
        if self._wait_review is None:
            return ""
        try:
            self._wait_review(owner[0], pull, asked)
        except Exception:  # noqa: BLE001 - the message still says where the review is
            log.warning("work-router: could not register %s's review wait", pull, exc_info=True)
            return ""
        return ", and this chat now waits on it: end the turn on that wait"

    def _send_refusal(self, delivery: Delivery, owner: Owner, why: str) -> None:
        """Tell the chat its draft-and-ready did not ask for a review, and why."""
        self._tell(
            delivery, owner, f"AgentDeck: marking it ready again did not start a review: {why}."
        )

    def _tell(self, delivery: Delivery, owner: Owner, text: str) -> None:
        """Send ``text`` to the chat under the delivery's message id."""
        now = datetime.now(UTC)
        self._deliver(
            ScheduledMessage(
                id=f"work-event:{delivery.delivery_id}",
                chat_id=owner[0],
                text=text,
                due_at=now,
                created_at=now,
            )
        )

    async def ask_again(
        self, repository: str, number: int, note: str = "", *, no_verdict: bool = False
    ) -> dict:
        """Review the pull request's ready head again, even one already reviewed (#3183).

        For the Work's agent when a person settled a finding without a code change, so no
        push will ever start the next review, or, with ``no_verdict``, when the last review
        ended without a verdict for the head (#3453). Raises ``AgainRefused`` with the reason
        when there is no ready head, a review of it is already asked for, or the asks of its
        kind (``MAX_AGAIN``, ``MAX_NO_VERDICT``) were spent on it. Answers ``asked`` or
        ``pending`` (the start is retried on its own).
        """
        repository = repository.lower()
        pull = f"{repository}#{number}"
        owner = self._owner_of_pull(repository, number)
        if owner is None:
            raise AgainRefused(f"no Work's chat registered {pull}")
        if not self._switched(repository):
            raise AgainRefused(f"{repository} is not a repository any poller project declares")
        head = await self._live_ready_head(repository, number)
        if not head:
            raise AgainRefused(
                f"{pull} is not open and ready for review (a draft, closed, or unreadable now):"
                " mark it ready with `gh pr ready` instead"
            )
        key = f"{pull}@{head}"
        why = self._again_refusal(repository, number, head, no_verdict=no_verdict)
        if why:
            raise AgainRefused(why)
        kind = NO_VERDICT if no_verdict else ""
        again = kind + secrets.token_hex(6) if key in self.reviewed else ""
        if again and not note.strip():
            # The note is the only thing that tells the reviewer what changed at a commit it
            # already reviewed; without one the new chat reads as a duplicate (#3344).
            raise AgainRefused(
                f"{pull} at {head[:12]} was already reviewed: say in the note what changed"
                " since (a person's decision, a posted walkthrough)"
            )
        if again:
            asked = Event(REVIEW, repository, number, True, head=head, again=again, note=note)
            why = self._reserve_again(asked, head, again)
            if why:
                raise AgainRefused(why)
        await self._review(
            owner,
            Event(REVIEW, repository, number, True, head=head, again=again, note=note),
            asked=True,
        )
        spent = f"{key}+{again}" if again else key
        return {
            "pull_request": pull,
            "head": head,
            "again": bool(again),
            "outcome": "asked" if spent in self.reviewed else "pending",
        }

    async def _may_instruct(self, event: Event) -> bool:
        """Does the comment's author hold the project's Work tier, as the Poller requires?

        A switched Work's agent acts on what it hears, so a person's words reach it only from a
        login the Manifest's ``[authorization.work]`` admits (#2895 review): on a public
        repository anyone can comment. No port, an unreadable permission or an error is not a
        grant — the comment is dropped and logged, as the Poller refuses an unconfirmed one.
        """
        if self._authorized is None:
            return False
        try:
            allowed = bool(
                await self._authorized(event.repository, event.author, event.association)
            )
        except Exception:  # noqa: BLE001 - an unconfirmed author is not an authorized one
            log.warning("work-router: could not check %s", event.author, exc_info=True)
            allowed = False
        if not allowed:
            log.info(
                "work-router: %s on %s#%s holds no Work tier; not forwarded",
                event.author,
                event.repository,
                event.number,
            )
        return allowed

    def _is_paused(self, owner: Owner) -> bool:
        """Is the Work paused, on this deck or by the label its cached issue carries?

        Read for the owning Work, never from the delivery: a pull request event or a comment
        on the pull request carries no label of the issue's (#2956).
        """
        try:
            return bool(self._paused(_work_id(owner)))
        except Exception:  # noqa: BLE001 -- an unreadable pause holds nothing back
            log.warning("work-router: could not tell whether %s is paused", owner[1], exc_info=True)
            return False

    async def _unpause(self, owner: Owner) -> bool:
        """Resume a paused Work an automatic event reached; whether the event goes on.

        The owner's choice (#2956): news about the Work's own pull requests or its review
        resumes it rather than being dropped or held. A person's own comment needs no resume:
        it reaches a paused Work as their ask (#2944). A resume that fails sends nothing.
        """
        work = _work_id(owner)
        if self._resume is None:
            log.info("work-router: %s is paused; nothing resumes it here", work)
            return False
        try:
            resumed = bool(await self._resume(work))
        except Exception:  # noqa: BLE001 -- not resumed: the event waits for a person
            log.warning("work-router: could not resume %s", work, exc_info=True)
            return False
        log.info("work-router: %s was paused; %s", work, "resumed" if resumed else "not resumed")
        return resumed

    def _send(self, delivery: Delivery, owner: Owner, event: Event) -> bool:
        now = datetime.now(UTC)
        chat_id, _work = owner
        text = event.text
        refused = self._refusal(chat_id, text)
        if refused is not None:
            # The CLI would accept the words and hand its model nothing (#2895 review):
            # the link carries the news without them.
            log.info("work-router: %s cannot carry the comment's text: %s", chat_id, refused)
            text = event.link
        message = ScheduledMessage(
            id=event.message_id or f"work-event:{delivery.delivery_id}",
            chat_id=chat_id,
            text=text,
            due_at=now,
            created_at=now,
        )
        if not self._deliver(message):
            return False
        log.info(
            "work-router: %s %s#%s to %s",
            delivery.event,
            event.repository,
            event.number,
            chat_id,
        )
        return True

    def _deliver(self, message: ScheduledMessage) -> bool:
        """Store the row, or write it down as pending; ``True`` once the row exists."""
        key = f"message:{message.id}"
        try:
            recorded = self._schedule(message)
            # ``None`` is the store absorbing an id it already holds, or refusing the row:
            # only the store itself can say which.
            stored = recorded is not None or self._exists is None or self._exists(message.id)
        except Exception:  # noqa: BLE001 - kept pending and retried, never lost
            log.warning("work-router: storing %s failed", message.id, exc_info=True)
            stored = False
        if stored:
            self.pending.discard(key)
            return True
        self.pending.put(
            key,
            {"kind": MESSAGE, "id": message.id, "chat_id": message.chat_id, "text": message.text},
            self._clock(),
        )
        log.info("work-router: %s not stored; kept pending", message.id)
        return False

    async def _unchanged(self, owner: Owner, pull: str, head: str, *, skip: bool = True) -> str:
        """The digest of ``pull``'s diff at ``head``, or ``""`` once it was not reviewed (#3314).

        A head whose diff has the digest of the newest reviewed head, whose review came back with
        a verdict, reached by commits that touch none of the pull request's files, only merged
        its base: its review would read the
        same change again and could only repeat the verdict, settled findings and all. It is
        written down as reviewed, carried from that head, and the chat is told the earlier
        verdict stands; the release credits that verdict to it (``same_change``). A digest or a
        comparison that could not be read is never called the same.
        """
        if self._digest is None:
            return ""
        try:
            digest = (await self._digest(pull, head) or "").lower()
        except Exception:  # noqa: BLE001 - an unread digest only means it is reviewed
            log.warning("work-router: could not read %s's digest", pull, exc_info=True)
            return ""
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            return ""
        before, known = self.reviewed.last_digest(pull)
        if not skip or not known or known != digest or before == head.lower():
            return digest
        if not self.reviewed.answered(pull, before):
            # Its review has not come back (#3314 review): still running, failed or never
            # started. No verdict stands yet, so the new head is reviewed as any other.
            return digest
        # The digest leaves out where the change sits, so the same lines moved into another
        # function digest the same (#3314 review): only commits that touch none of the pull
        # request's files prove the change itself is what was reviewed.
        repository, _, number = pull.rpartition("#")
        try:
            proven = self._base_only is not None and bool(
                await self._base_only(repository, int(number), before, head.lower())
            )
        except Exception:  # noqa: BLE001 - unproven is reviewed
            log.warning("work-router: could not compare %s's heads", pull, exc_info=True)
            proven = False
        if not proven:
            return digest
        key = f"{pull}@{head}"
        self.reviewed.note_digest(pull, head, digest, carried_from=before)
        now = datetime.now(UTC)
        delivered = self._deliver(
            ScheduledMessage(
                id=f"work-unchanged:{key}",
                chat_id=owner[0],
                text=(
                    f"AgentDeck: {pull} is now at {head[:12]}, but its own diff is the same as at"
                    f" {before[:12]}: only its base branch changed. No new review was started;"
                    " the review verdict on that diff stands. A pass carries over to this head."
                    " Findings about that diff still need an answer: fix"
                    " them and push, or, when a person settled one on the issue, ask for a fresh"
                    " review of this commit with your session's `review-request`, quoting the"
                    " decision as its note."
                ),
                due_at=now,
                created_at=now,
            )
        )
        if delivered and self._on_verdict is not None:
            # No verdict will come for this head, so the chat's wait on one ends here.
            with contextlib.suppress(Exception):
                self._on_verdict(owner[0], [pull])
        self.reviewed.add(key)
        self.pending.discard(f"review:{key}")
        log.info("work-router: %s has %s's diff; not reviewed again", key, before[:12])
        return ""

    async def _no_code_fixed(self, owner: Owner, event: Event, head: str = "") -> bool:
        """Whether ``event``'s head answers a ``no-code`` verdict, which then counts as a pass
        there and no review starts (#3322).

        At the verdict's own head only a ready press says the fix is done (a walkthrough or
        the pull request body changes no file). A later head counts when ``no_code_between``
        proves its commits changed only doc files or merged the base, or the router already
        proved it a base merge away (``same_change``). Anything else is reviewed as usual.
        """
        release = self._release
        if release is None or not hasattr(release, "no_code_head"):
            return False
        pull = f"{event.repository}#{event.number}".lower()
        head = (head or event.head).lower()
        work = _work_id(owner)
        try:
            held = release.no_code_head(work, pull)
        except Exception:  # noqa: BLE001 - unread is reviewed
            log.warning("work-router: could not read %s's no-code verdict", pull, exc_info=True)
            return False
        if held is None or len(head) < 7:
            return False
        old, before = held
        if head.startswith(old) or old.startswith(head):
            if not event.explicit:
                return False
        elif not self.reviewed.same_change(pull, head, old):
            try:
                proven = self._no_code is not None and bool(
                    await self._no_code(event.repository, event.number, old, head, before)
                )
            except Exception:  # noqa: BLE001 - unproven is reviewed
                log.warning("work-router: could not compare %s's heads", pull, exc_info=True)
                proven = False
            if not proven:
                return False
        try:
            answer = str(await release.no_code_fixed(work, pull, head) or "")
        except Exception:  # noqa: BLE001 - not recorded: reviewed as usual
            log.warning("work-router: no-code pass of %s failed", pull, exc_info=True)
            return False
        from .work_release import PASS_RECORDED

        log.info("work-router: %s@%s answers its no-code review: %s", pull, head[:12], answer)
        if not answer.startswith(PASS_RECORDED):
            return False
        key = f"{pull}@{head}"
        if self._digest is not None and not head.startswith(old):
            # So a later base merge of this head is not reviewed either (#3314).
            with contextlib.suppress(Exception):
                digest = (await self._digest(pull, head) or "").lower()
                if re.fullmatch(r"[0-9a-f]{64}", digest):
                    self.reviewed.note_digest(pull, head, digest, carried_from=old)
        self.reviewed.add(key)
        self.pending.discard(f"review:{key}")
        return True

    async def _review(self, owner: Owner, event: Event, *, asked: bool = False) -> None:
        """Start the review of ``event``'s head. ``asked`` is the agent's own request, which
        reviews even a diff an earlier head's review read (#3314)."""
        pull = f"{event.repository}#{event.number}"
        key = f"{pull}@{event.head}" + (f"+{event.again}" if event.again else "")
        pending_key = f"review:{key}"
        if key in self.reviewed:
            self.pending.discard(pending_key)
            return
        if key in self._in_flight:
            return
        # In memory while it runs, so a registration check and an event for the same head
        # never ask twice; on disk only once it returned (see ``ReviewedHeads``).
        self._in_flight.add(key)
        try:
            digest = ""
            if not event.again and not asked and await self._no_code_fixed(owner, event):
                return
            if not event.again:
                digest = await self._unchanged(owner, pull, event.head, skip=not asked)
                if not digest and key in self.reviewed:
                    return
            await self._ask_reviewer(owner, event, pull, key, digest, asked=asked)
        finally:
            self._in_flight.discard(key)

    async def _ask_reviewer(
        self, owner: Owner, event: Event, pull: str, key: str, digest: str, *, asked: bool
    ) -> None:
        pending_key = f"review:{key}"
        _chat, work = owner
        retry = {
            "kind": REVIEW,
            "repository": event.repository,
            "number": event.number,
            "head": event.head,
        }
        if asked:
            retry["asked"] = True
        asks: dict = {}
        if event.again:
            retry.update(again=event.again, note=event.note)
            asks = {"note": event.note, "again": True}
        try:
            outcome = await self._start_review(pull, work, **asks)
        except ReviewBusy:
            # Somebody is mid-review of this Work's chat and nothing was pressed, so the
            # head is not spent: it is asked for again after the back-off.
            self.pending.put(pending_key, retry, self._clock())
            log.info("work-router: review of %s is busy; kept pending", key)
        except ReviewError as exc:
            # Refused before anything was written (``open_review`` finds the open chat
            # before it writes one), so asking again later is safe.
            self.pending.put(pending_key, retry, self._clock())
            log.warning("work-router: review of %s refused: %s; kept pending", key, exc)
        except Exception:  # noqa: BLE001 - one review that could not start must not stop routing
            # It may have written and pressed: spent, as an uncertain start always is.
            self.reviewed.add(key)
            self.reviewed.discard(key + RESERVED)
            self.pending.discard(pending_key)
            log.warning("work-router: review of %s could not start", key, exc_info=True)
        else:
            if not covers(outcome, pull, event.head):
                # The open chat is still about an older commit, held `in-progress` by a
                # worker or otherwise busy, so it was kept rather than replaced: this head
                # was not asked about yet, and is once the worker lets that chat go.
                self.pending.put(pending_key, retry, self._clock())
                log.info("work-router: %s left an older chat open; kept pending", key)
                return
            self.reviewed.note_digest(pull, event.head, digest)
            self.reviewed.add(key)
            self.reviewed.discard(key + RESERVED)
            self.pending.discard(pending_key)
            log.info("work-router: review of %s asked for (Work %s)", key, work)


class AgainRefused(ValueError):
    """A request to review a head again that the router turns down, with the reason."""


class AgainBusy(AgainRefused):
    """A review of the head is already asked for and still to start or finish: the asker
    keeps waiting rather than going to a person (#3453 review)."""


def _review_ended(row, now: float) -> bool:
    """Whether a review chat's board row says it ended with no verdict still to come (#3584).

    Closed, or labelled ``blocked`` while open, at least ``STOP_SETTLE_S`` ago: a verdict a
    reviewer posted as it closed may still be on its way to the release that far. Anything
    else, an undated row included, is a review still running.
    """
    if row.state == "closed":
        age = row.closed_seconds_ago(now=now)
    elif "blocked" in row.labels:
        age = row.idle_for(now=now)
    else:
        return False
    return age is not None and age >= STOP_SETTLE_S


def _cap(key: str, no_verdict: bool) -> tuple[str, str, int]:
    """``(prefix, exclude, limit)`` of the asks again of ``key`` of one kind (#3453)."""
    if no_verdict:
        return f"{key}+{NO_VERDICT}", "", MAX_NO_VERDICT
    return f"{key}+", f"{key}+{NO_VERDICT}", MAX_AGAIN


def _cap_refusal(repository: str, number: int, head: str, no_verdict: bool) -> str:
    if no_verdict:
        return (
            f"{repository}#{number} at {head[:12]} was already reviewed again {MAX_NO_VERDICT}"
            " times after a review with no verdict. Ask a person in your question box what to"
            " do, rather than asking the reviewer once more"
        )
    return (
        f"{repository}#{number} at {head[:12]} was already reviewed again {MAX_AGAIN}"
        " times. Ask a person in your question box how to settle the finding, rather"
        " than asking the reviewer once more"
    )


def _work_id(owner: Owner) -> str:
    """The work id of an owner's ``owner/repo#n`` Work."""
    return f"github_issue:{owner[1].lower()}"


def _start_is_secured(outcome: object) -> bool:
    """Whether a Work start is spent/successful rather than safe and necessary to retry."""
    return outcome in {"started", "already started", "uncertain"}


def _start_is_retryable(outcome: object) -> bool:
    """Whether a Work start was turned down before anything ran, so trying again is safe
    (#2971): refused by the account, no account or checkout yet, or not recorded."""
    return isinstance(outcome, str) and not _start_is_secured(outcome)


def covers(outcome: object, pull: str, head: str) -> bool:
    """Whether the review chat ``start_review`` answered with asks about ``head``.

    ``open_review`` keeps an open chat a worker holds even when it names an older commit,
    and answers with that chat's own commits (``heads``), so a normal return is not proof
    the new head was asked about. A kept chat that names no commit covers none either; only
    a caller that answers with no review outcome at all is taken at its word.
    """
    heads = getattr(outcome, "heads", None)
    if heads is None:
        return True
    wanted = pull.casefold()
    head = head.lower()
    return any(
        str(ref).casefold() == wanted
        and bool(sha)
        and (str(sha).lower().startswith(head) or head.startswith(str(sha).lower()))
        for ref, sha in heads
    )


# --- who owns an issue or a pull request, from what the deck already knows ---


def _latest_chat(state, chat_ids: Iterable[str]) -> str | None:
    """The most recently active of these chats, by its working leg's last activity."""
    best: tuple[datetime, str] | None = None
    floor = datetime.min.replace(tzinfo=UTC)
    for chat_id in dict.fromkeys(chat_ids):
        chat = state.chat_by_id(chat_id)
        session = state.current_session_for_chat(chat) if chat is not None else None
        if session is None:
            session = state.sessions.get(chat_id)
        stamp = getattr(session, "last_activity", None) or floor
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        if best is None or stamp > best[0]:
            best = (stamp, chat_id)
    return best[1] if best else None


def owner_of_issue(state, repository: str, number: int, projection=None) -> Owner | None:
    """The chat that owns the Work for ``repository#number``: its most recent direct member.

    ``projection`` is a caller's already-built Work grouping. The Work overview passes its
    pass snapshot's: asking ``state`` again once per card rebuilt the projection's cache
    key over every session each time, which was a sixth of the web process's busy time on a
    deck with 2,000 sessions (#3010).
    """
    from .work import WorkRef

    projection = projection if projection is not None else state.work_projection()
    work = projection.get(WorkRef(repository.lower(), number))
    if work is None:
        return None
    chat = _latest_chat(state, (m.chat_id for m in work.memberships if m.direct))
    return (chat, f"{work.ref.repository}#{work.ref.number}") if chat else None


def owner_of_pull(state, repository: str, number: int) -> Owner | None:
    """The chat that registered this pull request, and the one Work that chat joined.

    ``None`` when no chat registered it, or the chat joined no Work or several: a review
    the router cannot place in exactly one Work is left to whoever asks for it by hand.
    """
    wanted = (repository.lower(), number)
    legs = {
        leg: ref.work_id
        for leg, refs in state.session_pull_requests.items()
        for ref in refs
        if ref.identity == wanted
    }
    return _place_pull(state, legs, state.work_projection)


def _place_pull(state, legs: dict[str, str], projection) -> Owner | None:
    """``owner_of_pull``'s rule for the legs that registered one pull request.

    ``legs`` maps each leg to the Work its registration named, "" for none. The Work the
    chosen chat's registration named wins while the chat is still in it (#3392); otherwise
    the one Work the chat joined. ``projection`` is called only once a chat is found, and
    the caller may hand the same projection to every pull request it places.
    """
    chat = _latest_chat(state, (state.chat_identity_key(leg) for leg in legs))
    if chat is None:
        return None
    from .work import WorkRef

    works = projection().works_chat_joined(chat)
    named = {
        (ref.repository, ref.number)
        for leg, work_id in legs.items()
        if state.chat_identity_key(leg) == chat
        and (ref := WorkRef.from_work_id(work_id)) is not None
    }
    placed = [work for work in works if (work.repository.lower(), work.number) in named]
    if len(placed) == 1:
        works = placed
    if len(works) != 1:
        return None
    return chat, f"{works[0].repository}#{works[0].number}"


def work_pulls(state, work_id: str) -> tuple[str, ...]:
    """Every pull request registered for this Work, as ``owner_of_pull`` places it.

    The release's set: a pull request counts for the Work its registering chat joined, never
    by what its body says. Lowercased ``owner/repo#n``.

    One walk of the registrations and one Work projection for the whole deck (#2954): asking
    ``owner_of_pull`` for each of thousands of registered pull requests walked them all again
    and rebuilt the projection's key every time, and the release asks every couple of seconds
    while a job runs, holding the event loop for seconds each time.
    """
    wanted = work_id.partition(":")[2].lower()
    legs_by_pull: dict[tuple[str, int], dict[str, str]] = {}
    for leg, refs in tuple(state.session_pull_requests.items()):
        for ref in refs:
            legs_by_pull.setdefault(ref.identity, {}).setdefault(leg, ref.work_id)
    projected = None

    def projection():
        nonlocal projected
        if projected is None:
            projected = state.work_projection()
        return projected

    found: set[str] = set()
    for (repository, number), legs in legs_by_pull.items():
        owner = _place_pull(state, legs, projection)
        if owner is not None and owner[1].lower() == wanted:
            found.add(f"{repository}#{number}")
    return tuple(sorted(found))


def work_busy(state, work_id: str) -> bool:
    """Is a chat of this Work mid-turn (working or stalled), read from the live sessions?

    The release's hold while an agent may still push (#2481, #2895): never the Work
    overview's snapshot, which nothing rebuilds while no dashboard watches. Working is the
    Work overview's own ``_leg_working`` (a turn or a helper agent, not a background command
    left running), and a stalled turn counts too: it may wake and push.
    """
    from .work import WorkRef
    from .work_overview import _leg_working

    ref = WorkRef.from_work_id(work_id)
    work = state.work_projection().get(ref) if ref is not None else None
    for membership in work.memberships if work is not None else ():
        chat = state.chat_by_id(membership.chat_id)
        session = state.current_session_for_chat(chat) if chat is not None else None
        session = session or state.sessions.get(membership.chat_id)
        if session is not None and (_leg_working(session) or session.stalled):
            return True
    return False
