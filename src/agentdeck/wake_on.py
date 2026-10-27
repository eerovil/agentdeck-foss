"""Wake a chat when the GitHub event it is waiting on lands (#2775).

The session guide forbids waiting by polling, so an agent waiting on a merge, a CI run
or a deploy ends its turn. Before this, nothing brought it back when the event happened:
the deck wakes a chat for a person's message, a dispatch or a finished ``agentdeck job``,
and the owner ended up being the wake-up call.

A chat registers what it is waiting for (``POST /api/sessions/<key>/wake-on``), and this
consumer of the raw webhook streams sends it one message when a matching delivery lands —
or when the registration's timeout passes first, because a webhook can be dropped and a
quiet stream looks exactly like an event that never came. Either way the registration is
spent: one registration, one message.

The rules this holds to:

* **A wake says "look now", never "this is true".** The message tells the agent to
  confirm with one read before acting, and nothing here writes state anything else reads.
* **At most one message per registration.** The message goes through the ordinary
  scheduled-message path with ``wake-on:<id>`` as its delivery id, so a replay after a
  crash between scheduling and forgetting the registration reschedules the same id, which
  the store absorbs, instead of writing a second turn.
* **Only deliveries received after the registration count.** The tailer keeps a durable
  offset, so deliveries that land while the web process is down are still read after it
  comes back; the received-at check stops one that predates the registration from firing.
  The guide asks agents to register *before* their one read, which closes the race the
  other way.

A time wait (``at`` or ``after_minutes``, #2968) is the one kind with no event: long local
work such as a render reports nothing to GitHub, so its deadline is the wake.

A review wait (``review``, #3183) waits on the reviewer's verdict on a pull request. The
deck already hands that verdict to the Work's chat as a message of its own, so the verdict
ends the wait without a second one (``answer_reviews``); only its timeout sends a message. A
review chat whose reviewer stops — labelled ``blocked`` or ``needs-more-info``, or closing
with a ``Reviewed … at <sha>:`` line — moves that timeout up to ``STOP_SETTLE_S`` away and
words it with the reviewer's own note (#3316). A verdict that does reach the chat in that
time still ends the wait quietly; one that never comes (the reviewer could not post it, or a
repeat review found its findings already sent) no longer leaves the agent waiting for hours
on nothing. A merge wait (``pull_request``) on the same pull request is moved up the same
way (#3611), keeping its own deadline in ``cut_from``: a verdict told in that time gives the
deadline back, so a clean review still lets it wait for the merge in silence; a verdict told
before the stop (``WakeOnRegistry.answered``) keeps that review chat from cutting it at all,
even for a merge wait registered after it. A wait the agent registered with its own request
for a review (``review-request``, #3453) is not cut short by a review chat opened before
that request: the review it asked for is still to come, and an older chat closing on its own
summary said nothing about it. A review wait times out after ``REVIEW_TIMEOUT_MINUTES`` (20)
unless the chat asks for longer, and its message sends the worker to check the reviewer is
moving — the pull request's head, an open review chat at that head, recent activity there —
and to tell the person when it is not, rather than only to wait again (#3389).

A commit wait on a pull request's head goes stale once that head is replaced (#3271): CI
reports on the new head, so the wait would only time out and wake a chat for nothing. When a
pull request a chat registered moves to a new head, or merges or closes, that chat's commit
waits on the head it left are dropped without a message.

A needs-you wait (``needs_you``, #3446) is the one kind that is not spent by a message: the
owner's deck chat answers "what needs me?", and it wants a message each time a Work moves into
Needs answer or Ready for merge, without registering again after every question. It has no
GitHub event either: the Work overview hands each pass's Needs you list to ``needs_you``, and
every Work that entered a wanted category after the registration is told once, its stop (the
Work and when it entered its category) kept on the wait so the next pass does not repeat it.
Its own timeout, a week by default, is the only thing that ends it besides a ``DELETE``.
It wakes its chat only while the owner is in it (#3464): the overview passes whether a chat
has a page open on it, and the chat page closes its stream once hidden unless voice mode is
on, so that is "open in front of them, or talking to it". A stop met while nobody is there
is marked told without a message: nobody reads it, and the deck already lists that Work.
Its timeout ends it silently too while nobody is there.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import chat_board, chat_review, review_refs, watch_retry
from .github_webhook_log import raw_body
from .models import PullRequestRef
from .scheduled_messages import ScheduledMessage
from .shared_file import SharedFile
from .webhook_tailer import Delivery, WebhookTailer, WebhookWatcher

log = logging.getLogger(__name__)

CONSUMER = "wake-on"
REGISTRY_FILENAME = "wake-on.json"

KIND_PULL_REQUEST = "pull_request"
KIND_COMMIT = "commit"
KIND_DEPLOYMENT = "deployment"
#: This deck's own deploy, which no GitHub event reports: the deploy timer restarts the web
#: process on the new code, so the process that comes up checks its own revision.
KIND_DECK_DEPLOY = "deck_deploy"
#: A plain time, for work no GitHub event reports: a long local job such as a render on
#: another machine (#2968). It has no event, so its deadline is the wake.
KIND_TIME = "time"
#: The reviewer's verdict on a pull request (#3183). The verdict reaches the chat as the
#: deck's own message, which ends this wait quietly; only a timeout wakes the chat from here.
KIND_REVIEW = "review"
#: A Work moving into Needs answer or Ready for merge (#3446): the deck's own Needs you list,
#: no GitHub event. Not spent by a message; one message per Work that moves in.
KIND_NEEDS_YOU = "needs_you"
#: What ``only`` may name, and the ``work_attention`` category each stands for.
NEEDS_YOU_CATEGORIES = {"answer": "needs_answer", "merge": "ready_for_merge"}
#: A needs-you wait is meant to stay registered: a week, unless the chat asks for less.
NEEDS_YOU_TIMEOUT_MINUTES = 7 * 24 * 60
#: How long a deck-deploy check git could not answer waits before it is asked again.
UNSURE_RETRY_S = 60.0
#: How often a deck-deploy wait whose commit the bundled skills do not include yet reads the
#: deploy's skills marker again (#3584). The deploy installs them seconds after the web
#: reload, and the read is one small file, so this is short.
SKILLS_RETRY_S = 10.0
#: How much of a repository's stream a deployment wait's reply reads back for the latest
#: deploys the deck has seen (#3584): a busy repository writes a day's deliveries in this.
DEPLOY_TAIL_BYTES = 4 * 1024 * 1024
#: How long a wake whose store write failed waits before it is written again. The store is
#: the deck's own SQLite, so a failure is rare and usually brief.
PENDING_RETRY_S = 10.0

#: How long after a review chat's reviewer stops a review wait on its pull requests is woken,
#: unless the verdict reaches the chat first (#3316). The verdict and the chat's closing note
#: land on two streams in either order, and the deck reads the pull request before it tells
#: the chat, so this is minutes rather than seconds.
STOP_SETTLE_S = 300.0
#: How long a verdict that reached a chat is remembered (``WakeOnRegistry.answered``): a day,
#: the longest a wait may run.
ANSWERED_KEEP_S = 24 * 3600.0
#: The labels a reviewer puts on a review chat it stopped on (its repository's protocol).
STOP_LABELS = frozenset({"blocked", "needs-more-info"})
#: How much of a reviewer's note a stopped review's wake quotes.
MAX_STOP_NOTE = 1500
#: GitHub stamps a chat's opening to the second, and its clock is not this host's: a chat opened
#: this close to a review request may be the one it asked for (#3453).
ASK_SLACK_S = 10.0
#: Review chats whose newest reviewer comment is kept for a label that comes after it.
MAX_NOTED_CHATS = 50
DEFAULT_TIMEOUT_MINUTES = 120
#: A review wait's default (#3389). A reviewer that stalled or never started — a stale head
#: on the deck once started no review at all — went unnoticed for the full two hours, so
#: the worker is sent to check the reviewer's progress after this instead.
REVIEW_TIMEOUT_MINUTES = 20
MAX_TIMEOUT_MINUTES = 24 * 60
#: Per chat. A chat waiting on more than this at once is more likely looping than waiting.
MAX_ACTIVE_PER_CHAT = 10
#: The longest ``waiting_for`` a time wait takes: it is shown in the Work's status line.
MAX_NOTE = 80

#: A commit status or deployment status in one of these states is a result; anything
#: else (``pending``, ``queued``, ``in_progress``) is a run still going.
_TERMINAL_STATUS = frozenset({"success", "failure", "error"})

_REPOSITORY_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
#: An ``owner/repo#n`` or ``repo#n`` in a reviewer's closing line.
_REF_RE = re.compile(r"(?:([A-Za-z0-9_.-]+)/)?([A-Za-z0-9_.-]+)#(\d+)")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_CLOCK_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")


def registry_path(streams_root: Path | str) -> Path:
    """Where the registrations live: beside the streams, like the tailers' own state."""
    return Path(streams_root).expanduser().parent / REGISTRY_FILENAME


