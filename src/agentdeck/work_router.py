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
* **An issue's last blocker closed** (#3764, #4216): an issue's close, under any login, or a
  person removing a blocker. Nothing is sent: every open issue it blocked that is left with no
  open blocker, read live, starts — one with no start label gets the Start button's own write
  (assigned where another deck takes unassigned issues, then the start label), one already
  labelled, which waited, starts its Work — so nobody has to remember to start it. Any issue,
  not only a wayfinder ticket: a map's build issues are filed blocked and unlabelled too. A
  labelled issue with an open blocker waits for it rather than starting (owner's choice):
  every Work start, its retries included, goes through ``work_signals.start_unless_blocked``.
* **A blocker's pull request merged** (#4398): a pull request a Work registered merged into
  its repository's default branch. The merge is recorded, its Work counts as done for every
  blocker check, and the issues it blocks start as on its close; a reopen of the Work's issue
  drops the record.
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
  told the verdict on that diff stands, and the release keeps it for the new head. Before
  each such review the chat gets the pre-review checklist instead (#3880), and the review
  waits until the agent says it went through it (``pre_review_done``), with no time limit
  (owner's choice). A push while the checklist waits sends nothing more; the review reads
  the head current when the agent is done. Marking it ready again at a new head sends that
  head's checklist (#3896). A review asked for again skips it.
* **A review asked for again at an unchanged head** (#3183) — the agent's ``POST
  /api/sessions/<key>/review-request`` (``ask_again``) with a note saying what changed, such as
  a walkthrough the reviewer asked for, and nothing else could ever start the next review. A
  finding the owner accepted needs none (#3828): ``review-accepted`` (``accept``) counts the
  review of that head as a pass. Each asks once more, at most ``MAX_AGAIN`` times per head;
  past that a person decides, and the chat is told so. A pull request turned back to draft and
  marked ready again at a head already reviewed no longer asks (#3344): it carries no reason,
  and every chat it opened read to the reviewer as a plain duplicate. The chat is told what is
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
* **Two web workers may both take a head's last ask again.** The caps count the ``asked``
  events in the head's review history, and a start in flight in the other worker of a hot
  reload is not one yet.
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
import base64
import contextlib
import json
import logging
import re
import secrets
import time
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Iterable,
    Iterator,
    Mapping,
)
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import (
    code_lines,
    deck_notices,
    design_review,
    review_history,
    review_paperwork,
    wayfinder_lists,
    wayfinder_map,
)
from .poller_engine import github_authority
from .poller_engine.authorization import reviewer_logins
from .providers.instructions import PRE_REVIEW_HOW
from .review_refs import REVIEWER_OFF_TEXT, ReviewBusy, ReviewError, ReviewOff
from .scheduled_messages import ScheduledMessage
from .shared_file import SharedFile
from .wake_on import STOP_SETTLE_S
from .watch_retry import Backoff
from .webhook_tailer import Delivery, WebhookTailer, WebhookWatcher
from .work import WorkRef
from .work_claims import assignee_of
from .work_release import (
    ACCEPTED_ASK,
    MINOR,
    MINOR_LINES,
    NO_CODE,
    OWNER_CALL_ASK,
    SHIP_ASK,
    SIBLING_SWEEP_ASK,
    _match_in,
    findings_target,
    heading_targets,
    same_commit,
)
from .work_signals import BLOCKED, blocker_done
from .work_signals import MARKER as START_MARKER

log = logging.getLogger(__name__)

CONSUMER = "work-router"
PENDING_FILENAME = "work-router-pending.json"
#: The Works whose issue went to another deck, as ``handed-over:<work id>`` (#3731).
HANDED_OVER_FILENAME = "work-router-handed-over.json"
#: The router's own review records before each head's review history replaced them (#3955):
#: deleted at start, with nothing copied over.
RETIRED_FILENAMES = ("work-router-reviews.json", "work-router-pre-review.json")
#: Where the reviewer's own rules live inside its repository, linked from the checklist (#3880).
REVIEW_POLICY_PATH = "blob/main/skills/github-repo-reviewer/references/review-policy.md"
#: What the agent reads before each review of a ready head (#3880): the points it most often
#: forgot, which cost a whole review round each. Short on purpose (owner's choice: five items).
PRE_REVIEW_TEXT = """AgentDeck: before {pull} at {short} goes to review, check these.\
 The reviewer checks each one and sends the pull request back if it is missing. The deck\
 itself refuses `pre-review-done`, naming the gap, while the body lacks `Refs #<issue>`, the\
 issue lacks the `Use-case walkthrough:` line (before the first review, the `Helper checks:`\
 line), a new diagram, or one whose title, Mermaid block or colours changed, lacks its issue\
 comment (an edit to its introduction, a plain note under the block or `## Boxes` notes\
 alone needs none), a pull request of {big:,} changed lines or more lacks a `Reading guide:`\
 section in its body, or the issue's newest `**Design:**` comment has no passing design review.
{helpers}
1. No diagram work unless the owner asked for a diagram: then the issue's diagram comment\
 shows it. The reviewer does not read diagrams.
2. The issue has the `Use-case walkthrough:` line, and for a visible change the screenshots\
 with the `Ease of use and look:` line.
3. Post one comment on the issue mapping each spec decision, each owner answer and the\
 owner's latest comment to the `file:line` that does it, or "not done: <why>".
4. The tests cover the other cases too: events in another order or shape, two at once, a\
 restart, and for a page a live update and a 390px phone.
5. The Work summary is written whole (its second and last write, `agentdeck bookkeep\
 --summary`), and the body says `Refs #<issue>` (`Closes` only if the\
 issue asks).{grown}

Fix and push what is missing, keeping it ready, then call `POST\
 $AGENTDECK_URL/api/sessions/<session-key>/pre-review-done` with\
 `{{"pull_request": "{pull}"}}`: the review of its head then starts, and this chat waits on\
 it. The review waits until you call it, and reads the head current then, even after more\
 pushes or a draft and ready again.{policy}

{how}"""
#: Above a head's checklist when the push after a ``minor`` or ``no-code`` verdict did not carry
#: that verdict's pass (#4575): store#4161's agent took the pass as carried and told the owner
#: to press Merge, while the deck waited nine hours on a ``pre-review-done`` it never sent.
NOT_SMALL_TEXT = """AgentDeck: your push {short} to {pull} is not the small fix the {verdict}\
 review at {old} allows: {reason}. So that review does not count as a pass here, and\
 {pull} will not merge on it. {short} needs a review of its own, which starts only once you\
 go through {checklist} and call `pre-review-done`. Until then the Work and the review check\
 show it waiting on you.

"""
#: After a design gap the deck's own review start met (#4332).
_DESIGN_GAP_NEXT = " Then ask for the review with `review-request`."
#: The checklist's extra point when review fixes grew the diff past ``GROWTH_PERCENT`` of the
#: previous round's (#4220, owner's choice: an agent note, not a question or a reviewer brief).
GROWN_TEXT = """
6. Since the previous review round this pull request grew from {before} to {after} changed\
 lines (+{percent}%). Before fixing more, post a short note on the issue: is this still the\
 right design, and which remaining findings could be left to a follow-up issue instead?"""
#: The checklist's first point before a pull request's first review (#4437): three Haiku
#: helpers check the change and the coder fixes what is
#: real. Shown, and its comment checked by ``pre-review-done``, only while no review of the
#: pull request was asked for yet. The Work brief asks to run them at the first push (#4567):
#: a comment for an earlier head stands when the pushes since only fixed its finds, a
#: judgement no script makes, so the coder is told when to run them again. Once a review was
#: asked for, the command itself refuses to run (#4726, ``pre_review_helpers.review_asked``).
HELPERS_TEXT = """
Before these, as this pull request has had no review yet, run the helper checks: queue\
 `~/agentdeck/.venv/bin/agentdeck pre-review-helpers --cwd . --pull {pull}{issue}` as an\
 `agentdeck job` run in the checkout (how: below). Fix each real find and push; then post one\
 issue comment opening `Helper checks: {pull}` that lists each find as fixed, or dropped with a\
 one-line reason, or the `Helper checks: {pull} did not run (<why>)` line the command prints.\
 Already ran them on an earlier head and posted that comment? It stands when every push since\
 changed only what its finds asked for; otherwise, or when it says `did not run`, run them again\
 on this head.
"""
#: Growth over this percent of the previous round's changed lines (added plus removed, the
#: whole diff) adds ``GROWN_TEXT`` (#4220, owner's choice: 15%, against the previous round).
GROWTH_PERCENT = 15
#: Growth under this many lines never does, so a small pull request is left alone (#4220,
#: assumed: drop it if small pull requests turn out to grow off course too).
GROWTH_FLOOR = 100
#: A pending action's back-off (``watch_retry.Backoff``): its first wait and its ceiling.
RETRY_FIRST_S = 60.0
RETRY_MAX_S = 30 * 60.0
#: A pending action that has not happened within this long is dropped with a warning: the
#: person or the worker will have asked again some other way by then.
PENDING_MAX_AGE_S = 24 * 3600.0
#: The pending kinds of the published label (``work_published``, #4794): never aged out.
PUBLISHED_KINDS = frozenset({"published", "published-pass"})
#: How many times one unchanged head may be reviewed again on request (#3183); past that, the
#: point is a person's to settle, not another reviewer run's.
MAX_AGAIN = 2
#: How many times one head may be reviewed again because its review ended with no verdict
#: (#3453), counted apart from ``MAX_AGAIN``; past that the owner is asked.
MAX_NO_VERDICT = 2
#: Starts the request id of an ask after no verdict. Request ids are otherwise hex, so this
#: prefix tells the two kinds of ask apart.
NO_VERDICT = "nv"
#: The pull requests a timed-out review wait on a Work's issue reads back, newest ask first, and
#: how long before a head's first ask a verdict about it may be dated (#4478).
RECOVER_PULLS = 2
RECOVER_SLACK_S = 300.0

#: How long a ``gh pr ready`` waits for the checklist it starts, to print it in its reply
#: (#4253, assumed): well inside the shim's own wait for the deck's answer.
READY_PRESS_S = 4.0
#: A ready head with no checklist, no review and no verdict for this long is caught by the
#: backstop (#3903): long enough for a ready event and the router to act on it first.
READY_GRACE_S = 5 * 60.0
#: The backstop looks at most this often, however many deliveries wake it.
SWEEP_GAP_S = 60.0

#: A message is a nudge with a link, not a copy of the thread: a long comment is cut here.
MAX_BODY = 4000

MESSAGE = "message"
REVIEW = "review"
#: A Work start refused before anything ran (#2971), kept pending as ``start:<work id>``.
START = "start"
#: A Work's issue closed by someone else (#3188), held as ``close:<work id>`` for a merge.
CLOSE = "close"
#: A design review waiting for a reviewer place (#4342), kept as ``design:<owner/repo#n>`` and
#: tried again like a code review that found every place taken.
DESIGN = "design"
#: How much of ChatGPT's design verdict comment the entry keeps for telling the Work (#4722).
DESIGN_VERDICT_CHARS = 8000
#: How long a close waits for a merge of the Work's pull request, the usual closer, to show.
CLOSE_HOLD_S = RETRY_FIRST_S
#: A closing comment is quoted when its closer posted it this close to the close, cut here.
CLOSE_COMMENT_S = 60.0
CLOSE_QUOTE = 300

#: ``pull_request`` actions after which the pull request may sit ready at a new head.
_READY_ACTIONS = frozenset({"ready_for_review", "synchronize", "opened", "reopened"})
_VERDICT_RE = re.compile(r"^\s*/(?:reviewed|claude)\b", re.IGNORECASE)
#: The GitHub App ChatGPT's own connector posts through, under the login of whoever's ChatGPT
#: it is rather than the reviewer's (#4152): a comment's ``performed_via_github_app``.
REVIEWER_CONNECTOR = "chatgpt-codex-connector"
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
#: A commit id a reviewer's findings or escalation names (#3828 review): 7 to 40 hex digits.
_SHA_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{7,40}(?![0-9A-Za-z])")
#: A link to one comment, as a reviewer's closing line gives the findings it posted (#4279).
_COMMENT_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(issues|pull)/(\d+)#issuecomment-(\d+)"
)
_RELEASE_VERDICT_RE = re.compile(r"^\s*/reviewed\b", re.IGNORECASE)
_DECK_REVIEW_RE = re.compile(r"^\s*/(?:claude\s+Review\b|reviewed\b)", re.IGNORECASE)
#: The worker command on a line of its own anywhere in a comment.
_CLAUDE_LINE_RE = re.compile(r"^\s*/claude\b", re.IGNORECASE | re.MULTILINE)
#: A reviewer handing the review to the owner instead of passing it or sending it back (#3216),
#: as its first line: "Review loop escalated to the operator", since #4069 going on to name the
#: pull request and commit it is for. ChatGPT posts it through its own
#: GitHub connector, so under the owner's login rather than the reviewer's.
_ESCALATED_RE = re.compile(
    r"^\s*(?:/claude\s+)?review(?:\s+loop)?\s+(?:was\s+)?escalated\b", re.IGNORECASE
)
#: What the agent does with an escalation, appended to its message (#3216).
ESCALATED_ASK = (
    "The reviewer will not review this commit again by itself, so stop waiting on the review. "
    "Ask the owner in your question box: the reviewer's question, with its options as the "
    "choices. Once they answer, post their decision on the issue and act on it. "
    + ACCEPTED_ASK
    + " "
    + SHIP_ASK
)


def _headed_findings(body: str, repository: str) -> bool:
    """Findings that open with their heading (``Review findings for PR <owner/repo>#<n> at
    <sha>``) and carry ``/claude`` on a later line (#4279). The reviewer is asked to make that
    heading the first line (#4208), so ``/claude`` moved below it, and a comment of that shape
    reached no chat and marked no head: the Work then refused its owner's Ship now for good."""
    return bool(findings_target(body, repository)) and bool(_CLAUDE_LINE_RE.search(body))


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
    #: verdict is not one: the release ends them once it has taken the verdict (#3183 review)
    #: or told the chat it refused it (#4124), so a stale head leaves the wait to its timeout.
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


def _label_name(label: object) -> str:
    """A label's name, from a webhook's or REST's label object or a bare name."""
    return _text(label.get("name") if isinstance(label, dict) else label).strip()


def _open_blockers(rows: Iterable[dict], deployed: Collection[str] = ()) -> list[str]:
    """The dependency rows still blocking, as ``#n``, for the log: not closed, and not a
    Work deployed and waiting only for a person's try (``blocker_done``, #4384)."""
    return [
        f"#{row.get('number')}"
        for row in rows
        if isinstance(row, dict) and not blocker_done(row, deployed)
    ]


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


#: Under a reviewer's findings comment (#3314, #4497 review): the sibling sweep and the
#: shared-cause ask ride with the owner-call rule here too, since a project whose reviewer
#: posts on GitHub hands its findings to the agent only by this message.
FINDINGS_REPLY = f"{SIBLING_SWEEP_ASK} {OWNER_CALL_ASK}"


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


#: A review chat's labels that say its reviewer stopped with no verdict (#4422).
_STOP_LABELS = frozenset({"blocked", "needs-more-info"})
#: How much of a stopped review chat's reason is kept.
_CHAT_SAID_CHARS = 4000


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
        elif login.casefold() in reviewer_logins() and (
            _VERDICT_RE.match(body) or _headed_findings(body, repository)
        ):
            headline = f"AgentDeck event: the reviewer posted on {where}"
            verdict = findings
            reply = FINDINGS_REPLY if findings else ""
        elif login.casefold() in own_logins and (
            _DECK_REVIEW_RE.match(body) or _headed_findings(body, repository)
        ):
            headline = f"AgentDeck event: the deck's reviewer posted on {where}"
            verdict = findings
            reply = FINDINGS_REPLY if findings else ""
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


async def late_read(
    repository: str, number: int, old: str, live: str
) -> tuple[bool, set[str] | None]:
    """For a verdict about ``old`` that came after the fix push to ``live`` (#4474): whether
    ``live`` descends from ``old``, and the pull request's files at ``old``, renames under both
    names (``None`` when unread or cut short at 300 files). The files are read from the
    pull request's base to ``old``, the compare GitHub's own diff makes: the files now already
    hold the fix push, and a code file it added must not count as reviewed."""
    if await compared_files(repository, old, live) is None:
        return False, None
    base = await _gh_lines(
        repository, ["gh", "api", f"repos/{repository}/pulls/{number}", "--jq", ".base.sha"]
    )
    sha = (base or [""])[0].strip()
    if not _SHA_RE.fullmatch(sha):
        return True, None
    names = "[.files[] | .filename, (.previous_filename // empty)]"
    compared = await _gh_lines(
        repository,
        [
            "gh",
            "api",
            f"repos/{repository}/compare/{sha}...{old}",
            "--jq",
            f'"\\(.files | length)", {names}[]',
        ],
    )
    count = (compared or [""])[0]
    if not count.isdigit() or int(count) >= 300:
        return True, None
    return True, set(compared[1:])


async def compared_changes(
    repository: str, old: str, new: str
) -> tuple[dict[str, int], frozenset[str]] | None:
    """Lines added plus removed per file the commits from ``old`` to ``new`` touch, and both
    names of the files they rename; ``None`` when unread, cut short by GitHub (300 files)
    or ``new`` does not descend: the size of a fix after a ``minor`` verdict (#4474). A pure
    rename has no lines, so its names are reported apart rather than as a size: the new name
    too, as a file moved onto a path of the diff brings its whole content (#4594 review)."""
    rows = (
        '(.files[] | "\\(.additions + .deletions) \\(.filename)",'
        ' (select(.previous_filename) | "renamed \\(.previous_filename)", "renamed \\(.filename)"))'
    )
    compared = await _gh_lines(
        repository,
        [
            "gh",
            "api",
            f"repos/{repository}/compare/{old}...{new}",
            "--jq",
            f'"\\(.status) \\(.files | length)", {rows}',
        ],
    )
    status, _, count = (compared or [""])[0].partition(" ")
    if status != "ahead" or not count.isdigit() or int(count) >= 300:
        return None
    changes: dict[str, int] = {}
    renamed: set[str] = set()
    for line in compared[1:]:
        size, _, name = line.partition(" ")
        if not name:
            return None
        if size == "renamed":
            renamed.add(name)
        elif size.isdigit():
            changes[name] = changes.get(name, 0) + int(size)
        else:
            return None
    return changes, frozenset(renamed)


async def _diff_lines(
    repository: str, base: str, head: str
) -> tuple[str, dict[str, list[str] | None]] | None:
    """The commit where ``head`` left ``base``, and the added and removed lines, in order, of
    each file ``head`` changes since (GitHub's three-dot compare: a pull request's own diff
    at that head); a file whose patch GitHub left out (binary or too large) is ``None``.
    ``None`` when unread, cut short at 300 files, or ``head`` is no longer ahead of
    ``base``."""
    compared = await _gh_lines(
        repository,
        [
            "gh",
            "api",
            f"repos/{repository}/compare/{base}...{head}",
            "--jq",
            '"\\(.status) \\(.files | length) \\(.merge_base_commit.sha)",'
            " (.files[] | [.filename, .patch] | @json | @base64)",
        ],
    )
    status, count, left = ((compared or [""])[0].split(" ") + ["", ""])[:3]
    # A head the base already holds (merged since) has no diff left to read: unread.
    if status not in ("ahead", "diverged") or not count.isdigit() or int(count) >= 300:
        return None
    if not _SHA.fullmatch(left):
        return None
    patches: dict[str, list[str] | None] = {}
    for line in compared[1:]:
        # Base64, as a patch may hold a character the line split above cuts at (U+2028).
        try:
            name, patch = json.loads(base64.b64decode(line, validate=True))
        except ValueError:
            return None
        if not isinstance(name, str):
            return None
        patches[name] = (
            [row for row in patch.split("\n") if row[:1] in ("+", "-")]
            if isinstance(patch, str)
            else None
        )
    return left, patches


_SHA = re.compile(r"[0-9a-f]{40}")


async def own_changes(
    repository: str, number: int, old: str, new: str, sizes: Mapping[str, int]
) -> int | None:
    """How many lines the fix itself changed from ``old`` to ``new`` in the files of
    ``sizes`` (each with its plain count of lines changed between the two heads), leaving
    out what a base merge brought into them (#4743). Per file, the larger of two counts:

    - the pull request's own diff against the base at each head, its added and removed
      lines compared in order: a line the fix adds, drops or rewrites counts, and so does
      what a merge's conflict resolution carries;
    - the plain count less the lines the base itself changed in that file between the two
      commits where the heads left it. The first count alone reads a block of the pull
      request's own lines moved past unchanged code as nothing, as its lines keep their
      order (#4744 review); here it is the block removed and added.

    ``None`` when anything is unread (a file whose patch GitHub left out included): the
    caller then keeps the plain count, which is never smaller for want of a read."""
    import difflib

    base = await _gh_lines(
        repository, ["gh", "api", f"repos/{repository}/pulls/{number}", "--jq", ".base.ref"]
    )
    if not base or not base[0].strip():
        return None
    ref = base[0].strip()
    read_was = await _diff_lines(repository, ref, old)
    read_now = await _diff_lines(repository, ref, new) if read_was is not None else None
    if read_was is None or read_now is None:
        return None
    (left_was, was), (left_now, now) = read_was, read_now
    merged: Mapping[str, int] = {}
    if left_was != left_now:
        # What the base changed between the two commits the heads left it at. A file it
        # renamed is listed under its new name, so nothing is taken off the old one.
        base_changes = await compared_changes(repository, left_was, left_now)
        if base_changes is None:
            return None
        merged = base_changes[0]
    lines = 0
    for name, plain in sizes.items():
        # A file absent from a diff has no line of its own there; one without a patch is unread.
        before, after = was.get(name, []), now.get(name, [])
        if before is None or after is None:
            return None
        same = sum(
            block.size
            for block in difflib.SequenceMatcher(
                None, before, after, autojunk=False
            ).get_matching_blocks()
        )
        lines += max(len(before) + len(after) - 2 * same, plain - merged.get(name, 0))
    return lines


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


#: How many base merges in a row ``ship_merge_between`` follows back to the shipped head.
SHIP_MERGE_STEPS = 10


async def ship_merge_between(repository: str, number: int, old: str, new: str) -> bool:
    """Whether ``new`` only merged the base into ``old``, a Ship now pass's head (#4576): the
    commits from ``new`` back to ``old`` along their first parents are all merge commits, and
    the lines the pull request contributes to each file that is not a doc are the same at both
    heads. The base merge that cleared a conflict touches the pull request's own files, so
    ``base_only_between`` cannot prove it; this proof keeps the owner's Ship now pass across it.

    The diffs are GitHub's comparisons of the pull request's base branch with each head, read
    with ``_contributed_diff``'s rule, file by file: the lines added and removed, in order,
    without the context around them or where they sit, the rule a digest carries any pass by.
    Anything unread, a commit that is not a merge, a chain longer than
    :data:`SHIP_MERGE_STEPS`, a diff GitHub refuses or cuts at 300 files is ``False``: unproven
    is reviewed.
    """
    old, new = old.lower(), new.lower()
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (old, new)) or old == new:
        return False
    current = new
    for _ in range(SHIP_MERGE_STEPS):
        parents = await _gh_lines(
            repository,
            # The git object only: ``commits/{sha}`` also builds a merge's whole diff.
            ["gh", "api", f"repos/{repository}/git/commits/{current}", "--jq", ".parents[].sha"],
        )
        if not parents or len(parents) < 2:
            return False  # unread, or a commit of its own: not only a base merge
        current = parents[0].strip().lower()
        if current == old:
            break
    else:
        return False
    base = await _gh_lines(
        repository, ["gh", "api", f"repos/{repository}/pulls/{number}", "--jq", ".base.ref"]
    )
    ref = (base or [""])[0].strip()
    if not ref:
        return False
    before = await _code_diffs(repository, ref, old)
    if before is None:
        return False
    after = await _code_diffs(repository, ref, new)
    return after is not None and after == before


#: How many base commits new to the branch ``same_files_merge`` reads.
SAME_FILES_COMMITS = 10


async def same_files_merge(repository: str, number: int, old: str, new: str) -> bool:
    """Whether ``new`` is one merge of the base into ``old`` that left every file as it was
    (#4635): the proof a person's Merge press stands across it, though the pull request's
    diff shrank because a pull request it was built on merged first.

    The same tree is not enough: a merge that threw the base's changes away (``-s ours``)
    keeps it too, and would land a revert nobody saw. So no base change may be undone either.
    The merged base commit must be on the base branch, and no base commit new to ``old`` may
    itself change a file ``new`` holds differently from the base. A merge commit changes only
    what it took from none of its parents. Reading each commit, not one merge base, matters:
    a stack has several, and an old one can hide a revert the base made since. Anything
    unread, cut short or not plainly so is ``False``: a new press is needed.
    """
    old, new = old.lower(), new.lower()
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (old, new)) or old == new:
        return False

    async def commit(sha: str) -> list[str]:
        argv = ["gh", "api", f"repos/{repository}/git/commits/{sha}"]
        lines = await _gh_lines(repository, [*argv, "--jq", ".tree.sha, .parents[].sha"])
        return [line.strip().lower() for line in lines or ()]

    merged, before = await commit(new), await commit(old)
    if len(merged) != 3 or not before or merged[0] != before[0] or merged[1] != old:
        return False  # unread, other files, or not one merge of two commits on top of ``old``
    base = merged[2]
    pull = ["gh", "api", f"repos/{repository}/pulls/{number}", "--jq", ".base.ref"]
    ref = ((await _gh_lines(repository, pull)) or [""])[0].strip()
    if not ref:
        return False
    on_base = await _gh_lines(
        repository,
        ["gh", "api", f"repos/{repository}/compare/{ref}...{base}", "--jq", ".ahead_by"],
    )
    if [line.strip() for line in on_base or ()] != ["0"]:
        return False  # not a commit of the base branch
    differs = await compared_files(repository, base, new)
    if differs is None:
        return False
    rows = await _gh_lines(
        repository,
        [
            "gh",
            "api",
            f"repos/{repository}/compare/{old}...{base}",
            "--jq",
            '"\\(.total_commits)", (.commits[] | [.sha, .parents[].sha] | join(" "))',
        ],
    )
    if not rows or rows[0].strip() != str(len(rows) - 1):
        return False  # unread, or GitHub listed only some of the commits
    commits = [row.lower().split() for row in rows[1:]]
    if not 0 < len(commits) <= SAME_FILES_COMMITS:
        return False
    for sha, *parents in commits:
        if not parents:
            return False
        own: set[str] | None = None
        for parent in parents:
            changed = await compared_files(repository, parent, sha)
            if changed is None:
                return False
            own = set(changed) if own is None else own & changed
        if own & differs:
            return False  # the base's own change to it is not in ``new``
    return True


async def _code_diffs(repository: str, base: str, head: str) -> dict[str, str] | None:
    """The contributed lines (``_contributed_diff``) per file of ``base...head`` that is not
    a doc file, keyed by the file's ``diff --git`` line; ``None`` when unread, unreadable, or
    when the diff's files are not the ones GitHub's comparison lists."""
    from . import gh_cli
    from .poller_engine.github import _contributed_diff
    from .work_release import is_doc_path

    count = await _gh_lines(
        repository,
        ["gh", "api", f"repos/{repository}/compare/{base}...{head}", "--jq", ".files | length"],
    )
    if not count or not count[0].isdigit() or int(count[0]) >= 300:
        return None  # unread, or GitHub cut the files short: the diff may be too
    argv = [
        "gh",
        "api",
        "-H",
        "Accept: application/vnd.github.diff",
        f"repos/{repository}/compare/{base}...{head}",
    ]
    try:
        code, out, _err = await gh_cli.run_once(argv, timeout=60, repo=repository)
    except (OSError, TimeoutError):
        return None
    if code != 0:
        return None
    text = (out or b"").decode("utf-8", "replace")
    # Lines gathered per file and joined once: adding to a growing string copies it each time.
    parts: list[list[str]] = []
    for line in text.split("\n"):
        if line.startswith("diff --git ") or not parts:
            parts.append([])
        parts[-1].append(line)
    # Every file GitHub listed has its section, doc files too: a diff cut short or missing a
    # file would compare equal on the files left and carry a pass to code nobody read.
    if sum(1 for lines in parts if lines[0].startswith("diff --git ")) != int(count[0]):
        return None
    found: dict[str, str] = {}
    for section in ("\n".join(lines) + "\n" for lines in parts):
        header = section.split("\n", 1)[0]
        if not header.startswith("diff --git a/"):
            if section.strip():
                return None  # text before the first file: not a diff
            continue
        names = header.removeprefix("diff --git a/").split(" b/", 1)
        if all(is_doc_path(name) for name in names):
            continue
        contributed = _contributed_diff(section)
        if contributed is None or header in found:
            return None
        found[header] = contributed
    return found


async def behind_base(repository: str, number: int, head: str) -> bool:
    """Whether ``head`` lacks commits of pull request ``number``'s base branch (#4576): GitHub
    compares the base with it as ``behind`` or ``diverged``. Unread is ``False``."""
    base = await _gh_lines(
        repository, ["gh", "api", f"repos/{repository}/pulls/{number}", "--jq", ".base.ref"]
    )
    ref = (base or [""])[0].strip()
    if not ref:
        return False
    status = await _gh_lines(
        repository,
        ["gh", "api", f"repos/{repository}/compare/{ref}...{head}", "--jq", ".behind_by"],
    )
    count = (status or [""])[0].strip()
    return count.isdigit() and int(count) > 0


async def no_code_between(
    repository: str,
    number: int,
    old: str,
    new: str,
    before: Iterable[str] | None,
    *,
    minor: bool = False,
    why: list[str] | None = None,
) -> bool:
    """Whether ``new`` descends from ``old`` through commits that change only doc files or
    files of neither diff, a base merge (#3322): the fix a ``no-code`` review asked for.

    ``before`` is the pull request's files at ``old``, kept with that verdict. Without them
    only doc files pass: a commit that reverts a file of the diff drops it from the file list
    now, and would read as a base merge. The same two reads as ``base_only_between``; anything
    unread is ``False``, so it is reviewed.

    ``minor`` (#4474): the fix a ``minor`` review asked for may also change test files and,
    by ``MINOR_LINES`` lines added plus removed at most, files of ``before``. A new code file
    of the pull request is reviewed.

    ``why`` (#4575), when given, gets one plain reason appended whenever the answer is
    ``False``: the agent is told it, so it never believes the earlier verdict carried over.
    """
    from .work_release import is_doc_path

    reasons: list[str] = why if why is not None else []
    if minor and before is not None:
        return await _minor_between(repository, number, old, new, frozenset(before), reasons)
    touched = await compared_files(repository, old, new)
    if touched is None:
        reasons.append(_UNREAD_COMPARISON)
        return False
    if all(is_doc_path(name) for name in touched):
        return True
    if before is None:
        reasons.append(
            "the deck has no record of the pull request's files at the reviewed head, so it"
            " cannot tell a small fix from new code: only doc files could count, and it changes `"
            + _first(name for name in touched if not is_doc_path(name))
            + "`"
        )
        return False
    files = await pull_files(repository, number)
    if not files:
        reasons.append(_UNREAD_FILES)
        return False
    outside = set(files) | set(before)
    code = [name for name in sorted(touched) if not is_doc_path(name) and name in outside]
    if code:
        reasons.append(
            f"it changes the code file `{code[0]}`, and the review asked for no code change"
        )
        return False
    return True


#: The reasons ``no_code_between`` gives for what it could not read (#4575).
_UNREAD_COMPARISON = (
    "the deck could not compare the two commits on GitHub (unread, cut short at 300 files, or"
    " the new one does not descend from the reviewed one)"
)
_UNREAD_FILES = "the deck could not read the pull request's file list"


def _first(names: Iterable[str]) -> str:
    return min(names, default="?")


async def _minor_between(
    repository: str,
    number: int,
    old: str,
    new: str,
    before: frozenset[str],
    why: list[str],
) -> bool:
    """``no_code_between`` for a ``minor`` verdict's fix (#4474): doc files and test files
    (``_free_in_minor_fix``) any size, files of neither diff (a base merge) any size, files of
    ``before`` up to ``MINOR_LINES`` lines in all; a code file of either diff renamed (#4593),
    anything else, or anything unread, is reviewed, its reason appended to ``why`` (#4575)."""
    from .work_release import is_doc_path

    # Code lines without whole-line comments, counted locally when a checkout here holds both
    # heads and ``new`` descends from ``old`` (#4812); GitHub's compare, ahead only, otherwise.
    local = await code_lines.COUNTER.count(repository, old, new, ancestor=True)
    compared = (
        (local.sizes(), local.renamed)
        if local is not None
        else await compared_changes(repository, old, new)
    )
    if compared is None:
        why.append(_UNREAD_COMPARISON)
        return False
    changes, renamed = compared

    code_moved = any(not _free_in_minor_fix(name) for name in renamed)
    if not code_moved and all(is_doc_path(name) for name in changes):
        return True
    files = await pull_files(repository, number)
    if not files:
        why.append(_UNREAD_FILES)
        return False
    # A code file of either diff moved, from or onto its path, even unchanged, removes or
    # replaces that code: reviewed. One of neither diff came in with a base merge, as any
    # other base file does (#4593); ``renamed`` holds both names of each rename.
    moved = sorted(
        name
        for name in renamed
        if not _free_in_minor_fix(name) and (name in before or name in files)
    )
    if moved:
        why.append(f"it renames or moves the code file `{moved[0]}`")
        return False
    lines = 0
    for name, size in sorted(changes.items()):
        if _free_in_minor_fix(name):
            continue
        if name in before:
            lines += size
        elif name in files:
            # A code file the fix added to the pull request.
            why.append(
                f"it adds the code file `{name}` to the pull request (a test file counts as one"
                " only inside a test folder such as `tests/` or `testing/`, or when named like"
                " `a.test.ts`)"
            )
            return False
    if lines > MINOR_LINES:
        # Lines a base merge brought into files of the diff are not the fix (#4743): count
        # again by what changed in the pull request's own diff.
        counted = {
            name: size
            for name, size in changes.items()
            if not _free_in_minor_fix(name) and name in before
        }
        own = await own_changes(repository, number, old, new, counted)
        merged = ""
        if own is not None and own < lines:
            lines, merged = own, ", not counting lines merged in from the base branch"
        if lines > MINOR_LINES:
            why.append(
                f"it changes {lines} lines of code (added plus removed) in files the pull"
                f" request already had{merged}, over the limit of {MINOR_LINES}"
            )
            return False
    return True


def _free_in_minor_fix(name: str) -> bool:
    """Whether a ``minor`` fix may change ``name`` at any size (#4474): a doc file, or any file
    inside a test folder, a helper or fixture beside the tests included (#4575), or a file
    named ``*.test.ts`` (any JavaScript or TypeScript ending) anywhere (#4604). A
    ``test_*`` module outside a test folder, like ``src/agentdeck/test_timings.py``, is code."""
    from .work_release import MINOR_TEST_NAME, is_doc_path

    return (
        is_doc_path(name)
        or _in_test_dir(name)
        or MINOR_TEST_NAME.fullmatch(name.rsplit("/", 1)[-1]) is not None
    )


def _in_test_dir(name: str) -> bool:
    """Whether ``name`` sits in a folder whose test files a ``minor`` fix may change freely
    (``work_release.MINOR_TEST_DIRS``, #4474, #4575)."""
    from .work_release import MINOR_TEST_DIRS

    return any(part.lower() in MINOR_TEST_DIRS for part in name.split("/")[:-1])


class KeyFile:
    """A set of keys in one small JSON file, re-read whenever the other web worker of a hot
    reload wrote it (#3070): the Works handed to another deck (#3731)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._file = SharedFile(self.path, prefix=".work-router.")
        self._keys: list[str] = []

    def _fresh(self) -> None:
        if not self._file.stale():
            return
        try:
            raw = self._file.read()
        except (OSError, ValueError):
            log.warning("work-router: ignoring unreadable %s", self.path)
            raw = {}
        keys = raw.get("heads") if isinstance(raw, dict) else None
        self._keys = [key for key in keys if isinstance(key, str)] if isinstance(keys, list) else []

    def __contains__(self, key: str) -> bool:
        self._fresh()
        return key in self._keys

    def add(self, key: str) -> None:
        with self._file.locked():
            if key not in self:
                self._write([*self._keys, key])

    def discard(self, key: str) -> None:
        with self._file.locked():
            if key in self:
                self._write([item for item in self._keys if item != key])

    def _write(self, keys: list[str]) -> None:
        self._file.write({"heads": keys})
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

    def put(self, key: str, fields: dict, now: float, *, at: float | None = None) -> None:
        """Write an action down as pending, or push its next attempt back if it already is.
        ``at`` names the next attempt's time itself, leaving the growing wait as it was: a
        wait with a deadline of its own (#4722)."""
        with self._file.locked():
            entry = dict(self.entries.get(key) or {"since": now, "attempts": 0})
            entry.update(fields)
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            if at is None:
                backoff = Backoff(first=RETRY_FIRST_S, cap=RETRY_MAX_S)
                backoff.next_s = float(entry.get("delay", 0.0))
                entry["delay"] = backoff.failed()
            entry["next_at"] = now + entry["delay"] if at is None else at
            self._save({**self.entries, key: entry})

    def hurry(self, prefixes: Iterable[str], now: float) -> list[str]:
        """Make every entry whose key starts with one of ``prefixes`` due at ``now``; the
        keys it did."""
        prefixes = tuple(prefixes)
        with self._file.locked():
            hurried = [key for key in self.entries if prefixes and key.startswith(prefixes)]
            if hurried:
                self._save(
                    {
                        key: {**entry, "next_at": now} if key in hurried else entry
                        for key, entry in self.entries.items()
                    }
                )
        return hurried

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
    """Which repositories' Works hear their own events, re-read when a manifest moves.

    The manifests are looked at most once per ``ttl_s``: the release asks for every Work in
    one pass, and a glob and stat per Work held the event loop for seconds (#3975).
    """

    def __init__(
        self,
        poller_root: Path | str,
        *,
        ttl_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = Path(poller_root).expanduser()
        self.ttl_s = ttl_s
        self._clock = clock
        self._checked_at: float | None = None
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
        now = self._clock()
        if self._checked_at is not None and 0 <= now - self._checked_at < self.ttl_s:
            return repository.lower() in self._repositories
        self._checked_at = now
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
    ``open_head(repository, number)`` reads the head of an open pull request, a draft too, for
    a verdict that answers a review asked of it (#4352); unset, ``ready_head`` stands in.
    ``own_logins`` is called per batch, so an edited authority config is picked up.
    ``digest(pull, head)`` is the review digest of the pull request's diff at that head
    (``""`` unread) and ``base_only(repository, number, old, new)`` whether the commits from
    ``old`` to ``new`` touch no file of the pull request: a new head both call the same is not
    reviewed (#3314).
    ``ship_merge(repository, number, old, new)`` whether ``new`` only merged the base into a
    Ship now pass's head ``old``: that pass carries over to it as well (#4576).
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
        reviewer_off: bool = False,
        pre_review: bool = False,
        review_policy: str = "",
        own_logins: Callable[[], Iterable[str]] | Iterable[str] = configured_own_logins,
        refusal: Callable[[str, str], str | None] = lambda chat_id, text: None,
        ready_head: Callable[[str, int], Awaitable[str]] | None = None,
        open_head: Callable[[str, int], Awaitable[str]] | None = None,
        enabled: bool = True,
        watcher: WebhookWatcher | None = None,
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
        read_comment: Callable[[str, int], Awaitable[dict | None]] | None = None,
        read_comments: Callable[[str, int, str], Awaitable[list[dict] | None]] | None = None,
        busy: Callable[[str], bool] = lambda work_id: False,
        on_verdict: Callable[[str, Iterable[str] | None], object] | None = None,
        digest: Callable[[str, str], Awaitable[str]] | None = None,
        base_only: Callable[[str, int, str, str], Awaitable[bool]] | None = None,
        ship_merge: Callable[[str, int, str, str], Awaitable[bool]] | None = None,
        no_code: Callable[..., Awaitable[bool]] | None = None,
        load_work: Callable[[str, int], Awaitable[object]] | None = None,
        read_issue: Callable[[str, int], Awaitable[dict | None]] | None = None,
        read_parent: Callable[[str, int], Awaitable[tuple[str, int] | None]] | None = None,
        read_dependencies: Callable[[str, int, str], Awaitable[list[dict] | None]] | None = None,
        start_ticket: Callable[[str, int], Awaitable[tuple[str, str]]] | None = None,
        hold_labels: Callable[[str], Iterable[str]] = lambda repository: (),
        deployed: Callable[[], Collection[str]] | None = None,
        note_merged: Callable[[str, str, str], bool] | None = None,
        forget_merged: Callable[[str], bool] | None = None,
        start_round: Callable[[str], bool] | None = None,
        earlier_pulls: Callable[[str], Collection[str]] = lambda work_id: (),
        read_body: Callable[[str, int], Awaitable[dict | None]] | None = None,
        write_body: Callable[[str, int, str], Awaitable[bool]] | None = None,
        review_chat: Callable[[str, str], tuple[str, object | None] | None] | None = None,
        wait_review: Callable[[str, str, bool], object] | None = None,
        mine: Callable[[str, str], bool] = lambda repository, assignee: True,
        interrupt: Callable[[str], Awaitable[bool]] | None = None,
        ready_pulls: Callable[[], Iterable[tuple[str, int, str]]] | None = None,
        review_event: review_history.Note = review_history.no_note,
        reviews: Callable[[str], Iterable[review_history.ReviewEvent]] = lambda pull: (),
        check: object | None = None,
        forget_reviews: Callable[[str], object] = lambda pull: None,
        chat_repository: str = "",
        newest_comment: Callable[[str, int, int], Awaitable[dict | None]] | None = None,
        diff_sizes: Callable[[str, int, tuple[str, ...]], Awaitable[dict[str, int]]]
        | None = None,
        paperwork: Callable[[str, int, str], Awaitable[list[str]]] | None = None,
        verification_rows: Callable[[str], Awaitable[list[dict]]] | None = None,
        tests_checked: Callable[[str], bool] = lambda repository: False,
        ship: ShipPorts | None = None,
        ship_runs: ShipRuns | None = None,
        pending_note: Callable[[str, str, str], bool] | None = None,
        design: design_review.DesignRound | None = None,
        design_chat: design_review.ChatRoute | None = None,
        light: object | None = None,
        close_review: Callable[..., Awaitable[object]] | None = None,
        published: object | None = None,
        works_of_pull: Callable[[str, int], Iterable[str]] = lambda repository, number: (),
    ) -> None:
        self._enabled = bool(enabled)
        # The published label on a Work's issue (#4794, ``work_published.PublishedLabels``);
        # ``None`` labels nothing. ``works_of_pull`` names the Works that registered one.
        self._published = published
        self._works_of_pull = works_of_pull
        # ``(work, pull, head, settled, reason)``: close the Work's review chat once the one
        # pull request it waits on passed after a late minor verdict (#4474); ``None`` closes
        # nothing, and that chat's own verdict decides.
        self._close_review = close_review
        # Lighter review (#4399, ``light_review.LightReview``): a light head starts no review.
        self._light = light
        # A big Work's design review (#4332); ``None`` refuses each ask.
        self._design = design
        # ChatGPT as the design reviewer (#4722); ``None`` asks the deck's own every time.
        self._design_chat = design_chat
        # The design chats this process is opening right now, as (key, design comment, asked
        # at): a retry try never takes one of them for a chat that failed to open.
        self._design_opening: set[tuple] = set()
        # One run at a time of whatever finishes or replaces a design's pending entry
        # (#4722): taken once on each path (the ask, the retry, the verdict comment), and
        # ``_finish_design_verdict`` runs under it without taking it.
        self._design_locks: dict[str, asyncio.Lock] = {}
        # (chat id, note id, text): a pending note for the chat's next message, waking nothing
        # (#4306): a pass carried over a base merge is told that way.
        self._pending_note = pending_note
        # Ship now, fix later (#4212): ``None`` refuses every press.
        self._ship = ship
        # Each Ship now still running, keyed by the ``pull@head`` its caller sent (#4558): a
        # second press joins it, and the release reads it for the Work's hold line.
        self.ship_runs = ship_runs if ship_runs is not None else ShipRuns()
        # The GitHub check (#4152): ``review_check.ReviewCheckPublisher``, ``None`` when off.
        # With it every eligible pull request is reviewed, a Work's or not.
        self._check = check
        self._forget_reviews = forget_reviews
        # The review chats' repository: a chat its reviewer stops in shows on the check (#4422),
        # with the chat's newest comment when it was labelled, which the reviewer posts as its
        # reason, read from GitHub (``work_signals.newest_comment``).
        self._chat_repository = (chat_repository or "").lower()
        self._newest_comment = newest_comment
        self._tailer = WebhookTailer(streams_root, consumer=CONSUMER)
        self._watcher = watcher
        self._switched = switched
        # Which chat holds an issue's or a pull request's Work at all, even one handed to
        # another deck; every other path reads ``_owner_of_issue``/``_owner_of_pull`` (#3731).
        self._chat_of_issue = owner_of_issue
        self._chat_of_pull = owner_of_pull
        self._schedule = schedule
        self._start_review = start_review
        # No Work reviewer at all (#3849): a ready head asks for nothing and stays unasked.
        self.reviewer_off = bool(reviewer_off)
        # The agent's own pre-review before each review of a ready head (#3880).
        self.pre_review_on = bool(pre_review)
        # The reviewer's own rules, linked from the checklist; "" links nothing.
        self._review_policy = review_policy
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
        self._read_comment = read_comment
        # A ``/reviewed`` verdict whose webhook never came is read back (#4478).
        self._read_comments = read_comments
        self._read_issue = read_issue
        self._read_parent = read_parent
        # An issue whose last blocker closed is started (#3764, #4216): the issues an issue
        # blocks or waits on, read live, and the Start button's own write. A labelled issue
        # with an open blocker waits for it.
        self._read_dependencies = read_dependencies
        self._start_ticket = start_ticket
        self._hold_labels = hold_labels
        # The Works deployed and waiting only for a person's try (``db.load_try_waits``): a
        # blocker among them counts as done, and its blocked issues start (#4384).
        self._deployed = deployed
        # A registered pull request merged into its default branch counts its Work as done for
        # the issues it blocks, which start; a reopen of the Work's issue undoes it (#4398).
        self._note_merged = note_merged
        self._forget_merged = forget_merged
        self._start_round = start_round
        self._earlier_pulls = earlier_pulls
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
        # Whether a head only merged the base into a Ship now pass's head, its own code
        # unchanged (``ship_merge_between``): that pass carries over too (#4576).
        self._ship_merge = ship_merge
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
        self._open_head = open_head
        # The backstop for a ready head nobody started a review of (#3903): every pull request
        # a Work registered whose cached row is open and not a draft, as ``(owner/repo, n,
        # cached head)``, read locally.
        self._ready_pulls = ready_pulls
        # Each head's review history (#3957): appended to, and read for every review record
        # the router keeps (#3955), ``reviews(owner/repo#n)`` giving every head's events.
        self._note = review_event
        # ``{head: added plus removed lines}`` of a pull request's diff at each given head, read
        # from GitHub when a checklist is sent: growth past the previous round's adds a design
        # check (#4220). Nothing writes a size, so every way a review starts counts alike.
        self._diff_sizes = diff_sizes
        # ``(repository, number, owner/repo#issue)`` -> each paperwork gap, read from GitHub
        # when the agent says it went through the checklist; any gap refuses it (#4261).
        self._paperwork = paperwork
        # ``github_issue:owner/repo#n`` -> that Work's verification jobs (evidence and live
        # queue): in a repository whose tests are checked, a head with none queued, running or
        # green, or a red one, refuses pre-review-done too (#4326).
        self._verification_rows = verification_rows
        # ``owner/repo`` -> whether its manifest entry says ``check_tests = true`` (#4326).
        self._tests_checked = tests_checked
        self._reviews = reviews
        #: ``owner/repo#n`` -> ``(cached head, since when it sat unstarted)``, ``None`` once
        #: the backstop read it live: its grace, and no second read of the same row.
        self._ready_since: dict[str, tuple[str, float | None]] = {}
        self._sweep_wake = asyncio.Event()
        self._swept_at: float | None = None
        self._in_flight: set[str] = set()
        #: ``owner/repo#n@head`` -> why a push after a ``minor`` or ``no-code`` verdict did not
        #: carry its pass (``NOT_SMALL_TEXT``), said once at the top of that head's checklist
        #: (#4575). In memory: a restart loses only the reason, never the checklist.
        self._not_small: dict[str, str] = {}
        # Starts whose issue closed or lost its label since: a late refusal does not revive
        # them (#3006).
        self._unwanted_starts: set[str] = set()
        # Repositories the deck is filing a New work in right now, and how many (#3590).
        self._filing: dict[str, int] = {}
        #: The filings a web worker holds from across the socket (#4016): each a hold with an
        #: end time, so a web that dies mid-filing cannot hold starts for ever.
        self.filing_holds: Callable[[str], bool] = lambda repository: False
        self._checks: set[asyncio.Task] = set()
        self._published_task: asyncio.Task | None = None
        #: Pull requests a ``gh pr ready`` is waiting on right now (``ready_press``): a
        #: checklist made for one while it waits goes into its reply, not to the chat (#4253).
        #: A waiting ``gh pr ready``'s claim per pull request: the chat that pressed, and the
        #: ``(head, text)`` of each checklist made for it while it waited (#4253).
        self._printing: dict[str, tuple[str, list[tuple[str, str]]]] = {}
        self.pending = pending or PendingActions(pending_path(streams_root))
        self.handed_over = KeyFile(handed_over_path(streams_root))
        for name in RETIRED_FILENAMES:
            with contextlib.suppress(OSError):
                (Path(streams_root).expanduser().parent / name).unlink(missing_ok=True)
        self._exists = exists
        self._clock = clock
        # The deck's own release (``work_release``, step 2): it hears verdicts, pull request
        # and check events for the Works it releases. ``None`` hears nothing.
        self._release = release
        self._task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._sweep_task: asyncio.Task | None = None
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
        if self._ready_pulls is not None:
            self._sweep_task = asyncio.create_task(self._sweep_loop(), name="work-router-sweep")

    async def stop(self) -> None:
        for check in list(self._checks):
            check.cancel()
        if self._published_task is not None:
            self._published_task.cancel()
        for name in ("_retry_task", "_sweep_task"):
            loop_task = getattr(self, name)
            setattr(self, name, None)
            if loop_task is not None:
                loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_task
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
        # Under the consumer's lock (#4002): a second process starting it waits, rather than
        # skipping history or reading the same deliveries alongside this one.
        async with self._tailer.held(self._stop) as holding:
            if holding:
                await self._route()

    async def _route(self) -> None:
        self.skip_history()
        while True:
            try:
                async for batch in self._tailer.watch(stop_event=self._stop, watcher=self._watcher):
                    await self.apply(batch)
                    await self._tailer.commit_off_loop()
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
        self._check_published()
        if self.reviewer_off:
            try:
                await self.settle_without_reviewer()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("work-router: settling reviews with no reviewer failed")
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

    async def _sweep_loop(self) -> None:
        """Run the ready-head backstop at start, after deliveries and at its deadlines (#3903).

        Not a timer (ADR 0007): it wakes when a switched delivery or a registration lands,
        once at start (a restart's catch-up), and when a ready head's grace runs out; at most
        once per ``SWEEP_GAP_S`` however many deliveries wake it.
        """
        failures = Backoff()
        while True:
            self._sweep_wake.clear()
            try:
                deadline = await self.sweep_ready()
                failures.succeeded()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("work-router: ready-head backstop failed")
                await asyncio.sleep(failures.failed())
                continue
            wait = None if deadline is None else max(0.0, deadline - self._clock())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._sweep_wake.wait(), timeout=wait)
            gap = SWEEP_GAP_S - (self._clock() - (self._swept_at or 0.0))
            if gap > 0:
                await asyncio.sleep(gap)

    def wake_sweep(self) -> None:
        """Have the backstop look again: cached pull request rows were rewritten (#3903)."""
        self._sweep_wake.set()

    def _history(self, pull: str) -> list[review_history.ReviewEvent]:
        """``pull``'s review history at every head; none when unreadable."""
        try:
            return list(self._reviews(pull.lower()))
        except Exception:  # noqa: BLE001 - unread: nothing asked, as with no history
            log.warning("work-router: %s's review history unreadable", pull, exc_info=True)
            return []

    def _spent(self, pull: str, head: str, again: str = "") -> bool:
        """Whether ``pull``'s review at ``head`` was asked for (under the request id ``again``,
        when given) or needs none: a verdict, a carried or a no-code pass, stands there."""
        return any(
            (event.kind == review_history.ASKED and str(event.data.get("again") or "") == again)
            or (not again and event.kind in review_history.VERDICTS)
            for event in review_history.at_head(self._history(pull), head)
        )

    def _head_moving(self, pull: str, head: str) -> bool:
        """Whether something already moves ``pull`` at ``head`` towards a verdict: a review
        asked, answered or kept pending, this head's pre-review checklist sent or kept to
        send while the agent's checklist waits, or the router on it right now."""
        key = f"{pull}@{head}"
        if self._light_moving(pull, head):
            return True
        if (
            key in self._in_flight
            or f"review:{key}" in self.pending
            or f"message:work-event:pre-review:{key}" in self.pending
            or self._spent(pull, head)
        ):
            return True
        events = self._history(pull)
        sent = any(
            event.kind == review_history.CHECKLIST and not event.data.get("covered")
            for event in review_history.at_head(events, head)
        )
        return self.pre_review_on and sent and review_history.checklist_waits(events)

    def _light_moving(self, pull: str, head: str) -> bool:
        """A light head (#4399), or one the release moved itself and has not decided about:
        its bench verdict moves it, not a review."""
        if self._light is None:
            return False
        try:
            return bool(self._light.head_moving(pull, head))
        except Exception:  # noqa: BLE001 - unread: the backstop goes on as before
            return False

    def _is_light(self, pull: str, head: str) -> bool:
        if self._light is None:
            return False
        try:
            return bool(self._light.is_light(pull, head))
        except Exception:  # noqa: BLE001 - unread: not light, so its review starts
            return False

    def _working_through(self, pull: str, owner: Owner) -> bool:
        """Whether an older head's checklist waits while the Work's agent is mid-turn: it is
        likely working through it and pushing fixes, which that checklist already covers (its
        review reads the head current at ``pre-review-done``). Idle, the new head gets its own
        checklist, as a ready press would send it (#3904 review)."""
        if not self.pre_review_held(pull):
            return False
        try:
            return bool(self._busy(_work_id(owner)))
        except Exception:  # noqa: BLE001 - unknown: taken as idle, so nothing is left silent
            return False

    async def sweep_ready(self) -> float | None:
        """Start what a missed or misread ready event left unstarted (#3903); the next deadline.

        Every Work's registered pull request whose cached row is open and ready, at a head
        with no checklist sent, no review asked or pending and no verdict, that stayed so for
        ``READY_GRACE_S``: its head is read live once and acted on as a ready press would be
        (``check_registered``): that head's checklist, or its review if the agent cleared it.
        One that still has nothing moving it is stuck: an ``unstarted`` event in its history,
        which the Work's card and page show until any later event at that head. A draft, a
        closed one and a head already moving cost no GitHub read; neither does a ready one
        inside its grace or already looked at.
        """
        self._swept_at = self._clock()
        if self.reviewer_off or self._ready_pulls is None or self._ready_head is None:
            return None
        rows = list(self._ready_pulls())
        now = self._clock()
        seen: dict[str, tuple[str, float | None]] = {}
        deadline: float | None = None
        for repository, number, cached in rows:
            repository, cached = repository.lower(), (cached or "").lower()
            pull = f"{repository}#{number}"
            if not cached or not self._switched(repository) or self._head_moving(pull, cached):
                continue
            owner = self._owner_of_pull(repository, number)
            if owner is None or self._is_paused(owner) or self._finished(_work_id(owner)):
                continue
            if self._working_through(pull, owner):
                # Looked at again once the turn may have ended; a local read, no GitHub one.
                again = now + READY_GRACE_S
                deadline = again if deadline is None else min(deadline, again)
                continue
            known, since = self._ready_since.get(pull, ("", now))
            since = now if known != cached else since
            seen[pull] = (cached, since)
            if since is None:
                continue  # read live already: the next row written for it is looked at afresh
            due = since + READY_GRACE_S
            if now < due:
                deadline = due if deadline is None else min(deadline, due)
                continue
            try:
                head = (await self._ready_head(repository, number) or "").lower()
            except Exception:  # noqa: BLE001 - unreadable now: looked at again after the grace
                log.warning("work-router: %s#%s unreadable", repository, number, exc_info=True)
                seen[pull] = (cached, now)
                retry = now + READY_GRACE_S
                deadline = retry if deadline is None else min(deadline, retry)
                continue
            seen[pull] = (cached, None)
            if not head or self._head_moving(pull, head):
                # Turned back to draft, closed, or pushed to a head already moving: the cached
                # row was behind.
                continue
            log.warning(
                "work-router: %s ready at %s with no checklist or review; starting it", pull,
                head[:12],
            )
            try:
                await self._review(
                    owner, Event(REVIEW, repository, number, True, head=head), checklist_again=True
                )
            except Exception:  # noqa: BLE001 - shown stuck; the other pull requests go on
                log.warning("work-router: starting %s@%s failed", pull, head[:12], exc_info=True)
            if not self._head_moving(pull, head):
                # Shown stuck on the Work until any later event at that head (#3956).
                self._note(
                    pull, head, review_history.UNSTARTED, f"unstarted:{pull}@{head}@{now:.0f}"
                )
                log.warning("work-router: %s@%s still has no review started", pull, head[:12])
        self._ready_since = seen
        return deadline

    async def settle_without_reviewer(self) -> None:
        """Answer every review kept pending, at once, on a deck with no reviewer (#3849).

        Kept before the upgrade, each waited out its back-off, and one past a day was dropped
        with nobody told: the Work that reported it would have heard nothing. Each goes the
        retry's way, so it follows the live ready head, frees its place, and its chat is told
        once that no review is coming.
        """
        for key, entry in list(self.pending.entries.items()):
            if entry.get("kind") == REVIEW:
                await self._retry_review(key, entry)
            elif entry.get("kind") == DESIGN:
                self.pending.discard(key)

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
        published = [(key, entry) for key, entry in due if entry.get("kind") in PUBLISHED_KINDS]
        if published:
            # Kept until a sync settles it, however long that takes, and run beside this
            # loop, so a slow GitHub read holds up no other action (#4794).
            self._start_published(published, now)
        for key, entry in due:
            if entry.get("kind") in PUBLISHED_KINDS:
                continue
            elif now - float(entry.get("since", now)) > PENDING_MAX_AGE_S:
                log.warning("work-router: dropping %s, still not done after a day", key)
                self.pending.discard(key)
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
            elif entry.get("kind") == DESIGN:
                await self._retry_design(key, entry)
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

    def _start_published(self, due: list[tuple[str, dict]], now: float) -> None:
        """Hand the due published-label entries to one task, or, while one runs, look at
        them again a few seconds on. Either way each moves its ``next_at`` past ``now``, so
        the deadline loop never spins on them; a sync then ends or re-arms its own."""
        from . import work_published

        running = self._published_task is not None and not self._published_task.done()
        for key, _ in due:
            with self.pending._file.locked():  # noqa: SLF001 - only an entry still there
                if key in self.pending.entries:
                    self.pending.put(key, {}, now, at=now + work_published.LOCK_WAIT_S)
        if running:
            return
        self._published_task = asyncio.create_task(
            self._run_published(due), name="work-router-published"
        )

    async def _run_published(self, due: list[tuple[str, dict]]) -> None:
        for key, entry in due:
            try:
                await self._retry_published(key, entry)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - logged; this entry backs off, the rest run
                log.warning("work-router: published label sync %s failed", key, exc_info=True)
                with contextlib.suppress(Exception):
                    self.pending.put(key, {}, self._clock())

    async def _retry_published(self, key: str, entry: dict) -> None:
        from . import work_published

        published = self._published
        if not isinstance(published, work_published.PublishedLabels):
            self.pending.discard(key)
        elif entry.get("kind") == work_published.PASS:
            await published.run_pass(self.pending, key, entry)
        else:
            await published.sync(self.pending, key, entry)

    def _check_published(self) -> None:
        """Reconcile a repository whose published label was turned on or changed (#4794)."""
        if self._published is None:
            return
        try:
            self._published.check_settings(self.pending)
        except Exception:  # noqa: BLE001 - logged; the next batch looks again
            log.warning("work-router: published label settings not checked", exc_info=True)

    def _note_published(self, delivery: Delivery) -> None:
        """A registered pull request closed or reopened, in a switched repository or not: its
        Works' labels are synced (#4794). Written to the pending file before the delivery's
        offset is committed; an unread registration or a failed write raises."""
        if self._published is None or delivery.event != "pull_request":
            return
        if _text(delivery.payload.get("action")) not in {"closed", "reopened"}:
            return
        pull = delivery.payload.get("pull_request")
        number = pull.get("number") if isinstance(pull, dict) else None
        if not isinstance(number, int):
            return
        from . import work_published

        works = list(self._works_of_pull(delivery.stream.lower(), number))
        if works:
            work_published.request_into(self.pending, works, self._clock(), strict=True)

    def note_published(self) -> None:
        """Another process wrote a published-label intent (#4794): the retry loop re-plans."""
        self.pending.changed.set()

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
        owner = self._review_owner(repository, number) if isinstance(number, int) else None
        if owner is None or not head:
            self.pending.discard(key)
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
                self.pending.discard(key)
                if not current:
                    return
                head, again = current, ""
                entry = {}
        pull = f"{repository}#{number}"
        # The agent's own request: no pass that was only carried here answers it (#4658).
        own = bool(entry.get("asked") or again) and not again.startswith(NO_VERDICT)
        if (
            entry
            and not self._carry_unfinished(pull, head)
            and self._passed_since(pull, head, entry.get("since"))
            and not (own and self._carried_pass(pull, head))
            and not (entry.get("request") and self._fix_pass(pull, head))
        ):
            # Kept while another review of this head ran, which then passed it (#3324): a
            # review now would only re-open a release that already waits for a person's Merge.
            log.info("work-router: %s passed since it was asked for; not reviewed", key)
            if not again and self._carried_pass(pull, head):
                self._stop_on_carried(owner, pull, head)
            self.pending.discard(key)
            return
        if entry.get("request") and not self._request_due(key, entry, owner, pull, head):
            return
        if entry.get("request") and not self._spent(pull, head):
            # The start it came in on ended without asking: it is the head's first review.
            fields = {"kind": REVIEW, "repository": repository, "number": number, "head": head}
            fields.update(asked=True, note=str(entry.get("note") or ""))
            self.pending.put(f"review:{pull}@{head}", fields, self._clock())
            self.pending.discard(key)
            again = ""
        note = str(entry.get("note") or "") if again or entry.get("asked") else ""
        # Only the agent's own request reviews a diff an earlier head's review read (#3314).
        await self._review(
            owner,
            Event(REVIEW, repository, number, True, head=head, again=again, note=note),
            asked=bool(entry.get("asked")),
        )
        if not own or not entry.get("asked"):
            return
        # A pass that came meanwhile may have ended the chat's wait (#4658): the review now
        # asked for has one, and one no longer coming has none. Still pending, it is left as
        # it is: renewed at every retry, its timeout would never tell the chat.
        since = float(entry.get("since") or 0)
        if any(
            event.kind == review_history.ASKED and event.at >= since
            for event in review_history.at_head(self._history(pull), head)
        ):
            self._wait_on_review(owner, pull, asked=True)
        elif not any(at.startswith(f"review:{pull}@{head}") for at in self.pending.entries):
            self._end_wait(owner, pull, head)

    def _requests(self, pull: str, head: str) -> list[str]:
        """The pending keys of the agent's own requests kept for ``head`` (``_keep_request``)."""
        prefix = f"review:{pull}@{head}+"
        return [
            key
            for key, entry in self.pending.entries.items()
            if key.startswith(prefix) and entry.get("request")
        ]

    def _keep_request(self, pull: str, head: str, note: str) -> None:
        """Keep the agent's own review request, made while an automatic start of ``head`` ran
        or waited, as a pending row of its own (#4658): that start may end without asking (a
        carried pass, a checklist, a busy reviewer), and the request must outlive it. The
        start's ``asked`` row meets it (``_note_asked``). One row per head, the last note."""
        repository, _, number = pull.rpartition("#")
        held = self._requests(pull, head)
        again = held[0].rpartition("+")[2] if held else secrets.token_hex(6)
        fields = {"kind": REVIEW, "repository": repository, "number": int(number), "head": head}
        if not note.strip() and held:
            note = str(self.pending.entries[held[0]].get("note") or "")
        fields.update(again=again, note=note, asked=True, request=True)
        self.pending.put(f"review:{pull}@{head}+{again}", fields, self._clock())

    def _request_due(self, key: str, entry: dict, owner: Owner, pull: str, head: str) -> bool:
        """Whether a kept request is to be asked now: not while a start of its head runs, nor
        once one asked after it, nor past the ask-again cap, which its chat is told."""
        if f"{pull}@{head}" in self._in_flight:
            self.pending.put(key, {}, self._clock())
            return False
        since = float(entry.get("since") or 0)
        asks = [
            event
            for event in review_history.at_head(self._history(pull), head)
            if event.kind == review_history.ASKED
        ]
        if any(event.at >= since for event in asks):
            self.pending.discard(key)
            return False
        repository, _, number = pull.rpartition("#")
        why = ""
        if self._spent(pull, head) and not str(entry.get("note") or "").strip():
            # As ``ask_again`` refuses one (#3344).
            why = f"{pull} at {head[:12]} has its verdict, and the request had no note"
        elif self._spent(pull, head) and self._again_count(pull, head) >= MAX_AGAIN:
            why = _cap_refusal(repository, int(number), head, False)
        if why:
            if owner[0]:
                now = datetime.now(UTC)
                self._deliver(
                    ScheduledMessage(
                        id=f"work-event:request-refused:{key.removeprefix('review:')}",
                        chat_id=owner[0],
                        text=f"AgentDeck: the review you asked for did not start: {why}.",
                        due_at=now,
                        created_at=now,
                    )
                )
            self._end_wait(owner, pull, head, refused=True)
            self.pending.discard(key)
            return False
        return True

    def _end_wait(self, owner: Owner, pull: str, head: str, *, refused: bool = False) -> None:
        """End the chat's wait on ``pull``'s verdict quietly: none is coming. Not while the
        agent's own request for ``head`` is kept, whose review the wait is for, unless that
        request is the one ``refused``."""
        if not refused and self._requests(pull, head):
            return
        if owner[0] and self._on_verdict is not None:
            with contextlib.suppress(Exception):
                self._on_verdict(owner[0], [pull])

    def _carried_pass(self, pull: str, head: str) -> bool:
        """Whether the pass standing at ``head`` was carried to it from an earlier head."""
        events = review_history.at_head(self._history(pull), head)
        verdict = review_history.standing_verdict(events)
        return review_history.passed(verdict) and verdict.key.startswith("carried:")

    def _stop_on_carried(self, owner: Owner, pull: str, head: str) -> None:
        """Every path that starts no review because a carried pass stands at ``head`` ends here
        (#4658): the running start, its retry, a replay or a repeated ready press. The chat's
        wait ends before the pending row goes, so a crash between leaves the retry to do both
        again."""
        pending_key = f"review:{pull}@{head}"
        entry = self.pending.entries.get(pending_key) or {}
        if entry.get("asked") and self._unasked_verdict(pull, head):
            # The row is the agent's own first request, kept for a busy reviewer: it is still
            # asked, whichever path met the carried pass.
            self._keep_request(pull, head, str(entry.get("note") or ""))
            self.pending.discard(pending_key)
            return
        if self._carry_unfinished(pull, head):
            self._carried_told(owner, pull, head)
            return
        if self._newest_waited(pull, head):
            self._end_wait(owner, pull, head)
        self.pending.discard(pending_key)
        log.info("work-router: %s@%s has a carried pass; not reviewed", pull, head[:12])

    def _passed_since(self, pull: str, head: str, since: object) -> bool:
        """Whether ``pull``'s pass at ``head`` stands, dated ``since`` or later (#3324)."""
        if not isinstance(since, (int, float)):
            return False
        events = review_history.at_head(self._history(pull), head)
        verdict = review_history.standing_verdict(events)
        return review_history.passed(verdict) and verdict.at >= since

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
        batch = list(batch)
        # First, before any act: a failed write raises, so the batch is not committed and is
        # read again with nothing done twice (#4794 review).
        for delivery in batch:
            self._note_published(delivery)
        own = self.own_logins()
        self._check_published()
        for delivery in batch:
            if self._check is not None and delivery.stream.lower() == self._chat_repository:
                try:
                    await self._observe_chat_stop(delivery)
                except Exception:  # noqa: BLE001 - a check hiccup must not stop routing
                    log.warning(
                        "work-router: chat stop of %s failed", delivery.delivery_id, exc_info=True
                    )
            if not self._switched(delivery.stream):
                continue
            # Any news from a switched repository is a reason for the backstop to look (#3903).
            self._sweep_wake.set()
            await self._load_owner(delivery)
            settled = False
            if self._release is not None:
                try:
                    settled = await self._observe_release(delivery)
                except Exception:  # noqa: BLE001 - a release hiccup must not stop routing
                    log.warning(
                        "work-router: release of %s failed", delivery.delivery_id, exc_info=True
                    )
            try:
                settled = await self._observe_design(delivery) or settled
            except Exception:  # noqa: BLE001 - a design hiccup must not stop routing
                log.warning(
                    "work-router: design verdict of %s failed", delivery.delivery_id, exc_info=True
                )
            try:
                self._observe_design_comment(delivery)
            except Exception:  # noqa: BLE001 - a design hiccup must not stop routing
                log.warning(
                    "work-router: design comment of %s failed", delivery.delivery_id, exc_info=True
                )
            if self._check is not None:
                try:
                    settled = await self._observe_check(delivery) or settled
                except Exception:  # noqa: BLE001 - a check hiccup must not stop routing
                    log.warning(
                        "work-router: check of %s failed", delivery.delivery_id, exc_info=True
                    )
            if self._start_work is not None:
                await self._maybe_start(delivery)
            await self._maybe_reassigned(delivery)
            self._maybe_foreign_start(delivery)
            if self._reread_issue is not None:
                self._maybe_reread(delivery)
            self._note_merge_or_reopen(delivery)
            await self._maybe_count_merge(delivery)
            # Before the close that follows an answer wakes the map: its chat reads the lists.
            await self._maybe_keep_map_lists(delivery)
            await self._maybe_map_ready(delivery)
            await self._maybe_start_unblocked(delivery)
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
                if self._check is not None and event.kind == REVIEW:
                    await self._check_review(delivery, event)
                continue
            if event.kind == CLOSE:
                self._hold_close(delivery, owner, event)
                continue
            reviewed = event.kind == REVIEW and self._spent(
                f"{event.repository}#{event.number}", event.head
            )
            if event.kind == REVIEW and (
                event.confirm or reviewed or (event.explicit and self._checklist_waits(event))
            ):
                # A draft-looking push, or a ready event naming a head already reviewed while
                # a push may be landing behind it (#2983): the live head decides, before a
                # paused Work is resumed for it. So does a ready press while a checklist
                # waits: pushed and marked ready at once, GitHub names the old head in it and
                # the new one only in the push after, which the waiting checklist keeps
                # quiet (#3896).
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
                if sent and event.verdict:
                    await self._note_answered(delivery, event, owner)
                if event.comment_id:
                    self._background(
                        self._ack(event.repository, event.comment_id), "work-router-ack"
                    )
            else:
                await self._review(owner, event)

    def _review_owner(self, repository: str, number: int) -> Owner | None:
        """Who a review of this pull request belongs to: its Work, else, once the check took
        it in (``_observe_check``), nobody's, as a chat-less owner named after itself."""
        owner = self._owner_of_pull(repository, number)
        if owner is not None or self._check is None:
            return owner
        return self._no_work(repository, number)

    def _no_work(self, repository: str, number: int) -> Owner | None:
        pull = f"{repository}#{number}".lower()
        seen = any(event.kind == review_history.SEEN for event in self._history(pull))
        return ("", pull) if seen else None

    async def _observe_check(self, delivery: Delivery) -> bool:
        """What the GitHub check takes from a delivery (#4152). ``True`` when it was a reviewer's
        answer on a pull request no Work owns, which has no chat to go on to."""
        payload = delivery.payload
        repository = delivery.stream.lower()
        action = _text(payload.get("action"))
        if delivery.event == "pull_request":
            self._check_pull_event(repository, payload, action)
        elif delivery.event == "check_run" and review_history.deck_check_delivery(
            "check_run", payload
        ):
            await self._check_pressed(delivery, repository, action)
        elif delivery.event == "issue_comment" and action == "created":
            return await self._check_comment(delivery, repository)
        return False

    async def _observe_chat_stop(self, delivery: Delivery) -> None:
        """A review chat whose reviewer stopped short, for the check (#4422): ``blocked`` or
        ``needs-more-info`` put on an open chat, or the last of them taken off again. Noted at
        each head the chat was opened about; nothing else ends a review that gives no verdict.

        The stop's reason is the chat's newest comment when it was labelled: the delivery's
        ``comments`` count names it, and GitHub gives it whoever wrote it. So it does not
        depend on who put the label on, on the order deliveries arrive, or on a restart, and a
        comment posted after the label is never taken for it."""
        payload = delivery.payload
        issue = payload.get("issue")
        number = issue.get("number") if isinstance(issue, dict) else None
        if (
            delivery.event != "issues"
            or not isinstance(number, int)
            or isinstance(number, bool)
            or isinstance(issue.get("pull_request"), dict)
        ):
            return
        action = _text(payload.get("action"))
        label = payload.get("label")
        name = _text(label.get("name")).lower() if isinstance(label, dict) else ""
        if (
            action not in ("labeled", "unlabeled")
            or name not in _STOP_LABELS
            or _text(issue.get("state")) != "open"
        ):
            return
        labels = issue.get("labels") if isinstance(issue.get("labels"), list) else []
        held = {
            _text(item.get("name")).lower() for item in labels if isinstance(item, dict)
        } & _STOP_LABELS
        if action == "unlabeled" and held:
            return  # the other label still holds it
        heads = self._chat_heads(number, issue)
        if not heads:
            return
        chat = f"{self._chat_repository}#{number}"
        data = {"chat": chat, "label": name}
        kind = review_history.RESUMED
        if action == "labeled":
            kind = review_history.STOPPED
            data["said"] = (await self._stop_reason(number, issue))[:_CHAT_SAID_CHARS]
        stamped = _when(_text(issue.get("updated_at")))
        at = stamped.timestamp() if stamped is not None else None
        for pull, full in heads:
            key = f"{kind}:{chat}:{pull}@{full}:{delivery.delivery_id}"
            self._note(pull, full, kind, key, at=at, data=data)
        self._check.wake()

    def _chat_heads(self, number: int, issue: dict) -> list[tuple[str, str]]:
        """``(pull, head)`` of each head this chat was opened about: pinned in its body, and
        the history's own record of the chat at that head."""
        from .chat_review import background_pulls, pinned_heads

        chat = f"{self._chat_repository}#{number}"
        found: list[tuple[str, str]] = []
        body = _text(issue.get("body"))
        pinned = pinned_heads(body, _text(issue.get("title")))
        # A pull request listed only as already passed background is not under review here,
        # so this chat's label neither stops nor resumes its review (#4599).
        background = background_pulls(body)
        for (repository, pull_number), head in pinned.items():
            if (repository.lower(), pull_number) in background:
                continue
            pull = f"{repository}#{pull_number}".lower()
            heads = {
                event.head
                for event in self._history(pull)
                if event.kind == review_history.CHAT
                and _text((event.data or {}).get("chat")).lower() == chat
                and same_commit(head, event.head)
            }
            found.extend((pull, full) for full in sorted(heads))
        return found

    async def _stop_reason(self, number: int, issue: dict) -> str:
        """The chat's newest comment when it was labelled; "" when it has none or it is unread."""
        count = issue.get("comments")
        if self._newest_comment is None or not isinstance(count, int) or count < 1:
            return ""
        try:
            found = await self._newest_comment(self._chat_repository, number, count)
        except Exception:  # noqa: BLE001 - the stop is still shown, without its reason
            log.warning("work-router: comments of chat #%s unread", number, exc_info=True)
            return ""
        return _text(found.get("body")).strip() if isinstance(found, dict) else ""

    def _check_pull_event(self, repository: str, payload: dict, action: str) -> None:
        pull = payload.get("pull_request")
        number = pull.get("number") if isinstance(pull, dict) else None
        if not isinstance(number, int):
            return
        ref = f"{repository}#{number}"
        owned = self._owner_of_pull(repository, number) is not None
        if action == "closed":
            self._check.wake(ref)
            if not owned and self._no_work(repository, number) is not None:
                # Nothing else ends a history no Work owns. Reopened, it starts again.
                self._forget_reviews(ref)
            return
        if action not in _READY_ACTIONS or pull.get("draft") or pull.get("state") != "open":
            return
        head = pull.get("head")
        sha = _text(head.get("sha")).lower() if isinstance(head, dict) else ""
        if not sha or self._check.from_fork(pull):
            return
        if owned or self._check.eligible(pull, self.own_logins()):
            self._note_seen(ref, sha)

    def _note_seen(self, pull: str, head: str) -> None:
        """A ready head, so its check says it waits even while its ask does."""
        self._note(pull, head, review_history.SEEN, f"seen:{pull}@{head}")
        self._check.wake()

    async def _check_pressed(self, delivery: Delivery, repository: str, action: str) -> None:
        """A person's Re-run or Handled on the check."""
        payload = delivery.payload
        run = payload.get("check_run")
        run = run if isinstance(run, dict) else {}
        head = _text(run.get("head_sha")).lower()
        run_id = run.get("id")
        # The run's own record says whose it is. The webhook's list names every pull request at
        # the commit, and a replaced run's press would answer a result nobody sees any more.
        pull = self._check.pull_of(repository, head, run_id) if isinstance(run_id, int) else ""
        found = pull.rpartition("#")[2]
        if not found.isdigit():
            log.info("work-router: %s on %s's unknown run %s dropped", action, repository, run_id)
            return
        number = int(found)
        live = await self._live_ready_head(repository, number)
        if not live or live != head:
            # A press on a check the branch has left says nothing about the live head.
            log.info("work-router: %s on %s's old check dropped", action, pull)
            return
        login, _kind = _login(payload)
        if action == "rerequested":
            # Without the second sentence an unchanged answer is not repeated, as the reviewer's
            # rules ask, and the check waits on this review for hours (#4152).
            note = (
                f"{login or 'A person'} pressed Re-run on the check on GitHub. Post your findings "
                "or verdict again, in full, even if they are the same as the last review's: the "
                "check shows only what this review posts."
            )
            try:
                await self.ask_again(repository, number, note)
            except AgainBusy:
                pass  # the review the press asks for is already on its way
            except AgainRefused as refused:
                log.info("work-router: Re-run of %s: %s", pull, refused)
                key = f"rerun-refused:{pull}@{head}:{delivery.delivery_id}"
                self._note(pull, head, review_history.RERUN_REFUSED, key)
                self._check.wake(pull)
        elif action == "requested_action" and self._is_handled_press(payload):
            if self._owner_of_pull(repository, number) is None:
                key = f"handled:{pull}@{head}:{delivery.delivery_id}"
                self._note(pull, head, review_history.HANDLED, key, data={"by": login})
                self._check.wake(pull)

    @staticmethod
    def _is_handled_press(payload: dict) -> bool:
        asked = payload.get("requested_action")
        return isinstance(asked, dict) and asked.get("identifier") == "handled"

    async def _check_comment(self, delivery: Delivery, repository: str) -> bool:
        """A reviewer's answer posted on a pull request no Work owns: a verdict, findings or an
        escalation. From anyone else it is a comment like any other (``_from_reviewer``)."""
        from .poller_engine.intake import CommandKind, parse_verdicts

        payload = delivery.payload
        issue, comment = payload.get("issue"), payload.get("comment")
        if not isinstance(issue, dict) or not isinstance(comment, dict):
            return False
        number = issue.get("number")
        login, _kind = _login(payload)
        if (
            not isinstance(number, int)
            or not isinstance(issue.get("pull_request"), dict)
            or self._owner_of_pull(repository, number) is not None
            or self._no_work(repository, number) is None
            or not await self._from_reviewer(repository, number, login, comment)
        ):
            return False
        body = _text(comment.get("body"))
        posted = _when(_text(comment.get("created_at")))
        at = posted.timestamp() if posted is not None else None
        key = f"comment:{comment.get('id')}"
        commands = parse_verdicts(body)
        if commands:
            for command in commands:
                if command.kind == CommandKind.REVIEWED:
                    await self._check_verdict(repository, number, command, key, by=login, at=at)
            return True
        if (
            _VERDICT_RE.match(body)
            or _ESCALATED_RE.match(body)
            or _headed_findings(body, repository)
        ):
            await self._check_findings(repository, number, body, key, at=at)
            return True
        return False

    async def _from_reviewer(self, repository: str, number: int, login: str, comment: dict) -> bool:
        """The reviewer's account, or the connector of the ChatGPT reviewing this pull request's
        live head. The app alone proves nothing: a colleague's ChatGPT has it too, and could pass
        their own pull request. The owner of the chat's repository is the one whose ChatGPT runs
        these reviews, so a comment needs both. Only the newest chat at the live head speaks: an
        older chat's owner has no say over a head it was not asked about."""
        if login.casefold() in reviewer_logins():
            return True
        app = comment.get("performed_via_github_app")
        if not login or not isinstance(app, dict) or app.get("slug") != REVIEWER_CONNECTOR:
            return False
        head = await self._live_ready_head(repository, number)
        chats = [
            _text(event.data.get("chat"))
            for event in self._history(f"{repository}#{number}")
            if event.kind == review_history.CHAT and head and same_commit(head, event.head)
        ]
        return bool(chats) and chats[-1].partition("/")[0].casefold() == login.casefold()

    async def check_request(
        self, repository: str, number: int, command: str, action_id: str
    ) -> dict | None:
        """A reviewer's ``agentdeck deck-request`` on a pull request no Work owns (#4152).

        ``None`` when it is not that, so the Work's own handling answers. The answer is the
        delivery, and nothing is posted: the check shows it.
        """
        from .poller_engine.intake import CommandKind, parse_verdicts

        repository = repository.lower()
        if (
            self._check is None
            or self._owner_of_pull(repository, number) is not None
            or self._no_work(repository, number) is None
        ):
            return None
        key = f"request:{action_id}"
        commands = parse_verdicts(command)
        if commands:
            answers = [
                await self._check_verdict(repository, number, item, key, by="", at=None)
                if item.kind == CommandKind.REVIEWED
                else "a verdict that did not read as one was dropped"
                for item in commands
            ]
            detail = "; ".join(answers)
        elif (
            _VERDICT_RE.match(command)
            or _ESCALATED_RE.match(command)
            or _headed_findings(command, repository)
        ):
            detail = await self._check_findings(repository, number, command, key, at=None)
        else:
            detail = "no Work owns this pull request, so a plain comment goes nowhere"
        return {"outcome": "done", "detail": f"{detail}. This answer is the delivery."}

    async def _check_verdict(
        self, repository: str, number: int, command, key: str, *, by: str, at: float | None
    ) -> str:
        """Record a verdict on a pull request no Work owns, by the rules the release keeps for
        a Work's, but with none of its effects: nothing merges from here."""
        pull = f"{repository}#{number}"
        targets = command.targets
        if targets and not any(_match_in(repository, target) == pull for target in targets):
            return f"a verdict about {targets[0]} was dropped on {pull}"
        head = await self._live_ready_head(repository, number)
        about = (command.head or "").lower()
        events = self._history(pull)
        if not head or not (
            same_commit(head, about) or review_history.same_change(events, head, about)
        ):
            return f"{pull} moved past {about[:12]}, so this verdict was dropped"
        kinds = {
            "pass": review_history.PASS,
            "changes-needed": review_history.CHANGES,
            NO_CODE: review_history.NO_CODE,
            MINOR: review_history.NO_CODE,
        }
        kind = kinds.get(command.verdict)
        if kind is None:
            return f"the verdict {command.verdict} was not understood"
        data = {"by": by, "chat": self._review_chat_name(pull, head)}
        if command.verdict == MINOR:
            data["minor"] = True
        if kind != review_history.CHANGES:
            try:
                digest = (await self._digest(pull, head) if self._digest else "") or ""
            except Exception:  # noqa: BLE001 - unread is unproven, and the reviewer is told
                log.warning("work-router: could not read %s's digest", pull, exc_info=True)
                digest = ""
            if not digest:
                return f"{pull}'s diff could not be read: send the same verdict again"
            if digest.lower() != (command.digest or "").lower():
                return f"{pull}'s diff is not the one reviewed, so this verdict was dropped"
            data["digest"] = digest.lower()
        self._note(pull, head, kind, f"{key}:{pull}:{kind}", at=at, data=data)
        self._check.wake()
        return f"the verdict on {pull} at {head[:12]} is on its check"

    def _review_chat_name(self, pull: str, head: str) -> str:
        review = review_history.head_review(review_history.at_head(self._history(pull), head))
        return review.review_chat

    async def _check_findings(
        self, repository: str, number: int, text: str, key: str, *, at: float | None
    ) -> str:
        """Record findings or an escalation at the live head, when they name it: findings about
        a commit the branch has left must not turn the new head's check red."""
        pull = f"{repository}#{number}"
        head = await self._live_ready_head(repository, number)
        if not head:
            return f"{pull} is not open and ready, so these findings were dropped"
        headed = {name.lower(): sha for name, sha in heading_targets(text, repository).items()}
        named = [headed.get(pull, "")] if headed else _SHA_RE.findall(text)
        if not any(same_commit(head, token) for token in named):
            return f"these findings do not name {pull}'s head {head[:12]}, so they were dropped"
        escalated = _ESCALATED_RE.match(text) is not None
        kind = review_history.ESCALATED if escalated else review_history.FINDINGS
        data = {"told": False, "text": review_history.clip_findings(text)}
        self._note(pull, head, kind, f"{key}:{pull}:{kind}", at=at, data=data)
        self._check.wake()
        return f"the findings on {pull} at {head[:12]} are on its check"

    async def _check_review(self, delivery: Delivery, event: Event) -> None:
        """Review a pull request no Work owns (#4152): ``apply``'s own steps for a Work's, minus
        all that speaks to a chat. ``_review`` does the rest with an owner that has none."""
        own = self.own_logins()
        if not self._check.eligible(delivery.payload.get("pull_request"), own):
            return
        pull = f"{event.repository}#{event.number}".lower()
        reviewed = self._spent(pull, event.head)
        if event.confirm or reviewed:
            head = await self._live_ready_head(event.repository, event.number)
            if not head or (event.explicit and reviewed and head == event.head):
                # Marked ready again at a head already reviewed: Re-run is the way to ask.
                return
            event = replace(event, head=head, confirm=False)
        self._note_seen(pull, event.head)
        await self._review(("", pull), event)

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

    async def _maybe_start_unblocked(self, delivery: Delivery) -> None:
        """Start an issue once its last blocker closes (#3764, #4216).

        A map files a blocked ticket, and after its destination the blocked build issues, with
        no agent; ``WAYFINDER.md`` once left adding it to whoever closed the last blocker, and
        a chat that never read that (a Work the map adopted) or a person closing it by hand
        left the issue unstarted. Here any issue's close, under any login, reads the issues it
        blocks; a person removing a blocker (``blocked_by_removed``) checks the one issue it
        named. Each candidate's own blockers are read live, and it starts only with every one
        closed, so an issue with two blockers waits for both and a re-asked ticket, whose
        replacement blocks the same tickets before it closes, starts none. Only wayfinder
        tickets were started until #4216, which left a map's build issues unstarted. A
        candidate held by the paused label, closed, a wayfinder map, or labelled and already
        owned by a chat is left alone. Best effort: a failed read or write is logged and the
        delivery moves on; the Start button is still there."""
        if self._read_dependencies is None or self._start_ticket is None:
            return
        payload = delivery.payload
        action = _text(payload.get("action"))
        if delivery.event == "issues" and action == "closed":
            issue = payload.get("issue")
            if (
                not isinstance(issue, dict)
                or not isinstance(issue.get("number"), int)
                or isinstance(issue.get("pull_request"), dict)
            ):
                return
            repository, number = delivery.stream.lower(), issue["number"]
            try:
                candidates = await self._read_dependencies(repository, number, "blocking")
            except Exception:  # noqa: BLE001 -- nothing started; Start still can
                log.warning("work-router: %s#%s blocks unread", repository, number, exc_info=True)
                return
            if candidates is None:
                return
            cause = f"{repository}#{number} closed"
        elif delivery.event == "issue_dependencies" and action == "blocked_by_removed":
            blocked = payload.get("blocked_issue")
            if not isinstance(blocked, dict):
                return
            repo = payload.get("blocked_issue_repo")
            name = repo.get("full_name") if isinstance(repo, dict) else None
            candidates = [
                {
                    "repository": name if isinstance(name, str) else delivery.stream,
                    "number": blocked.get("number"),
                    "state": blocked.get("state"),
                    "labels": blocked.get("labels"),
                    "assignee": assignee_of(blocked),
                }
            ]
            cause = "a blocker removed"
        else:
            return
        for candidate in candidates:
            await self._start_if_unblocked(candidate, cause)

    async def _maybe_count_merge(self, delivery: Delivery) -> None:
        """Count a Work as done for the issues it blocks once its pull request merges (#4398).

        A blocker's issue closes only after its live check, hours or days after its code
        merged. A ``pull_request`` ``closed`` delivery that merged into its repository's
        default branch, of a pull request a Work registered, records ``(work, pull, base)``;
        only once that row is written are the issues it blocks started, the same path a close
        runs. A merge into another branch (a stacked pull request) is not the code a dependent
        builds on, and records nothing. An ``issues`` ``reopened`` drops the rows of the
        issue's own Work, found from the payload alone, so a reopened blocker blocks again
        whether or not a chat of it is loaded. A failed write is logged and starts nothing:
        the blocker's close still counts, as before."""
        payload = delivery.payload
        action = _text(payload.get("action"))
        if delivery.event == "issues" and action == "reopened":
            issue = payload.get("issue")
            if (
                self._forget_merged is None
                or not isinstance(issue, dict)
                or not isinstance(issue.get("number"), int)
                or isinstance(issue.get("pull_request"), dict)
            ):
                return
            work_id = WorkRef(delivery.stream.lower(), issue["number"]).work_id
            try:
                ok = await asyncio.to_thread(self._forget_merged, work_id)
            except Exception:  # noqa: BLE001 - logged; the rows stay
                ok = False
            if not ok:
                log.warning("work-router: merges of reopened %s not dropped", work_id)
            if self._start_round is not None:
                # A new round (#4585): its earlier pull requests stop counting for the
                # release, the live check and the page. A repeat adds nothing and keeps it.
                try:
                    await asyncio.to_thread(self._start_round, work_id)
                except Exception:  # noqa: BLE001 - logged; the Work counts everything
                    log.warning("work-router: round of reopened %s not started", work_id)
            return
        if self._note_merged is None or delivery.event != "pull_request" or action != "closed":
            return
        pull = payload.get("pull_request")
        if not isinstance(pull, dict) or not pull.get("merged"):
            return
        number = pull.get("number")
        base = pull.get("base")
        branch = _text(base.get("ref")) if isinstance(base, dict) else ""
        repo = payload.get("repository")
        default = _text(repo.get("default_branch")) if isinstance(repo, dict) else ""
        repository = delivery.stream.lower()
        if not isinstance(number, int) or not branch or branch != default:
            if isinstance(number, int):
                log.info(
                    "work-router: %s#%s merged into %r, not the default branch %r",
                    repository,
                    number,
                    branch,
                    default,
                )
            return
        owner = self._owner_of_pull(repository, number)
        if owner is None:
            return
        work = _work_id(owner)
        ref = f"{repository}#{number}"
        try:
            earlier = ref.lower() in self._earlier_pulls(work)
        except Exception:  # noqa: BLE001 - unread: recorded as before rounds
            earlier = False
        if earlier:
            # A late delivery of an earlier round's merge (#4585): it unblocks nothing now.
            log.info("work-router: merge of %s is from an earlier round of %s", ref, work)
            return
        try:
            ok = await asyncio.to_thread(self._note_merged, work, ref, branch)
        except Exception:  # noqa: BLE001 - logged; nothing is started from this merge
            ok = False
        if not ok:
            log.warning(
                "work-router: merge of %s for %s not recorded: nothing started", ref, work
            )
            return
        if self._read_dependencies is None or self._start_ticket is None:
            return
        blocker = WorkRef.from_work_id(work)
        if blocker is None:
            return
        try:
            candidates = await self._read_dependencies(
                blocker.repository, blocker.number, "blocking"
            )
        except Exception:  # noqa: BLE001 - nothing started; its close or Start still can
            log.warning("work-router: %s blocks unread", work, exc_info=True)
            return
        for candidate in candidates or ():
            await self._start_if_unblocked(candidate, f"{ref} merged")

    def _deployed_works(self) -> Collection[str]:
        """The deployed Works' ids, or none when unset or unread: then a blocker blocks."""
        if self._deployed is None:
            return ()
        try:
            return self._deployed()
        except Exception:  # noqa: BLE001 - unread: nothing counts as deployed
            log.warning("work-router: deployed Works unread", exc_info=True)
            return ()

    async def note_deployed(self, repository: str, number: int) -> dict:
        """Start the issues ``repository#number`` blocks, now that it is deployed (#4384).

        The web calls this once per live-check record of a Work it shows as Waiting for a
        try, after the set holding it is saved. The same path an issue's close runs
        (``_maybe_start_unblocked``): its ``blocking`` list read live, each candidate started
        only with every blocker closed or deployed. ``in_set`` says whether the saved set held
        it when read here: the web notes the record only then, so a set not yet saved is tried
        again. A failed read or start is logged and noted all the same, as a close's is: the
        blocker's own close, or the Start button, still starts the issue."""
        repository = _text(repository).lower()
        work_id = WorkRef(repository, number).work_id
        in_set = work_id in self._deployed_works()
        if not in_set:
            log.info("work-router: %s#%s noted deployed, but not in the set", repository, number)
            return {"in_set": False}
        if self._read_dependencies is None or self._start_ticket is None:
            return {"in_set": True}
        try:
            candidates = await self._read_dependencies(repository, number, "blocking")
        except Exception:  # noqa: BLE001 - nothing started; its close or Start still can
            log.warning("work-router: %s#%s blocks unread", repository, number, exc_info=True)
            candidates = None
        for candidate in candidates or ():
            await self._start_if_unblocked(candidate, f"{repository}#{number} deployed")
        return {"in_set": True}

    async def _start_if_unblocked(self, candidate: dict, cause: str) -> None:
        repository = _text(candidate.get("repository")).lower()
        number = candidate.get("number")
        if not repository or not isinstance(number, int):
            return
        ticket = f"{repository}#{number}"
        labels = candidate.get("labels")
        names = (
            [name for name in (_label_name(label) for label in labels) if name]
            if isinstance(labels, list)
            else []
        )
        folded = {name.casefold() for name in names}
        starts = {item.casefold() for item in self._start_labels(repository)}
        holds = {item.casefold() for item in self._hold_labels(repository) if item}
        labelled = bool(folded & starts)
        assignee = _text(candidate.get("assignee"))
        why = ""
        if _text(candidate.get("state")).lower() != "open":
            why = "it is closed"
        elif wayfinder_map.is_map(names):
            why = "it is a wayfinder map"
        elif not starts:
            why = "its repository starts no Works"
        elif folded & holds:
            why = "it is paused"
        elif labelled and self._chat_of_issue(repository, number) is not None:
            why = "it already has its start label and a chat"
        elif not self._mine(repository, assignee):
            why = "another deck works it"
        elif labelled and self._start_work is None:
            why = "it already has its start label, and another deck works it"
        if why:
            log.info("work-router: %s, %s not started: %s", cause, ticket, why)
            return
        try:
            blockers = await self._read_dependencies(repository, number, "blocked_by")
        except Exception:  # noqa: BLE001 -- nothing started; Start still can
            log.warning("work-router: blockers of %s unread", ticket, exc_info=True)
            return
        if blockers is None:
            return
        still = _open_blockers(blockers, self._deployed_works())
        if still:
            log.info("work-router: %s, %s waits on %s", cause, ticket, ", ".join(still))
            return
        if labelled:
            # Labelled while blocked, so its label's start waited (#4216): it starts now.
            log.info("work-router: %s, %s has no open blocker: starting its Work", cause, ticket)
            await self._start(repository, number, names, assignee)
            return
        try:
            outcome, text = await self._start_ticket(repository, number)
        except Exception:  # noqa: BLE001 -- the write may not have landed; Start still can
            log.warning("work-router: %s, starting %s failed", cause, ticket, exc_info=True)
            return
        log.info("work-router: %s, %s has no open blocker: %s %s", cause, ticket, outcome, text)

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
            # Dated by the comment itself, so a verdict delivered late is not the newest (#3957).
            posted = _when(_text(comment.get("created_at")))
            # One verdict line per pull request, in one comment, each taken on its own (#3161).
            settled = True
            for command in commands:
                targets = command.targets or ((f"{repository}#{number}",) if is_pull else ())
                # Who may send a verdict is the release's call (the Poller's tier for it), not
                # this filter's: Release where a pass merges by itself, Work where a person
                # still merges.
                work_id = _work_id(owner)
                answer = await release.on_verdict(
                    work_id,
                    command.verdict,
                    command.head,
                    command.digest,
                    targets,
                    by=login,
                    source=f"comment:{comment.get('id')}" if comment.get("id") else "",
                    at=posted.timestamp() if posted is not None else None,
                    stamp=review_history.round_stamp(body),
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
        elif delivery.event == "push":
            # Any push to a branch: a pull request waiting on a Merge that merges into it may
            # conflict now, whoever's it was (#4217). The release looks only at those.
            ref = _text(payload.get("ref"))
            if ref.startswith("refs/heads/") and not payload.get("deleted"):
                looked = release.on_base_moved(repository, ref.removeprefix("refs/heads/"))
                if looked:
                    log.info("work-router: %s moved; looking again at %s", ref, looked)
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
        elif delivery.event in {"check_suite", "check_run"} and action == "completed":
            # A finished run too, not only its suite: GitHub can send the suite's ``completed``
            # before the rollup reads its last run done (#3911).
            suite = payload.get(delivery.event)
            pulls = suite.get("pull_requests") if isinstance(suite, dict) else None
            # The deck's own review check finishing is no CI news (#4152).
            own_check = review_history.deck_check_delivery(delivery.event, payload)
            for item in () if own_check or not isinstance(pulls, list) else pulls:
                number = item.get("number") if isinstance(item, dict) else None
                owner = self._owner_of_pull(repository, number) if isinstance(number, int) else None
                if owner is not None:
                    await release.on_checks_done(_work_id(owner))
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
        """Start a Work a label or an assignment delivery asks for, unless a filing holds it.
        An open blocker holds it in ``start_work`` (``start_unless_blocked``, #4216)."""
        filing = self._filing.get(repository) or self.filing_holds(repository)
        if filing and not delivery.delivery_id.startswith("deck-filed:"):
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
        if outcome == BLOCKED:
            # Its last blocker closing starts it (``_start_if_unblocked``), not a retry.
            log.info("work-router: start of %s#%s waits on a blocker", repository, number)
            self.pending.discard(key)
        elif _start_is_retryable(outcome):
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

    def note_filed(
        self, repository: str, number: int, labels: Iterable[str], assignee: str = ""
    ) -> bool:
        """The deck just filed or labelled this issue: start its Work now (#3371).

        GitHub can hold its webhook deliveries back for many minutes, and the backstop only
        looks every 15. This feeds ``_maybe_start`` the "label added" delivery GitHub would
        have sent, with the issue's first ``assignee`` (#4455: without it a deck that leaves
        unassigned issues to another refused the start), in the background so the filer is
        not held up by the start. When GitHub's own delivery arrives later, ``start_work``
        has already spent the Work's one start. ``False``: nothing to start here (not
        switched, no start label, or another deck's issue).
        """
        repository = repository.lower()
        names = [str(name).strip() for name in labels if str(name).strip()]
        label = self.starts_on(repository, names)
        assignee = str(assignee or "").strip().lower()
        if not label or not self._mine(repository, assignee):
            return False
        issue: dict[str, object] = {
            "number": number,
            "state": "open",
            "labels": [{"name": name} for name in names],
        }
        if assignee:
            issue["assignee"] = {"login": assignee}
        payload = {"action": "labeled", "issue": issue, "label": {"name": label}}
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
        self._sweep_wake.set()
        check = asyncio.create_task(
            self.check_registered(repository, number), name="work-router-registered"
        )
        self._checks.add(check)
        check.add_done_callback(self._checks.discard)

    async def ready_press(
        self, repository: str, number: int, budget: float = READY_PRESS_S, *, caller: str = ""
    ) -> dict:
        """A Work agent's own ``gh pr ready`` (#4253): the pre-review checklist it starts, as
        ``checklist`` text for that command's reply with the ``heads`` it is for, or an empty
        one when none was made while it waited.

        ``caller`` is the pressing chat (``chat_identity_key``). Only when it is the chat
        ``owner_of_pull`` places the pull request with is a checklist handed back (#4253
        review): a pull request another chat registered too may belong to that chat's Work,
        whose checklist then goes to it as a message.

        Looks at the pull request as a registration does (``check_registered``). A checklist
        any path makes for it within ``budget`` (this look, or the ready event racing it) is
        recorded as sent and handed back here instead of waking the chat. Past ``budget`` the
        look goes on in the background and its checklist is sent as a message, as before; so
        is one for a head whose checklist was already sent. Either way it goes out once.
        """
        repository = repository.lower()
        pull = f"{repository}#{number}"
        if not self._enabled or self._ready_head is None or not self._switched(repository):
            return {"checklist": "", "heads": []}
        claim: list[tuple[str, str]] = []
        self._printing[pull] = (caller, claim)
        check = asyncio.create_task(
            self.check_registered(repository, number), name="work-router-ready-press"
        )
        self._checks.add(check)
        check.add_done_callback(self._checks.discard)
        try:
            await asyncio.wait({check}, timeout=budget)
        finally:
            if (self._printing.get(pull) or ("", None))[1] is claim:
                del self._printing[pull]
        return {
            "checklist": "\n\n".join(text for _head, text in claim),
            "heads": [head for head, _text in claim],
        }

    def ready_received(
        self, repository: str, number: int, heads: Iterable[str], *, caller: str
    ) -> int:
        """The agent printed the checklist its ``gh pr ready`` was handed (#4253 review).

        Takes back the message each such checklist was written down as, so it is not sent
        too: only at the ``heads`` that reply named, and only when ``caller`` is the chat the
        message is for. How many were taken; one sent already, never handed over, or another
        chat's, is not touched.
        """
        pull = f"{repository.lower()}#{number}"
        taken = 0
        for head in {str(head).lower() for head in heads if head}:
            key = f"message:work-event:pre-review:{pull}@{head}"
            entry = self.pending.entries.get(key) or {}
            if entry.get("receipt") is True and caller and entry.get("chat_id") == caller:
                self.pending.discard(key)
                taken += 1
        return taken

    async def _live_ready_head(self, repository: str, number: int) -> str:
        """The open, non-draft pull request's head read live; ``""`` if none or unreadable."""
        if self._ready_head is None:
            return ""
        try:
            return (await self._ready_head(repository, number) or "").lower()
        except Exception:  # noqa: BLE001 - the next pull request event looks again
            log.warning("work-router: %s#%s unreadable", repository, number, exc_info=True)
            return ""

    async def _live_open_head(self, repository: str, number: int) -> str:
        """The open pull request's head read live, a draft's too; ``""`` if none or unreadable.

        A verdict answers the review asked of a head whatever the pull request became since:
        an agent that turned it back to draft for a change of scope while its review ran still
        has that head's findings to fix or to ship past (#4352). Without ``open_head`` only a
        ready head is read, as before."""
        if self._open_head is None:
            return await self._live_ready_head(repository, number)
        try:
            return (await self._open_head(repository, number) or "").lower()
        except Exception:  # noqa: BLE001 - unread marks nothing; the agent asks again
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
        # A registration is a deliberate look, like a ready press: a new head gets its own
        # checklist even while an older one waits, so a missed ready event is not silent (#3896).
        await self._review(
            owner, Event(REVIEW, repository, number, True, head=head), checklist_again=True
        )
        pull = f"{repository}#{number}"
        held = self.pre_review_held(pull)
        return self._spent(pull, head) or f"review:{pull}@{head}" in self.pending or held

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
        asked = self._again_count(f"{repository}#{number}", head, no_verdict=no_verdict)
        if asked >= (MAX_NO_VERDICT if no_verdict else MAX_AGAIN):
            return _cap_refusal(repository, number, head, no_verdict)
        return ""

    def _again_count(self, pull: str, head: str, *, no_verdict: bool = False) -> int:
        """How many reviews of ``head`` were asked for again, of the kind ``no_verdict`` says."""
        return sum(
            event.kind == review_history.ASKED
            and bool(str(event.data.get("again") or ""))
            and bool(event.data.get("no_verdict")) == no_verdict
            for event in review_history.at_head(self._history(pull), head)
        )

    async def _ready_again(
        self, delivery: Delivery, owner: Owner, repository: str, number: int, head: str
    ) -> None:
        """Answer a ready press at a head a review was already asked for, in four ways (#3584).

        An ``asked`` event only says a review was asked for, not that a verdict came: told "already
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
        if self.reviewer_off:
            self._send_refusal(delivery, owner, REVIEWER_OFF_TEXT)
            return
        if self._passed_since(pull, head, 0.0):
            # The head already passed (#3341): there is nothing to ask again, and nothing for
            # the chat to do, so it is not woken to read so (#4253).
            log.info("work-router: %s marked ready again; it already has a review pass", at)
            if self._carried_pass(pull, head):
                self._stop_on_carried(owner, pull, head)
            return
        # A verdict a newer ask came after is not standing: that review is awaited, and the
        # verdict before it may have been a pass (#4658).
        if self._has_verdict(pull, head):
            self._send_refusal(
                delivery,
                owner,
                f"{at} was reviewed and has findings: fix them. {SIBLING_SWEEP_ASK} Then push"
                " a new commit (or, if"
                " the owner settled them on the issue with no code change, POST"
                " $AGENTDECK_URL/api/sessions/<session-key>/review-accepted with a note saying"
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
        if waits:
            # The deck registered the chat's wait on that verdict itself: nothing is left for
            # the chat to do, so it is not woken to read so (#4253). Unregistered, it is told.
            log.info("work-router: %s marked ready again; its review runs%s", at, where)
            return
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

    def _tell_no_reviewer(self, owner: Owner, pull: str, head: str) -> None:
        """Tell the chat once per head that no review is coming (#3849); the id absorbs a
        replayed ready event and a pending ask retried after an upgrade."""
        if not owner[0]:
            return
        now = datetime.now(UTC)
        self._deliver(
            ScheduledMessage(
                id=f"work-event:no-reviewer:{pull}@{head}",
                chat_id=owner[0],
                text=f"AgentDeck: {pull} at {head[:12]} gets no review: {REVIEWER_OFF_TEXT}."
                " Mark it ready and end the turn; do not wait on a review.",
                due_at=now,
                created_at=now,
            )
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
        owner = self._review_owner(repository, number)
        if owner is None:
            raise AgainRefused(f"no Work's chat registered {pull}")
        if self.reviewer_off:
            raise AgainRefused(REVIEWER_OFF_TEXT)
        if not self._switched(repository):
            raise AgainRefused(f"{repository} is not a repository any poller project declares")
        head = await self._live_ready_head(repository, number)
        if not head:
            raise AgainRefused(
                f"{pull} is not open and ready for review (a draft, closed, or unreadable now):"
                " mark it ready with `gh pr ready` instead"
            )
        if self._spent(pull, head) and not self._has_verdict(pull, head):
            found = await self._recover_verdict(owner, [(pull, head)])
            if found:
                raise AgainRefused(
                    f"a verdict on {pull} at {head[:12]} was already posted ({found}), and"
                    " the deck had missed its webhook. It is now taken in and sent to this"
                    " chat, so no review is missing and none was started: act on it"
                )
        if no_verdict and not self._has_verdict(pull, head):
            found = await self._recover_findings(owner, pull, head, note)
            if found:
                raise AgainRefused(
                    f"the review chat's closing line points at findings already posted for"
                    f" {pull} at {head[:12]} ({found}), which the deck had not taken in. They"
                    " are now recorded at that head and sent to this chat, so no review is"
                    " missing: act on them, or ask the owner to accept them or ship now"
                )
        if no_verdict and self._has_verdict(pull, head):
            raise AgainRefused(
                f"{pull} at {head[:12]} has its verdict, so no review is missing: read it in"
                " this chat or on the Work page, and do not ask a person about this refusal"
            )
        key = f"{pull}@{head}"
        running = key in self._in_flight or f"review:{key}" in self.pending
        # A kept request left alone, its start ended, takes the newer note too (#4658).
        running = running or bool(self._requests(pull, head))
        if running and not no_verdict:
            self._keep_request(pull, head, note)
        why = self._again_refusal(repository, number, head, no_verdict=no_verdict)
        if why and no_verdict and self._hand_closed_to_owner(pull, head, note):
            raise AgainRefused(
                f"{pull} at {head[:12]} was reviewed again {MAX_NO_VERDICT} times after a"
                " review with no verdict, its last review chat has ended, and no findings it"
                " cites could be read back. The deck has handed this head to the owner as an"
                " escalation, so Ship now and `review-accepted` work: ask the owner in your"
                " question box whether to ship this commit as it is, accept it, or have you"
                " change something, and act on their answer"
            )
        if why:
            raise AgainRefused(why)
        kind = NO_VERDICT if no_verdict else ""
        again = kind + secrets.token_hex(6) if self._spent(pull, head) else ""
        if again and not note.strip():
            # The note is the only thing that tells the reviewer what changed at a commit it
            # already reviewed; without one the new chat reads as a duplicate (#3344).
            raise AgainRefused(
                f"{pull} at {head[:12]} was already reviewed: say in the note what changed"
                " since (a person's decision, a posted walkthrough)"
            )
        missing = await self._design_gap(owner)
        if missing:
            raise AgainRefused(f"{pull} gets no code review before its design: {missing}")
        # Its ``asked`` event ends a checklist still waiting (#3933), so the Work page does not
        # say it waits on it for good.
        await self._review(
            owner,
            Event(REVIEW, repository, number, True, head=head, again=again, note=note),
            asked=True,
        )
        return {
            "pull_request": pull,
            "head": head,
            "again": bool(again),
            "outcome": "pending"
            if self._requests(pull, head) or not self._spent(pull, head, again)
            else "asked",
        }

    async def _note_answered(
        self, delivery: Delivery, event: Event, owner: Owner, *, now: bool = False
    ) -> None:
        """Write down that findings or an escalation answered the review of each pull request
        they are about, as a ``findings`` or ``escalated`` event at its live head, when that
        head was sent for review and the comment is about it alone: its heading names that
        commit for that pull request (#4069), or with no heading at all its text names that
        commit id and no other head sent for review (#3828 review).

        Only such a head may pass on the owner's acceptance (``accept``): the review was
        asked for and came back with something for the owner to accept. Findings about an
        older head that arrive after a push name that head, not the live one, so they never
        mark it (owner's decision on #3828); text naming no commit marks nothing, and the
        agent asks for a fresh review instead. A heading names one commit however many older
        rounds the body retells, so an escalation marks its head at once (owner's decision on
        #4069).
        """
        named = {token.lower() for token in _SHA_RE.findall(event.text)}
        if not named:
            return
        # Dated by the comment itself, so a late delivery is not the newest (#3957), and sent:
        # this runs only once its message was stored.
        comment = delivery.payload.get("comment")
        posted = _when(_text(comment.get("created_at"))) if isinstance(comment, dict) else None
        if now:
            # Read back for a later review that cited it (#4279): it answers that review, so
            # it is dated now, after the asks it answers.
            posted = None
        body = _text(comment.get("body")) if isinstance(comment, dict) else ""
        headed = heading_targets(body, event.repository)
        pulls = self._verdict_pulls(event, owner)
        if pulls is None:
            read = getattr(getattr(self._release, "ports", None), "pulls", None)
            try:
                held = read(_work_id(owner)) if read else ()
                pulls = sorted({str(pull).lower() for pull in held})
            except Exception:  # noqa: BLE001 - unnoted: an acceptance is refused, not wrong
                log.warning("work-router: could not list %s's pulls", owner[1], exc_info=True)
                pulls = []
        for pull in pulls:
            repository, _, number = pull.lower().rpartition("#")
            if not number.isdigit():
                continue
            # A draft's head too (#4352): the review was asked of it while it was ready.
            head = await self._live_open_head(repository, int(number))
            if not head:
                continue
            # Exactly one head sent for review named, the live one (owner's decision on the
            # second #3828 escalation): text about an older head that mentions the new one
            # in passing names two, and marks nothing. A heading says which pull requests
            # and commits it means, so only those count, whatever the body quotes.
            pull = f"{repository}#{number}"
            if headed and pull not in headed:
                continue
            about = {headed[pull]} if headed else named
            history = self._history(pull)
            # A replaced review chat's findings answer an old round (#4386): the release's
            # ``stale_findings`` drops them before they are sent, and a recovered copy, which
            # is sent here without it, marks nothing either.
            if review_history.replaced_round(history, review_history.round_stamp(body)):
                continue
            asked = {e.head for e in history if e.kind == review_history.ASKED}
            if {item for item in asked if any(item.startswith(t) for t in about)} == {head}:
                escalated = ESCALATED_ASK in event.text
                kind = review_history.ESCALATED if escalated else review_history.FINDINGS
                at = posted.timestamp() if posted is not None else None
                key = f"answered:{delivery.delivery_id}:{pull}"
                data = {"told": True, "text": review_history.clip_findings(body)}
                self._note(pull, head, kind, key, at=at, data=data)

    def _closed_review(self, pull: str, head: str) -> str:
        """What to do when ``head``'s newest review chat ended with no verdict the deck took
        in, as the tail of a refusal; ``""`` while it runs or is unknown (#4279). Waiting then
        only repeats the refusal: the way out is asking again with the chat's closing line."""
        if self._has_verdict(pull, head):
            return ""
        chat, row = self._review_chat_of(pull, head)
        if row is None or not _review_ended(row, self._clock()):
            return ""
        return (
            f". Its review chat {chat} has ended with no verdict the deck took in, so waiting"
            " changes nothing: POST $AGENTDECK_URL/api/sessions/<session-key>/review-request"
            f' with `{{"pull_request": "{pull}", "no_verdict": true, "note": "<the review'
            " chat's closing line, with any findings link it gives>\"}`. When that line links"
            " findings already posted for this head, the deck reads them and records them"
            " here, and Ship now and `review-accepted` then work; otherwise it asks for a fresh"
            " review, and once those asks are spent it hands this head to the owner, after"
            " which Ship now and `review-accepted` work too"
        )

    def _hand_closed_to_owner(self, pull: str, head: str, note: str) -> bool:
        """Record an escalation at ``head`` when its no-verdict asks are spent and its last
        review chat has ended with no verdict, so the owner can accept or ship past it; whether
        one was recorded (#4279 review). Without it a reviewer that keeps closing with no
        verdict and no findings the deck can read back leaves the head with nothing to accept,
        nothing to ship past and no review left to ask for. Once, by key, per head."""
        if self._has_verdict(pull, head):
            return False
        _chat, row = self._review_chat_of(pull, head)
        if row is None or not _review_ended(row, self._clock()):
            return False
        text = (
            f"The review of {pull} at {head} ended {MAX_NO_VERDICT + 1} times with no verdict"
            " the deck could take in. Its last closing line:\n\n"
            + (note.strip() or "(none given)")
        )
        data = {"told": True, "text": review_history.clip_findings(text), "closed": True}
        key = f"closed-no-verdict:{pull}@{head}"
        self._note(pull, head, review_history.ESCALATED, key, data=data)
        return True

    async def _recover_findings(self, owner: Owner, pull: str, head: str, note: str) -> str:
        """The link of findings about ``pull`` at ``head`` that a review chat's closing line,
        quoted in ``note``, points at and the deck never took in; handed to the Work's chat
        and recorded at that head as the reviewer's comment would have been. ``""`` when there
        are none (#4279).

        A reviewer closes a later review of a head it already answered by citing its earlier
        findings and posting nothing new. When the deck had missed that first comment, every
        later review ended the same way, and the head had neither findings for the owner to
        accept or ship past nor a review left to ask for. Only a comment on the Work's own
        issue or pull request counts, from the reviewer, whose heading names this head.
        """
        if self._read_comment is None or not owner[0] or not note:
            return ""
        own = self.own_logins()
        for found in _COMMENT_URL_RE.finditer(note):
            stream, kind = found.group(1).lower(), found.group(2)
            number, ident = int(found.group(3)), int(found.group(4))
            if owner not in (
                self._owner_of_issue(stream, number),
                self._owner_of_pull(stream, number),
            ):
                continue
            try:
                comment = await self._read_comment(stream, ident)
            except Exception:  # noqa: BLE001 - unread recovers nothing; the review is asked
                log.warning("work-router: comment %s unread", ident, exc_info=True)
                continue
            if not isinstance(comment, dict):
                continue
            body = _text(comment.get("body"))
            about = heading_targets(body, stream).get(pull, "")
            if not same_commit(head, about):
                continue
            # From a review chat a newer review replaced (#4386): an old round's findings,
            # which this round's owner must not accept, so neither sent nor recorded.
            if review_history.replaced_round(self._history(pull), review_history.round_stamp(body)):
                continue
            issue: dict = {"number": number}
            if kind == "pull":
                issue["pull_request"] = {}
            payload = {
                "action": "created",
                "issue": issue,
                "comment": comment,
                "sender": comment.get("user"),
            }
            delivery = Delivery(
                stream=stream,
                delivery_id=f"recovered-findings:{ident}",
                event="issue_comment",
                received_at=datetime.now(UTC).isoformat(),
                headers={},
                raw_body=b"",
                offset=0,
                _payload=payload,
            )
            event = classify(delivery, own_logins=own)
            if event is None or not event.verdict:
                continue
            if not self._send(delivery, owner, event):
                continue
            if self._on_verdict is not None:
                try:
                    self._on_verdict(owner[0], [pull])
                except Exception:  # noqa: BLE001 - a stale wait times out on its own
                    log.warning("work-router: ending review waits failed", exc_info=True)
            await self._note_answered(delivery, event, owner, now=True)
            if self._has_verdict(pull, head):
                log.info("work-router: recovered %s's findings at %s from %s", pull, head, ident)
                return found.group(0)
        return ""

    async def recover_review(self, repository: str, number: int) -> str:
        """Take in a verdict posted for a timed-out review wait on ``repository#number`` that
        its webhook never brought (#4478); the URL of the comment, or ``""``.

        The wait names a pull request, or the Work's issue while one of its pull requests is
        reviewed (#4476). The head is the one each pull request's review was last asked for,
        from its own history: a wait stores none, and no GitHub read is needed for it.
        """
        repository = repository.lower()
        ref = f"{repository}#{number}"
        pulls = [ref] if self._newest_ask(ref) is not None else []
        owner = self._owner_of_pull(repository, number) if pulls else None
        if not pulls:
            owner = self._owner_of_issue(repository, number)
            if owner is None:
                return ""
            pulls = self._work_pulls(owner)
        asks = [(pull, self._newest_ask(pull)) for pull in pulls]
        newest = sorted(
            ((pull, ask) for pull, ask in asks if ask is not None),
            key=lambda item: item[1].at,
            reverse=True,
        )[:RECOVER_PULLS]
        return await self._recover_verdict(owner, [(pull, ask.head) for pull, ask in newest])

    def _newest_ask(self, pull: str) -> review_history.ReviewEvent | None:
        asks = [e for e in self._history(pull) if e.kind == review_history.ASKED]
        return max(asks, key=lambda event: event.at) if asks else None

    def _work_pulls(self, owner: Owner) -> list[str]:
        """The pull requests ``owner``'s Work registered, lowercased; none when unread."""
        read = getattr(getattr(self._release, "ports", None), "pulls", None)
        try:
            held = read(_work_id(owner)) if read else ()
            return sorted({str(pull).lower() for pull in held})
        except Exception:  # noqa: BLE001 - none recovered: today's path runs
            log.warning("work-router: could not list %s's pulls", owner[1], exc_info=True)
            return []

    async def _recover_verdict(self, owner: Owner | None, candidates: list[tuple[str, str]]) -> str:
        """The URL of a ``/reviewed`` comment about one of ``candidates`` (``pull``, ``head``)
        that the deck never took in, now taken in through ``apply``, as its webhook would have
        been, with every check that path makes; ``""`` when none (#4478).

        Only the Work's issue and each candidate's own thread are read, once each, from just
        before the first ask at its head, and only lines from the reviewer's account, the
        deck's own, or the reviewer's connector are handed on. Who may send it, which pull
        request, head, diff and round it answers stay the release's call. A read that fails
        finds nothing, and the caller goes on as it did before.
        """
        from .poller_engine.intake import CommandKind, parse_verdicts

        if self._read_comments is None or not candidates:
            return ""
        threads: dict[tuple[str, int], bool] = {}
        if owner is not None and owner[1]:
            stream, _, issue = owner[1].lower().rpartition("#")
            if issue.isdigit():
                threads[(stream, int(issue))] = False
        for pull, _head in candidates:
            stream, _, number = pull.rpartition("#")
            if number.isdigit():
                threads[(stream, int(number))] = True
        firsts = [
            min(event.at for event in asks)
            for pull, head in candidates
            if (
                asks := [
                    event
                    for event in review_history.at_head(self._history(pull), head)
                    if event.kind == review_history.ASKED
                ]
            )
        ]
        since = ""
        if firsts:
            moment = datetime.fromtimestamp(min(firsts) - RECOVER_SLACK_S, UTC)
            since = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        own = self.own_logins()
        reviewers = reviewer_logins()
        heads = [head for _pull, head in candidates]
        found: dict[str, str] = {}  # head -> the URL of the comment about it
        # A comment the deck already took in is never handed on again: its verdict was told.
        taken = {
            event.key.rsplit(":", 1)[0]
            for pull, _head in candidates
            for event in self._history(pull)
            if event.key.startswith("comment:")
        }
        seen: set[object] = set()
        for (stream, number), is_pull in threads.items():
            try:
                comments = await self._read_comments(stream, number, since)
            except Exception:  # noqa: BLE001 - unread recovers nothing: today's path runs
                log.warning("work-router: %s#%s comments unread", stream, number, exc_info=True)
                continue
            for comment in comments or ():
                if not isinstance(comment, dict) or comment.get("id") in seen:
                    continue
                seen.add(comment.get("id"))
                if f"comment:{comment.get('id')}" in taken:
                    continue
                user = comment.get("user")
                login = _text(user.get("login")) if isinstance(user, dict) else ""
                app = comment.get("performed_via_github_app")
                if not (
                    login.casefold() in reviewers
                    or login.casefold() in own
                    or (isinstance(app, dict) and app.get("slug") == REVIEWER_CONNECTOR)
                ):
                    continue
                about = [
                    head
                    for command in parse_verdicts(_text(comment.get("body"))) or ()
                    if command.kind == CommandKind.REVIEWED
                    for head in heads
                    if same_commit(head, (command.head or "").lower())
                ]
                if not about:
                    continue
                issue: dict = {"number": number}
                if is_pull:
                    issue["pull_request"] = {}
                delivery = Delivery(
                    stream=stream,
                    delivery_id=f"recovered-verdict:{comment.get('id')}",
                    event="issue_comment",
                    received_at=datetime.now(UTC).isoformat(),
                    headers={},
                    raw_body=b"",
                    offset=0,
                    _payload={
                        "action": "created",
                        "issue": issue,
                        "comment": comment,
                        "sender": user,
                        "repository": {"full_name": stream},
                    },
                )
                log.info(
                    "work-router: taking in %s's missed verdict %s",
                    stream,
                    _text(comment.get("html_url")),
                )
                await self.apply([delivery])
                # Only a verdict the release recorded names its comment: a refused one (a
                # wrong digest, a stale round) is no answer to the wait (#4479 review).
                key = f"comment:{comment.get('id')}"
                for pull, head in candidates:
                    if head in about and any(
                        event.key.rsplit(":", 1)[0] == key
                        for event in review_history.at_head(self._history(pull), head)
                    ):
                        found[head] = _text(comment.get("html_url")) or f"{stream}#{number}"
        for pull, head in candidates:
            if head in found and self._has_verdict(pull, head):
                return found[head]
        return ""

    async def accept(self, repository: str, number: int, note: str) -> dict:
        """Count the review of the pull request's ready head as a pass, because the owner
        accepted its findings with no code change (#3828).

        Only at a head whose review came back with findings or an escalation
        (``review_history.answered``), so no diff the reviewer never finished passes this way.
        Raises ``AgainRefused`` with the reason otherwise; answers the release's ``outcome``
        (``review pass recorded for …``).
        """
        repository = repository.lower()
        pull = f"{repository}#{number}"
        owner = self._owner_of_pull(repository, number)
        if owner is None:
            raise AgainRefused(f"no Work's chat registered {pull}")
        if not self._switched(repository) or self._release is None:
            raise AgainRefused(f"the deck does not release {repository}'s Works")
        head = await self._live_ready_head(repository, number)
        if not head:
            raise AgainRefused(
                f"{pull} is not open and ready for review (a draft, closed, or unreadable now):"
                " mark it ready with `gh pr ready` first"
            )
        history = self._history(pull)
        if not review_history.answered(history, head):
            # Asked for is not answered (#3828 review): a review still running has returned
            # nothing the owner could have accepted.
            why = (
                f"no findings or escalation came back from a review of {pull} at {head[:12]},"
                " so there is nothing at it to accept"
            )
            closed = self._closed_review(pull, head)
            if closed:
                raise AgainRefused(why + closed)
            if review_history.head_review(review_history.at_head(history, head)).review_since:
                # An escalation with no commit in its first line that retells older rounds
                # names several heads, so its comment marks none; the review chat's closing line
                # names the one it reviewed, minutes later (#4069). Waiting for that line costs
                # no review round.
                raise AgainRefused(
                    why + " yet: its review is still open. If the reviewer's escalation or"
                    " findings are already posted, its review chat's closing line records them"
                    f" at {head[:12]} within minutes: register a 10-minute time wait"
                    " (`wake-on` with `after_minutes`) and call `review-accepted` again then,"
                    " rather than asking for a new review"
                )
            raise AgainRefused(
                why + ": wait for the review's verdict, and a push after the review is"
                " reviewed as usual"
            )
        answer = str(await self._release.owner_accepted(_work_id(owner), pull, head, note))
        from .work_release import PASS_RECORDED

        if not answer.startswith(PASS_RECORDED):
            raise AgainRefused(answer)
        self.pending.discard(f"review:{pull}@{head}")
        return {"pull_request": pull, "head": head, "outcome": answer}

    async def ship_now(self, repository: str, number: int, head: str = "") -> dict:
        """Ship the pull request at the head its review sent back with findings, and leave
        those findings to this same Work (#4212): the owner's Ship now, fix later.

        The head must be the one the reviewer answered, with no newer review asked for
        (``review_history.shippable``), so nothing the reviewer never read ships. A draft the
        agent made of it is marked ready first, and refused if the branch moved meanwhile. The
        head is recorded for this Work, whose agent fixes its findings (the owner chose no new
        issue); only then does the review count as a pass there,
        as an owner's acceptance does, and a press cut off on the way is finished by the next
        one (``_ship_now``). A project that waits for a person's Merge gets that press too,
        since this press is one. The Work stays open after the merge: its agent fixes the
        findings in a new pull request, reviewed as any other (the owner chose no re-read of
        the shipped change). The agent hears all of it in one message.

        ``head``, when given, is the head whose findings the owner's confirm showed: a live
        head past it ships nothing (#4212 review).

        A press can take minutes (#4558: three steps under the Work's lock, each reading every
        pull request of the Work), longer than the web waits for a call. So the press runs as
        a task of its own, keyed by the ``pull@head`` the caller sent, and the call waits at
        most :data:`SHIP_WAIT_S` for it: then it answers ``state`` ``running`` and the task
        goes on, telling the Work's chat how it ended. A second call for the same key joins
        the task; one for a head already marked shipped answers ``recorded`` before any
        GitHub read, so a repeat after the merge is safe too.

        Raises ``AgainRefused`` with the reason; answers ``pull_request``, ``head``, ``state``
        (``recorded`` or ``running``) and ``outcome``.
        """
        repository = repository.lower()
        pull = f"{repository}#{number}"
        owner = self._owner_of_pull(repository, number)
        if owner is None:
            raise AgainRefused(f"no Work's chat registered {pull}")
        ship = self._ship
        if not self._switched(repository) or self._release is None or ship is None:
            raise AgainRefused(f"the deck does not release {repository}'s Works")
        shown = head.lower()
        recorded = self._recorded_ship(ship, owner, pull, shown)
        if recorded is not None:
            return recorded
        runs = self.ship_runs
        key = f"{pull}@{shown}"
        task = runs.tasks.get(key)
        if task is None:
            press = self._ship_task(key, ship, owner, repository, number, shown)
            task = asyncio.ensure_future(press)
            # Retrieved here, so a task whose every caller left logs no "never retrieved".
            task.add_done_callback(lambda done: done.cancelled() or done.exception())
            runs.tasks[key] = task
        # ``wait`` leaves the task running, and tells its own TimeoutError from the wait's: a
        # press that ended answers with its end, and one still going is marked left in the
        # same step, so its end can never miss the chat (#4559 helper checks).
        await asyncio.wait({task}, timeout=SHIP_WAIT_S)
        if task.done():
            return task.result()
        runs.left.add(key)
        return {
            "pull_request": pull,
            "head": shown,
            "state": SHIP_RUNNING,
            "outcome": (
                f"Ship now of {pull}" + (f" at {shown[:12]}" if shown else "") + " is still"
                " running in the merge service"
            ),
        }

    def _recorded_ship(self, ship: ShipPorts, owner: Owner, pull: str, shown: str) -> dict | None:
        """The ``recorded`` answer when ``pull`` at ``shown`` is already marked shipped for
        this Work, read before anything from GitHub (#4558): ``pull`` itself, or the pull
        request split from it to ship that commit (its ``fix`` is ``pull``). ``None`` else."""
        if len(shown) != 40 or ship.shipped_by_work is None:
            return None
        try:
            records = ship.shipped_by_work(owner[1].lower())
        except Exception:  # noqa: BLE001 - unread: the press itself finds what is recorded
            log.warning("work-router: shipped heads of %s unreadable", owner[1], exc_info=True)
            return None
        for record in records:
            if record.shipped and record.head.lower() == shown:
                if pull in (record.pull.lower(), (record.fix or "").lower()):
                    return _recorded(record.pull.lower(), shown)
        return None

    async def _ship_task(
        self,
        key: str,
        ship: ShipPorts,
        owner: Owner,
        repository: str,
        number: int,
        shown: str,
    ) -> dict:
        """One press, run apart from its callers (#4558). A press its callers stopped waiting
        for tells the Work's chat when it was refused, decided after the closing look so a
        caller whose wait ran out during that look hears it too; a finished one already does.
        Every end takes the task out of the table first and then makes the release's one look,
        so the Work's hold line no longer says Ship now is under way."""
        runs = self.ship_runs
        work_id = _work_id(owner)
        pull = f"{repository}#{number}"
        stopped = ""
        try:
            return await self._ship_press(key, ship, owner, repository, number, shown)
        except AgainRefused as refused:
            stopped = str(refused)
            raise
        except Exception as exc:
            log.exception("work-router: Ship now of %s stopped", key)
            stopped = f"it failed ({exc})"
            raise
        finally:
            # One rule for every end (#4558 redesign): off the running list first, then the
            # release's one look, which leaves the Work due again if it raises.
            held = {pull, runs.resolved.get(key, "").rpartition("@")[0]} - {""}
            runs.tasks.pop(key, None)
            runs.resolved.pop(key, None)
            # Review starts held while it ran (#4576) go now, with its pass written or not: a
            # pull request no other press holds is reviewed, or its pass carried, at once.
            free = [name for name in held if not self._ship_holds(name)]
            if self.pending.hurry((f"review:{name}@" for name in free), self._clock()):
                log.info("work-router: review starts held by Ship now of %s are due", key)
            # Test fakes of the release may have only ``evaluate``.
            look = getattr(self._release, "look_after_ship", None) or self._release.evaluate
            try:
                await look(work_id)
            except Exception:  # noqa: BLE001 - the real look marks its own retry
                log.warning("work-router: release of %s after Ship now failed", pull)
            # Decided only after the look: a caller whose wait ran out during the look was
            # answered ``running`` and must hear the refusal too (#4559 review).
            if stopped:
                self._tell_ship_stopped(key, owner, pull, shown, stopped)
            if key not in runs.tasks:  # a newer press of the same head keeps its own mark
                runs.left.discard(key)

    def _tell_ship_stopped(self, key: str, owner: Owner, pull: str, shown: str, why: str) -> None:
        """Tell the Work's chat a press it was told is running ended without shipping."""
        if key not in self.ship_runs.left:
            return
        now = datetime.now(UTC)
        at = f" at {shown[:12]}" if shown else ""
        self._deliver(
            ScheduledMessage(
                id=f"work-event:ship-stopped:{key}:{int(now.timestamp())}",
                chat_id=owner[0],
                text=(
                    f"AgentDeck: Ship now of {pull}{at}, which ship-now answered as still"
                    f" running, stopped without shipping: {why}. Nothing more ships by itself."
                    " Tell the owner; the Work page offers Ship now again, and calling ship-now"
                    " again with the same pull request and head is safe."
                ),
                due_at=now,
                created_at=now,
            )
        )

    async def _ship_press(
        self,
        key: str,
        ship: ShipPorts,
        owner: Owner,
        repository: str,
        number: int,
        shown: str,
    ) -> dict:
        """``ship_now``'s checks and steps, inside its task (``_ship_task``)."""
        pull = f"{repository}#{number}"
        try:
            live, draft = await ship.open_head(repository, number)
        except Exception:  # noqa: BLE001 - unreadable ships nothing
            log.warning("work-router: %s unreadable for Ship now", pull, exc_info=True)
            live, draft = "", False
        live = (live or "").lower()
        if len(live) != 40:
            raise AgainRefused(f"{pull} is not open, or could not be read now")
        split = 0
        if shown and live != shown:
            # The agent pushed a fix after the findings the owner was shown. The reviewed
            # commit ships in a pull request of its own, and this one carries on with the fix
            # (#4212: the owner chose this over a force-push).
            source = pull
            if len(shown) != 40 or review_history.shippable(self._history(pull), shown) is None:
                raise AgainRefused(
                    f"{pull} moved past {shown[:12]}, whose findings the owner was shown, and"
                    " the deck holds no findings standing at that commit (none were recorded"
                    " there, or a newer review of it was asked for), so it was not shipped"
                )
            split = await self._split(ship, owner, repository, number, shown)
            number, pull = split, f"{repository}#{split}"
            try:
                live, draft = await ship.open_head(repository, number)
            except Exception:  # noqa: BLE001 - unreadable ships nothing
                live, draft = "", False
            if (live or "").lower() != shown:
                raise AgainRefused(f"{pull}, opened to ship {source} at {shown[:12]}, is not open")
        head = shown or live
        history = self._history(pull)
        # One rule finishes a press (#4212 review): a head whose pass an earlier press recorded
        # (``ship_pass_head``, the head it passed when base updates carried it) is finished
        # by the next press, which records what is still missing and records nothing twice.
        passed = review_history.ship_pass_head(history, head)
        if passed:
            earlier = ship.shipped(pull, passed)
            if earlier is not None and earlier.shipped:
                return _recorded(pull, passed)
            head, draft = passed, draft and bool(split)
        elif not split and review_history.shippable(history, head) is None:
            raise AgainRefused(
                f"the review of {pull} at {head[:12]} has not come back with findings, or a"
                " newer review of it is still running, so there is nothing to ship past yet"
                + self._closed_review(pull, head)
            )
        # The release's hold line names the head the press resolved to, too (#4558). Two
        # callers' heads can resolve to one (a carried pass, or the original and its split
        # pull request): the later one waits for the press already shipping it, so no head
        # is shipped by two presses at once (#4559 helper checks).
        target = f"{pull}@{head}"
        runs = self.ship_runs
        for other, at in list(runs.resolved.items()):
            if at == target and other != key and other in runs.tasks:
                return await asyncio.shield(runs.tasks[other])
        runs.resolved[key] = target
        return await self._ship_now(
            ship,
            owner,
            repository,
            number,
            head,
            draft,
            accepted=bool(passed),
            source=source if split else "",
        )

    async def _ship_now(
        self,
        ship: ShipPorts,
        owner: tuple[str, str],
        repository: str,
        number: int,
        head: str,
        draft: bool,
        *,
        accepted: bool = False,
        source: str = "",
    ) -> dict:
        """``ship_now`` past its checks, one press of a head at a time. Each step is kept
        before the next, so a press cut off anywhere is finished by the next press: the head
        is recorded for this Work, then the review counts as
        passed (``accepted`` when an earlier press recorded that), then a Work that waits for a
        Merge press has it, and last the head is marked shipped. The release merges a Ship now
        pass only once it is (``Ports.ship_unfinished``), and a press refused after its pass
        leaves the Work page offering Ship now again (#4212 review).

        ``source`` is the pull request the deck opened this one from, at the reviewed commit
        its fix was pushed on top of (``_split``): its draft is marked ready only after the
        pass, so a review never starts on a commit the reviewer already answered."""
        pull = f"{repository}#{number}"
        if draft and not source:
            await self._ship_ready(ship, repository, number, head)
        # Its findings stay with this Work, fixed in its next pull request (#4212: no new
        # issue, the owner's choice after it went live).
        issue = owner[1].lower()
        # False: an earlier press recorded it. A split head's fix is the pull request it was
        # split from, so the Work's issue stays open until that merges (#4212 review).
        if source:
            ship.claim(pull, head, issue, source)
        else:
            ship.claim(pull, head, issue)
        if ship.shipped(pull, head) is None:
            raise AgainRefused(f"{pull} at {head[:12]} could not be recorded, so nothing shipped")
        work_id = _work_id(owner)
        from .work_release import MERGE_OFFERED, PASS_RECORDED, PressRefused

        if accepted:
            answer = f"Ship now of {pull} at {head[:12]} finished"
        else:
            answer = str(await self._release.owner_accepted(work_id, pull, head, SHIP_NOTE))
            if not answer.startswith(PASS_RECORDED):
                raise AgainRefused(answer)
        if draft and source:
            await self._ship_ready(ship, repository, number, head)
        unfinished = (
            "so it does not merge yet. Its pass is recorded; press Ship now again to finish it"
        )
        merge = ""
        if self._release.approval(work_id) == MERGE_OFFERED:
            # Pressing Ship now is the person's Merge too, on a project that waits for one.
            try:
                merge = str(await self._release.request_merge(work_id))
            except PressRefused as refused:
                raise AgainRefused(
                    f"{pull}'s Merge was not recorded ({refused}), {unfinished}"
                ) from None
        if not self._mark_shipped(ship, pull, head):
            raise AgainRefused(f"{pull} at {head[:12]} could not be marked shipped, {unfinished}")
        self.pending.discard(f"review:{pull}@{head}")
        # Also in the chat's message: a caller answered ``running`` never sees the outcome.
        behind = await self._ship_behind(ship, repository, number, head)
        warning = SHIP_BEHIND_TEXT.format(pull=pull, head=head[:12]) if behind else ""
        # Marked last; the release looks again once the press left the running list
        # (``_ship_task``), so that look cannot say it is still under way (#4558 redesign).
        now = datetime.now(UTC)
        self._deliver(
            ScheduledMessage(
                id=f"work-event:ship-now:{pull}@{head}",
                chat_id=owner[0],
                text=_ship_message(pull, head, issue, source) + (f" {warning}." if warning else ""),
                due_at=now,
                created_at=now,
            )
        )
        outcome = f"{answer}; its findings are left to this Work"
        if warning:
            outcome += f". {warning}"
        return {
            "pull_request": pull,
            "head": head,
            "state": SHIP_RECORDED,
            "outcome": f"{outcome}; {merge}" if merge else outcome,
        }

    async def _ship_behind(self, ship: ShipPorts, repository: str, number: int, head: str) -> bool:
        """Whether the shipped ``head`` is behind its base branch; unread is not behind."""
        if ship.behind is None:
            return False
        try:
            return bool(await ship.behind(repository, number, head))
        except Exception:  # noqa: BLE001 - only a warning is lost
            log.warning("work-router: could not compare %s#%s with its base", repository, number)
            return False

    async def _ship_ready(self, ship: ShipPorts, repository: str, number: int, head: str) -> None:
        """Mark the draft ``repository#number`` ready at ``head``, or refuse the press."""
        pull = f"{repository}#{number}"
        try:
            readied = bool(await ship.mark_ready(repository, number))
        except Exception:  # noqa: BLE001 - a draft not readied ships nothing
            log.warning("work-router: could not mark %s ready", pull, exc_info=True)
            readied = False
        if not readied:
            raise AgainRefused(f"{pull} is a draft and could not be marked ready")
        if await self._live_ready_head(repository, number) != head:
            raise AgainRefused(
                f"{pull} moved past {head[:12]} or is still a draft, so it was not shipped:"
                " a fix is already in review, and its new head is reviewed as usual"
            )

    async def _split(
        self, ship: ShipPorts, owner: Owner, repository: str, number: int, head: str
    ) -> int:
        """The number of the pull request that ships ``number``'s reviewed ``head``, opened as
        a draft from a branch at it and registered to the same Work, ``number`` turned into a
        draft that carries only the fix (#4212). Every step finds what an earlier press made."""
        pull = f"{repository}#{number}"
        if ship.split is None or ship.register is None:
            raise AgainRefused(
                f"{pull} moved past {head[:12]}, whose findings the owner was shown, and this"
                " deck cannot open a pull request for that commit"
            )
        issue = owner[1].rpartition("#")[2]
        if not issue.isdigit():
            raise AgainRefused(f"{pull}'s Work {owner[1]} has no issue number")
        try:
            found = int(await ship.split(repository, number, head, int(issue)))
        except Exception as exc:  # noqa: BLE001 - nothing opened ships nothing
            log.warning("work-router: could not split %s at %s", pull, head[:12], exc_info=True)
            raise AgainRefused(
                f"{pull} moved past {head[:12]}, and a pull request for that commit could not"
                f" be opened ({exc}); press Ship now again"
            ) from None
        if found <= 0 or not await ship.register(repository, number, found):
            raise AgainRefused(
                f"the pull request opened to ship {pull} at {head[:12]} could not be added to"
                " this Work; press Ship now again"
            )
        return found

    @staticmethod
    def _mark_shipped(ship: ShipPorts, pull: str, head: str) -> bool:
        """Mark ``pull`` at ``head`` shipped, the last step of a press: only then does the
        release merge its pass and the Work page say Shipped (#4212 review). Whether it is
        marked, now or before."""
        try:
            if ship.note_shipped(pull, head):
                return True
            record = ship.shipped(pull, head)
        except Exception:  # noqa: BLE001 - not marked: the next press marks it
            log.warning("work-router: could not mark %s at %s shipped", pull, head[:12])
            return False
        return record is not None and bool(record.shipped)

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

        A Ship now pass carries further (#4576): to a head that only merged the base in, even
        one that touched the pull request's files or changed its doc lines, as long as every
        code file contributes the same lines (``ship_merge_between``). The owner shipped that
        code; a conflict with the base must not send it back to a review.
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
        events = self._history(pull)
        before, known = review_history.last_digest(events)
        if not skip or not known or before == head.lower():
            return digest
        # Its standing verdict only: one a review was asked for again after is still awaited.
        verdict = review_history.standing_verdict(review_history.at_head(events, before))
        if verdict is None:
            # Its review has not come back (#3314 review): still running, failed or never
            # started. No verdict stands yet, so the new head is reviewed as any other.
            return digest
        # The digest leaves out where the change sits, so the same lines moved into another
        # function digest the same (#3314 review): only commits that touch none of the pull
        # request's files prove the change itself is what was reviewed.
        repository, _, number = pull.rpartition("#")
        proven = known == digest and await self._proven(
            self._base_only, repository, int(number), before, head
        )
        shipped = not proven and bool(review_history.ship_pass_head(events, before))
        if shipped:
            proven = await self._proven(self._ship_merge, repository, int(number), before, head)
        if not proven:
            return digest
        # Read again after the last wait, with none before the row is written: a review asked
        # for, or a newer verdict, meanwhile leaves nothing to carry (#4658).
        events = self._history(pull)
        fresh = review_history.standing_verdict(review_history.at_head(events, before))
        moved = review_history.last_digest(events) != (before, known)
        if moved or fresh is None or fresh.key != verdict.key or self._spent(pull, head):
            return digest
        key = f"{pull}@{head}"
        # Kept pending until the chat is told, so a crash between the row and the telling is
        # finished by the retry (#4576 design): the row alone spends the head.
        self.pending.put(
            f"review:{key}",
            {"kind": REVIEW, "repository": repository, "number": int(number), "head": head},
            self._clock(),
        )
        # Written as the head's own verdict, copied from the one it keeps: so the head needs no
        # review, and a later base merge of it compares with this digest.
        data = {**verdict.data, "digest": digest, "carried_from": before}
        self._note(pull, head, verdict.kind, f"carried:{key}", data=data)
        self._carried_told(owner, pull, head)
        return ""

    async def _proven(
        self,
        port: Callable[[str, int, str, str], Awaitable[bool]] | None,
        repository: str,
        number: int,
        before: str,
        head: str,
    ) -> bool:
        """``port``'s answer about the commits from ``before`` to ``head``; unread is False."""
        if port is None:
            return False
        try:
            return bool(await port(repository, number, before, head.lower()))
        except Exception:  # noqa: BLE001 - unproven is reviewed
            log.warning("work-router: could not compare %s#%s's heads", repository, number)
            return False

    def _carried_told(self, owner: Owner, pull: str, head: str) -> None:
        """Tell the chat the verdict carried to ``head`` (``_unchanged``'s row) stands, end
        its wait on a verdict, and only then drop the head's pending start: the one hand-off
        a start that finds the row with its start still pending finishes again (#4576)."""
        key = f"{pull}@{head}"
        verdict = review_history.standing_verdict(review_history.at_head(self._history(pull), head))
        before = str((verdict.data or {}).get("carried_from") or "") if verdict else ""
        if verdict is None or not before:
            self.pending.discard(f"review:{key}")
            return
        if not owner[0]:
            # A pull request no Work owns: only its check says so, and no chat waits.
            self.pending.discard(f"review:{key}")
            return
        if verdict.kind in (review_history.PASS, review_history.ACCEPTED):
            # A pass (or one the owner accepted) carries over and nothing is left for the chat
            # to do (#4253): its wait on a verdict ends quietly, and the release merges or
            # offers Merge as for any pass.
            self._end_wait(owner, pull, head)
            shipped = review_history.ship_pass_head(self._history(pull), head)
            if shipped:
                text = (
                    f"AgentDeck: {pull} is now at {head[:12]}, which only merged its base into"
                    f" {shipped[:12]}, the commit the owner shipped with Ship now; its code"
                    " changes are the same, so that pass carries over and no new review"
                    " started. The deck merges it, or offers Merge, as for any pass."
                )
            else:
                text = (
                    f"AgentDeck: {pull} is now at {head[:12]}, but only its base branch changed"
                    f" since {before[:12]}, whose review passed: the pass carries over and no"
                    " new review started. The deck merges it, or offers Merge, as for any pass;"
                    " a head that still needs its tests run is told on its own."
                )
            self._keep_note(owner[0], f"work-unchanged:{key}", text)
            self.pending.discard(f"review:{key}")
            log.info("work-router: %s has %s's passed diff; not reviewed again", key, before[:12])
            return
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
                    " them and push, or, when the owner settled them on the issue with no code"
                    " change, tell the deck with your session's `review-accepted`, quoting the"
                    " decision as its note."
                ),
                due_at=now,
                created_at=now,
            )
        )
        if delivered:
            # No verdict will come for this head, so the chat's wait on one ends here.
            self._end_wait(owner, pull, head)
        self.pending.discard(f"review:{key}")
        log.info("work-router: %s has %s's diff; not reviewed again", key, before[:12])

    def _keep_note(self, chat_id: str, note_id: str, text: str) -> bool:
        """Keep ``text`` for the chat's next message, waking nothing (#4306). A note not kept
        is the silence of before."""
        if self._pending_note is None or not chat_id:
            return False
        try:
            return bool(self._pending_note(chat_id, note_id, text))
        except Exception:  # noqa: BLE001 - a note lost is the silence of before
            log.warning("work-router: could not keep note %s", note_id, exc_info=True)
            return False

    async def _no_code_fixed(self, owner: Owner, event: Event, head: str = "") -> bool:
        """Whether ``event``'s head answers a ``no-code`` verdict, which then counts as a pass
        there and no review starts (#3322), or keeps a standing pass through doc files alone
        (#4257: the release's ``no_code_head`` names either, with no files for a pass, so
        ``no_code_between`` then takes doc files only).

        At the verdict's own head only a ready press says the fix is done (a walkthrough or
        the pull request body changes no file). A later head counts when ``no_code_between``
        proves its commits changed only doc files or merged the base, or the router already
        proved it a base merge away (``same_change``). Anything else is reviewed as usual.
        """
        release = self._release
        if not owner[0] or release is None or not hasattr(release, "no_code_head"):
            # With no Work the release has no record of it, and must not make one (#4152).
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
        old, before, *rest = held
        # A ``minor`` verdict's fix may change a little code too (#4474).
        minor = {"minor": True} if rest and rest[0] else {}
        if head.startswith(old) or old.startswith(head):
            if not event.explicit:
                return False
        elif not review_history.same_change(self._history(pull), head, old):
            why: list[str] = []
            try:
                proven = self._no_code is not None and bool(
                    await self._no_code(
                        event.repository, event.number, old, head, before, **minor, why=why
                    )
                )
            except Exception:  # noqa: BLE001 - unproven is reviewed
                log.warning("work-router: could not compare %s's heads", pull, exc_info=True)
                proven = False
                why.append("the deck could not compare the two commits")
            if not proven and (minor or self._no_code_verdict_at(pull, old)):
                # Said in this head's checklist (#4575): the agent must not believe the
                # earlier verdict carried over when the deck's record says it did not. A
                # standing pass a code push leaves behind is the usual next round: unsaid.
                self._not_small[f"{pull}@{head}"] = NOT_SMALL_TEXT.format(
                    checklist="{checklist}",
                    pull=pull,
                    old=old[:12],
                    short=head[:12],
                    verdict="minor-fixes" if minor else "no-code",
                    reason=why[0] if why else "the deck could not prove it small",
                )
                log.info("work-router: %s@%s is no small fix: %s", pull, head[:12], why)
            if not proven:
                return False
        try:
            answer = str(await release.no_code_fixed(work, pull, head) or "")
        except Exception:  # noqa: BLE001 - not recorded: reviewed as usual
            log.warning("work-router: no-code pass of %s failed", pull, exc_info=True)
            return False
        from .work_release import PASS_RECORDED

        log.info("work-router: %s@%s needs no new review: %s", pull, head[:12], answer)
        if not answer.startswith(PASS_RECORDED):
            return False
        # Its pass, in the history with the head's digest, is what spends the head.
        self.pending.discard(f"review:{pull}@{head}")
        return True

    def _no_code_verdict_at(self, pull: str, head: str) -> bool:
        """Whether the verdict standing at ``head`` is a ``no-code`` one, not a pass a doc-only
        push would keep (#4257): ``no_code_head`` names either alike (#4575)."""
        events = review_history.at_head(self._history(pull), head)
        verdict = review_history.standing_verdict(events)
        return verdict is not None and verdict.kind == review_history.NO_CODE

    async def _review(
        self, owner: Owner, event: Event, *, asked: bool = False, checklist_again: bool = False
    ) -> None:
        """Start the review of ``event``'s head. ``asked`` is the agent's own request, which
        reviews even a diff an earlier head's review read (#3314). ``checklist_again`` sends a
        new head its own checklist while an older head's waits, as a ready press does."""
        pull = f"{event.repository}#{event.number}"
        key = f"{pull}@{event.head}" + (f"+{event.again}" if event.again else "")
        pending_key = f"review:{key}"
        if self._spent(pull, event.head, event.again):
            if not event.again and asked and self._unasked_verdict(pull, event.head):
                # The agent's own request met a verdict carried here as it came in (#4658).
                self._keep_request(pull, event.head, event.note)
                self.pending.discard(pending_key)
                return
            if not event.again and self._carried_pass(pull, event.head):
                self._stop_on_carried(owner, pull, event.head)
            elif not event.again and self._carry_unfinished(pull, event.head):
                # The carried row was written and the router stopped before the chat heard
                # (#4576 design): finish that hand-off, with the same ids, never a review.
                self._carried_told(owner, pull, event.head)
            self.pending.discard(pending_key)
            return
        if self._ship_holds(pull):
            # A Ship now press of this pull request is still running (#4576 design): its pass
            # may not be written yet, so a base merge pushed meanwhile would be reviewed. The
            # start waits pending; the press's end makes it due at once.
            self._hold_review(event, pending_key, asked=asked)
            return
        if not asked and self._is_light(pull, event.head):
            # No reviewer for a light head (#4399): its bench verdict and bin/test.sh decide.
            # A review the agent asks for still starts; its ``asked`` event makes the pull
            # request full for good (rule 2).
            self.pending.discard(pending_key)
            return
        if event.again.startswith(NO_VERDICT) and self._has_verdict(pull, event.head):
            # The verdict came while this ask waited for the open review chat to close. Its
            # ``asked`` row would void that pass, as metso#77's did at 20:49 (#4089).
            self.pending.discard(pending_key)
            log.info("work-router: %s has its verdict; no-verdict ask dropped", key)
            return
        if key in self._in_flight:
            if asked and not event.again and pending_key not in self.pending:
                # That start may end without asking, as ``ask_again`` says (#4658).
                self._keep_request(pull, event.head, event.note)
            return
        # A pass a late minor or no-code verdict makes while this start runs ends it (#4474);
        # one that stood before it began, or a review asked again, does not.
        settle = not event.again and not self._fix_pass(pull, event.head)
        if self.reviewer_off:
            # Nothing pending, nothing reviewed (#3849): kept, it was retried for a day while
            # every later ask heard "already asked, keep waiting".
            self.pending.discard(pending_key)
            self._tell_no_reviewer(owner, pull, event.head)
            return
        # In memory while it runs, so a registration check and an event for the same head
        # never ask twice; in the history only once it returned (``_note_asked``).
        self._in_flight.add(key)
        try:
            digest = ""
            if not event.again and not asked and await self._no_code_fixed(owner, event):
                settle = False  # no chat was opened for this head: nothing to close
                return
            if not event.again:
                digest = await self._unchanged(owner, pull, event.head, skip=not asked)
                if not digest and self._spent(pull, event.head):
                    return
            # A pass the release carried to this head while the start ran (#4658): an ask
            # over it would void it, and a checklist would hold a head that passed.
            overtaken = not event.again and not asked
            if overtaken and self._carried_pass(pull, event.head):
                settle = False  # no chat was opened for this head: nothing to close
                self._stop_on_carried(owner, pull, event.head)
                return
            if (
                self.pre_review_on
                and owner[0]
                and not asked
                and not event.again
                and await self._held_for_checklist(
                    owner, pull, event, again=event.explicit or checklist_again
                )
            ):
                return
            missing = await self._design_gap(owner)
            if overtaken and self._carried_pass(pull, event.head):
                settle = False
                self._stop_on_carried(owner, pull, event.head)
                return
            if missing:
                # Every start passes here, a review request and a retry too (#4332 review).
                self.pending.discard(pending_key)
                self._tell_design_gap(owner, pull, event.head, missing)
                return
            await self._ask_reviewer(owner, event, pull, key, digest, asked=asked, settle=settle)
        finally:
            self._in_flight.discard(key)
            # A reason no checklist took (pre-review off, a checklist already waiting) goes.
            self._not_small.pop(f"{pull.lower()}@{event.head.lower()}", None)
            if settle and self._overtaken(pull, event.head, asked=asked):
                await self._close_settled(owner, pull, event.head)

    def _carry_unfinished(self, pull: str, head: str) -> bool:
        """Whether ``head``'s standing verdict is a carried one whose hand-off to the chat did
        not finish: its start is still pending, which the hand-off drops last, and ``head`` is
        still the newest (``_newest_waited``)."""
        if f"review:{pull}@{head}" not in self.pending:
            return False
        events = self._history(pull)
        verdict = review_history.standing_verdict(review_history.at_head(events, head))
        if verdict is None or not verdict.key.startswith("carried:"):
            return False
        return self._newest_waited(pull, head)

    def _newest_waited(self, pull: str, head: str) -> bool:
        """Whether ``head`` is the newest head of ``pull`` a chat can wait on, so ending its
        waits on ``pull`` (not kept per head) cannot end a newer head's (#4576 design review)."""
        events = self._history(pull)
        # A chat waits on a head once its review is asked for or a pass carried to it; a late
        # row at an older head (a verdict, an unstarted note) starts no wait.
        waits = [
            event
            for event in events
            if event.kind == review_history.ASKED or event.key.startswith("carried:")
        ]
        newest = max(waits, key=lambda event: (event.at, event.seq))
        return newest.head.lower() == head.lower()

    def _ship_holds(self, pull: str) -> bool:
        """Whether a Ship now press of ``pull`` runs in this process now (``ShipRuns``)."""
        runs = self.ship_runs
        return bool(runs.tasks or runs.resolved) and bool(runs.head_of(pull))

    def _hold_review(self, event: Event, pending_key: str, *, asked: bool) -> None:
        """Keep ``event``'s review start pending, untried, until a press lets it go."""
        retry: dict = {
            "kind": REVIEW,
            "repository": event.repository,
            "number": event.number,
            "head": event.head,
        }
        if asked:
            retry.update(asked=True, note=event.note)
        if event.again:
            retry.update(again=event.again, note=event.note)
        self.pending.put(pending_key, retry, self._clock())
        log.info("work-router: %s waits for the Ship now press of its pull request", pending_key)

    def _fix_pass(self, pull: str, head: str) -> bool:
        """Whether the pass standing at ``head`` is one a ``minor`` or ``no-code`` verdict's
        fix made with no new review (``no_code_fixed``'s ``minor-fixed`` or ``no-code-fixed``
        row, #4474)."""
        events = review_history.at_head(self._history(pull), head)
        verdict = review_history.standing_verdict(events)
        return (
            verdict is not None
            and verdict.kind == review_history.PASS
            and verdict.key.startswith(("minor-fixed:", "no-code-fixed:"))
        )

    def _overtaken(self, pull: str, head: str, *, asked: bool) -> bool:
        """Whether a pass that needs no review came to ``head`` while its review started: a
        late verdict's fix (#4474), or one carried, which the agent's own request outranks
        (#4658)."""
        return self._fix_pass(pull, head) or (not asked and self._carried_pass(pull, head))

    def _unasked_verdict(self, pull: str, head: str) -> bool:
        """Whether a verdict stands at ``head`` though no review was ever asked for there."""
        return self._has_verdict(pull, head) and not any(
            event.kind == review_history.ASKED
            for event in review_history.at_head(self._history(pull), head)
        )

    async def _close_settled(self, owner: Owner, pull: str, head: str) -> None:
        """Close the Work's review chat that waits only on ``pull`` at ``head``, which passed
        with no new review after a late minor or no-code verdict (#4474). A chat that still
        asks about another pull request is left open; a close that fails leaves the chat's
        own verdict to decide, as before."""
        if self._close_review is None or not owner[1]:
            return

        def settled(ref: str, sha: str) -> bool:
            events = review_history.at_head(self._history(ref.lower()), sha)
            return review_history.passed(review_history.standing_verdict(events))

        why = (
            "only its base branch changed since a commit that passed"
            if self._carried_pass(pull, head)
            else "the review before it asked only for minor fixes, and the fix stayed within the"
            " limit"
        )
        reason = (
            f"{pull} at {head[:12]} passed without a second review: {why}. Nothing else in this"
            " chat waits on a review."
        )
        try:
            closed = await self._close_review(owner[1], pull, head, settled, reason)
        except Exception:  # noqa: BLE001 - left open, its own verdict decides
            log.warning("work-router: could not close %s's review chat", pull, exc_info=True)
            return
        if closed:
            log.info("work-router: closed review chat %s of %s@%s", closed, pull, head[:12])

    async def late_fixed(self, work_id: str, pull: str) -> None:
        """Check the fix the agent pushed before a late ``minor`` or ``no-code`` verdict came
        (#4474), as a ready press does, and close the review chat that push started once it
        passed. A start still running for that head closes it itself, in its ``finally``."""
        repository, _, number = pull.lower().rpartition("#")
        if not number.isdigit():
            return
        owner = self._owner_of_pull(repository, int(number))
        if owner is None or _work_id(owner) != work_id.lower():
            return
        head = await self._live_ready_head(repository, int(number))
        if not head:
            return
        event = Event(REVIEW, repository, int(number), True, head=head)
        if not await self._no_code_fixed(owner, event):
            # Its checklist went out before the verdict came (#4575 review): say why apart.
            self._say_not_small(owner, f"{repository}#{number}", head)
            return
        key = f"{repository}#{number}@{head}".lower()
        if any(
            item.lower() == key or item.lower().startswith(f"{key}+") for item in self._in_flight
        ):
            return
        await self._close_settled(owner, f"{repository}#{number}", head)

    def _has_verdict(self, pull: str, head: str) -> bool:
        """Whether a verdict stands at ``head``, so an ask made for want of one has no reason
        left (#4089). A review asked for after that verdict leaves none standing."""
        events = review_history.at_head(self._history(pull), head)
        return review_history.standing_verdict(events) is not None

    async def _held_for_checklist(
        self, owner: Owner, pull: str, event: Event, *, again: bool = False
    ) -> bool:
        """Whether the review of ``event``'s head waits on the agent's pre-review (#3880).

        The first ready head sends the checklist and opens the hold; a push while it is open
        sends nothing more. Marking it ready again at a new head (turned back to draft and
        pushed), or registering it again (``again``), sends that head's checklist, so the one for
        an older head never leaves it silent (#3896); the same head's is absorbed by its message
        id. A head the agent cleared goes on to review.
        """
        settled = self._checklist_settled(pull, event, again=again)
        if settled is not None:
            self._say_not_small(owner, pull, event.head)
            return settled
        grown = await self._growth(pull, event)
        if not event.again and self._carried_pass(pull, event.head):
            return False  # passed during the read (#4658): the start stops on it, unheld
        # The read let other work run, which may have sent a checklist or cleared this head.
        settled = self._checklist_settled(pull, event, again=again)
        if settled is not None:
            self._say_not_small(owner, pull, event.head)
            return settled
        now = datetime.now(UTC)
        policy = f" The reviewer's full rules: {self._review_policy}" if self._review_policy else ""
        helpers = ""
        if self._first_round(pull):
            issue = f" --issue {owner[1]}" if owner[1] else ""
            helpers = HELPERS_TEXT.format(pull=pull, issue=issue)
        not_small = self._not_small.pop(f"{pull.lower()}@{event.head.lower()}", "")
        text = not_small.replace("{checklist}", "the checklist below") + PRE_REVIEW_TEXT.format(
            pull=pull,
            short=event.head[:12],
            policy=policy,
            grown=grown,
            helpers=helpers,
            how=PRE_REVIEW_HOW,
            big=review_paperwork.BIG_PULL_LINES,
        )
        message_id = f"work-event:pre-review:{pull}@{event.head}"
        caller, claim = self._printing.get(pull) or ("", None)
        if claim is not None and (not caller or caller != owner[0]):
            claim = None  # pressed by another chat than the Work's: its owner gets a message
        if claim is not None:
            # The agent's ``gh pr ready`` is waiting on it: printed in that command's reply,
            # so no turn of its own (#4253). Handed over is not delivered (#4253 review): the
            # answer can be lost on its way, so the message is written down as pending, due
            # after the first retry wait, and only the agent's own word that it printed the
            # checklist (``ready_received``) takes it back. Silence sends it as before.
            claim.append((event.head, text))
            self.pending.put(
                f"message:{message_id}",
                {
                    "kind": MESSAGE,
                    "id": message_id,
                    "chat_id": owner[0],
                    "text": deck_notices.routine(text),
                    "receipt": True,
                },
                self._clock(),
            )
        else:
            # A row the store refuses is kept pending and sent later by ``_deliver`` itself.
            self._deliver(
                ScheduledMessage(
                    id=message_id,
                    chat_id=owner[0],
                    text=deck_notices.routine(text),
                    due_at=now,
                    created_at=now,
                )
            )
        # The hold is this event: it waits until a checklist-done or an ask follows it.
        self._note(
            pull,
            event.head,
            review_history.CHECKLIST,
            message_id,
            data={"printed": True} if claim is not None else None,
        )
        log.info("work-router: %s@%s waits on its pre-review", pull, event.head)
        return True

    def _say_not_small(self, owner: Owner, pull: str, head: str) -> None:
        """Send the reason ``head`` is no small fix as a message of its own (#4575 review): a
        push a waiting checklist covers, or a late verdict's, gets no checklist of its own to
        carry it. Nothing to say, or no chat, is a no-op; the reason is dropped either way."""
        reason = self._not_small.pop(f"{pull.lower()}@{head.lower()}", "")
        if not reason or not owner[0]:
            return
        if not review_history.checklist_waits(self._history(pull)):
            return  # cleared, so the review is asked as any other
        now = datetime.now(UTC)
        text = reason.replace("{checklist}", "the pre-review checklist the deck already sent")
        self._deliver(
            ScheduledMessage(
                id=f"work-event:not-small:{pull}@{head}",
                chat_id=owner[0],
                text=deck_notices.routine(text.strip()),
                due_at=now,
                created_at=now,
            )
        )

    def _checklist_settled(self, pull: str, event: Event, *, again: bool) -> bool | None:
        """Whether ``_held_for_checklist`` is answered without sending a checklist: ``False``
        once the agent cleared the head, ``True`` for a push a waiting checklist covers, and
        ``None`` when this head's checklist is to be sent."""
        events = self._history(pull)
        if any(
            item.kind == review_history.CHECKLIST_DONE
            for item in review_history.at_head(events, event.head)
        ):
            return False
        # Nothing to retry while the agent works through it: ``pre_review_done`` asks.
        self.pending.discard(f"review:{pull}@{event.head}")
        if any(
            item.kind == review_history.CHECKLIST and not item.data.get("covered")
            for item in review_history.at_head(events, event.head)
        ):
            # This head's checklist went out already. A message's id absorbed a second send;
            # one printed in a ``gh pr ready`` reply has no id, so it is answered here (#4253).
            return True
        if review_history.checklist_waits(events) and not again:
            covered = f"checklist-covered:{pull}@{event.head}"
            self._note(pull, event.head, review_history.CHECKLIST, covered, data={"covered": True})
            return True
        return None

    async def _growth(self, pull: str, event: Event) -> str:
        """``GROWN_TEXT`` for ``event``'s head, or "" (#4220).

        The previous round is the head of the newest ``asked`` row at another head, whoever
        wrote it: the router's own ask, a ``review-request``, the backstop or a person's press
        on the Review form or Work page. A checklist not yet cleared is no round. Both sizes
        are read from GitHub here, so no review start has to record one; unread is nothing.
        """
        if self._diff_sizes is None:
            return ""
        rounds = [
            item
            for item in self._history(pull)
            if item.kind == review_history.ASKED and item.head.lower() != event.head.lower()
        ]
        previous = max(rounds, key=lambda item: (item.at, item.seq), default=None)
        if previous is None:
            return ""
        heads = (previous.head.lower(), event.head.lower())
        try:
            sizes = await self._diff_sizes(event.repository, event.number, heads)
        except Exception:  # noqa: BLE001 - unread: the checklist goes out without the point
            log.warning("work-router: sizes of %s unread", pull, exc_info=True)
            return ""
        sizes = {str(head).lower(): size for head, size in (sizes or {}).items()}
        return _grown(sizes.get(heads[0]), sizes.get(heads[1]))

    def _checklist_waits(self, event: Event) -> bool:
        """Whether ``event``'s pull request has a pre-review checklist waiting (#3896)."""
        return self.pre_review_held(f"{event.repository}#{event.number}")

    def pre_review_held(self, pull: str) -> bool:
        """Whether ``owner/repo#n``'s review waits on its agent's checklist (#3880 review)."""
        return self.pre_review_on and review_history.checklist_waits(self._history(pull))

    async def pre_review_done(self, repository: str, number: int) -> dict:
        """The agent went through the pre-review checklist (#3880): review the pull request's
        ready head now, and register the chat's wait on its verdict.

        Raises ``AgainRefused`` with the reason when it is not ready, or its ready head was
        already asked about. Answers ``asked``, ``pending`` (the start is retried on its own,
        or another call is starting it right now, #4208) or ``not reviewed`` (its diff was
        already reviewed, and the chat was told so).

        A ready head nobody asked about is asked about even with no checklist waiting: a
        reload that cut off an earlier call between dropping the hold and asking (#3893), or a
        ready event the deck never acted on (#3896), left it so, and the agent says it went
        through the checklist.
        """
        repository = repository.lower()
        pull = f"{repository}#{number}"
        owner = self._owner_of_pull(repository, number)
        if owner is None:
            raise AgainRefused(f"no Work's chat registered {pull}")
        head = await self._live_ready_head(repository, number)
        if not head:
            raise AgainRefused(
                f"{pull} is not open and ready for review (a draft, closed, or unreadable now):"
                " mark it ready with `gh pr ready`, then call this again"
            )
        key = f"{pull}@{head}"
        if self._requests(pull, head) and self._has_verdict(pull, head):
            # Its own request is still to be asked, over the verdict carried here (#4658).
            waits = self._wait_on_review(owner, pull, asked=True)
            return {
                "pull_request": pull,
                "head": head,
                "outcome": "pending",
                "waiting": bool(waits),
            }
        if self._carried_pass(pull, head):
            # Carried here before the checklist was done: no verdict is coming (#4658).
            return {"pull_request": pull, "head": head, "outcome": "not reviewed"}
        if self._spent(pull, head):
            # The agent's own review request asked first (#3933), and its ask ended the hold.
            raise AgainRefused(
                f"no pre-review checklist waits for {pull}: a review of {head[:12]} was already"
                " asked for, and its verdict comes to this chat; push a fix and mark it ready"
                " for the next one"
            )
        gaps = await self._paperwork_gaps(repository, number, owner[1])
        gaps += await self._verification_gaps(repository, _work_id(owner), head)
        missing = await self._design_gap(owner)
        gaps += [missing] if missing else []
        if gaps == [design_review.WAITING_GAP]:
            # Only the design review it already asked for holds it (#4781): nothing to fix.
            raise AgainRefused(
                f"{pull} at {head[:12]} gets no code review yet, so its review did not start:"
                f" {missing}."
            )
        if gaps:
            listed = "\n".join(f"{n}. {gap}" for n, gap in enumerate(gaps, 1))
            raise AgainRefused(
                f"{pull} at {head[:12]} is missing paperwork, so its review did not start:\n"
                f"{listed}\nFix it, keep the pull request ready, then call this again."
            )
        self._note(pull, head, review_history.CHECKLIST_DONE, f"checklist-done:{key}")
        # Going through the checklist is the agent's word that it has it: one printed in a
        # ``gh pr ready`` reply whose receipt never came back is not sent after all.
        self.ready_received(repository, number, [head], caller=owner[0])
        if self._light is not None:
            from .light_review import LightError

            try:
                decision = await self._light.checklist(pull, head, _work_id(owner))
            except LightError as exc:
                raise AgainRefused(f"{exc}; call this again") from exc
            if decision is not None and decision.light:
                return {
                    "pull_request": pull,
                    "head": head,
                    "outcome": "light",
                    "next": LIGHT_NEXT_TEXT.format(pull=pull, head=head[:12]),
                }
        await self._review(owner, Event(REVIEW, repository, number, True, head=head))
        if self._requests(pull, head):
            outcome = "pending"
        elif self._has_verdict(pull, head):
            # Its wait would be for a verdict that never comes (#4658).
            return {"pull_request": pull, "head": head, "outcome": "not reviewed"}
        elif self._spent(pull, head):
            outcome = "asked"
        elif f"review:{key}" in self.pending or key in self._in_flight:
            # In flight: another call (the ready event, or one just before) is starting this
            # very review, so ``_review`` returned at once. Twice on store#3943 that answered
            # "not reviewed" and the agent went to the logs to find its review (#4208).
            outcome = "pending"
        else:
            return {"pull_request": pull, "head": head, "outcome": "not reviewed"}
        waits = self._wait_on_review(owner, pull, asked=True)
        return {"pull_request": pull, "head": head, "outcome": outcome, "waiting": bool(waits)}

    async def design_review(self, issue: str, checkout: str = "", owner: str = "") -> dict:
        """Start the design review of ``issue`` (#4332, ``DesignRound.ask``); a refusal is
        ``{"refused": why, "status": http status}``. With every reviewer place taken it is
        kept and tried again like a code review (#4342): ``{"queued": True, ...}``; asked again
        while it waits, the same answer, and nothing added. With ChatGPT as the code reviewer
        it is asked of ChatGPT first (#4722, ``_design_by_chat``). ``owner`` is the owner's
        answer at the series limit (#4765, ``DesignRound.owner_answer``)."""
        if self._design is None:
            return {"refused": "reviews are off on this deck", "status": 409}
        key = f"{DESIGN}:{issue.lower()}"
        if owner:
            try:
                skipped = await self._design.owner_answer(issue, owner)
            except design_review.Refused as exc:
                return {"refused": str(exc), "status": exc.status}
            log.info("work-router: the owner's answer at %s's series limit: %s", issue, owner)
            if skipped is not None:
                return skipped
        asked = await self._design_by_chat(key, issue, checkout)
        if asked is not None:
            return asked
        try:
            answer = await self._design.ask(issue, checkout)
        except design_review.Refused as exc:
            if exc.why == design_review.CLOSED:
                self.pending.discard(key)  # nothing is left to wait for (#4675)
            if exc.why == design_review.LIMIT:  # the review point counts these (#4765)
                log.info("work-router: %s reached the design series limit", issue)
            return {"refused": str(exc), "status": exc.status}
        except design_review.Busy as exc:
            if key not in self.pending.entries:
                log.info("work-router: design review of %s waits for a place (%s)", issue, exc)
                fields = {"kind": DESIGN, "issue": issue.lower(), "checkout": checkout}
                self.pending.put(key, fields, self._clock())
            return {
                "issue": issue,
                "queued": True,
                "next": f"no reviewer place is free ({exc}), so the deck keeps the request and"
                " starts its reviewer by itself once a place frees up; the verdict comes to this"
                " chat as a message and is posted on the issue. End the turn and write no code"
                " until it passes; asking again adds nothing. Closing the issue drops the request"
                " at its next try: after a reopen, ask again",
            }
        self.pending.discard(key)
        return answer

    async def _retry_design(self, key: str, entry: dict) -> None:
        """One try of a design review waiting for a place (#4342): dropped once its Work is
        gone, its issue closed, its design has a verdict or has no design; otherwise tried
        again later."""
        issue = str(entry.get("issue") or "")
        repository, _, number = issue.partition("#")
        gone = (
            self._design is None
            or not number.isdigit()
            or self._owner_of_issue(repository, int(number)) is None
        )
        if entry.get("comment") and self._design_chat is not None:
            async with self._design_lock(key):
                entry = self.pending.entries.get(key) or {}
                waits = bool(entry.get("comment")) and await self._retry_design_chat(
                    key, entry, gone
                )
            if waits:
                return
            entry = self.pending.entries.get(key) or {}
            if not entry:
                return
        if gone:
            self.pending.discard(key)
            return
        if entry.get("close") and self._design_chat is not None:
            # The chat ChatGPT gave no verdict in (#4722): closed again on each try, which is
            # harmless, so a crash right after the fallback leaves none open.
            why = "No design verdict in time: the deck's own reviewer takes it."
            await self._design_chat.close(str(entry["close"]), why)
        try:
            started = await self._design.ask(issue, str(entry.get("checkout") or ""))
        except design_review.Refused as exc:
            over = (
                design_review.DECIDED,
                design_review.NO_DESIGN,
                design_review.CLOSED,
                design_review.LIMIT,
            )
            if exc.why in over:
                self.pending.discard(key)
            else:
                log.warning("work-router: design review of %s refused: %s; kept", issue, exc)
                self.pending.put(key, {}, self._clock())
        except Exception:  # noqa: BLE001 - Busy, or a fault: the ledger stops a second reviewer
            self.pending.put(key, {}, self._clock())
        else:
            self.pending.discard(key)
            log.info("work-router: design review of %s started: %s", issue, started)

    async def _design_by_chat(self, key: str, issue: str, checkout: str) -> dict | None:
        """Ask ChatGPT for the design review of ``issue`` (#4722), or ``None`` when the deck's
        own reviewer is to be asked: ChatGPT is not this deck's reviewer now, a request
        already waits for a place, or the chat could not be opened.

        The pending entry is the record that ChatGPT was asked, and it is reserved before the
        chat is opened: a second ask, or a retry try, that finds it opens no second chat.
        The entry's lock is held until the reservation, so no run finishing an earlier
        verdict acts in between; the chat itself opens outside it."""
        async with self._design_lock(key):
            reserved = await self._design_reserve(key, issue, checkout)
        if not isinstance(reserved, tuple):
            return reserved
        return await self._design_open(key, issue, *reserved)

    def _design_lock(self, key: str) -> asyncio.Lock:
        return self._design_locks.setdefault(key, asyncio.Lock())

    async def _design_reserve(self, key: str, issue: str, checkout: str):
        """The first half of ``_design_by_chat``, under the entry's lock: its answer, or what
        ``_design_open`` needs once the entry is reserved for the newest design."""
        route = self._design_chat
        waiting = self.pending.entries.get(key) or {}
        if route is not None and waiting.get("verdict_comment"):
            # A verdict not yet told: told now if its design is still the newest, else
            # dropped; the record keeps a recorded one for the round count.
            await self._finish_design_verdict(key, waiting)
            waiting = self.pending.entries.get(key) or {}
        if route is None or (not waiting.get("comment") and (waiting or not route.wanted())):
            return None
        try:
            url, number, earlier = await self._design.target(issue)
        except design_review.Refused as exc:
            if exc.why == design_review.CLOSED:  # nothing is left to wait for (#4675)
                await self._drop_design_chat(key, "The Work's issue is closed.")
            if exc.why == design_review.LIMIT:  # the review point counts these (#4765)
                log.info("work-router: %s reached the design series limit", issue)
            return {"refused": str(exc), "status": exc.status}
        entry = dict(self.pending.entries.get(key) or {})
        if entry.get("comment") and entry.get("comment") != number and not route.wanted():
            # A newer design, and ChatGPT is no longer the reviewer: the deck's own takes it.
            await self._drop_design_chat(key, "The Work posted a newer design.")
            return None
        # Nothing is awaited from here to the reservation: it decides between two asks.
        entry = dict(self.pending.entries.get(key) or {})
        if entry and not entry.get("comment"):
            return None  # fell back to the deck's own reviewer meanwhile
        if entry.get("comment") != number and self._design.reviewing(issue.lower(), number):
            # The deck's own reviewer is already on this design (asked while the switch was
            # on): one design has one reviewer, so it keeps it and no chat opens.
            if entry:
                await self._drop_design_chat(key, "The Work posted a newer design.")
            return None
        answer = {
            "issue": issue,
            "design": url,
            "started": "ChatGPT was asked to review the design",
            "next": "the verdict comes to this chat as a message, and is posted on the issue;"
            " write no code until it passes. With none from ChatGPT in"
            f" {int(design_review.CHAT_LIMIT_S // 60)} minutes the deck's own reviewer takes"
            " it by itself; asking again adds nothing",
        }
        if entry.get("comment") == number:
            return answer
        replaced = str(entry.get("chat") or "")
        now = self._clock()
        mark = (key, number, now)
        self.pending.discard(key)
        fields = {
            "kind": DESIGN,
            "issue": issue.lower(),
            "checkout": checkout,
            "comment": number,
            "chat": "",
            "asked_at": now,
        }
        self.pending.put(key, fields, now, at=now + RETRY_FIRST_S)
        self._design_opening.add(mark)
        return answer, url, number, earlier, replaced, now, mark

    async def _design_open(
        self,
        key: str,
        issue: str,
        answer: dict,
        url: str,
        number: str,
        earlier: str,
        replaced: str,
        now: float,
        mark: tuple,
    ) -> dict | None:
        """The second half of ``_design_by_chat``: open the chat the entry was reserved for."""
        route = self._design_chat
        chat, pressed = "", False
        try:
            if replaced:
                await route.close(replaced, "The Work posted a newer design: this one is over.")
            title = design_review.chat_title(issue, number)
            body = design_review.chat_brief(issue=issue, design_url=url, earlier=earlier)
            chat, pressed = await route.open(title, body)
        except Exception:  # noqa: BLE001 - not opened: the deck's own reviewer takes it
            log.warning("work-router: no ChatGPT design chat for %s", issue, exc_info=True)
        finally:
            self._design_opening.discard(mark)
        mine = self.pending.entries.get(key) or {}
        ours = (
            mine.get("comment") == number and mine.get("asked_at") == now and not mine.get("chat")
        )
        if not ours:
            # A newer ask or the fallback took the entry while the chat opened: this chat is
            # nobody's, and a verdict from it would be a second one.
            if chat:
                await route.close(chat, "This design review was asked again elsewhere.")
            return answer
        opened = self._clock()
        if not chat:
            self.pending.put(key, {"comment": "", "asked_at": 0}, opened)
            return None
        # ``pressed`` is kept for the log only: a start not pressed now is owed and pressed
        # by the run in flight, so every opened chat gets the whole limit.
        limit = opened + design_review.CHAT_LIMIT_S
        self.pending.put(key, {"chat": chat, "opened_at": opened}, opened, at=limit)
        log.info("work-router: design of %s went to ChatGPT in %s (%s)", issue, chat, pressed)
        return answer

    async def _drop_design_chat(self, key: str, why: str) -> None:
        """Forget the design ChatGPT was asked about, and close its chat."""
        chat = str((self.pending.entries.get(key) or {}).get("chat") or "")
        self.pending.discard(key)
        if chat and self._design_chat is not None:
            await self._design_chat.close(chat, why)

    async def _retry_design_chat(self, key: str, entry: dict, gone: bool) -> bool:
        """One try of a design ChatGPT was asked to review (#4722); ``False`` once the deck's
        own reviewer is to take it, the entry by then a plain waiting one.

        Until the limit it only waits: it asks nobody. The limit is counted from when the chat
        opened, not from the growing retry wait. At the limit the entry stops naming the chat
        first, so a verdict that arrives after it is dropped, and only then is the chat
        closed."""
        now = self._clock()
        chat = str(entry.get("chat") or "")
        if (key, entry.get("comment"), entry.get("asked_at")) in self._design_opening:
            # This process is still opening the chat: not "never opened" yet.
            self.pending.put(key, {}, now, at=now + RETRY_FIRST_S)
            return True
        if gone:  # before a waiting verdict: nobody is left to tell it to
            self.pending.discard(key)
            if chat:
                await self._design_chat.close(chat, "The Work this design was for is gone.")
            return True
        if entry.get("verdict_comment"):
            await self._finish_design_verdict(key, entry)
            mine = self.pending.entries.get(key) or {}
            return bool(mine.get("comment")) or not mine
        # A reservation with no chat is one a restart cut off: it waits a minute, no more.
        since = float(entry.get("opened_at") or entry.get("asked_at") or 0)
        limit = since + (design_review.CHAT_LIMIT_S if chat else RETRY_FIRST_S)
        if now < limit:
            self.pending.put(key, {}, now, at=limit)
            return True
        self.pending.put(key, {"comment": "", "chat": "", "asked_at": 0, "close": chat}, now)
        log.info("work-router: no ChatGPT verdict on %s: the deck's reviewer takes it", key)
        return False

    def _design_chat_sender(self, login: str, comment: dict, chat: str) -> bool:
        """Whether ``login`` may give the verdict of the design chat ``chat``
        (``owner/repo#n``): a reviewer account, or ChatGPT's own GitHub connector under the
        login that owns that chat's repository (as ``_from_reviewer``)."""
        if login.casefold() in reviewer_logins():
            return True
        app = comment.get("performed_via_github_app")
        return (
            bool(login)
            and isinstance(app, dict)
            and app.get("slug") == REVIEWER_CONNECTOR
            and chat.partition("/")[0].casefold() == login.casefold()
        )

    def _observe_design_comment(self, delivery: Delivery) -> None:
        """Hand a comment created, edited or deleted on an issue to the series-limit note
        (``DesignRound.observe_comment``, #4800 review): a withdrawal is an edit, and a new or
        deleted design a comment event, which no gate call sees until the agent's next one."""
        payload = delivery.payload
        issue, comment = payload.get("issue"), payload.get("comment")
        if (
            self._design is None
            or delivery.event != "issue_comment"
            or _text(payload.get("action")) not in {"created", "edited", "deleted"}
            or not isinstance(issue, dict)
            or not isinstance(comment, dict)
            or isinstance(issue.get("pull_request"), dict)
        ):
            return
        deleted = _text(payload.get("action")) == "deleted"
        found = comment.get("id")
        number = design_review.comment_id(_text(comment.get("html_url"))) or (
            str(found) if isinstance(found, int) else ""
        )
        ref = f"{delivery.stream.lower()}#{issue.get('number')}"
        self._design.observe_comment(ref, number, _text(comment.get("body")), deleted)

    async def _observe_design(self, delivery: Delivery) -> bool:
        """Take the design verdict ChatGPT posted on a Work's issue (#4722). ``True`` for
        every issue comment that opens with that verdict line, taken or dropped: this is its
        only reader, and it never reaches the Work's chat as a person's comment."""
        payload = delivery.payload
        issue, comment = payload.get("issue"), payload.get("comment")
        if (
            delivery.event != "issue_comment"
            or _text(payload.get("action")) != "created"
            or not isinstance(issue, dict)
            or not isinstance(comment, dict)
            or isinstance(issue.get("pull_request"), dict)
        ):
            return False
        body = _text(comment.get("body"))
        verdict = design_review.chat_verdict_of(body)
        if verdict is None:
            return False
        ref = f"{delivery.stream.lower()}#{issue.get('number')}"
        try:
            async with self._design_lock(f"{DESIGN}:{ref}"):
                await self._take_design_verdict(delivery, verdict)
        except Exception:  # noqa: BLE001 - still settled; a taken one is finished by the retry
            log.warning(
                "work-router: design verdict of %s failed", delivery.delivery_id, exc_info=True
            )
        return True

    async def _take_design_verdict(self, delivery: Delivery, verdict) -> None:
        """Take or drop one verdict comment (``_observe_design``)."""
        payload = delivery.payload
        issue, comment = payload["issue"], payload["comment"]
        body = _text(comment.get("body"))
        ref = f"{delivery.stream.lower()}#{issue.get('number')}"
        key = f"{DESIGN}:{ref}"
        entry = self.pending.entries.get(key) or {}
        chat = str(entry.get("chat") or "")
        number = str(comment.get("id") or "")
        login, _kind = _login(payload)
        if entry.get("verdict_comment") == number and number:
            await self._finish_design_verdict(key, entry)  # the same delivery again
            return
        why = ""
        if self._design is None or self._design_chat is None or verdict.issue != ref:
            why = "it names another issue, or this deck asks ChatGPT for no design review"
        elif not chat or entry.get("comment") != verdict.comment or not number:
            why = f"no ChatGPT chat is open on design comment {verdict.comment}"
        elif entry.get("verdict_comment"):
            why = "that design already has its verdict"
        elif not self._design_chat_sender(login, comment, chat):
            why = f"{login or 'its sender'} is not the reviewer of {chat}"
        elif self._design.verdicts.of(ref).get(verdict.comment):
            why = "that design already has its verdict from the deck's own reviewer"
        if why:
            log.info("work-router: design verdict on %s dropped: %s", ref, why)
            return
        now = self._clock()
        fields = {
            "verdict": verdict.verdict,
            "verdict_comment": number,
            "verdict_url": _text(comment.get("html_url")),
            "verdict_text": body[:DESIGN_VERDICT_CHARS],
            "verdict_at": now,
        }
        # Written before it is recorded or told: a crash from here on is finished by the retry.
        self.pending.put(key, fields, now, at=now + RETRY_FIRST_S)
        await self._finish_design_verdict(key, self.pending.entries.get(key) or {})

    async def _finish_design_verdict(self, key: str, entry: dict) -> None:
        """The one step that finishes a verdict ChatGPT gave (#4722), run under the entry's
        lock by the observer, a replayed delivery, the retry and the agent's next ask alike.

        Every run asks anew whether the verdict's design is still the issue's newest: only
        then is it recorded (once), told to the Work (once, by its comment's id), its chat
        closed and the entry dropped. A newer design drops it untold, a recorded verdict
        staying in the record; an unreadable issue keeps it for the next try. A design
        posted between this read and the message is the window that stays: the message
        names its design comment, and the code review gate reads the newest design."""
        issue = str(entry.get("issue") or "")
        number = str(entry.get("verdict_comment") or "")
        repository, _, issue_number = issue.partition("#")
        chat = str(entry.get("chat") or "")

        def mine() -> bool:  # after a wait: only the run whose verdict the entry still holds
            return (self.pending.entries.get(key) or {}).get("verdict_comment") == number

        async def drop(why: str) -> None:
            if mine():
                self.pending.discard(key)
                if chat and self._design_chat is not None:
                    await self._design_chat.close(chat, why)

        if self._design is None or not number or not issue_number.isdigit():
            self.pending.discard(key)
            return
        owner = self._owner_of_issue(repository, int(issue_number))
        if owner is None or self._release is None:
            await drop("The Work this design was for is gone.")
            return
        if await self._design.closed(issue):
            await drop("The Work's issue is closed.")
            return
        taken = design_review.Verdict(
            issue, str(entry.get("verdict") or ""), str(entry.get("comment") or "")
        )
        try:
            stale = await self._design.stale(issue, taken)
        except design_review.Refused:
            stale = None
        if not mine():
            return
        now = self._clock()
        if stale is None:
            since = float(entry.get("verdict_at") or now)
            if now - since < design_review.CHAT_LIMIT_S or self._design.verdicts.of(issue).get(
                taken.comment
            ):
                # Unread: tried again. A verdict already recorded is never handed to the
                # deck's own reviewer, whose ask that record would refuse: it waits here.
                self.pending.put(key, {}, now)
                return
            # Unread for the whole limit and never recorded: as if ChatGPT gave none.
            log.info("work-router: design verdict on %s unread too long: falls back", issue)
            gone = {"comment": "", "chat": "", "asked_at": 0, "close": chat}
            gone.update(verdict="", verdict_comment="", verdict_url="", verdict_text="")
            self.pending.put(key, gone, now)
            if chat and self._design_chat is not None:
                # Closed here too: an ask that starts the deck's reviewer drops the entry.
                await self._design_chat.close(chat, "The design verdict could not be checked.")
            return
        if stale:
            log.info("work-router: design verdict on %s dropped: %s", issue, stale)
            await drop("The Work posted a newer design.")
            return
        recorded = self._design.take(issue, taken)
        told = await self._release.on_design_review(
            _work_id(owner),
            f"comment:{number}",
            design_review.restated(str(entry.get("verdict_text") or ""), recorded),
            str(entry.get("verdict_url") or ""),
            gap=recorded == design_review.GAP,
            notes=recorded == design_review.NOTES,
        )
        if not told:
            # No chat, or the store would not keep it: tried again with the same id.
            if mine():
                self.pending.put(key, {}, self._clock())
            return
        await drop(f"The deck took the design verdict on {issue}.")

    async def design_accept(self, issue: str, comment: str, verdict: str, token: str) -> dict:
        """Whether the deck takes a design verdict before posting it (#4332,
        ``DesignRound.accept``): ``{"stale": why, "verdict": recorded}``, "" when taken, with
        the verdict the deck will record (#4463: a last-round gap is ``notes``), which the
        post then shows, and whether the issue is ``closed``
        (#4675); ``{"refused": why, "status": n}`` when its sender is not that design's
        reviewer, or unread."""
        if self._design is None:
            return {"refused": "reviews are off on this deck", "status": 409}
        try:
            taken = design_review.Verdict(issue.lower(), verdict, comment)
            stale = await self._design.accept(issue, taken, token)
            closed = not stale and await self._design.closed(issue)
            return {
                "stale": stale,
                "verdict": self._design.kept(issue, taken),
                "closed": closed,
            }
        except design_review.Refused as exc:
            return {"refused": str(exc), "status": exc.status}

    async def design_record(self, issue: str, comment: str, verdict: str, token: str) -> dict:
        """Record the design verdict the deck took and posted (#4332): the gate's evidence.
        ``verdict`` in the answer is what it recorded: a gap in the last round is ``notes``
        (#4463)."""
        known = (design_review.PASS, design_review.GAP, design_review.NOTES)
        if self._design is None or verdict not in known:
            return {"recorded": False}
        try:
            taken = design_review.Verdict(issue.lower(), verdict, comment)
            recorded = self._design.record(issue, taken, token)
        except design_review.Refused as exc:
            return {"recorded": False, "refused": str(exc), "status": exc.status}
        return {"recorded": True, "verdict": recorded}

    def _first_round(self, pull: str) -> bool:
        """Whether no review of ``owner/repo#n`` was asked for yet, at any head: the helper
        checks are asked for, and their comment checked, only then (#4437).

        Unreadable history is not a first round: a fix round is never held for the comment
        on a failed read (#4437 helper check)."""
        try:
            history = list(self._reviews(pull.lower()))
        except Exception:  # noqa: BLE001 - unread: no extra requirement
            log.warning("work-router: %s's review history unreadable", pull, exc_info=True)
            return False
        return not any(item.kind == review_history.ASKED for item in history)

    async def _paperwork_gaps(self, repository: str, number: int, work: str) -> list[str]:
        """The paperwork ``review_paperwork`` finds missing (#4261); ``[]`` when there is no
        check or it failed, so the review starts as before. Before the first review it asks
        for the helper checks' comment too (#4437)."""
        if self._paperwork is None:
            return []
        first = self._first_round(f"{repository}#{number}")
        try:
            return list(await self._paperwork(repository, number, work, first=first))
        except Exception:  # noqa: BLE001 - an unread check never holds a review back
            log.warning("work-router: paperwork of %s#%s unread", repository, number, exc_info=True)
            return []

    async def _design_gap(self, owner: Owner) -> str:
        """What holds the code review of the Work ``owner`` back until its newest
        ``**Design:**`` comment passed its design review (#4332), or "". The one gate every
        review start passes (``_review``); ``pre_review_done`` and ``ask_again`` read it
        first only to refuse with it. Unread comments hold the review too."""
        work = owner[1]
        if self._design is None or not owner[0] or "#" not in work:
            return ""
        queued = self.pending.entries.get(f"{DESIGN}:{work.lower()}")
        try:
            return await self._design.missing(work, queued if isinstance(queued, dict) else None)
        except Exception:  # noqa: BLE001 - an unread check holds the review, said so
            log.warning("work-router: design of %s unread", work, exc_info=True)
            return f"the design review of {work} could not be checked"

    def _tell_design_gap(self, owner: Owner, pull: str, head: str, missing: str) -> None:
        """Tell the chat, once per head and gap, why its code review did not start (#4332)."""
        now = datetime.now(UTC)
        # A review asked and unanswered (#4781) says itself what comes next.
        next_step = "" if missing == design_review.WAITING_GAP else _DESIGN_GAP_NEXT
        self._deliver(
            ScheduledMessage(
                id=f"work-event:design-gap:{pull}@{head}:{missing[:120]}",
                chat_id=owner[0],
                text=f"AgentDeck: {pull} at {head[:12]} gets no code review before its design:"
                f" {missing}.{next_step}",
                due_at=now,
                created_at=now,
            )
        )

    async def _verification_gaps(self, repository: str, work: str, head: str) -> list[str]:
        """The test run ``review_paperwork.verification_gap`` finds missing at ``head`` in a
        repository whose tests are checked (#4326), for the Work ``work``
        (``github_issue:owner/repo#n``, as jobs name it); ``[]`` elsewhere or when unread."""
        if self._verification_rows is None or not self._tests_checked(repository):
            return []
        try:
            rows = list(await self._verification_rows(work))
        except Exception:  # noqa: BLE001 - an unread check never holds a review back
            log.warning("work-router: verification jobs of %s unread", work, exc_info=True)
            return []
        gap = review_paperwork.verification_gap(rows, work, head)
        return [gap] if gap else []

    async def _ask_reviewer(
        self,
        owner: Owner,
        event: Event,
        pull: str,
        key: str,
        digest: str,
        *,
        asked: bool,
        settle: bool = False,
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
            retry.update(asked=True, note=event.note)
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
        except ReviewOff:
            # No reviewer at all (#3849): it never starts, so nothing is kept to retry.
            self.pending.discard(pending_key)
            self._tell_no_reviewer(owner, pull, event.head)
        except ReviewError as exc:
            # Refused before anything was written (``open_review`` finds the open chat
            # before it writes one), so asking again later is safe.
            self.pending.put(pending_key, retry, self._clock())
            log.warning("work-router: review of %s refused: %s; kept pending", key, exc)
        except Exception:  # noqa: BLE001 - one review that could not start must not stop routing
            # It may have written and pressed: spent, as an uncertain start always is.
            self.pending.discard(pending_key)
            if settle and self._overtaken(pull, event.head, asked=asked):
                # A late minor verdict passed this head meanwhile (#4474): an ``asked`` row
                # newer than that pass would void it. ``_review`` closes any chat opened.
                if self._carried_pass(pull, event.head):
                    self._stop_on_carried(owner, pull, event.head)
                return
            self._note_asked(pull, event, digest, uncertain=True)
            log.warning("work-router: review of %s could not start", key, exc_info=True)
        else:
            if not covers(outcome, pull, event.head):
                # The open chat is still about an older commit, held `in-progress` by a
                # worker or otherwise busy, so it was kept rather than replaced: this head
                # was not asked about yet, and is once the worker lets that chat go.
                self.pending.put(pending_key, retry, self._clock())
                log.info("work-router: %s left an older chat open; kept pending", key)
                return
            if settle and self._overtaken(pull, event.head, asked=asked):
                # As above (#4474): no ``asked`` row over the pass; ``_review`` closes the chat.
                if self._carried_pass(pull, event.head):
                    self._stop_on_carried(owner, pull, event.head)
                self.pending.discard(pending_key)
                log.info("work-router: %s passed while its review started", key)
                return
            self.pending.discard(pending_key)
            self._note_asked(pull, event, digest, chat=str(getattr(outcome, "chat", "") or ""))
            log.info("work-router: review of %s asked for (Work %s)", key, work)

    def _note_asked(
        self, pull: str, event: Event, digest: str, *, uncertain: bool = False, chat: str = ""
    ) -> None:
        """The ``asked`` event of ``event``'s head (#3957), once per ask-again request.

        ``chat`` is the review chat the ask got (#4665): a replaced chat's pass of another pull
        request still counts only while every ask since names its own chat, so an uncertain
        start, which names none, keeps it from counting (``review_history.kept_pass``)."""
        data = {"digest": digest, "again": event.again, "note": event.note}
        data.update(no_verdict=event.again.startswith(NO_VERDICT), uncertain=uncertain)
        if chat:
            data["chat"] = chat.lower()
        # A head is asked once (``_spent``) unless asked again under a request id (#3344), so
        # the head and that id name one ask; its retry or a replay writes nothing new.
        key = f"asked:{pull}@{event.head}+{event.again}"
        self._note(pull, event.head, review_history.ASKED, key, data=data)
        if not event.again:
            # The review now asked for is the one the agent's own request wanted (#4658).
            for held in self._requests(pull, event.head):
                self.pending.discard(held)


def _grown(before: object, after: object) -> str:
    """``GROWN_TEXT`` when ``after`` changed lines grew past ``GROWTH_PERCENT`` and
    ``GROWTH_FLOOR`` of the previous round's ``before`` (#4220); "" when either is unread."""
    if not _lines(before) or not _lines(after):
        return ""
    growth = after - before
    if before <= 0 or growth < GROWTH_FLOOR or growth * 100 <= before * GROWTH_PERCENT:
        return ""
    return GROWN_TEXT.format(before=before, after=after, percent=growth * 100 // before)


def _lines(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class ShipPorts:
    """What Ship now, fix later reads and writes beyond the router's own ports (#4212).

    ``open_head(repository, n)`` is ``(head, draft)`` of the open pull request read live, or
    ``("", False)``; ``mark_ready`` takes it out of draft. ``claim(pull, head, work[, fix])``,
    ``note_shipped`` (the head shipped: its review counted as passed) and ``shipped`` are the
    ``shipped_heads`` table, ``claim`` answering ``False`` for a head already recorded.
    """

    open_head: Callable[[str, int], Awaitable[tuple[str, bool]]]
    mark_ready: Callable[[str, int], Awaitable[bool]]
    claim: Callable[..., bool]
    note_shipped: Callable[[str, str], bool]
    shipped: Callable[[str, str], review_history.ShippedHead | None]
    #: ``(repository, n, head, issue)``: the number of the draft pull request that ships
    #: ``n``'s reviewed ``head`` now that a fix was pushed on top, ``n`` turned into a draft
    #: (``ship_split.open_ship_pull``); raises with the reason.
    split: Callable[[str, int, str, int], Awaitable[int]] | None = None
    #: ``(repository, n, new)``: register pull request ``new`` with every chat that registered
    #: ``n``, for the same Work; whether it is.
    register: Callable[[str, int, int], Awaitable[bool]] | None = None
    #: The heads the Work of issue ``owner/repo#n`` shipped (``shipped_heads``, marked shipped
    #: only), read before GitHub so a repeat press answers ``recorded`` (#4558).
    shipped_by_work: Callable[[str], list[review_history.ShippedHead]] | None = None
    #: ``(repository, n, head)``: whether ``head`` lacks commits of ``n``'s base branch
    #: (``behind_base``), so the answer warns a conflict may come (#4576).
    behind: Callable[[str, int, str], Awaitable[bool]] | None = None


#: How long a ship-now call waits for its press before answering ``running`` (#4558): below
#: the web's wait for a merge-service call (``services_link.CALL_TIMEOUT_S``, 60 s).
SHIP_WAIT_S = 40.0
#: A ship-now answer's ``state``: the head is marked shipped (not yet merged) ...
SHIP_RECORDED = "recorded"
#: ... or its press is still running, and the Work's chat hears how it ends.
SHIP_RUNNING = "running"


class ShipRuns:
    """The Ship now presses running in this process (#4558), shared by the router, which runs
    them, and the release, whose hold line says a press is under way rather than unfinished.

    ``tasks`` is each press by the ``pull@head`` its caller sent; ``resolved`` the
    ``pull@head`` that press settled on (a split's new pull request, or the head an earlier
    press passed); ``left`` the keys whose caller got ``running``, so the chat hears the end.
    """

    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Future] = {}
        self.resolved: dict[str, str] = {}
        self.left: set[str] = set()

    def running(self, pull: str, head: str) -> bool:
        """Whether a press of ``pull`` at ``head`` is running now."""
        key = f"{pull.lower()}@{head.lower()}"
        return key in self.tasks or key in self.resolved.values()

    def head_of(self, pull: str) -> str:
        """The head a press of ``pull`` runs at, as its caller sent it or as it resolved
        (a split's new pull request, a carried pass), or "" when none runs."""
        wanted = pull.lower()
        for key in (*self.tasks, *self.resolved.values()):
            name, _, head = key.rpartition("@")
            if name == wanted and head:
                return head
        return ""


def _recorded(pull: str, head: str) -> dict:
    """The answer for a head an earlier press already marked shipped (#4558)."""
    return {
        "pull_request": pull,
        "head": head,
        "state": SHIP_RECORDED,
        "outcome": f"{pull} at {head[:12]} was already shipped: its findings are left to this Work",
    }


def _ship_message(pull: str, head: str, issue: str, source: str) -> str:
    """What the Work's agent hears once Ship now shipped ``pull`` at ``head`` (#4212)."""
    if source:
        return (
            f"AgentDeck: the owner chose Ship now on {source} at {head[:12]}, the commit its"
            f" review came back with findings on. A fix was already pushed on top of it, so the"
            f" deck opened {pull} from a branch at that commit: it merges as it is once its tests"
            f" are green, with no new review. Push nothing to {pull}. {source} is now a draft"
            f" holding your fix: keep it a draft until {pull} merges, then merge its base into it"
            f" and mark it ready, and it is reviewed as usual. The findings stay with this Work"
            f" ({issue}): fix any your fix does not cover in {source}. {SIBLING_SWEEP_ASK}"
            f" The live check is asked"
            f" for once {source} merges too, and the issue stays open until then."
            + SHIP_CONFLICT_HOW
        )
    return (
        f"AgentDeck: the owner chose Ship now on {pull} at {head[:12]}:"
        " it merges as it is once its tests are green, with no new review. Stop"
        " fixing the review's findings here and push nothing more to this pull"
        " request; a push would need a new review and hold the merge back. Its"
        f" findings stay with this Work ({issue}): once it merges and its live"
        " check is recorded, keep the issue open and fix them in a new pull request"
        " for this same issue. Register that pull request with"
        f' `"fixes": "{pull}"` (POST …/pull-requests), so the deck knows it is the fix'
        " and keeps the issue open until it merges. If the owner later chooses no fix,"
        f" record that with `review-accepted` on {pull}, quoting their decision."
        + SHIP_CONFLICT_HOW
    )


#: What a shipped head behind its base is told (#4576): a conflict may hold its merge.
SHIP_BEHIND_TEXT = (
    "{pull} at {head} is behind its base branch, so it may conflict before it merges. If it"
    " does, merge the base in with a merge commit and push nothing else: its Ship now pass"
    " carries over while its code changes stay the same, and any other change is reviewed"
    " again"
)
#: Said in every ship message: the one push a shipped head may take (#4576).
SHIP_CONFLICT_HOW = (
    " The one push it may take: should it conflict with its base, merge the base in with a"
    " merge commit and push only that, keeping its code changes as they are; the Ship now"
    " pass carries over to it."
)


#: The owner's note kept on a pass that ships with its findings left to the Work (#4212).
SHIP_NOTE = review_history.SHIP_NOTE


#: ``pre-review-done``'s answer for a light head (#4399).
LIGHT_NEXT_TEXT = (
    "{pull} at {head} stays outside the guarded code, so it takes lighter review: no reviewer."
    " The deck runs the bench verdict now and sends its answer to this chat; it merges once the"
    " verdict is no worse than master and a run of exactly `bin/test.sh` is green at this head."
    " Ask for a full review with review-request if you want one; that is final for this pull"
    " request."
)


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


def issue_owner_of_pull(state, repository: str, number: int) -> Owner | None:
    """The chat that owns the issue of this pull request's Work (#4753): the one the release
    tells of its merge. ``owner_of_pull`` names the Work as ``owner/repo#n``."""
    placed = owner_of_pull(state, repository, number)
    if placed is None:
        return None
    work_repository, _, work_number = placed[1].rpartition("#")
    if not work_number.isdigit():
        return None
    return owner_of_issue(state, work_repository, int(work_number))


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

    ``legs`` maps each leg to the Work its registration named, "" for none. A registration
    naming a Work its chat joined says what the pull request is for, so only those chats are
    weighed, however long ago the others last moved (#4125). The Work the chosen chat's
    registration named wins while the chat is still in it (#3392); otherwise the one Work the
    chat joined. ``projection`` is called only once a chat or a named Work is found, and the
    caller may hand the same projection to every pull request it places.
    """
    from .work import WorkRef

    def joined_named(leg: str, work_id: str) -> bool:
        ref = WorkRef.from_work_id(work_id)
        return ref is not None and any(
            (work.repository.lower(), work.number) == (ref.repository, ref.number)
            for work in projection().works_chat_joined(state.chat_identity_key(leg))
        )

    named_legs = [leg for leg, work_id in legs.items() if work_id and joined_named(leg, work_id)]
    chat = _latest_chat(state, (state.chat_identity_key(leg) for leg in named_legs or legs))
    if chat is None:
        return None
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


def work_pulls(state, work_id: str, projection=None) -> tuple[str, ...]:
    """Every pull request registered for this Work, as ``owner_of_pull`` places it.

    The release's set: a pull request counts for the Work its registering chat joined, never
    by what its body says. Lowercased ``owner/repo#n``. Only the pull requests a chat of this
    Work registered are placed, since ``_place_pull`` names no other Work's: placing every
    one of the deck's was most of a Work page's build (#4639). A caller asking about many
    Works takes ``pulls_lookup`` instead. ``projection`` is a caller's Work grouping.
    """
    projection = projection if projection is not None else state.work_projection()
    work = work_id.partition(":")[2].lower()
    snapshot = getattr(state, "chat_snapshot", None)
    chats = snapshot() if snapshot is not None else None
    joined: dict[str, bool] = {}

    def in_work(leg: str) -> bool:
        chat = state.chat_identity_key(leg, chats)
        if chat not in joined:
            joined[chat] = any(
                f"{ref.repository}#{ref.number}".lower() == work
                for ref in projection.works_chat_joined(chat)
            )
        return joined[chat]

    wanted = {
        ref.identity
        for leg, refs in tuple(state.session_pull_requests.items())
        if refs and in_work(leg)
        for ref in refs
    }
    if not wanted:
        return ()
    return pulls_by_work(state, projection, wanted).get(work, ())


def pulls_lookup(state) -> Callable[[str], tuple[str, ...]]:
    """``work_pulls`` for any Work, from one walk of the deck taken now (#3993).

    A commit status asks about every Work the release keeps; calling ``work_pulls`` for each
    of almost a thousand walked thousands of registrations each time and held the web event
    loop for a minute per status.
    """
    placed = pulls_by_work(state)
    return lambda work_id: placed.get(work_id.partition(":")[2].lower(), ())


def pulls_by_work(
    state, work_projection=None, only: Collection[tuple[str, int]] | None = None
) -> dict[str, tuple[str, ...]]:
    """Each Work's registered pull requests, keyed by lowercased ``owner/repo#n`` of the Work.

    One walk of the registrations and one Work projection for the whole deck (#2954): asking
    ``owner_of_pull`` for each of thousands of registered pull requests walked them all again
    and rebuilt the projection's key every time. ``work_projection`` is a caller's
    already-built Work grouping. The Work pages read this too, so they list what the release
    holds (#4125), and pass ``only``, the ``PullRequestRef.identity`` of the pull requests
    they show: placing every one of the deck's doubled the time of a Work page.
    """
    from . import work_counter

    work_counter.count(work_counter.PULL_WALK)
    legs_by_pull: dict[tuple[str, int], dict[str, str]] = {}
    for leg, refs in tuple(state.session_pull_requests.items()):
        for ref in refs:
            legs_by_pull.setdefault(ref.identity, {}).setdefault(leg, ref.work_id)
    projected = work_projection

    def projection():
        nonlocal projected
        if projected is None:
            projected = state.work_projection()
        return projected

    found: dict[str, set[str]] = {}
    for (repository, number), legs in legs_by_pull.items():
        if only is not None and (repository, number) not in only:
            continue
        owner = _place_pull(state, legs, projection)
        if owner is not None:
            found.setdefault(owner[1].lower(), set()).add(f"{repository}#{number}")
    return {work: tuple(sorted(pulls)) for work, pulls in found.items()}


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


def design_queue(path: Path, issue: str) -> dict | None:
    """The design review of ``issue`` waiting for a reviewer place, read from the router's
    pending file at ``path`` (``GET …/design-review``, #4342): when it was asked (``since``)
    and when it is next tried, or ``None``."""
    import json

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = data.get("pending") if isinstance(data, dict) else None
    mine = entries.get(f"{DESIGN}:{issue.lower()}") if isinstance(entries, dict) else None
    if not isinstance(mine, dict):
        return None

    def at(seconds) -> str:
        return datetime.fromtimestamp(float(seconds or 0), UTC).isoformat()

    found = {"since": at(mine.get("since")), "next_try": at(mine.get("next_at"))}
    if mine.get("comment"):
        # ChatGPT was asked for this design comment (#4722), in this chat once it opened.
        found.update(comment=str(mine["comment"]), chat=str(mine.get("chat") or ""))
    return found


def design_asked(streams_root: Path | str, ledger: Path, issue: str, now: float) -> bool:
    """Whether a review of one of ``issue``'s design comments was asked for and has not
    answered (#4781), read from local files only: the release's hold and the turn-end nudge
    then name the design review, not ``pre-review-done``, which the design gate refuses.

    True while the router's request waits for a reviewer place, or names a comment (ChatGPT's
    chat) with no verdict recorded, or the deck's own reviewer on a comment with no verdict has
    a ledger line younger than its age limit. It reads no GitHub, so it does not know which
    comment is the newest: a reviewer on an older one counts until its line goes."""
    from . import agent_review

    verdicts = design_review.Verdicts.beside(Path(ledger)).of(issue)
    queued = design_queue(pending_path(streams_root), issue)
    if queued is not None and (not queued.get("comment") or queued["comment"] not in verdicts):
        return True
    for line in agent_review.design_lines(Path(ledger), issue):
        comment = str(line.get("head") or "").removeprefix("comment-")
        age = now - float(line.get("started") or 0)
        if comment and comment not in verdicts and age < agent_review.MAX_AGE_S:
            return True
    return False


def design_limited(ledger: Path, issue: str) -> bool:
    """Whether the series limit stops a review of ``issue``'s design until the owner answers
    (#4800), from the deck's own record (``Verdicts.limited``) with no GitHub call. The design
    gate refuses ``pre-review-done`` then, so the deck must not ask the agent for it."""
    return bool(design_review.Verdicts.beside(Path(ledger)).limited(issue))


def handed_over_path(streams_root: Path | str) -> Path:
    """The Works handed to another deck (``KeyFile``, #3731), beside the webhook log root:
    the router writes it, and the web reads it for the Works' status (#4450)."""
    return Path(streams_root).expanduser().parent / HANDED_OVER_FILENAME


def pending_path(streams_root: Path | str) -> Path:
    """The router's pending file beside the webhook log root (``PendingActions``)."""
    return Path(streams_root).expanduser().parent / PENDING_FILENAME
