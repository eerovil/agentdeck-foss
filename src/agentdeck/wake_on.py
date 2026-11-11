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
work such as a render reports nothing to GitHub, so its deadline is the wake. It may fall up
to ``MAX_TIME_WAIT_MINUTES`` (14 days, #4266) ahead, so a chat can come back for a check a
week after its change shipped; its Work stays current (``waiting_work_ids`` in
``web/work_view``) and reads "Waiting on …" until then. It may carry a ``check`` (#4256): a
shell command the deck runs every so often until the deadline. Exit 0
says the awaited thing happened and wakes the chat with the output; ``CHECK_NOT_YET`` says
not yet and stays silent; any other exit, a run past ``CHECK_TIMEOUT_S`` or a command that
will not start wakes the chat with the reason. Most day-later live checks woke a chat only
to read "nothing yet" (#4244), so the deck reads it instead. The deck never records a live
check or closes an issue from a check: a woken chat reads the result and does that.

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
import os
import re
import secrets
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import chat_board, chat_review, deck_notices, review_refs, watch_retry
from .github_webhook_log import raw_body
from .models import IssueRef, PullRequestRef
from .review_history import deck_check_delivery
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
#: The waits on a GitHub event whose timeout is given one more round (#4080): most timeouts
#: woke a chat only to read that the build, merge or deploy was still running.
PATIENT_KINDS = frozenset({KIND_PULL_REQUEST, KIND_COMMIT, KIND_DEPLOYMENT})
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
#: How far ahead a time wait may fall (#4266). It was ``MAX_TIMEOUT_MINUTES`` (#2968), so a
#: chat asked to check back a week after a change shipped could only re-register a day at a
#: time, each wake re-reading the whole chat for nothing. An event wait keeps the day: its
#: timeout only covers a dropped webhook.
MAX_TIME_WAIT_MINUTES = 14 * 24 * 60
#: The exit code a time wait's check gives for "not yet": run again later, wake nobody.
#: Distinct from 1 and 2, which most commands give for an error a chat must hear of.
CHECK_NOT_YET = 3
#: How often a check runs, in minutes: the default, and the bounds a chat may ask for.
CHECK_EVERY_DEFAULT = 60
CHECK_EVERY_MIN = 5
CHECK_EVERY_MAX = 24 * 60
#: How long one run of a check may take before it is stopped and the chat woken.
CHECK_TIMEOUT_S = 300.0
#: The longest check command a time wait takes.
MAX_CHECK_COMMAND = 2000
#: How much of a check's output a wake quotes, from its end.
MAX_CHECK_OUTPUT = 3000
#: How much of a "not yet" run's output is kept for the deadline's message.
MAX_CHECK_LAST = 600
#: Checks running at once in this process: each is a read, but a burst must not swamp the host.
MAX_CHECKS_AT_ONCE = 4
#: Per chat. A chat waiting on more than this at once is more likely looping than waiting.
MAX_ACTIVE_PER_CHAT = 10
#: The longest ``waiting_for`` a time wait takes: it is shown in the Work's status line.
MAX_NOTE = 80
#: A merge or CI wait whose extra round passed with nothing in the stream is read from GitHub
#: once (#4253); still running, it waits this much longer, up to ``LIVE_READ_CAP_S`` from its
#: registration, and wakes its chat only with a result or at that cap.
LIVE_READ_AGAIN_S = 30 * 60.0
LIVE_READ_CAP_S = 24 * 3600.0
#: The waits that read: a deploy has no cheap read, so it keeps the timeout message.
LIVE_READ_KINDS = frozenset({KIND_PULL_REQUEST, KIND_COMMIT})
#: A merge wait ends with no message when the release's own merge notice reached the same chat
#: this recently (#4253): the chat already heard of the merge.
MERGE_TOLD_S = 30 * 60.0
#: The release's merge notice: ``work-release:<work>:live-check:<owner/repo#n,...>``.
RELEASE_NOTICE_PREFIX = "work-release:"
LIVE_CHECK_KEY = ":live-check:"
#: Outcomes the chat hears as a routine notice (``deck_notices.routine``); failures never are.
SUCCESS_STATES = frozenset({"success", "merged"})
#: The words of a review stop that closed on a comment (``review_stop``): the stop the deck
#: answers by asking for a fresh review itself (#4253); a label stop still wakes the chat.
CLOSED_REVIEW = "closed its review"

#: A commit status or deployment status in one of these states is a result; anything
#: else (``pending``, ``queued``, ``in_progress``) is a run still going.
_TERMINAL_STATUS = frozenset({"success", "failure", "error"})
#: The deliveries that report CI on a commit, finished or not.
_CI_EVENTS = {"check_suite", "check_run", "workflow_run", "status"}
#: Check conclusions that say nothing about the commit: a job skipped by its ``if:``, such as a
#: deploy that runs only on ``main``. An unnamed commit wait woke on one and its agent had to
#: read the checks by hand and wait again (#3910); a wait naming the check still takes it.
_NO_RESULT = frozenset({"skipped", "neutral"})

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
    #: The one more round a patient wait still has (``PATIENT_KINDS``), in seconds; 0 once it
    #: is spent, on any other wait, and on a wait stored before #4080.
    extra_s: float = 0.0
    #: What the chat is to do once this wait ends, when the deck registered it on the chat's
    #: behalf (#4253): the live check after a merge, carried by the deploy wait the deck
    #: registered instead of asking the chat to. "" on a wait the chat registered itself.
    then: str = ""
    #: The Work this wait is for (#4272), a ``github_issue:`` Work id; "" when the chat did
    #: not name one. A chat directly in several Works names it, or only the one Work its
    #: wait points at stays current (``waiting_work_ids`` in ``web/work_view``).
    work_id: str = ""
    #: A time wait's check (#4256): the shell command, where it runs, how often, when it next
    #: runs, how many runs said "not yet", and the newest of those (its time and output tail).
    check_command: str = ""
    check_cwd: str = ""
    check_every_s: float = 0.0
    check_at: float = 0.0
    check_runs: int = 0
    check_last: str = ""
    #: Which registration of this wait the check belongs to: registering it again bumps it,
    #: so a run started before that is ignored when it ends (it ran the old command).
    check_gen: int = 0

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
            self.work_id,
        ) == (
            other.chat_id,
            other.kind,
            other.repository,
            other.number,
            other.sha,
            other.name.lower(),
            other.note.lower(),
            other.only,
            other.work_id,
        )

    def payload(self) -> dict:
        check = (
            {
                "command": self.check_command,
                "cwd": self.check_cwd,
                "every_minutes": round(self.check_every_s / 60),
                "next_run": self.check_at,
                "not_yet_runs": self.check_runs,
            }
            if self.check_command
            else None
        )
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
            "work_id": self.work_id or None,
            **({"check": check} if check else {}),
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
    work_id: str | None = None,
    check: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Validate one registration body into the fields of a ``WakeRequest``.

    ``now`` is when a time wait counts from, an aware datetime whose zone a bare ``11:30``
    is read in; this host's clock and zone by default. ``work_id`` names the Work the wait
    is for (#4272), as a Work id or ``owner/repo#n``.
    """
    fields = _request_fields(
        pull_request=pull_request,
        review=review,
        commit=commit,
        deployment=deployment,
        deck_deploy=deck_deploy,
        at=at,
        after_minutes=after_minutes,
        needs_you=needs_you,
        only=only,
        waiting_for=waiting_for,
        name=name,
        timeout_minutes=timeout_minutes,
        check=check,
        now=now,
    )
    wanted = (work_id or "").strip()
    if not wanted:
        return fields
    if fields["kind"] == KIND_NEEDS_YOU:
        raise WakeRequestError("a needs_you wait listens to every Work; it takes no work_id")
    return {**fields, "work_id": _work_id(wanted)}


def _work_id(value: str) -> str:
    """One spelling of the Work ``value`` names: a Work id or ``owner/repo#n``."""
    from .work import WorkRef

    ref = WorkRef.from_work_id(value)
    if ref is None and (issue := IssueRef.parse(value)) is not None:
        ref = WorkRef.from_issue(issue)
    if ref is None:
        raise WakeRequestError(f"work_id must be a Work id or owner/repo#number; got {value!r}")
    return ref.work_id


def _request_fields(
    *,
    pull_request: str | None,
    review: str | None,
    commit: str | None,
    deployment: str | None,
    deck_deploy: str | None,
    at: str | None,
    after_minutes: int | None,
    needs_you: str | None,
    only: str | None,
    waiting_for: str | None,
    name: str | None,
    timeout_minutes: int | None,
    check: dict | None,
    now: datetime | None,
) -> dict:
    """``parse_request`` without the Work the wait is for."""
    at = at or None
    given = [
        value
        for value in (
            pull_request,
            review,
            commit,
            deployment,
            deck_deploy,
            at,
            after_minutes,
            needs_you,
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
        fields = _time_request(at, after_minutes, note, name, timeout_minutes, now)
        if check is not None:
            fields.update(_check_fields(check, fields["timeout_s"]))
        return fields
    if check is not None:
        raise WakeRequestError("check belongs to a time wait (at or after_minutes)")
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
    if not 1 <= minutes <= MAX_TIME_WAIT_MINUTES:
        raise WakeRequestError(
            "a time wait must fall between 1 minute and"
            f" {MAX_TIME_WAIT_MINUTES // (24 * 60)} days from now"
        )
    return {
        "timeout_s": minutes * 60.0,
        "name": "",
        "kind": KIND_TIME,
        "repository": "",
        "note": note,
    }


def _check_fields(check: object, wait_s: float) -> dict:
    """A time wait's ``check`` (#4256): ``command``, and optional ``every_minutes`` and ``cwd``."""
    if not isinstance(check, dict):
        raise WakeRequestError("check must be an object with a command")
    unknown = set(check) - {"command", "every_minutes", "cwd"}
    if unknown:
        raise WakeRequestError(f"check takes command, every_minutes and cwd; got {sorted(unknown)}")
    command = check.get("command")
    if not isinstance(command, str) or not command.strip():
        raise WakeRequestError("check.command must be a shell command")
    if len(command) > MAX_CHECK_COMMAND:
        raise WakeRequestError(f"check.command is at most {MAX_CHECK_COMMAND} characters")
    every = check.get("every_minutes")
    every = CHECK_EVERY_DEFAULT if every is None else every
    if isinstance(every, bool) or not isinstance(every, int):
        raise WakeRequestError("check.every_minutes must be a whole number of minutes")
    if not CHECK_EVERY_MIN <= every <= CHECK_EVERY_MAX:
        raise WakeRequestError(
            f"check.every_minutes must be between {CHECK_EVERY_MIN} and {CHECK_EVERY_MAX}"
        )
    if every * 60.0 >= wait_s:
        raise WakeRequestError(
            "check.every_minutes must be shorter than the wait, or the check would never run"
        )
    cwd = check.get("cwd")
    if cwd is None:
        cwd = ""
    if not isinstance(cwd, str):
        raise WakeRequestError("check.cwd must be an absolute directory")
    if cwd and not (Path(cwd).is_absolute() and Path(cwd).is_dir()):
        raise WakeRequestError(f"check.cwd must be an existing absolute directory; got {cwd!r}")
    return {
        "check_command": command.strip(),
        "check_cwd": cwd,
        "check_every_s": every * 60.0,
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


#: The webhook events that are only ever CI. A commit status may be a deploy (Vercel reports
#: one so) and a deployment status always is, so their result always reaches the chat.
CI_EVENTS = frozenset({"check_suite", "check_run", "workflow_run"})


@dataclass(frozen=True)
class Outcome:
    """What a matching delivery said, in words for the woken chat."""

    summary: str
    #: The result in one lowercased word: a check's conclusion, a deploy's state, or
    #: ``merged`` / ``closed`` for a pull request; "" when unknown.
    state: str = ""
    #: Whether it is surely CI: a check run, check suite or workflow run. A deploy reports as
    #: a deployment status or, like Vercel, as a commit status, so neither is CI here; only
    #: surely-CI green on a ready head is the deck's own to settle (#4253, ``CI_EVENTS``).
    ci: bool = False


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
    if request.kind == KIND_COMMIT and not request.name and conclusion.lower() in _NO_RESULT:
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
    return Outcome(summary, conclusion.lower(), ci=delivery.event in CI_EVENTS)


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
        return Outcome(f"pull request {where} was merged{commit}", "merged")
    return Outcome(f"pull request {where} was closed without merging", "closed")


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _result(event: str, payload: dict) -> tuple[str, str, str, str] | None:
    """``(sha, name, conclusion, url)`` for a finished CI or deploy result, else ``None``."""
    if event in {"check_suite", "check_run", "workflow_run"}:
        if payload.get("action") != "completed" or deck_check_delivery(event, payload):
            # The deck's own review check is no CI result to wake on (#4152).
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
    #: The chat was closed (#4253), which says nothing new to a wait an earlier stop of the
    #: same chat already cut: its closing comment is the better note.
    closed: bool = False


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
        note = f"the review chat {url} {CLOSED_REVIEW}, saying:\n\n{_quote(body)}"
    elif delivery.event == "issues" and action == "labeled":
        label = payload.get("label")
        name = _text(label.get("name")).strip().lower() if isinstance(label, dict) else ""
        if name not in STOP_LABELS or _text(issue.get("state")) != "open":
            return None
        said = (noted or {}).get(number, "")
        note = f"the review chat {url} was labelled `{name}`: its reviewer stopped"
        note += f", saying:\n\n{_quote(said)}" if said else "; its newest comment says why."
        lines = []
    elif delivery.event == "issues" and action == "closed":
        # A review chat closed with no verdict line (#4253): the deck asks for a fresh
        # review itself once the settle window passes with no verdict reaching the chat.
        said = (noted or {}).get(number, "")
        note = f"the review chat {url} {CLOSED_REVIEW}: it was closed"
        note += f", its newest comment saying:\n\n{_quote(said)}" if said else " with no comment."
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
    closed = delivery.event == "issues" and action == "closed"
    return ReviewStop(number, pulls, note, opened_at, closed) if pulls else None


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
        " (409). Never post the reviewer's verdict for it." + _merge_wait_note(request)
    )


def _merge_wait_note(request: WakeRequest) -> str:
    return (
        " Your wait on its merge ended early because its review stopped: once you have"
        " acted, register the merge wait again."
        if request.kind == KIND_PULL_REQUEST
        else ""
    )


def reask_refused_message(request: WakeRequest, refusal: str = "", down: str = "") -> str:
    """A stopped review the deck could not answer by asking again itself (#4253): what it tried."""
    head = (
        f"AgentDeck wake-on: {request.stopped}\n\nNo verdict reached this chat for"
        f" {request.target} in the {round(STOP_SETTLE_S / 60)} minutes after that."
    )
    if down:
        return (
            f"{head} The deck did not ask for a fresh review itself, because the reviewer is not"
            f" running: {down}. Do not ask for one either; it would only open another chat"
            " nobody reads. Tell the person in the question box that reviews are down and why,"
            " and wait for them." + _merge_wait_note(request)
        )
    return (
        f"{head} The deck already asked for a fresh review of"
        f" {request.repository}#{request.number} itself (the same request as `review-request`"
        f" with `no_verdict`), and it was refused: {refusal}. Do not ask again. Read the review"
        " chat once: act on findings it names and answer a question it asks. If it needs a"
        " person's decision, ask in the question box. Never post the reviewer's verdict for it."
        + _merge_wait_note(request)
    )


def fired_message(request: WakeRequest, outcome: Outcome, *, read: bool = False) -> str:
    """The wake for a result; a success opens with the routine line (#4253).

    ``read`` is a result the deck read from GitHub itself at the wait's timeout.
    """
    asked = f"AgentDeck wake-on: {outcome.summary}. You asked to be woken for {request.target}."
    if request.then:
        # The deck registered it when it merged the chat's pull request (#4253), carrying
        # what the chat is to do once it lands.
        text = (
            f"AgentDeck wake-on: {outcome.summary}. The deck registered this wait for"
            f" {request.target} when it merged your pull request.\n\n{request.then}"
        )
    elif read:
        text = asked + (
            " No webhook brought it, so the deck read it from GitHub itself when the wait timed"
            " out. Carry on with the work that was waiting for it."
        )
    else:
        text = asked + (
            " Webhooks are a hint, so confirm it with one read before you act on it, then"
            " carry on with the work that was waiting for it."
        )
    return deck_notices.routine(text) if outcome.state in SUCCESS_STATES else text


def deck_deploy_message(request: WakeRequest, revision: str, skills: str = "") -> str:
    installed = f", and bundled skills are installed at {skills[:12]}" if skills else ""
    landed = (
        f"AgentDeck wake-on: the web worker serving this deck now runs {revision[:12]}, which"
        f" includes commit {request.sha}{installed}."
    )
    if request.then:
        # The deck registered it when it merged the chat's pull request (#4253), carrying
        # what the chat is to do once it is live.
        return deck_notices.routine(
            f"{landed} The deck registered this wait for {request.target} when it merged your"
            f" pull request.\n\n{request.then}"
        )
    return deck_notices.routine(
        f"{landed} You asked to be woken for"
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
    newest: dict[str, SeenDeploy] = {}
    done: SeenDeploy | None = None
    for record in _tail_records(streams_root, repository, tail_bytes, {"deployment_status"}):
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


def _tail_records(
    streams_root: Path | str, repository: str, tail_bytes: int, events: set[str]
) -> Iterable[dict]:
    """The records of ``events`` in the last ``tail_bytes`` of a repository's stream, in
    the order they were stored."""
    path = _stream_path(Path(streams_root).expanduser(), repository)
    if path is None:
        return []
    try:
        with path.open("rb") as handle:
            start = max(0, handle.seek(0, 2) - tail_bytes)
            handle.seek(start)
            lines = handle.read().split(b"\n")
    except OSError:
        return []
    if start:
        lines = lines[1:]  # cut mid-record
    records = []
    for line in lines:
        if not any(event.encode() in line for event in events):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("event") in events:
            records.append(record)
    return records


def _subject(event: str, payload: dict) -> tuple[str, str, datetime | None]:
    """``(sha, name, when)`` a CI delivery is about, finished or not; ``when`` is the
    source's own time of that state (``None`` when it has none that parses)."""
    if event == "status":
        return (
            _text(payload.get("sha")),
            _text(payload.get("context")),
            _instant(payload.get("updated_at") or payload.get("created_at")),
        )
    if event == "deployment_status":
        status, deployment = payload.get(event), payload.get("deployment")
        if not isinstance(status, dict) or not isinstance(deployment, dict):
            return "", "", None
        return (
            _text(deployment.get("sha")),
            _text(status.get("environment")) or _text(deployment.get("environment")),
            _instant(status.get("updated_at") or status.get("created_at")),
        )
    run = payload.get(event)
    if not isinstance(run, dict) or deck_check_delivery(event, payload):
        return "", "", None
    if event == "check_suite":
        app = run.get("app")
        name = _text(app.get("name")) if isinstance(app, dict) else ""
        return _text(run.get("head_sha")), name, _instant(run.get("updated_at"))
    if event == "check_run":
        when = _instant(run.get("completed_at") or run.get("started_at"))
        return _text(run.get("head_sha")), _text(run.get("name")), when
    return _text(run.get("head_sha")), _text(run.get("name")), _instant(run.get("updated_at"))


def _instant(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def seen_result(
    streams_root: Path | str, request: WakeRequest, *, tail_bytes: int = DEPLOY_TAIL_BYTES
) -> Outcome | None:
    """A finished result a commit wait would fire on, already in the end of the stream and
    not started again since (#3863), else ``None``.

    An agent whose one read of GitHub lagged registered a second wait on a check the first
    had already met; it stayed open, and its Work read Waiting over Ready for merge. A
    result is per check: its newest state by the source's own time decides, not by arrival
    (GitHub can deliver an older success after a newer re-run), so a check re-run since (a
    ``pending`` status, a ``created`` run) is still waited on. A check with a delivery whose
    time does not parse, or a finished and an unfinished state at the same time, is waited
    on too. Like ``seen_deployments``, an empty answer proves nothing.
    """
    if request.kind != KIND_COMMIT or not request.sha:
        return None
    return _newest_outcome(streams_root, request, _CI_EVENTS, tail_bytes)


def seen_deploy_result(
    streams_root: Path | str, request: WakeRequest, *, tail_bytes: int = DEPLOY_TAIL_BYTES
) -> Outcome | None:
    """A finished deploy a deployment wait would fire on, already in the end of the stream,
    else ``None``: what the wait's timeout reads (#4080).

    It takes what the wait itself takes: a failure as well as a success, a commit status
    when the wait has a name, and any commit when it names none. The newest state of each
    environment decides, as in ``seen_result``, so a failed deploy after a good one is told
    as failed and one running again is waited on; for a named wait everything the name
    matches counts as one deploy, across both events. A wait with no commit is about deploys
    after it was registered, so an older one does not count.
    """
    if request.kind != KIND_DEPLOYMENT:
        return None
    events = {"deployment_status", "status"} if request.name else {"deployment_status"}
    return _newest_outcome(streams_root, request, events, tail_bytes)


def _newest_outcome(
    streams_root: Path | str, request: WakeRequest, events: set[str], tail_bytes: int
) -> Outcome | None:
    #: Per check: (its newest state's time, that state's outcome); None once unknowable.
    newest: dict[tuple[str, str], tuple[datetime, Outcome | None] | None] = {}
    for record in _tail_records(streams_root, request.repository, tail_bytes, events):
        delivery = Delivery(
            stream=request.repository,
            delivery_id=str(record.get("delivery_id") or ""),
            event=str(record.get("event") or ""),
            received_at=str(record.get("received_at") or ""),
            headers={},
            raw_body=_raw(record),
            offset=0,
        )
        sha, name, when = _subject(delivery.event, delivery.payload)
        if not sha.lower().startswith(request.sha) or (
            request.name and not _name_matches(request.name, name)
        ):
            continue
        if not request.sha and when is not None and when.timestamp() < request.registered_at:
            continue
        # A named deploy can report by commit status and by deployment status, under
        # different words, and both are the one thing the wait names.
        one_deploy = request.kind == KIND_DEPLOYMENT and request.name
        key = ("", "") if one_deploy else (delivery.event, name.lower())
        if key in newest and newest[key] is None:
            continue
        if when is None:
            newest[key] = None
            continue
        outcome = match(request, delivery)
        held = newest.get(key)
        if held is None or when > held[0] or (when == held[0] and outcome is None):
            newest[key] = (when, outcome)
    done = [held for held in newest.values() if held is not None and held[1] is not None]
    return max(done, key=lambda held: held[0])[1] if done else None


@dataclass(frozen=True)
class NothingToReport:
    """A live read's third answer (#4253 review): GitHub holds nothing that will ever report
    for this wait, so it ends now rather than being read again. ``found`` says what the read
    found, in words for the chat."""

    found: str


async def read_live(
    request: WakeRequest, fetch: Callable[[str], Awaitable[object]]
) -> Outcome | NothingToReport | None:
    """What GitHub says now about a timed-out merge or CI wait (#4253), as one of three
    answers: an ``Outcome`` is the result; ``None`` means something is still open, pending or
    queued, so it is worth reading again; ``NothingToReport`` means nothing will ever report
    (no checks or statuses at all, or for an unnamed wait only skipped or neutral checks, as
    a webhook of those says nothing to it either). ``fetch`` reads one REST path and returns
    its JSON; a failed read raises, and so does an answer of the wrong shape.

    A commit wait reads the commit's check runs and statuses (``name`` filters them as a
    delivery's name is filtered); it is finished once every one it counts completed. A pull
    request wait reads the pull request.
    """
    if request.kind == KIND_PULL_REQUEST:
        pull = await fetch(f"repos/{request.repository}/pulls/{request.number}")
        if not isinstance(pull, dict):
            raise ValueError("the pull request read was not an object")
        if str(pull.get("state") or "").lower() != "closed":
            return None
        payload = {"action": "closed", "number": request.number, "pull_request": pull}
        return _pull_request_outcome(request, "pull_request", payload)
    if request.kind != KIND_COMMIT:
        return None
    base = f"repos/{request.repository}/commits/{request.sha}"
    runs = await fetch(f"{base}/check-runs?per_page=100")
    statuses = await fetch(f"{base}/status?per_page=100")
    if not isinstance(runs, dict) or not isinstance(statuses, dict):
        raise ValueError("the commit's checks read was not an object")
    found: list[tuple[str, bool, str]] = []  # name, finished, conclusion
    for run in runs.get("check_runs") or []:
        if isinstance(run, dict):
            finished = str(run.get("status") or "") == "completed"
            found.append((_text(run.get("name")), finished, _text(run.get("conclusion"))))
    for status in statuses.get("statuses") or []:
        if isinstance(status, dict):
            state = _text(status.get("state"))
            found.append((_text(status.get("context")), state != "pending", state))
    if request.name:
        found = [item for item in found if _name_matches(request.name, item[0])]
    if not found:
        named = f" named {request.name}" if request.name else ""
        return NothingToReport(f"the commit has no check runs or commit statuses{named}")
    if not all(finished for _, finished, _ in found):
        return None
    has_status = any(
        _name_matches(request.name, _text(status.get("context"))) if request.name else True
        for status in statuses.get("statuses") or []
        if isinstance(status, dict)
    )
    counted = [(name, result.lower()) for name, _, result in found if result.lower()]
    results = [item for item in counted if item[1] not in _NO_RESULT]
    if counted and not results:
        if not request.name:
            # Only skipped or neutral checks: they say nothing about the commit, as the same
            # result by webhook says nothing to an unnamed wait (#3910), and nothing else will
            # report (#4253 review). A named wait asked for that check, and takes it.
            names = ", ".join(name or "(unnamed)" for name, _ in counted[:5])
            return NothingToReport(f"its only finished checks were skipped or neutral ({names})")
        results = counted
    failing = [name or "(unnamed)" for name, result in results if result != "success"]
    state = next((result for _, result in results if result != "success"), "success")
    if not results:
        state = "neutral"
    summary = (
        f"CI on {request.repository}@{request.sha[:12]} finished: {state}"
        f" ({len(found)} check{'s' if len(found) != 1 else ''}"
        + (f"; not passing: {', '.join(failing[:5])}" if failing else "")
        + ")"
    )
    # Only check runs read are surely CI: a commit status among them may be a deploy.
    return Outcome(summary, state, ci=not has_status)


def merge_notice_told(message_ids: Iterable[str], pull: str) -> bool:
    """Whether one of these stored message ids is the release's merge notice about ``pull``
    (``owner/repo#n``, matched case-blind): ``work-release:<work>:live-check:<refs>``."""
    wanted = pull.casefold()
    for message_id in message_ids:
        if not str(message_id).startswith(RELEASE_NOTICE_PREFIX):
            continue
        _, found, refs = str(message_id).partition(LIVE_CHECK_KEY)
        if found and wanted in {ref.strip().casefold() for ref in refs.split(",")}:
            return True
    return False


def _raw(record: dict) -> bytes:
    try:
        return raw_body(record)
    except (ValueError, KeyError, TypeError):
        return b""


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


def review_timeout_message(
    request: WakeRequest, minutes: int, review_chats: str = "", reviewer_down: str = ""
) -> str:
    """Send the worker to check the reviewer is moving, not just to wait again (#3389).

    ``reviewer_down`` says why no reviewer is starting at all, when the deck knows (#4064):
    a fresh review would then only open another chat nobody reads, so the worker tells the
    person instead.
    """
    if reviewer_down:
        return (
            f"AgentDeck wake-on: no verdict reached this chat for {request.target} within"
            f" {minutes} minutes, and the reviewer is not running: {reviewer_down}. Do not"
            " ask for a fresh review; it would only open another chat nobody reads. Tell the"
            " person in the question box that reviews are down and why, and wait for them."
        )
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
        " label)? If so it is working: register this review wait again and end the turn. A"
        " `Claimed:` comment with nothing after it is a claim, not progress: when it goes"
        " silent the deck starts its own reviewer on that chat by itself and tells this chat"
        " (#4189), so register the wait again for that too.\n\n"
        "If the chat is open at your head but nothing moved in it since your last wait, or"
        " this is your second timeout on the same head with no review queued, or a fresh"
        " review is refused (409), stop waiting and tell the person in the question box what"
        " you found."
    )


def timeout_message(request: WakeRequest, review_chats: str = "", reviewer_down: str = "") -> str:
    if request.stopped:
        return stopped_message(request)
    if request.kind == KIND_TIME and request.check_command:
        said = (
            f" Your check said not yet {request.check_runs} time(s); the last run, at"
            f" {request.check_last}"
            if request.check_runs
            else " Your check never got to run."
        )
        return (
            f"AgentDeck wake-on: the time you set for {request.target} has come, and your"
            f" check never said it happened.{said} Decide what that means now: no evidence by"
            " the deadline may itself be the answer. Register another time wait only if it"
            " is still running."
        )
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
        return review_timeout_message(request, minutes, review_chats, reviewer_down)
    text = (
        f"AgentDeck wake-on: nothing arrived for {request.target} within {minutes}"
        " minutes. A webhook can be dropped, so check its state once yourself, then carry"
        " on, or register the wait again if it is still running."
    )
    if request.then:
        # The deck registered it when it merged (#4253): what the chat was to do once the
        # deploy landed is still to do, whether or not it did.
        text += (
            f" The deck registered this wait when it merged your pull request.\n\n{request.then}"
        )
    return text


def _clock(at: float) -> str:
    return datetime.fromtimestamp(at).astimezone().strftime("%H:%M")


def _fenced(output: str) -> str:
    text = output.strip() or "(no output)"
    if len(text) > MAX_CHECK_OUTPUT:
        text = "…" + text[-MAX_CHECK_OUTPUT:]
    return "\n```\n" + text.replace("```", "` ` `") + "\n```"


def check_message(request: WakeRequest, code: int | None, output: str, why: str, at: float) -> str:
    """The wake a check's run sends: exit 0 (it happened), else what went wrong."""
    if code == 0:
        return (
            f"AgentDeck wake-on: your check for {request.target} says it happened (exit 0 at"
            f" {_clock(at)}), so the wait has ended. Its output:{_fenced(output)}"
        )
    return (
        f"AgentDeck wake-on: your check for {request.target} went wrong at {_clock(at)}"
        f" ({why}), so the wait has ended. Its output:{_fenced(output)}\nLook at it yourself"
        f" once, then fix the command and register the wait again (exit {CHECK_NOT_YET} means"
        " not yet), or carry on."
    )


#: How much of a check's output the deck holds while it runs: the tail, never the whole of a
#: busy log query's output in the web process.
CHECK_OUTPUT_HELD = 4 * MAX_CHECK_OUTPUT


async def run_check(
    command: str, cwd: str, timeout_s: float, env: dict[str, str] | None = None
) -> tuple[int | None, str, str]:
    """Run one check: (exit code or None, output tail, why it failed in words, or "").

    ``env`` is laid over the web's environment, without its own ``CLAUDE_CONFIG_DIR``: the
    check runs as its chat's account (``WakeOnService``'s ``check_env``, built by
    ``check_account_env``), and with none named ``agentdeck mcp-call`` refuses.
    """
    from .mcp_call import MCP_CONFIG_ENV

    # Never the web's own account: with no ``env`` naming one, ``mcp-call`` refuses.
    environment = {
        k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", MCP_CONFIG_ENV)
    }
    environment[MCP_CONFIG_ENV] = ""
    environment.update(env or {})
    try:
        process = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-c",
            command,
            cwd=cwd or str(Path.home()),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        return None, "", f"it could not start: {exc}"
    held = bytearray()

    async def drain() -> None:
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            held.extend(chunk)
            del held[:-CHECK_OUTPUT_HELD]

    def tail() -> str:
        return held.decode("utf-8", "replace")[-(MAX_CHECK_OUTPUT + 1) :]

    reading = asyncio.ensure_future(drain())
    try:
        await asyncio.wait_for(asyncio.gather(reading, process.wait()), timeout=timeout_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        reading.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await reading
        with contextlib.suppress(Exception):
            await process.wait()
        return None, tail(), f"it ran past {round(timeout_s / 60)} minutes and was stopped"
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        reading.cancel()
        raise
    code = process.returncode
    return code, tail(), "" if code in (0, CHECK_NOT_YET) else f"exit {code}"


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
                        extra_s=request.extra_s,
                        # The deck's live-check request rides on whichever wait it lands on
                        # (#4253): a chat that registered the same deploy wait still gets it.
                        then=request.then or held.then,
                        # Registered again with a check, or without one: the new one stands.
                        check_command=request.check_command,
                        check_cwd=request.check_cwd,
                        check_every_s=request.check_every_s,
                        check_at=request.check_at,
                        check_runs=0,
                        check_last="",
                        check_gen=held.check_gen + 1,
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
        naming none of its pull requests, it answers the chat's one review wait. With several
        it ends none, as nothing says which one it answers: findings about store#3849 ended
        metso#77's wait too, and nothing then told the chat no verdict had come for it (#4089).

        The verdict is recorded (``answered``, #3611): the reviewer's closing line usually
        lands after it, and must not then cut short a merge wait held now or registered later.
        A merge wait a stop already brought forward (``cut_from``) gets its own deadline back:
        the verdict told the chat what the stop would have.
        """
        wanted = None if pulls is None else {str(pull).casefold() for pull in pulls}
        if wanted is None and sum(i.kind == KIND_REVIEW for i in self.for_chat(chat_id)) > 1:
            return 0

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

    def claim_check(self, request_id: str, now: float) -> WakeRequest | None:
        """Take a time wait's due check run, moving its next run on; None when it is not due.

        Under the file's lock, so of two web workers side by side (#3070) only one runs it.
        """
        with self._file.locked():
            current = list(self.all())
            for index, held in enumerate(current):
                if (
                    held.id == request_id
                    and held.check_command
                    and not held.pending_text
                    and held.check_at <= now < held.deadline
                ):
                    current[index] = replace(held, check_at=now + held.check_every_s)
                    self._save(current)
                    return current[index]
        return None

    def settle_check(
        self,
        request_id: str,
        gen: int,
        now: float,
        *,
        last: str = "",
        wake: Callable[[WakeRequest], str] | None = None,
    ) -> WakeRequest | None:
        """End one check run on the wait as it is held now, in one locked step.

        Only while the run still belongs to it: same registration (``check_gen``), not
        owing a message, before its deadline. ``wake`` gives the message it then owes,
        stored as ``pending_text`` and returned for sending; without it the run said not
        yet and is counted (``last``). None when the run no longer belongs to the wait.
        """
        with self._file.locked():
            current = list(self.all())
            for index, held in enumerate(current):
                if (
                    held.id != request_id
                    or held.check_gen != gen
                    or held.pending_text
                    or now >= held.deadline
                ):
                    continue
                if wake is None:
                    settled = replace(held, check_runs=held.check_runs + 1, check_last=last)
                else:
                    settled = replace(held, pending_text=wake(held))
                current[index] = settled
                self._save(current)
                return settled
        return None

    def extend(self, request_id: str, now: float) -> None:
        """Give a timed-out wait its one more round, on the wait as it is held now: one
        renewed or brought forward in between keeps what that write gave it."""
        with self._file.locked():
            current = list(self.all())
            for index, held in enumerate(current):
                if held.id == request_id and held.extra_s and held.deadline <= now:
                    current[index] = replace(held, deadline=now + held.extra_s, extra_s=0.0)
                    self._save(current)
                    return

    def revise(self, request_id: str, due_by: float, **changes) -> WakeRequest | None:
        """Change a wait the deck settled itself (#4253), on the wait as it is held now, and
        only while it is still due by ``due_by`` and owes no message: one renewed, answered or
        cancelled in between keeps what that write gave it. The wait as written, else None."""
        with self._file.locked():
            current = list(self.all())
            for index, held in enumerate(current):
                if held.id == request_id and held.deadline <= due_by and not held.pending_text:
                    current[index] = replace(held, **changes)
                    self._save(current)
                    return current[index]
        return None

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


def register(
    registry: WakeOnRegistry,
    session_key: str,
    chat_id: str,
    *,
    timeout_s: float,
    asked: bool = False,
    clock: Callable[[], float] = time.time,
    **fields,
) -> WakeRequest:
    """Store one wait in ``registry``: the service's ``register``, and the services process's,
    which keeps the registry without the service (#4016)."""
    now = clock()
    request = WakeRequest(
        id=secrets.token_hex(6),
        session_key=session_key,
        chat_id=chat_id,
        registered_at=now,
        deadline=now + timeout_s,
        asked_at=now if asked else 0.0,
        extra_s=timeout_s if fields.get("kind") in PATIENT_KINDS else 0.0,
        # A check first runs one interval on: the chat has just looked for itself.
        check_at=now + fields["check_every_s"] if fields.get("check_command") else 0.0,
        **fields,
    )
    return registry.add(request)


def settled_head(row: dict, sha: str) -> bool:
    """Whether a cached pull-request row's head is ``sha`` and the release settles its green
    CI (#4253): the head of an open, non-draft pull request it will merge, or of one it
    merged, whose deploy wait and live check it now carries. Green CI landing after the
    merge is the same result, so it wakes nobody either (#4253 review); a pull request
    closed without a merge settles nothing."""
    if not sha or not str(row.get("headRefOid") or "").lower().startswith(sha.lower()):
        return False
    if row.get("mergedAt") or str(row.get("state") or "").lower() == "merged":
        return True
    return str(row.get("state") or "").lower() == "open" and not row.get("isDraft")


def register_deck_wait(
    registry: WakeOnRegistry,
    streams_root: Path | str,
    session_key: str,
    chat_id: str,
    *,
    then: str,
    **fields,
) -> WakeRequest:
    """Store a wait the deck registers for a chat when it merges (#4253), carrying ``then``.

    A deploy that already finished before the wait was stored (a late release pass, a
    restart) sends no second delivery, so the wait would only find it at its timeout; it is
    made due at once instead, and the web's deadline pass reads the same result and delivers
    it with ``then`` (#4253 review). Should that read miss, the wait gets one full round.
    """
    wait = register(registry, session_key, chat_id, then=then, **fields)
    if seen_deploy_result(streams_root, wait) is not None:
        made = registry.revise(wait.id, wait.deadline, deadline=wait.registered_at)
        wait = made or wait
    return wait


def check_account_env(config: object, session_key: str) -> dict[str, str]:
    """The environment a chat's check adds: which account's MCP servers it may use.

    ``AGENTDECK_MCP_CONFIG`` (``mcp_call.MCP_CONFIG_ENV``) names the chat's own Claude
    account's ``.claude.json``, and ``CLAUDE_CONFIG_DIR`` points at that account as the
    deck's Claude processes do (``claude_env``). A Codex chat or an account the config no
    longer names gets it empty, and ``agentdeck mcp-call`` then refuses: no fallback to
    another account's server of the same name.
    """
    from . import claude_env
    from .mcp_call import MCP_CONFIG_ENV
    from .restart_nudge import session_key_parts

    parts = session_key_parts(session_key)
    env: dict[str, str] = {MCP_CONFIG_ENV: ""}
    for account in getattr(config, "accounts", ()) if parts else ():
        if account.provider == parts[0] == "claude_code" and account.label == parts[1]:
            claude_env.point_at(env, account.root)
            folder = Path.home() if claude_env.uses_default_login(account.root) else account.root
            env[MCP_CONFIG_ENV] = str(Path(folder) / ".claude.json")
    return env


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
        reviewer_down: Callable[[], str] = lambda: "",
        live_read: Callable[[WakeRequest], Awaitable[Outcome | NothingToReport | None]]
        | None = None,
        ask_review: Callable[[str, int, str], Awaitable[str]] | None = None,
        ready_head: Callable[[WakeRequest], bool] = lambda request: False,
        release_notices: Callable[[str, datetime], Iterable[str]] = lambda chat_id, since: (),
        check_env: Callable[[str], dict[str, str]] = lambda session_key: {},
    ) -> None:
        self._enabled = bool(enabled)
        # The routine wakes the deck settles itself (#4253). ``live_read`` reads a timed-out
        # merge or CI wait from GitHub (``read_live``); ``ask_review`` asks for a fresh review
        # of a pull request whose review chat closed with no verdict, answering "" when asked
        # (or already asked) and the refusal in words otherwise; ``ready_head`` says, from
        # cached rows alone, whether a commit wait's commit is the head of an open, non-draft
        # pull request the release merges, or of one it merged (``settled_head``);
        # ``release_notices`` lists the release's message ids stored for a chat since a time.
        # None of the reads blocks the event loop.
        self._live_read = live_read
        self._ask_review = ask_review
        self._ready_head = ready_head
        self._release_notices = release_notices
        # Waits a read or a re-ask is settling now: ``expire_due`` and ``next_due`` leave them.
        self._settling: set[str] = set()
        self._side_tasks: set[asyncio.Task] = set()
        # What a chat's check runs with on top of the web's environment, by session key: its
        # own Claude account's ``CLAUDE_CONFIG_DIR``, so ``agentdeck mcp-call`` reads that
        # account's MCP servers and keys, never another account's of the same name.
        self._check_env = check_env
        # Why no reviewer is starting, in words, or "" (#4064): a review wait's timeout says
        # so rather than sending the worker to ask for another chat nobody reads.
        self._reviewer_down = reviewer_down
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
        # Time-wait checks running now (#4256), by wait id.
        self._checks: dict[str, asyncio.Task] = {}
        self._check_slots = asyncio.Semaphore(MAX_CHECKS_AT_ONCE)
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

    def seen_result(self, request: WakeRequest) -> Outcome | None:
        """``seen_result`` over this service's streams."""
        return seen_result(self._streams_root, request)

    def _seen_at_timeout(self, request: WakeRequest) -> Outcome | None:
        """What the deck already holds for a wait at its timeout, else ``None``: the delivery
        may have come in while the tailer was behind, or before the wait was registered.

        A pull request wait has no such read: its delivery fires it from the stream, and the
        deck keeps no other record of a merge. This runs on the event loop, once per timeout;
        the largest stream took 12 ms to read back.
        """
        try:
            if request.kind == KIND_COMMIT:
                return self.seen_result(request)
            if request.kind == KIND_DEPLOYMENT:
                return seen_deploy_result(self._streams_root, request)
        except Exception:  # noqa: BLE001 - a read of the stream never blocks a wake
            log.warning("wake-on: stored result unreadable", exc_info=True)
        return None

    def register(
        self, session_key: str, chat_id: str, *, timeout_s: float, asked: bool = False, **fields
    ) -> WakeRequest:
        """Store one wait; ``asked`` is a review wait the chat asked for the review of now."""
        return register(
            self.registry,
            session_key,
            chat_id,
            timeout_s=timeout_s,
            asked=asked,
            clock=self._clock,
            **fields,
        )

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="wake-on")
        self._deadline_task = asyncio.create_task(self._deadlines(), name="wake-on-deadlines")

    async def stop(self) -> None:
        tasks = [task for task in (self._task, self._deadline_task) if task is not None]
        tasks += list(self._side_tasks)
        tasks += list(self._checks.values())
        self._checks.clear()
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
            if request.id in self._settling:
                continue  # its read or re-ask re-plans when it ends
            if request.pending_text:
                due.append(self._retry_at.get(request.id, now))
                continue
            due.append(request.deadline)
            if request.check_command and request.id not in self._checks:
                due.append(request.check_at)
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
                for request in self.due_checks():
                    self._checks[request.id] = asyncio.create_task(
                        self._run_check(request), name=f"wake-on-check-{request.id}"
                    )
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
                    await self._tailer.commit_off_loop()
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
                    self._deliver(request, outcome)
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
                or (stop.closed and request.stopped)
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
            if request.id in self._settling:
                continue
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
                if request.kind in PATIENT_KINDS and not request.stopped and not request.cut_from:
                    outcome = self._seen_at_timeout(request)
                    if outcome is not None:
                        self._deliver(request, outcome)
                        continue
                    if request.extra_s:
                        self.registry.extend(request.id, now)
                        continue
                    if (
                        request.kind in LIVE_READ_KINDS
                        and self._live_read is not None
                        and self._settle(request, self._read_at_timeout)
                    ):
                        continue
                if (
                    CLOSED_REVIEW in request.stopped
                    and request.kind in (KIND_REVIEW, KIND_PULL_REQUEST)
                    and request.number is not None
                    and self._ask_review is not None
                    and self._settle(request, self._ask_again)
                ):
                    continue
                down = ""
                if request.kind == KIND_REVIEW:
                    try:
                        down = self._reviewer_down()
                    except Exception:  # noqa: BLE001 - a health read never blocks a wake
                        log.warning("wake-on: reviewer health unreadable", exc_info=True)
                self._fire(request, timeout_message(request, self._review_chats, down))

    def _settle(self, request: WakeRequest, work: Callable[[WakeRequest], Awaitable[None]]) -> bool:
        """Start ``work`` on a due wait off the deadline pass, once; False with no event loop,
        when the wait takes today's path instead."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._settling.add(request.id)

        async def run() -> None:
            try:
                await work(request)
            except Exception:  # noqa: BLE001 - the wait is told on its next pass
                log.exception("wake-on: settling %s failed", request.id)
            finally:
                self._settling.discard(request.id)
                self._changed.set()

        task = loop.create_task(run(), name=f"wake-on-settle-{request.id}")
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)
        return True

    async def _read_at_timeout(self, request: WakeRequest) -> None:
        """B1 of #4253: read a timed-out merge or CI wait from GitHub once. A result wakes
        the chat; still running, it waits again up to a day from its registration, silently;
        a failed read, nothing that will ever report, or the day's end, sends the timeout
        message saying what the deck read, and ends the wait."""
        failed = ""
        outcome: Outcome | NothingToReport | None = None
        try:
            outcome = await self._live_read(request)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - a failed read falls back to the timeout
            log.warning("wake-on: live read of %s failed", request.target, exc_info=True)
            failed = str(exc).strip()[:200] or type(exc).__name__
        now = self._clock()
        held = next((item for item in self.registry.all() if item.id == request.id), None)
        if held is None or held.deadline > now or held.pending_text:
            return  # answered, cancelled or renewed while it read
        if failed:
            self._fire(
                held,
                timeout_message(held)
                + f" The deck tried to read it from GitHub itself at the timeout, and the read"
                f" failed ({failed}).",
            )
            return
        if isinstance(outcome, NothingToReport):
            self._fire(
                held,
                timeout_message(held)
                + f" The deck read it from GitHub itself at the timeout: {outcome.found}, so"
                " nothing is left to report and it stopped waiting.",
            )
            return
        if outcome is not None:
            self._deliver(held, outcome, read=True)
            return
        cap = held.registered_at + LIVE_READ_CAP_S
        if now >= cap - 1:
            self._fire(
                held,
                timeout_message(held)
                + " The deck read it from GitHub itself at each timeout and it was still"
                " running, so it waited again, up to a day from when you registered the wait.",
            )
            return
        deadline = min(now + LIVE_READ_AGAIN_S, cap)
        log.info("wake-on: %s still running; reading again at %s", request.target, deadline)
        self.registry.revise(held.id, now, deadline=deadline)

    async def _ask_again(self, request: WakeRequest) -> None:
        """B2 of #4253: a review chat closed with no verdict, so ask for a fresh review the way
        ``review-request`` with ``no_verdict`` does. Accepted, the wait keeps waiting without
        a message; refused, or with the reviewer down, the chat hears what the deck tried."""
        down = ""
        try:
            down = self._reviewer_down()
        except Exception:  # noqa: BLE001 - a health read never blocks a wake
            log.warning("wake-on: reviewer health unreadable", exc_info=True)
        refusal = ""
        if not down:
            note = (
                "The deck asked again by itself: the last review ended with no verdict for this"
                f" head. {request.stopped}"
            )[:1800]
            try:
                refusal = await self._ask_review(  # type: ignore[misc]
                    request.repository, int(request.number or 0), note
                )
            except Exception as exc:  # noqa: BLE001 - an unclear ask is told, never retried
                log.warning("wake-on: asking again for %s failed", request.target, exc_info=True)
                refusal = f"the request failed ({str(exc).strip()[:200] or type(exc).__name__})"
        now = self._clock()
        held = next((item for item in self.registry.all() if item.id == request.id), None)
        if held is None or held.deadline > now or held.pending_text or not held.stopped:
            return  # a verdict, a renewal or a cancel reached it meanwhile
        if down or refusal:
            self._fire(held, reask_refused_message(held, refusal, down))
            return
        log.info("wake-on: asked again for %s; %s keeps waiting", request.target, held.session_key)
        if held.kind == KIND_REVIEW:
            deadline = now + REVIEW_TIMEOUT_MINUTES * 60
        else:
            deadline = max(held.cut_from, now)
        self.registry.revise(
            held.id, now, deadline=deadline, stopped="", cut_from=0.0, asked_at=now
        )

    def _deliver(self, request: WakeRequest, outcome: Outcome, *, read: bool = False) -> None:
        """Wake the chat with a result, unless the deck already settles it (#4253): green CI
        on the head of a ready pull request the release merges or merged (a deploy's result
        at that head still wakes it), or a merge the release's own
        notice told the chat of in the last ``MERGE_TOLD_S``."""
        quiet = ""
        try:
            if (
                request.kind == KIND_COMMIT
                and outcome.ci
                and outcome.state == "success"
                and self._ready_head(request)
            ):
                quiet = "green on the head of a ready pull request the deck merges"
            elif request.kind == KIND_PULL_REQUEST and outcome.state == "merged":
                since = datetime.now(UTC) - timedelta(seconds=MERGE_TOLD_S)
                ids = self._release_notices(request.chat_id, since)
                if merge_notice_told(ids, f"{request.repository}#{request.number}"):
                    quiet = "the deck's own merge notice already told the chat"
        except Exception:  # noqa: BLE001 - an unread cache wakes the chat as before
            log.warning("wake-on: could not tell whether %s is settled", request.id, exc_info=True)
        if quiet:
            log.info(
                "wake-on: ended %s's wait on %s: %s", request.session_key, request.target, quiet
            )
            self._retry_at.pop(request.id, None)
            self.registry.remove([request.id])
            return
        self._fire(request, fired_message(request, outcome, read=read))

    def due_checks(self) -> list[WakeRequest]:
        """Claim every time-wait check due now that is not already running here."""
        now = self._clock()
        claimed = []
        for request in self.registry.all():
            if (
                request.check_command
                and request.id not in self._checks
                and not request.pending_text
                and request.check_at <= now < request.deadline
            ):
                taken = self.registry.claim_check(request.id, now)
                if taken is not None:
                    claimed.append(taken)
        return claimed

    async def _run_check(self, request: WakeRequest) -> None:
        try:
            async with self._check_slots:
                code, output, why = await run_check(
                    request.check_command,
                    request.check_cwd,
                    CHECK_TIMEOUT_S,
                    env=self._check_env(request.session_key),
                )
            self.check_done(request, code, output, why)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one check's fault never stops the deadlines
            log.exception("wake-on: check for %s failed", request.id)
        finally:
            self._checks.pop(request.id, None)
            self._changed.set()

    def check_done(self, request: WakeRequest, code: int | None, output: str, why: str) -> None:
        """Act on one check run: not yet is counted silently, anything else wakes the chat.

        Both go through one locked registry step that first checks the run still belongs
        to the wait as held (``WakeOnRegistry.settle_check``): a wait cancelled, timed out,
        already owing its message, or registered again while this ran (by either web
        worker) is left alone. A wake is written into the wait as the message it owes
        before it is sent, so a renewal after that cannot change or repeat it.
        """
        now = self._clock()
        if code == CHECK_NOT_YET:
            tail = " ".join(output.split())[-MAX_CHECK_LAST:]
            last = f"{_clock(now)}, printed: {tail or '(nothing)'}"
            self.registry.settle_check(request.id, request.check_gen, now, last=last)
            return
        owed = self.registry.settle_check(
            request.id,
            request.check_gen,
            now,
            wake=lambda held: check_message(held, code, output, why, now),
        )
        if owed is not None:
            self._fire(owed, owed.pending_text)

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