@dataclass(frozen=True)
class WakeRequest:
    """One chat waiting on one GitHub event."""

    id: str
    session_key: str
    chat_id: str
    kind: str
    repository: str  # lowercased owner/repo
    number: int | None = None
    sha: str = ""  # lowercased, possibly a prefix
    name: str = ""  # case-insensitive whole-word filter on the result's name, see _name_matches
    registered_at: float = 0.0
    deadline: float = 0.0
    #: What a time wait is for, in the agent's words ("the render on the GTX 970").
    note: str = ""
    #: The message this wait already owes but could not save yet. Kept so a store
    #: failure retries the same text instead of losing the wake (#2775 review).
    pending_text: str = ""
    #: A review wait whose review chat stopped (#3316): what the reviewer said, which its
    #: shortened timeout passes on.
    stopped: str = ""
    #: A merge wait a stopped review brought forward (#3611): the deadline it had before, which
    #: a verdict told in the meantime gives back; 0 when nothing brought it forward.
    cut_from: float = 0.0
    #: When the agent asked for the review this wait is on (``review-request``, #3453); 0 when
    #: it only waits. A review chat opened before then was not asked for by it.
    asked_at: float = 0.0
    #: A needs-you wait's category filter, a key of ``NEEDS_YOU_CATEGORIES``; "" for both.
    only: str = ""
    #: A needs-you wait's stops already told (``NeedsYouStop.key``), so a pass repeats none.
    told: tuple[str, ...] = ()

    @property
    def target(self) -> str:
        if self.kind == KIND_PULL_REQUEST:
            return f"pull request {self.repository}#{self.number}"
        if self.kind == KIND_REVIEW:
            return f"the review of pull request {self.repository}#{self.number}"
        if self.kind == KIND_DECK_DEPLOY:
            return f"this deck's own deploy of commit {self.sha}"
        if self.kind == KIND_TIME:
            return self.note or "a check-back time"
        if self.kind == KIND_NEEDS_YOU:
            labels = {"answer": "Needs answer", "merge": "Ready for merge"}
            which = labels.get(self.only, "Needs answer or Ready for merge")
            where = f" in {self.repository}" if self.repository else ""
            return f"any Work{where} moving into {which}"
        what = "CI or a deployment on commit" if self.kind == KIND_COMMIT else "a deployment in"
        where = f"{self.repository}@{self.sha}" if self.sha else self.repository
        named = f" named like {self.name!r}" if self.name else ""
        return f"{what} {where}{named}"

    def same_wait(self, other: WakeRequest) -> bool:
        return (
            self.chat_id,
            self.kind,
            self.repository,
            self.number,
            self.sha,
            self.name.lower(),
            self.note.lower(),
            self.only,
        ) == (
            other.chat_id,
            other.kind,
            other.repository,
            other.number,
            other.sha,
            other.name.lower(),
            other.note.lower(),
            other.only,
        )

    def payload(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "repository": self.repository,
            "number": self.number,
            "sha": self.sha or None,
            "name": self.name or None,
            "only": self.only or None,
            "waiting_for": self.target,
            "registered_at": self.registered_at,
            "deadline": self.deadline,
        }


_FIELDS = frozenset(field.name for field in fields(WakeRequest))


class WakeRequestError(ValueError):
    """A registration the API refuses, with the reason the caller is shown."""


def parse_request(
    *,
    pull_request: str | None = None,
    review: str | None = None,
    commit: str | None = None,
    deployment: str | None = None,
    deck_deploy: str | None = None,
    at: str | None = None,
    after_minutes: int | None = None,
    needs_you: str | None = None,
    only: str | None = None,
    waiting_for: str | None = None,
    name: str | None = None,
    timeout_minutes: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Validate one registration body into the fields of a ``WakeRequest``.

    ``now`` is when a time wait counts from, an aware datetime whose zone a bare ``11:30``
    is read in; this host's clock and zone by default.
    """
    at = at or None
    given = [
        value
        for value in (
            pull_request, review, commit, deployment, deck_deploy, at, after_minutes, needs_you
        )
        if value is not None and value != ""
    ]
    if len(given) != 1:
        raise WakeRequestError(
            "give exactly one of pull_request (owner/repo#number), review"
            " (owner/repo#number: its reviewer's verdict), commit"
            " (owner/repo@sha), deployment (owner/repo or owner/repo@sha),"
            " deck_deploy (a commit sha of this deck), at (a time such as 11:30),"
            " after_minutes (a number of minutes) or needs_you (all, or owner/repo: Works"
            " moving into Needs answer or Ready for merge)"
        )
    if needs_you:
        return _needs_you_request(needs_you, only, waiting_for, name, timeout_minutes)
    if only:
        raise WakeRequestError("only filters a needs_you wait")
    note = " ".join((waiting_for or "").split())
    if at is not None or after_minutes is not None:
        return _time_request(at, after_minutes, note, name, timeout_minutes, now)
    if note:
        raise WakeRequestError("waiting_for describes a time wait (at or after_minutes)")
    default = REVIEW_TIMEOUT_MINUTES if review else DEFAULT_TIMEOUT_MINUTES
    minutes = default if timeout_minutes is None else timeout_minutes
    if not 1 <= minutes <= MAX_TIMEOUT_MINUTES:
        raise WakeRequestError(f"timeout_minutes must be between 1 and {MAX_TIMEOUT_MINUTES}")
    fields: dict = {"timeout_s": minutes * 60.0, "name": (name or "").strip()}
    if fields["name"] and not _words(fields["name"]):
        raise WakeRequestError(f"name must contain a letter or digit; got {name!r}")
    if pull_request or review:
        given_ref = pull_request or review
        field = "pull_request" if pull_request else "review"
        ref = PullRequestRef.parse(given_ref)
        if ref is None:
            raise WakeRequestError(
                f"{field} must be a pull request URL or owner/repo#number; got {given_ref!r}"
            )
        if fields["name"]:
            raise WakeRequestError("name filters a commit or deployment wait, not a pull request")
        fields.update(
            kind=KIND_PULL_REQUEST if pull_request else KIND_REVIEW,
            repository=ref.repository.lower(),
            number=ref.number,
        )
        return fields
    if deck_deploy:
        sha = deck_deploy.strip()
        if not _SHA_RE.match(sha):
            raise WakeRequestError(f"deck_deploy must be a commit sha, 7-40 hex; got {sha!r}")
        if fields["name"]:
            raise WakeRequestError("name filters a commit or deployment wait, not deck_deploy")
        fields.update(kind=KIND_DECK_DEPLOY, repository="", sha=sha.lower())
        return fields
    if commit:
        repository, sha = _repository_at(commit, sha_required=True)
        fields.update(kind=KIND_COMMIT, repository=repository, sha=sha)
        return fields
    repository, sha = _repository_at(str(deployment), sha_required=False)
    fields.update(kind=KIND_DEPLOYMENT, repository=repository, sha=sha)
    return fields


def _needs_you_request(
    value: str, only: str | None, note: str | None, name: str | None, timeout_minutes: int | None
) -> dict:
    """A wait on Works moving into Needs you (#3446): ``all`` or one ``owner/repo``."""
    if name or note:
        raise WakeRequestError("a needs_you wait takes no name or waiting_for; only filters it")
    scope = value.strip()
    if scope.lower() != "all" and not _REPOSITORY_RE.match(scope):
        raise WakeRequestError(f"needs_you must be all or owner/repo; got {value!r}")
    which = (only or "").strip().lower()
    if which and which not in NEEDS_YOU_CATEGORIES:
        raise WakeRequestError(f"only must be answer or merge; got {only!r}")
    minutes = NEEDS_YOU_TIMEOUT_MINUTES if timeout_minutes is None else timeout_minutes
    if not 1 <= minutes <= NEEDS_YOU_TIMEOUT_MINUTES:
        raise WakeRequestError(
            f"a needs_you wait's timeout_minutes must be between 1 and {NEEDS_YOU_TIMEOUT_MINUTES}"
        )
    return {
        "timeout_s": minutes * 60.0,
        "kind": KIND_NEEDS_YOU,
        "repository": "" if scope.lower() == "all" else scope.lower(),
        "only": which,
    }


def _time_request(
    at: str | None,
    after_minutes: int | None,
    note: str,
    name: str | None,
    timeout_minutes: int | None,
    now: datetime | None,
) -> dict:
    """A wait on a plain time: its deadline is the wake, so it takes no timeout (#2968)."""
    if name or timeout_minutes is not None:
        raise WakeRequestError("a time wait takes no name or timeout_minutes: its time is the wake")
    if len(note) > MAX_NOTE:
        raise WakeRequestError(f"waiting_for is at most {MAX_NOTE} characters")
    current = now or datetime.now().astimezone()
    if after_minutes is not None:
        minutes = after_minutes
    else:
        due = _parse_at(str(at), current)
        minutes = (due - current).total_seconds() / 60
    if not 1 <= minutes <= MAX_TIMEOUT_MINUTES:
        raise WakeRequestError(
            f"a time wait must fall between 1 minute and {MAX_TIMEOUT_MINUTES // 60} hours from now"
        )
    return {
        "timeout_s": minutes * 60.0,
        "name": "",
        "kind": KIND_TIME,
        "repository": "",
        "note": note,
    }


def _parse_at(value: str, now: datetime) -> datetime:
    """``11:30`` (the next 11:30 on this host's clock) or an ISO time, as an aware datetime."""
    text = value.strip()
    clock = _CLOCK_RE.match(text)
    if clock:
        due = now.replace(hour=int(clock[1]), minute=int(clock[2]), second=0, microsecond=0)
        return due if due > now else due + timedelta(days=1)
    try:
        due = datetime.fromisoformat(text)
    except ValueError:
        raise WakeRequestError(
            f"at must be a time such as 11:30 or an ISO date and time; got {value!r}"
        ) from None
    return due if due.tzinfo is not None else due.replace(tzinfo=now.tzinfo)


def _repository_at(value: str, *, sha_required: bool) -> tuple[str, str]:
    text = value.strip()
    repository, at, sha = text.partition("@")
    if not _REPOSITORY_RE.match(repository) or (at and not _SHA_RE.match(sha)):
        raise WakeRequestError(
            f"expected owner/repo@<commit sha, 7-40 hex>; got {value!r}"
            if sha_required
            else f"expected owner/repo or owner/repo@<commit sha>; got {value!r}"
        )
    if sha_required and not sha:
        raise WakeRequestError(f"commit needs a sha: owner/repo@<sha>; got {value!r}")
    return repository.lower(), sha.lower()


@dataclass(frozen=True)
class Outcome:
    """What a matching delivery said, in words for the woken chat."""

    summary: str


def match(request: WakeRequest, delivery: Delivery) -> Outcome | None:
    """The outcome this delivery reports for this registration, or ``None``."""
    if (
        request.kind in {KIND_DECK_DEPLOY, KIND_TIME, KIND_REVIEW, KIND_NEEDS_YOU}
        or delivery.stream.lower() != request.repository
    ):
        return None
    payload = delivery.payload
    if request.kind == KIND_PULL_REQUEST:
        return _pull_request_outcome(request, delivery.event, payload)
    result = _result(delivery.event, payload)
    if result is None:
        return None
    sha, name, conclusion, url = result
    # A deploy is reported either as a deployment status or, like Vercel, only as a
    # commit status; a check suite or workflow run is CI, and can finish before the
    # deploy it belongs to does. A commit status is also how CI such as Drone reports,
    # so a deployment wait takes one only when a name says which status is the deploy.
    if request.kind == KIND_DEPLOYMENT:
        if delivery.event == "status" and not request.name:
            return None
        if delivery.event not in {"deployment_status", "status"}:
            return None
    if request.sha and not sha.lower().startswith(request.sha):
        return None
    if request.name and not _name_matches(request.name, name):
        return None
    label = {
        "check_suite": "check suite",
        "check_run": "check run",
        "workflow_run": "workflow",
        "status": "commit status",
        "deployment_status": "deployment to",
    }[delivery.event]
    summary = (
        f"{label} {name or '(unnamed)'} on {request.repository}@{sha[:12]} finished: {conclusion}"
    )
    if url:
        summary += f" ({url})"
    return Outcome(summary)


_WORD_RE = re.compile(r"[^\W_]+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _name_matches(wanted: str, name: str) -> bool:
    """Whether ``wanted`` names ``name``: its words appear in a row among the name's words.

    A raw substring let a wait named ``drone/pr`` fire on the status
    ``continuous-integration/drone/promote/staging`` and be spent, so the real
    ``continuous-integration/drone/pr`` woke nobody (#3172). Matching whole words, with
    punctuation and case ignored, keeps ``drone/pr``, ``pr`` and ``Vercel`` working.
    """
    want, have = _words(wanted), _words(name)
    size = len(want)
    return bool(want) and any(have[i : i + size] == want for i in range(len(have) - size + 1))


def _pull_request_outcome(request: WakeRequest, event: str, payload: dict) -> Outcome | None:
    if event != "pull_request" or payload.get("action") != "closed":
        return None
    pull = payload.get("pull_request")
    if not isinstance(pull, dict):
        return None
    number = payload.get("number", pull.get("number"))
    if number != request.number:
        return None
    where = f"{request.repository}#{request.number}"
    if pull.get("merged"):
        sha = pull.get("merge_commit_sha")
        commit = f" as {sha[:12]}" if isinstance(sha, str) and sha else ""
        return Outcome(f"pull request {where} was merged{commit}")
    return Outcome(f"pull request {where} was closed without merging")


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _result(event: str, payload: dict) -> tuple[str, str, str, str] | None:
    """``(sha, name, conclusion, url)`` for a finished CI or deploy result, else ``None``."""
    if event in {"check_suite", "check_run", "workflow_run"}:
        if payload.get("action") != "completed":
            return None
        run = payload.get(event)
        if not isinstance(run, dict):
            return None
        if event == "check_suite":
            app = run.get("app")
            name = _text(app.get("name")) if isinstance(app, dict) else ""
            url = ""
        else:  # a check run (one CI job or app check) or a workflow run
            name = _text(run.get("name"))
            url = _text(run.get("html_url"))
        return _text(run.get("head_sha")), name, _text(run.get("conclusion")) or "completed", url
    if event == "status":
        state = _text(payload.get("state"))
        if state not in _TERMINAL_STATUS:
            return None
        return (
            _text(payload.get("sha")),
            _text(payload.get("context")),
            state,
            _text(payload.get("target_url")),
        )
    if event == "deployment_status":
        status = payload.get("deployment_status")
        deployment = payload.get("deployment")
        if not isinstance(status, dict) or not isinstance(deployment, dict):
            return None
        state = _text(status.get("state"))
        if state not in _TERMINAL_STATUS:
            return None
        environment = _text(status.get("environment")) or _text(deployment.get("environment"))
        url = _text(status.get("environment_url")) or _text(status.get("target_url"))
        return _text(deployment.get("sha")), environment, state, url
    return None


def replaced_heads(delivery: Delivery) -> tuple[tuple[str, int], tuple[str, ...], str] | None:
    """``((repository, number), heads, reason)`` when a delivery leaves heads of a pull
    request behind, else ``None``.

    A push to the branch (``synchronize``) leaves ``before``; a merge or a close leaves the
    head it ended on. Only heads a delivery names are known: the payload keeps no history.
    """
    if delivery.event != "pull_request":
        return None
    payload = delivery.payload
    pull = payload.get("pull_request")
    number = payload.get("number", pull.get("number") if isinstance(pull, dict) else None)
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    action = payload.get("action")
    if action == "synchronize":
        heads = (_text(payload.get("before")),)
        reason = "its head moved"
    elif action == "closed" and isinstance(pull, dict):
        head = pull.get("head")
        heads = (_text(head.get("sha")) if isinstance(head, dict) else "",)
        reason = "it merged" if pull.get("merged") else "it closed"
    else:
        return None
    heads = tuple(sha.lower() for sha in heads if _SHA_RE.match(sha))
    if not heads:
        return None
    return (delivery.stream.lower(), number), heads, reason


@dataclass(frozen=True)
class ReviewStop:
    """A review chat whose reviewer stopped, and the pull requests it was about (#3316)."""

    chat: int
    pulls: tuple[str, ...]  # lowercased owner/repo#n
    note: str
    opened_at: float | None = None  # when the chat was opened, if the payload says


def _quote(body: str) -> str:
    body = body.strip()
    return body if len(body) <= MAX_STOP_NOTE else body[:MAX_STOP_NOTE].rstrip() + " …"


def review_stop(
    delivery: Delivery, chat_repository: str, noted: dict[int, str] | None = None
) -> ReviewStop | None:
    """The stop this delivery reports on a review chat in ``chat_repository``, or ``None``.

    A comment carrying a verdict line is the reviewer's closing note: its verdict should be
    on the pull request by now. A ``blocked`` or ``needs-more-info`` label on an open chat is a
    reviewer that stopped short, and its reason is the newest comment, which ``noted`` keeps
    per chat from the comments this reader saw before the label.
    """
    if not chat_repository or delivery.stream.lower() != chat_repository.lower():
        return None
    payload = delivery.payload
    issue = payload.get("issue")
    if not isinstance(issue, dict) or isinstance(issue.get("pull_request"), dict):
        return None
    number, title = issue.get("number"), _text(issue.get("title"))
    if not isinstance(number, int) or review_refs.review_target(title) is None:
        return None
    action = payload.get("action")
    url = _text(issue.get("html_url")) or f"{chat_repository}#{number}"
    if delivery.event == "issue_comment" and action == "created":
        comment = payload.get("comment")
        body = _text(comment.get("body")) if isinstance(comment, dict) else ""
        if not body.strip():
            return None
        if noted is not None:
            noted.pop(number, None)
            noted[number] = body
            while len(noted) > MAX_NOTED_CHATS:
                noted.pop(next(iter(noted)))
        lines = chat_board.verdict_lines(body)
        if not lines:
            return None
        note = f"the review chat {url} closed its review, saying:\n\n{_quote(body)}"
    elif delivery.event == "issues" and action == "labeled":
        label = payload.get("label")
        name = _text(label.get("name")).strip().lower() if isinstance(label, dict) else ""
        if name not in STOP_LABELS or _text(issue.get("state")) != "open":
            return None
        said = (noted or {}).get(number, "")
        note = f"the review chat {url} was labelled `{name}`: its reviewer stopped"
        note += f", saying:\n\n{_quote(said)}" if said else "; its newest comment says why."
        lines = []
    else:
        return None
    pinned = sorted(
        (repository.lower(), pull)
        for repository, pull in chat_review.pinned_heads(_text(issue.get("body")), title)
    )
    if lines:
        # A closing line is about the pull requests it names: in a chat covering several, the
        # others may still be under review (#3316 review). A one-pull-request chat whose line
        # names only the card is about that one pull request.
        named = {
            (owner.lower(), name.lower(), int(pull))
            for line in lines
            for owner, name, pull in _REF_RE.findall(line)
        }
        covered = [
            (repository, pull)
            for repository, pull in pinned
            if any(
                (owner or repository.partition("/")[0], name, n)
                == (*repository.partition("/")[::2], pull)
                for owner, name, n in named
            )
        ]
        pinned = covered or (pinned if len(pinned) == 1 else [])
    pulls = tuple(f"{repository}#{pull}" for repository, pull in pinned)
    try:
        opened_at = datetime.fromisoformat(_text(issue.get("created_at"))).timestamp()
    except ValueError:
        opened_at = None
    return ReviewStop(number, pulls, note, opened_at) if pulls else None


def stopped_message(request: WakeRequest) -> str:
    return (
        f"AgentDeck wake-on: {request.stopped}\n\nNo verdict reached this chat for"
        f" {request.target} in the {round(STOP_SETTLE_S / 60)} minutes after that. Read the"
        " review chat once. Act on findings it names, and answer a question it asks. If it"
        " ended with no verdict for your head, ask for the review again yourself, without"
        " asking the person: `POST $AGENTDECK_URL/api/sessions/<session-key>/review-request`"
        f' with `{{"pull_request": "{request.repository}#{request.number}", "no_verdict": true,'
        ' "note": "<what the last review chat said instead of a verdict>"}`. An answer with'
        ' `"outcome": "already asked"` means a review is still to come: end the turn on the'
        " wait it kept. Ask the person in the question box only if the request is refused"
        " (409). Never post the reviewer's verdict for it."
        + (
            " Your wait on its merge ended early because its review stopped: once you have"
            " acted, register the merge wait again."
            if request.kind == KIND_PULL_REQUEST
            else ""
        )
    )


def fired_message(request: WakeRequest, outcome: Outcome) -> str:
    return (
        f"AgentDeck wake-on: {outcome.summary}. You asked to be woken for {request.target}."
        " Webhooks are a hint, so confirm it with one read before you act on it, then"
        " carry on with the work that was waiting for it."
    )


def deck_deploy_message(request: WakeRequest, revision: str, skills: str = "") -> str:
    installed = f", and bundled skills are installed at {skills[:12]}" if skills else ""
    return (
        f"AgentDeck wake-on: the web worker serving this deck now runs {revision[:12]}, which"
        f" includes commit {request.sha}{installed}. You asked to be woken for"
        f" {request.target}. Check the deployed behaviour live, then carry on with the work"
        " that was waiting for it."
    )


@dataclass(frozen=True)
class SeenDeploy:
    """One deployment status the deck stored for a repository (#3584)."""

    environment: str
    state: str
    sha: str
    time: str  # the status's own ``updated_at``, else when the deck received it

    def payload(self) -> dict:
        return asdict(self)


def _stream_path(streams_root: Path, repository: str) -> Path | None:
    """``<owner>/<repo>.jsonl`` under the streams, matched without case: the log files a
    delivery under GitHub's own spelling (``Example-Org``), a wait is lowercased."""
    owner, _, name = repository.partition("/")
    wanted = f"{name}.jsonl".lower()
    try:
        for owner_dir in streams_root.iterdir():
            if owner_dir.name.lower() != owner.lower() or not owner_dir.is_dir():
                continue
            for stream in owner_dir.iterdir():
                if stream.name.lower() == wanted and stream.is_file():
                    return stream
    except OSError:
        return None
    return None


def _seen_deploy(record: dict) -> SeenDeploy | None:
    try:
        payload = json.loads(raw_body(record))
    except (ValueError, KeyError, TypeError):
        return None
    status = payload.get("deployment_status") if isinstance(payload, dict) else None
    deployment = payload.get("deployment") if isinstance(payload, dict) else None
    if not isinstance(status, dict) or not isinstance(deployment, dict):
        return None
    return SeenDeploy(
        environment=_text(status.get("environment")) or _text(deployment.get("environment")),
        state=_text(status.get("state")),
        sha=_text(deployment.get("sha")).lower(),
        time=_text(status.get("updated_at"))
        or _text(status.get("created_at"))
        or _text(record.get("received_at")),
    )


def _when(seen: SeenDeploy) -> datetime:
    try:
        moment = datetime.fromisoformat(seen.time)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def seen_deployments(
    streams_root: Path | str,
    repository: str,
    *,
    sha: str = "",
    name: str = "",
    tail_bytes: int = DEPLOY_TAIL_BYTES,
) -> tuple[list[SeenDeploy], SeenDeploy | None]:
    """The newest deployment status per environment in the end of a repository's stream,
    newest first, and a ``success`` for ``sha`` (a prefix; filtered by ``name`` like a wait)
    when one is there (#3584).

    Only the last ``tail_bytes`` are read, so an old deploy or one in a rotated archive is
    not seen: an empty answer proves nothing, like the stream itself.
    """
    path = _stream_path(Path(streams_root).expanduser(), repository)
    if path is None:
        return [], None
    try:
        with path.open("rb") as handle:
            start = max(0, handle.seek(0, 2) - tail_bytes)
            handle.seek(start)
            lines = handle.read().split(b"\n")
    except OSError:
        return [], None
    if start:
        lines = lines[1:]  # cut mid-record
    newest: dict[str, SeenDeploy] = {}
    done: SeenDeploy | None = None
    for line in lines:
        if b"deployment_status" not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("event") != "deployment_status":
            continue
        seen = _seen_deploy(record)
        if seen is None:
            continue
        key = seen.environment.lower()
        if key not in newest or _when(seen) >= _when(newest[key]):
            newest[key] = seen
        if (
            sha
            and seen.state == "success"
            and seen.sha.startswith(sha.lower())
            and (not name or _name_matches(name, seen.environment))
        ):
            done = seen
    return sorted(newest.values(), key=_when, reverse=True), done


@dataclass(frozen=True)
class NeedsYouStop:
    """One Work in the deck's Needs you list, in what a needs-you wake tells (#3446)."""

    work_id: str
    category: str  # a ``work_attention`` category
    label: str  # its words: "Needs answer"
    title: str
    issue: str  # owner/repo#n
    issue_url: str
    page: str  # the Work page's path on this deck
    reason: str  # the question, or what the category waits on; "" when the label says it all
    stopped_at: float  # when it entered this category
    #: The Work's own chats, by durable identity: none is woken about its own Work.
    chats: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.work_id}@{self.stopped_at:.3f}:{self.category}"


def _wants(request: WakeRequest, stop: NeedsYouStop) -> bool:
    if stop.category not in NEEDS_YOU_CATEGORIES.values():
        return False
    if request.only and stop.category != NEEDS_YOU_CATEGORIES[request.only]:
        return False
    repository = stop.issue.rpartition("#")[0].lower()
    if request.repository and repository != request.repository:
        return False
    return request.chat_id not in stop.chats


def needs_you_message(request: WakeRequest, stop: NeedsYouStop) -> str:
    said = f'\n\nIt says: "{_quote(stop.reason)}"' if stop.reason else ""
    return (
        f"AgentDeck wake-on: {stop.label} — {stop.title} ({stop.issue}, {stop.issue_url}),"
        f" Work page {stop.page}.{said}{_how_to_answer(stop)}\n\nYou asked to be woken for"
        f" {request.target}. This wait stays registered and sends one message per Work that"
        " moves in; tell the person now, and DELETE the wait when you no longer want these."
    )


def _how_to_answer(stop: NeedsYouStop) -> str:
    """Where the person's answer goes (#3481): a message leaves a question box open."""
    if stop.category != NEEDS_YOU_CATEGORIES["answer"]:
        return ""
    path = stop.issue.replace("#", "/")
    return (
        f"\n\nWhen the person answers, read the question with GET"
        f" $AGENTDECK_URL/api/works/{path}/question. Where it says"
        ' `"answer_with": "question_box"`, POST their answer as JSON to its answer_url:'
        ' {"interaction_id": "<interaction.id>", "answers": {"<question id>": "<option'
        ' value or their own words>"}}; where it says "message", send it to that chat as a'
        " message. Never answer a question box by chat message or issue comment: the chat"
        " reads that, but the box stays open and the Work keeps showing Needs answer."
    )


def _git_contains(repo: Path, sha: str, revision: str) -> bool | None:
    """Is ``sha`` an ancestor of (or equal to) ``revision`` in ``repo``?

    ``None`` when git cannot say — the commit is not fetched yet, or git failed — which
    is "ask again later", unlike a definite no.
    """
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, revision],
            cwd=str(repo),
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode in (0, 1):
        return result.returncode == 0
    return None


#: The checkout this process runs from; ``app._revision`` reads its HEAD the same way.
_DECK_REPO = Path(__file__).resolve().parent.parent.parent


def review_timeout_message(request: WakeRequest, minutes: int, review_chats: str = "") -> str:
    """Send the worker to check the reviewer is moving, not just to wait again (#3389)."""
    where = (
        f"an issue titled `Review <owner/repo>#<your issue number>` in `{review_chats}`"
        if review_chats
        else "the review chat the Work page links"
    )
    pull = f"{request.repository}#{request.number}"
    return (
        f"AgentDeck wake-on: no verdict reached this chat for {request.target} within"
        f" {minutes} minutes. Check once that the reviewer is making progress:\n\n"
        f"1. Is the pull request's head on GitHub the commit you pushed (`gh pr view"
        f" {request.number} --repo {request.repository} --json headRefOid,isDraft`)? If not,"
        " push it, and mark the pull request ready if it is a draft.\n"
        f"2. Is a review chat open for that head? It is {where}, and its body names {pull}"
        " at the commit it reviews. If none is open, or it names an older commit, ask for a"
        " fresh review: `POST $AGENTDECK_URL/api/sessions/<session-key>/review-request` with"
        f' `{{"pull_request": "{pull}", "no_verdict": true, "note": "no review chat started'
        ' for this head"}`. Say in the note what you found, and do not ask the person first.'
        ' An answer with `"outcome": "already asked"` means that review is queued: end the'
        " turn on the wait it kept.\n"
        "3. Has the reviewer written anything there recently (a comment, or an `in-progress`"
        " label)? If so it is working: register this review wait again and end the turn.\n\n"
        "If the chat is open at your head but nothing moved in it since your last wait, or"
        " this is your second timeout on the same head with no review queued, or a fresh"
        " review is refused (409), stop waiting and tell the person in the question box what"
        " you found."
    )


def timeout_message(request: WakeRequest, review_chats: str = "") -> str:
    if request.stopped:
        return stopped_message(request)
    if request.kind == KIND_TIME:
        return (
            f"AgentDeck wake-on: it is time to check on {request.target}, as you asked."
            " Look at it once now, then carry on, or register another time wait if it is"
            " still running."
        )
    minutes = round((request.deadline - request.registered_at) / 60)
    if request.kind == KIND_NEEDS_YOU:
        return (
            f"AgentDeck wake-on: your wait for {request.target} ran its {minutes} minutes and"
            " has ended. Register it again if you still want these messages."
        )
    if request.kind == KIND_REVIEW:
        return review_timeout_message(request, minutes, review_chats)
    return (
        f"AgentDeck wake-on: nothing arrived for {request.target} within {minutes}"
        " minutes. A webhook can be dropped, so check its state once yourself, then carry"
        " on, or register the wait again if it is still running."
    )


def _received_at(delivery: Delivery) -> float | None:
    try:
        stamp = datetime.fromisoformat(delivery.received_at)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.timestamp()


class WakeOnRegistry:
    """The registrations, in one small JSON file the web workers share.

    Every write replaces the file atomically, so a crash leaves the old list or the new
    one, never half of either. Reads go through memory, but a hot reload runs two web
    workers side by side (#3070): each look checks the file is still the version this
    process last saw, re-reads it when the other worker wrote, and every change is one
    locked read-modify-write.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._file = SharedFile(self.path, prefix=".wake-on.", on_foreign_write=self.all)
        #: What ``answer_reviews`` stamps a merge wait with; the service sets its own clock.
        self.clock: Callable[[], float] = time.time
        self._requests: list[WakeRequest] = []
        #: Fields a newer deck wrote that this code does not know, by wait id, written back
        #: as they were (#3560). A deploy runs old and new code side by side for a minute or
        #: two, and the old one used to drop every wait carrying a new field as malformed.
        self._unknown: dict[str, dict] = {}
        #: When a verdict last reached a chat, by ``chat|owner/repo#n`` (``|*`` for a verdict
        #: on the whole Work), kept apart from the waits (#3611): the reviewer's closing line
        #: usually lands after its verdict, and a merge wait registered in between must not
        #: read that line as a stop either.
        self._answered: dict[str, float] = {}
        #: Called after every write, so whoever sleeps until the next deadline re-plans.
        self.on_change: Callable[[], None] | None = None

    def all(self) -> tuple[WakeRequest, ...]:
        if self._file.stale():
            before = self._requests
            self._requests = self._load()
            if before != self._requests and self.on_change is not None:
                # Another worker wrote: a wait it added must be planned here too.
                self.on_change()
        return tuple(self._requests)

    def for_chat(self, chat_id: str) -> tuple[WakeRequest, ...]:
        return tuple(item for item in self.all() if item.chat_id == chat_id)

    def add(self, request: WakeRequest) -> WakeRequest:
        """Store one wait; the same wait registered again only moves its deadline."""
        with self._file.locked():
            current = list(self.all())
            for index, held in enumerate(current):
                if held.same_wait(request):
                    renewed = replace(
                        held,
                        # Keep the first registration time: a matching delivery that
                        # landed between the two registrations, and is not tailed yet,
                        # still counts.
                        session_key=request.session_key,
                        deadline=request.deadline,
                        # Registered again after its review chat stopped: a fresh wait.
                        stopped="",
                        cut_from=0.0,
                        asked_at=max(held.asked_at, request.asked_at),
                    )
                    current[index] = renewed
                    self._save(current)
                    return renewed
            if len(self.for_chat(request.chat_id)) >= MAX_ACTIVE_PER_CHAT:
                raise WakeRequestError(
                    f"this chat already waits on {MAX_ACTIVE_PER_CHAT} events; cancel one first"
                )
            current.append(request)
            self._save(current)
            return request

    def update(self, request: WakeRequest) -> None:
        with self._file.locked():
            current = list(self.all())
            for index, held in enumerate(current):
                if held.id == request.id:
                    current[index] = request
                    self._save(current)
                    return

    def answer_reviews(self, chat_id: str, pulls: Iterable[str] | None = None) -> int:
        """End this chat's review waits without a message: the deck just sent it a verdict.

        ``pulls`` are the ``owner/repo#n`` the verdict is about, matched case-blind, and only
        their waits end. ``None`` is a verdict about the whole Work: posted on its issue and
        naming none of its pull requests, it answers the Work's one review chat, which covers
        every pull request the Work holds, so every review wait of the chat ends.

        The verdict is recorded (``answered``, #3611): the reviewer's closing line usually
        lands after it, and must not then cut short a merge wait held now or registered later.
        A merge wait a stop already brought forward (``cut_from``) gets its own deadline back:
        the verdict told the chat what the stop would have.
        """
        wanted = None if pulls is None else {str(pull).casefold() for pull in pulls}

        def about(item: WakeRequest) -> bool:
            return wanted is None or f"{item.repository}#{item.number}".casefold() in wanted

        with self._file.locked():
            current = list(self.all())
            now = self.clock()
            for pull in ["*"] if wanted is None else sorted(wanted):
                self._answered[f"{chat_id}|{pull}"] = now
            for index, held in enumerate(current):
                if (
                    held.kind == KIND_PULL_REQUEST
                    and held.cut_from
                    and held.chat_id == chat_id
                    and not held.pending_text
                    and about(held)
                ):
                    current[index] = replace(held, deadline=held.cut_from, stopped="", cut_from=0.0)
            self._save(current)
        return self.remove(
            item.id for item in self.for_chat(chat_id) if item.kind == KIND_REVIEW and about(item)
        )

    def tell(self, request_id: str, told: tuple[str, ...]) -> None:
        """Record a needs-you wait's told stops on the wait as it is held now, so a renewal
        written in between keeps its deadline."""
        with self._file.locked():
            current = list(self.all())
            for index, held in enumerate(current):
                if held.id == request_id and tuple(held.told) != told:
                    current[index] = replace(held, told=told)
                    self._save(current)
                    return

    def answered(self, chat_id: str, pull: str) -> float:
        """When a verdict on ``pull`` (or on the chat's whole Work) last reached ``chat_id``;
        0 when none is recorded."""
        self.all()  # reload when another process wrote
        return max(
            self._answered.get(f"{chat_id}|{pull.casefold()}", 0.0),
            self._answered.get(f"{chat_id}|*", 0.0),
        )

    def remove(self, ids: Iterable[str]) -> int:
        doomed = set(ids)
        with self._file.locked():
            current = list(self.all())
            kept = [item for item in current if item.id not in doomed]
            if len(kept) != len(current):
                self._save(kept)
            return len(current) - len(kept)

    def _load(self) -> list[WakeRequest]:
        try:
            raw = self._file.read()
        except (OSError, ValueError):
            log.warning("wake-on: ignoring unreadable registry at %s", self.path)
            return []
        items = raw.get("requests") if isinstance(raw, dict) else None
        loaded: list[WakeRequest] = []
        unknown: dict[str, dict] = {}
        for item in items if isinstance(items, list) else []:
            try:
                known = {key: value for key, value in item.items() if key in _FIELDS}
                request = WakeRequest(**known)
                loaded.append(replace(request, told=tuple(request.told)))
            except (TypeError, AttributeError):
                log.warning("wake-on: skipping malformed registration %r", item)
                continue
            if len(known) < len(item):
                unknown[request.id] = {k: v for k, v in item.items() if k not in _FIELDS}
        self._unknown = unknown
        answered = raw.get("answered") if isinstance(raw, dict) else None
        self._answered = {
            str(key): float(value)
            for key, value in (answered.items() if isinstance(answered, dict) else ())
            if isinstance(value, (int, float))
        }
        return loaded

    def _save(self, requests: list[WakeRequest]) -> None:
        # A verdict older than the longest wait can no longer meet a stop worth skipping.
        oldest = self.clock() - ANSWERED_KEEP_S
        self._answered = {key: at for key, at in self._answered.items() if at >= oldest}
        self._file.write(
            {
                "requests": [
                    {**self._unknown.get(item.id, {}), **asdict(item)} for item in requests
                ],
                "answered": self._answered,
            },
            indent=1,
        )
        self._requests = requests
        if self.on_change is not None:
            self.on_change()


class WakeOnService:
    """Tail the streams and send each registered chat its one wake message.

    ``schedule`` is ``ScheduledMessageService.schedule``: a message due now rides the same
    durable delivery as a message held while the runtime restarts, including its stable
    delivery id.
    ``message_exists`` says whether a message with a given id is already stored, which is
    how a refused ``schedule`` tells "already saved" from "not saved".
    """

    def __init__(
        self,
        streams_root: Path | str,
        *,
        schedule: Callable[[ScheduledMessage], ScheduledMessage | None],
        message_exists: Callable[[str], bool],
        enabled: bool = True,
        registry: WakeOnRegistry | None = None,
        clock: Callable[[], float] = time.time,
        revision: Callable[[], str | None] = lambda: None,
        contains: Callable[[str, str], bool | None] | None = None,
        watcher: WebhookWatcher | None = None,
        pull_requests: Callable[[str], Iterable[PullRequestRef]] = lambda session_key: (),
        review_chats: str = "",
        present: Callable[[WakeRequest], bool] = lambda request: True,
        skills_sha: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._enabled = bool(enabled)
        self._streams_root = Path(streams_root).expanduser()
        # Whether the owner is in a needs-you wait's chat (#3464): only then is it woken,
        # by a Work or by its own timeout.
        self._present = present
        self._tailer = WebhookTailer(streams_root, consumer=CONSUMER)
        # The web process's one shared watch (#2865); None opens a private one.
        self._watcher = watcher
        # The pull requests a chat registered, by session key: only their old heads are
        # dropped from that chat's commit waits (#3271).
        self._pull_requests = pull_requests
        # The repository review chats live in (``chat_board.repository``), whose stops cut
        # review waits short (#3316), and each chat's newest comment there, for a label that
        # follows it.
        self._review_chats = review_chats
        self._noted: dict[int, str] = {}
        self.registry = registry or WakeOnRegistry(registry_path(streams_root))
        self.registry.clock = clock
        self._schedule = schedule
        self._message_exists = message_exists
        self._clock = clock
        # This process's own revision never changes, so whether it includes a commit is
        # asked once per commit; a deploy brings a new process with an empty cache.
        self._revision = revision
        self._contains = contains or (lambda sha, rev: _git_contains(_DECK_REPO, sha, rev))
        self._included: dict[str, bool] = {}
        self._unsure_at: dict[str, float] = {}
        # Whether this worker accepts connections (#3584). A replacement web worker runs its
        # startup, this service included, before the supervisor hands it the listener, so
        # the supervised worker clears this until then; any other process serves at once.
        self._accepting = True
        # The commit the deploy last installed bundled skills at (its ``skills_sha``
        # marker); None where no deploy records one. A deck-deploy wait waits for it too.
        self._skills_sha = skills_sha
        self._skills_marker: str | None = None
        self._skills_answers: dict[str, bool] = {}
        self._skills_wait: dict[str, float] = {}
        self._retry_at: dict[str, float] = {}
        # A needs-you pass whose save failed (#3446 review): its list, retried at the time
        # beside it, so a lone question is not left to the next change of any Work.
        self._needs_you_retry: tuple[list[NeedsYouStop], bool, float] | None = None
        self._task: asyncio.Task | None = None
        self._deadline_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Set by every registry write, so the deadline task re-plans when a wait is added,
        # renewed, answered or cancelled — the cancel route writes the registry directly.
        self._changed = asyncio.Event()
        self.registry.on_change = self._changed.set

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_accepting(self, accepting: bool) -> None:
        """Whether this worker serves the deck now; a deck-deploy wait waits for it (#3584).

        Turning it on re-plans the deadline task at once, so a wait the worker's new code
        satisfies is woken as soon as the worker serves rather than on the next retry.
        """
        self._accepting = bool(accepting)
        if self._accepting:
            self._changed.set()

    def seen_deployments(
        self, repository: str, *, sha: str = "", name: str = ""
    ) -> tuple[list[SeenDeploy], SeenDeploy | None]:
        """``seen_deployments`` over this service's streams."""
        return seen_deployments(self._streams_root, repository, sha=sha, name=name)

    def register(
        self, session_key: str, chat_id: str, *, timeout_s: float, asked: bool = False, **fields
    ) -> WakeRequest:
        """Store one wait; ``asked`` is a review wait the chat asked for the review of now."""
        now = self._clock()
        request = WakeRequest(
            id=secrets.token_hex(6),
            session_key=session_key,
            chat_id=chat_id,
            registered_at=now,
            deadline=now + timeout_s,
            asked_at=now if asked else 0.0,
            **fields,
        )
        return self.registry.add(request)

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="wake-on")
        self._deadline_task = asyncio.create_task(self._deadlines(), name="wake-on-deadlines")

    async def stop(self) -> None:
        tasks = [task for task in (self._task, self._deadline_task) if task is not None]
        self._task = self._deadline_task = None
        if not tasks:
            return
        self._stop.set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def next_due(self) -> float | None:
        """When the next time-derived thing falls due, on ``clock``; None when nothing will.

        Three things are owed by time rather than by a delivery: a wait's own timeout, a
        message whose store write failed (retried ``PENDING_RETRY_S`` after the failure),
        a deck-deploy check git could not answer (asked again ``UNSURE_RETRY_S`` on), and
        one whose bundled skills are behind (``SKILLS_RETRY_S`` on).
        A deck-deploy wait git answered "no" for is not due again in this process: its
        revision never changes, so only the timeout or a deploy's new process can end it.
        """
        due: list[float] = []
        now = self._clock()
        for request in self.registry.all():
            if request.pending_text:
                due.append(self._retry_at.get(request.id, now))
                continue
            due.append(request.deadline)
            if request.kind == KIND_DECK_DEPLOY and request.sha in self._unsure_at:
                due.append(self._unsure_at[request.sha] + UNSURE_RETRY_S)
            if request.kind == KIND_DECK_DEPLOY and request.sha in self._skills_wait:
                due.append(self._skills_wait[request.sha] + SKILLS_RETRY_S)
        if self._needs_you_retry is not None:
            due.append(self._needs_you_retry[2])
        return min(due) if due else None

    async def _deadlines(self) -> None:
        """Run `expire_due` when something is due or the registrations change (#2865).

        Before #2865 it rode the tailer's five-second re-read; now it sleeps exactly until
        `next_due`, and a registration added, renewed or removed re-plans at once.
        """
        backoff = watch_retry.Backoff()
        while not self._stop.is_set():
            self._changed.clear()
            retry_s: float | None = None
            try:
                self.expire_due()
                due = self.next_due()
                backoff.succeeded()
            except Exception:  # noqa: BLE001 - one bad wait must not stop the others' clocks
                log.exception("wake-on deadline pass failed")
                # What failed is still due, so ``next_due`` stays in the past: wait out a
                # back-off rather than spinning on the event loop (#2865).
                retry_s = backoff.failed()
                try:
                    due = self.next_due()
                except Exception:  # noqa: BLE001 - the back-off alone then
                    due = None
            delay = None if due is None else max(0.0, due - self._clock())
            if retry_s is not None:
                delay = retry_s if delay is None else max(delay, retry_s)
            if delay is None:
                await self._changed.wait()
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout=delay)

    def skip_history_if_nothing_waits(self) -> bool:
        """With no offset yet, skip the streams' history — but only when no wait is held.

        A first start would otherwise read every stream from byte zero, and with nothing
        registered there is nothing in that history to match. When waits are held (the
        offset file was lost on its own), replay instead: the received-at filter keeps
        older deliveries from firing, and one that landed while the deck was down still
        wakes its chat.
        """
        if self._tailer.state_path.exists() or self.registry.all():
            return False
        for _ in self._tailer.deliveries():
            pass
        self._tailer.commit()
        return True

    async def _run(self) -> None:
        self.skip_history_if_nothing_waits()
        while True:
            try:
                async for batch in self._tailer.watch(stop_event=self._stop, watcher=self._watcher):
                    self.apply(batch)
                    self._tailer.commit()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("wake-on pass failed; retrying")
                await asyncio.sleep(5)

    def apply(self, batch: Iterable[Delivery]) -> None:
        """Fire every registration a delivery in this batch matches, each once."""
        for delivery in batch:
            waiting = self.registry.all()
            if not waiting:
                continue
            replaced = replaced_heads(delivery)
            if replaced is not None:
                waiting = self._drop_replaced(waiting, *replaced)
            received = _received_at(delivery)
            stop = review_stop(delivery, self._review_chats, self._noted)
            if stop is not None:
                waiting = self._cut_short(waiting, stop, received)
            for request in waiting:
                if request.pending_text:
                    continue  # already owes its message; expire_due retries it
                if received is not None and received < request.registered_at:
                    continue
                outcome = match(request, delivery)
                if outcome is not None:
                    self._fire(request, fired_message(request, outcome))
        self.expire_due()

    def _cut_short(
        self, waiting: tuple[WakeRequest, ...], stop: ReviewStop, received: float | None
    ) -> tuple[WakeRequest, ...]:
        """Bring the review waits on ``stop``'s pull requests due ``STOP_SETTLE_S`` from it.

        Merge waits too (#3611): an agent waiting for its pull request to merge is waiting on
        this review as well. #3529's agent waited only for the merge, so a reviewer's "the same
        finding persists" reached nobody for two hours. Such a wait keeps the deadline it had
        (``cut_from``), which a verdict told in the meantime gives back.

        Only waits registered before the stop was received: one registered after it was
        made by an agent that already knows, and not one whose own review request came after
        the chat opened (#3453). A verdict told in the meantime still ends the wait without a
        message (``answer_reviews``).
        """
        at = received if received is not None else self._clock()
        changed = []
        for request in waiting:
            if (
                request.kind not in (KIND_REVIEW, KIND_PULL_REQUEST)
                or request.pending_text
                or at < request.registered_at
                or f"{request.repository}#{request.number}" not in stop.pulls
            ):
                continue
            if (
                request.asked_at
                and stop.opened_at is not None
                and stop.opened_at < request.asked_at - ASK_SLACK_S
            ):
                # An older chat closing on its own summary: the review this chat asked for
                # is still to start, and it is what the wait is for (#3453).
                continue
            answered = self.registry.answered(
                request.chat_id, f"{request.repository}#{request.number}"
            )
            if answered and (stop.opened_at is None or stop.opened_at < answered):
                # This review chat's verdict already reached the chat, before or after the
                # wait was registered (#3611): its closing line is no news. A review chat
                # opened after that verdict still cuts.
                continue
            cut = replace(
                request,
                deadline=min(request.deadline, at + STOP_SETTLE_S),
                stopped=stop.note,
                cut_from=(
                    request.cut_from or request.deadline
                    if request.kind == KIND_PULL_REQUEST
                    else 0.0
                ),
            )
            if cut != request:
                log.info(
                    "wake-on: review chat #%d stopped; %s's wait on %s is due by %s",
                    stop.chat,
                    request.session_key,
                    request.target,
                    datetime.fromtimestamp(cut.deadline, UTC).isoformat(timespec="seconds"),
                )
                self.registry.update(cut)
                changed.append(cut)
        if not changed:
            return waiting
        by_id = {request.id: request for request in changed}
        return tuple(by_id.get(request.id, request) for request in waiting)

    def _drop_replaced(
        self,
        waiting: tuple[WakeRequest, ...],
        pull: tuple[str, int],
        heads: tuple[str, ...],
        reason: str,
    ) -> tuple[WakeRequest, ...]:
        """Forget the commit waits on ``heads`` of the chats that registered ``pull``.

        No received-at check: a head that was replaced stays replaced, so a wait registered
        on it after the push is just as stale. A wait that already owes its message keeps it.
        """
        repository, number = pull
        stale = [
            request
            for request in waiting
            if request.kind == KIND_COMMIT
            and not request.pending_text
            and request.repository == repository
            and any(head.startswith(request.sha) for head in heads)
            and any(ref.identity == pull for ref in self._pull_requests(request.session_key))
        ]
        if not stale:
            return waiting
        for request in stale:
            log.info(
                "wake-on: dropping %s's wait on %s: pull request %s#%d: %s",
                request.session_key,
                request.target,
                repository,
                number,
                reason,
            )
        self.registry.remove(request.id for request in stale)
        doomed = {request.id for request in stale}
        return tuple(request for request in waiting if request.id not in doomed)

    def expire_due(self) -> None:
        """Retry every owed message, wake each deck-deploy wait this process satisfies,
        then tell each chat whose wait timed out, once."""
        now = self._clock()
        if self._needs_you_retry is not None and self._needs_you_retry[2] <= now:
            stops, complete, _ = self._needs_you_retry
            self.needs_you(stops, complete=complete)
        for request in self.registry.all():
            if request.pending_text:
                self._fire(request, request.pending_text)
            elif request.kind == KIND_DECK_DEPLOY and self._deck_includes(request.sha):
                self._fire(
                    request,
                    deck_deploy_message(request, self._revision() or "", self._skills_marker or ""),
                )
            elif request.deadline <= now:
                if request.kind == KIND_NEEDS_YOU and not self._present(request):
                    # Its week is up with nobody in the chat: end it without a word (#3464).
                    log.info("wake-on: %s is away; ended %s", request.session_key, request.id)
                    self.registry.remove([request.id])
                    continue
                self._fire(request, timeout_message(request, self._review_chats))

    def needs_you_waited(self) -> bool:
        """Whether any chat waits on Works needing the owner: the overview then rebuilds on
        every change, as for a page that shows it."""
        return any(request.kind == KIND_NEEDS_YOU for request in self.registry.all())

    def needs_you(self, stops: Iterable[NeedsYouStop], *, complete: bool = True) -> int:
        """Tell each needs-you wait about every wanted Work that moved in since it was
        registered and was not told yet; how many messages were saved.

        A wait whose chat the owner is not in (``present``) marks those stops told and sends
        nothing (#3464), so they are not saved up for when the owner comes back.

        One message per Work and stop, under a delivery id made from the wait and the stop,
        so a replay the store already holds is not written twice. A wait keeps only the stops
        still in the list, which bounds it; ``complete`` False (a degraded pass that may have
        dropped Works) forgets none, so one coming back is not news.
        """
        waits = [request for request in self.registry.all() if request.kind == KIND_NEEDS_YOU]
        unsent = self._needs_you_retry[0] if self._needs_you_retry is not None else []
        self._needs_you_retry = None
        if not waits:
            return 0
        stops = list(stops)
        current = {stop.key for stop in stops}
        if not complete:
            # A degraded pass may have dropped a Work whose message is still unsent: keep
            # trying it (#3446 review). Only a complete pass says it left Needs you.
            stops += [stop for stop in unsent if stop.key not in current]
        saved = 0
        failed = False
        for request in waits:
            told = set(request.told)
            here: bool | None = None  # asked once, and only when a stop is owed
            for stop in stops:
                if (
                    stop.key in told
                    or stop.stopped_at < request.registered_at
                    or not _wants(request, stop)
                ):
                    continue
                if here is None:
                    here = self._present(request)
                if not here:
                    log.info(
                        "wake-on: %s is away; not waking it for %s",
                        request.session_key,
                        stop.issue,
                    )
                    told.add(stop.key)
                    continue
                digest = hashlib.sha256(stop.key.encode()).hexdigest()[:12]
                message_id = f"wake-on:{request.id}:{digest}"
                now = datetime.now(UTC)
                scheduled = self._schedule(
                    ScheduledMessage(
                        id=message_id,
                        chat_id=request.chat_id,
                        text=needs_you_message(request, stop),
                        due_at=now,
                        created_at=now,
                    )
                )
                if scheduled is None and not self._message_exists(message_id):
                    log.warning("wake-on: could not save %s's wake for %s", request.id, stop.key)
                    failed = True  # untold: retried below, or by the next overview pass
                    continue
                log.info(
                    "wake-on: waking %s: %s is %s", request.session_key, stop.issue, stop.label
                )
                told.add(stop.key)
                saved += 1
            kept = tuple(sorted(told & current if complete else told))
            if kept != tuple(request.told):
                self.registry.tell(request.id, kept)
        if failed:
            # The overview passes again only when some Work changes, so the deadline task
            # retries this list itself; a restart's first pass covers a lost retry.
            self._needs_you_retry = (stops, complete, self._clock() + PENDING_RETRY_S)
            self._changed.set()
        return saved

    def _deck_includes(self, sha: str) -> bool:
        """Whether the deck now serves code that includes ``sha``, its skills installed.

        Three things must hold (#3584): this process's revision includes it, this process
        is the worker accepting connections (``set_accepting``), and the deploy's bundled
        skills marker includes it. Only the first is kept as an answer; the other two move
        forward on their own, so their "not yet" is asked again.
        """
        if not self._revision_includes(sha) or not self._accepting:
            return False
        return self._skills_include(sha)

    def _skills_include(self, sha: str) -> bool:
        """Whether the deploy installed bundled skills at a commit including ``sha``.

        No marker (no deploy timer, as in development) does not hold a wait. A marker
        behind it, or a git that cannot say, is read again ``SKILLS_RETRY_S`` on; answers
        are kept only while the marker stays the same.
        """
        marker = self._skills_sha()
        if marker != self._skills_marker:
            self._skills_marker = marker
            self._skills_answers = {}
        if not marker:
            self._skills_wait.pop(sha, None)
            return True
        answer = self._skills_answers.get(sha)
        if answer is None:
            answer = self._contains(sha, marker)
            if answer is not None:
                self._skills_answers[sha] = answer
        if answer:
            self._skills_wait.pop(sha, None)
            return True
        self._skills_wait[sha] = self._clock()
        return False

    def _revision_includes(self, sha: str) -> bool:
        """Whether this process runs code that includes ``sha``.

        A definite answer is kept, since this process's revision never changes. An
        unknown revision or a git that cannot say answers "not yet" without being kept,
        and git is asked again at most once a minute.
        """
        if sha in self._included:
            return self._included[sha]
        revision = self._revision()
        if not revision:
            return False
        now = self._clock()
        if now - self._unsure_at.get(sha, float("-inf")) < UNSURE_RETRY_S:
            return False
        answer = self._contains(sha, revision)
        if answer is None:
            self._unsure_at[sha] = now
            return False
        self._unsure_at.pop(sha, None)
        self._included[sha] = answer
        return answer

    def _fire(self, request: WakeRequest, text: str) -> None:
        """Save the one message this wait owes, and forget the wait only once it is saved."""
        now = datetime.now(UTC)
        message_id = f"wake-on:{request.id}"
        scheduled = self._schedule(
            ScheduledMessage(
                id=message_id,
                chat_id=request.chat_id,
                text=text,
                due_at=now,
                created_at=now,
            )
        )
        if scheduled is None and not self._message_exists(message_id):
            # The store refused and holds nothing under this id: the write failed. Keep
            # the wait with the text it owes, and try again at its retry deadline.
            log.warning("wake-on: could not save the wake for %s; will retry", request.id)
            self._retry_at[request.id] = self._clock() + PENDING_RETRY_S
            if request.pending_text != text:
                self.registry.update(replace(request, pending_text=text))
            return
        # Saved now, or saved before under this id (a replay that met another matching
        # delivery first): either way the one message is stored, so never write a second.
        log.info("wake-on: waking %s for %s", request.session_key, request.target)
        self._retry_at.pop(request.id, None)
        self.registry.remove([request.id])
