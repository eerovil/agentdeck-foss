"""Application-level injection coordination and result tracking."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from . import pid_watch
from .action_context import client_action_context
from .attachments import cleanup_uploaded_files, display_message
from .config import InjectConfig
from .deployment import is_deploy_checkout
from .images import media_type_for_suffix
from .models import Account, Capability, ForkFrom, InjectResult, Session, TranscriptEvent
from .providers.base import SessionProvider

log = logging.getLogger(__name__)

DelegationSessionRole = Literal["primary", "delegated"]

#: How often a web process looks for delegations whose watcher has exited (#2582), used
#: only while one of those processes cannot be watched for its exit (#2865).
DELEGATION_TAKEOVER_INTERVAL_S = 5.0
#: The least a taken-over wait gets, however much of its budget the restart used:
#: enough to read a child that finished while nobody was watching it.
DELEGATION_RESUME_FLOOR_S = 30.0
#: How long a finished delegation's result stays readable.
DELEGATION_RETENTION_S = 7 * 24 * 3600.0


class DelegationStore(Protocol):
    """Where delegation status lives so a web deploy does not lose it (#2582)."""

    def save_delegation(self, record: Mapping[str, Any]) -> None: ...

    def load_delegation(self, delegation_id: str) -> dict[str, Any] | None: ...

    def unfinished_delegations(self) -> list[dict[str, Any]]: ...

    def claim_delegation(
        self, delegation_id: str, expected_owner: int | None, owner: int
    ) -> bool: ...

    def prune_delegations(self, finished_before: float) -> None: ...


class _NoDelegationStore:
    """A service built without a database keeps delegations for its own lifetime only."""

    def save_delegation(self, record: Mapping[str, Any]) -> None: ...

    def load_delegation(self, delegation_id: str) -> dict[str, Any] | None:
        return None

    def unfinished_delegations(self) -> list[dict[str, Any]]:
        return []

    def claim_delegation(
        self, delegation_id: str, expected_owner: int | None, owner: int
    ) -> bool:
        return False

    def prune_delegations(self, finished_before: float) -> None: ...


def _process_alive(pid: int | None) -> bool:
    """Whether the AgentDeck process with this id still runs on this host.

    A process id the kernel has since handed to an unrelated program counts as
    gone, or its delegations would never be taken over.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    return b"agentdeck" in command


def _sibling_pids() -> set[int]:
    """The other AgentDeck processes started by this process's parent (#2865).

    Under ``web_supervisor`` that is the web worker being replaced during a deploy,
    the one process that can still leave a delegation behind for this one. Nothing
    when ``/proc`` cannot say (no ``children`` file): the owners are still watched.
    """
    me = os.getpid()
    parent = os.getppid()
    try:
        raw = Path(f"/proc/{parent}/task/{parent}/children").read_text()
    except OSError:
        return set()
    siblings = set()
    for value in raw.split():
        if value.isdigit() and int(value) != me and _process_alive(int(value)):
            siblings.add(int(value))
    return siblings


def _submitted_message(message: str, files: list[Path] | None) -> str:
    """The person's text, excluding AgentDeck's generated attachment note."""
    return display_message(message) if files else message


# A refusal for now only: the deck's own retries match it to ask again later (#3285).
RUNTIME_UNREACHABLE = (
    "this account's runtime is not reachable right now (it may be restarting); "
    "try again in a minute"
)


def _cannot_start(provider, account) -> str:
    """Why ``account`` cannot start a chat now, in words the form can show (#2975)."""
    if provider.offers_new_session(account):
        return RUNTIME_UNREACHABLE
    return "this provider cannot start sessions"


def _delivery_text_matches(sent: str, durable: str | None) -> bool:
    """Match exact text, plus CLI-normalized whitespace after slash commands."""
    if sent == durable:
        return True
    if durable is None:
        return False

    def command_parts(text: str) -> tuple[str, str] | None:
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        parts = stripped.split(maxsplit=1)
        return parts[0], parts[1] if len(parts) == 2 else ""

    sent_command = command_parts(sent)
    return sent_command is not None and sent_command == command_parts(durable)


#: How long an accepted message may wait for its transcript event before the UI
#: stops calling it in-flight. A healthy worker writes the user event within a
#: second or two, so a minute is far past normal without being twitchy about a
#: slow spawn. It is also how long the delivery loop watches the transcript
#: before deciding whether the send is safe to make again.
ACKNOWLEDGEMENT_GRACE_S = 60.0

#: How often that watch re-reads the transcript. Slow on purpose: the answer it
#: is waiting for normally arrives in the first read or two, and nothing depends
#: on catching it to the second.
ACKNOWLEDGEMENT_POLL_S = 1.0

#: How many times the delivery loop may hand one message to a provider. The
#: first send plus, when the transcript proves the first one landed nowhere, one
#: automatic resend. A message that goes unacknowledged twice is not retried
#: again — at that point the resending is not helping and it is a person's call.
MAX_DELIVERY_ATTEMPTS = 2

#: Browser reconciliation metadata is best-effort UI state, not a provider
#: delivery fence. Keep it long enough for a sleeping/reconnecting tab, but
#: bound both age and volume so a long-lived web process cannot accumulate one
#: full QueuedMessage per turn forever. A page older than this can always reload
#: its durable transcript; delivery safety lives in providers/delivery.py.
OBSERVED_CORRELATION_TTL_S = 24 * 3600.0
MAX_OBSERVED_CORRELATIONS = 4096


def _observed_event_fingerprint(
    text: str | None, image_media_types: tuple[str, ...]
) -> bytes:
    digest = hashlib.sha256()
    digest.update((text or "").encode("utf-8"))
    digest.update(b"\0")
    for media_type in image_media_types:
        digest.update(media_type.encode("utf-8"))
        digest.update(b"\0")
    return digest.digest()


@dataclass(frozen=True)
class _ObservedMessage:
    """Compact identity that lets another browser retire its optimistic row.

    Once a queued message is durable, none of its delivery payload belongs in
    memory for reconciliation. The transcript seq plus exact durable-event
    fingerprint protects against a rewritten transcript reusing a seq, while the
    client action ID is all the renderer needs.
    """

    id: int
    client_action_id: str | None
    event_at: datetime | None
    event_fingerprint: bytes
    linked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class InjectionStatus:
    state: str
    reason: str | None = None
    items: tuple[QueuedMessage, ...] = ()
    session_key: str | None = None


@dataclass
class QueuedMessage:
    id: int
    text: str
    images: tuple[Path, ...] = ()
    files: tuple[Path, ...] = ()
    client_action_id: str | None = None
    effort: str | None = None
    allow_over_budget: bool = False
    # Whether sending this counts as giving the chat something new to do.
    #
    # False only for a durable retry behind an action id that has already been
    # sent once: the fence downstream keeps it from becoming a second message, so
    # the chat is handed nothing and must not be stamped as freshly working. The
    # stamp is what ``session_awaiting_reply`` measures the chat's own activity
    # against, and a retry that re-stamps it on a timer moves the line the chat
    # has to cross faster than the chat can ever cross it — which is how a chat
    # whose turn died silently sat in Deckhand's Working group for good (#2003).
    counts_as_work: bool = True
    # Whether `on_handover` has already fired for this item. Two paths can be the
    # first to learn the provider took the send — the transcript event arriving,
    # and the provider call returning — and either may come first, so the flag is
    # what keeps one delivery to one announcement.
    handover_announced: bool = False
    state: str = "queued"
    reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    observed_seq: int | None = None
    observed_at: datetime | None = None
    after_seq: int | None = None
    accepted_at: datetime | None = None
    attempts: int = 0

    @property
    def image_media_types(self) -> tuple[str, ...]:
        return tuple(
            media_type_for_suffix(image.suffix) or "" for image in self.images
        )

    @property
    def is_pending(self) -> bool:
        """The message is still in flight — queued, being delivered, or handed
        off but not yet confirmed in the transcript. While pending its optimistic
        row is shown and its uploaded images must stay served; once ``observed``,
        ``complete``, or ``failed`` it no longer is.

        This is distinct from the narrower internal subsets used inside the
        delivery loop (matching a durable event, or failing in-flight items on
        error) — do not fold those into this predicate.
        """
        return self.state in ("queued", "running", "accepted")

    @property
    def is_unacknowledged(self) -> bool:
        """Handed to the agent long enough ago that its silence is now evidence.

        Acceptance only means the provider took the message; the transcript is
        what proves the agent read it. A CLI can take a message, write nothing,
        and go back to idle — a deleted working directory does it, and so does a
        prompt the CLI drops on the floor. Nothing errors, so without this the
        optimistic row sits on "waiting for transcript" forever and the message
        looks sent.

        Derived rather than a state transition on purpose: a late transcript
        event still links this item normally, so a slow worker resolves itself
        instead of being written off. The row stays pending either way — this
        only changes what it claims.
        """
        if self.state != "accepted" or self.accepted_at is None:
            return False
        return (
            datetime.now(UTC) - self.accepted_at
        ).total_seconds() >= ACKNOWLEDGEMENT_GRACE_S

    @property
    def is_being_resent(self) -> bool:
        """On its way to the provider again after a send that landed nowhere.

        Only the delivery loop's automatic resend puts an item back on its way
        with an attempt already spent, so this says the same thing to the person
        watching that the log line says: the first send was not acknowledged and
        the deck is having another go by itself.
        """
        return self.attempts > 0 and self.state in ("queued", "running")


def is_resendable(item: QueuedMessage) -> bool:
    """Whether a failed message can be put back in the queue as it is.

    Uploads are cleaned up when a send fails, so only a message without any can
    be sent again from what the deck still holds.
    """
    return not item.images and not item.files


def unsent_messages(status: InjectionStatus | None) -> list[QueuedMessage]:
    """The messages behind the chat's current failure, oldest first (#2662).

    Only while the newest send is the one that failed: the failed item and the
    ones that failed with it ("not sent because the previous turn failed").
    Anything that failed before a later send went through is history, not
    something the person is still waiting on.
    """
    if status is None or status.state != "failed":
        return []
    unsent: list[QueuedMessage] = []
    for item in reversed(status.items):
        if item.state != "failed":
            break
        unsent.append(item)
    return unsent[::-1]


@dataclass(frozen=True)
class InjectionReceipt:
    """The outcome of a delivery, plus its optimistic row."""

    result: InjectResult
    item: QueuedMessage | None


@dataclass(frozen=True)
class DelegationStatus:
    id: str
    state: str
    account_key: str
    session_key: str | None = None
    reason: str | None = None
    final_message: str | None = None
    # The deck-minted token the child was started with, saved before the start so a
    # web process that dies mid-start leaves a way to find the chat (#2582).
    session_token: str | None = None
    # When the delegation began; a taken-over wait spends what is left of its budget.
    created_at: float | None = None

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> DelegationStatus:
        return cls(
            id=str(record["id"]),
            state=str(record["state"]),
            account_key=str(record["account_key"]),
            session_key=record.get("session_key"),
            reason=record.get("reason"),
            final_message=record.get("final_message"),
            session_token=record.get("session_token"),
            created_at=record.get("created_at"),
        )


@dataclass
class StartedChat:
    """A start the deck can show a page for before the provider names a session.

    Spawning an agent takes seconds, and until it answers there is no session key
    to navigate to. The token is minted *before* the provider is called, so this
    record is the chat's identity in the meantime: it carries what the person
    already knows (their first message, where it runs) and, once the start
    settles, its outcome (#765).
    """

    token: str
    account_key: str
    cwd: str
    message: str
    image_count: int = 0
    model: str | None = None
    effort: str | None = None
    origin: str = "new"
    source_session_key: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: InjectionStatus = InjectionStatus("running")


class InjectionService:
    _MAX_STATUSES = 200
    _MAX_STARTS = 50
    _HISTORY_ITEMS = 12
    _MAX_DISCARDED_ACTIONS = 200

    def __init__(
        self,
        config: InjectConfig,
        *,
        on_change: Callable[[str], None] | None = None,
        # The deck is handing this session work. Called once per delivery, as the
        # handoff starts and deliberately before the provider has taken anything,
        # so a not-working -> working change the deck caused can never be missed
        # (#1450). Being early makes it optimistic, which is what ``on_send_failed``
        # below is for.
        on_sent: Callable[[str], None] | None = None,
        # That handoff reached nothing — refused, errored, or cancelled — so the
        # working stamp ``on_sent`` wrote has to come back off. Without it a chat
        # the deck failed to reach stays classified as working for good, and the
        # stop that should have carded it is never seen (#1450).
        on_send_failed: Callable[[str], None] | None = None,
        # A send under this client action id has reached the provider, so the
        # durable delivery fence downstream is armed and any later send behind the
        # same id will be collapsed into this one. Called from the delivery's own
        # background lifecycle rather than from whoever queued it, because that
        # caller may have stopped waiting long before the provider answered — and
        # a durable retry deciding it is new work, on evidence sampled before the
        # handoff finished, is how #2003 came back. Fired at most once per id.
        on_handover: Callable[[str, str], None] | None = None,
        on_delegation_started: Callable[[str, str | None], None] | None = None,
        # Whether a delegated child may start now (#2207): the durable delegated-work
        # cutover must exist first, so a child can never be recorded before it.
        delegation_ready: Callable[[], bool] | None = None,
        # (token, session_key) once a started session has a provider id.
        on_session_token: Callable[[str, str], None] | None = None,
        # Marks a pre-reserved Continue-handoff token immediately before the
        # provider call. Tokens for ordinary starts have no reservation, so the
        # callback is intentionally a harmless no-op for them.
        on_new_session_dispatch: Callable[[str], None] | None = None,
        # A provider answered definitively without allocating a session. Providers
        # must return the allocated session id even when its first turn is rejected;
        # only an id-less rejection is safe to release for another Continue attempt.
        on_new_session_refused: Callable[[str], None] | None = None,
        # Whether a turn is running in this session right now. Consulted before
        # an unacknowledged message is sent again: a CLI that is slow to start
        # has written nothing yet either, and resending into it is exactly the
        # duplicate the retry exists to avoid.
        session_busy: Callable[[str], bool] | None = None,
        # A person threw away the optimistic row for this action id — passed the
        # leg they were looking at, for the record — and whether they have already
        # done so. Backed by the control database in the real app, because the
        # in-memory note below dies with the web process while the caller retrying
        # behind that action id does not: the Mac program re-posts every 30
        # seconds for as long as an outcome stays unconfirmed, and a frontend
        # deploy lands several times a day. Left as no-ops for a service built
        # without a database — the in-memory note still covers that process's own
        # lifetime (#2031).
        on_dismissed: Callable[[str, str], None] | None = None,
        was_dismissed: Callable[[str], bool] | None = None,
        # Delegation status that outlives this process, and the token lookup a
        # takeover uses for a child whose start was cut off (#2582).
        delegation_store: DelegationStore | None = None,
        session_key_for_token: Callable[[str], str | None] | None = None,
    ):
        self.config = config
        # Why the deck sends nothing to this session now, or None (#2559): a person paused a
        # Work it belongs to. Set by the app once its Work overview exists; consulted before a
        # message is queued and again before one is taken off the queue, so a paused Work
        # starts no new turn. A steer into the turn already running is not a new turn.
        self.held: Callable[[str], str | None] = lambda _session_key: None
        self._on_change = on_change or (lambda _session_key: None)
        self._on_sent = on_sent or (lambda _session_key: None)
        self._on_send_failed = on_send_failed or (lambda _session_key: None)
        self._on_handover = on_handover or (lambda _session_key, _action_id: None)
        self._on_session_token = on_session_token or (
            lambda _token, _session_key: None
        )
        self._on_new_session_dispatch = on_new_session_dispatch or (lambda _token: None)
        self._on_new_session_refused = on_new_session_refused or (lambda _token: None)
        self._delegation_ready = delegation_ready or (lambda: True)
        self._on_delegation_started = on_delegation_started or (
            lambda _session_key, _parent_session_id: None
        )
        self._session_busy = session_busy or (lambda _session_key: False)
        self._on_dismissed = on_dismissed or (lambda _action_id, _session_key: None)
        self._was_dismissed = was_dismissed or (lambda _action_id: False)
        self._delegation_store: DelegationStore = delegation_store or _NoDelegationStore()
        self._session_key_for_token = session_key_for_token or (lambda _token: None)
        self._delegation_takeover: asyncio.Task | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._revivals: dict[str, asyncio.Event] = {}
        self._status: dict[str, InjectionStatus] = {}
        self._items: dict[str, list[QueuedMessage]] = {}
        # Action ids whose optimistic row the person dismissed, newest last.
        #
        # Dismissing takes the row out of `_items`, and `_items` is the only thing
        # that stopped a caller with a stable action id from queueing the same
        # outcome again — the Mac runner re-posts every 30 seconds until the deck
        # confirms delivery, and a message the agent never wrote down is never
        # confirmed. So the dismissal has to be remembered here, or the row the
        # person just removed is rebuilt from scratch on the next retry (#2031).
        #
        # By action id alone, not by the leg it was dismissed on: a callback that
        # arrives after the chat has rolled over is retargeted to whichever leg is
        # working then, so a leg-keyed note would miss the retry it exists to
        # stop. Action ids are unique per send, so there is nothing for this to
        # collide with.
        self._discarded_actions: dict[str, None] = {}
        # Compact transcript->optimistic-row links. Unlike `_items`, these carry
        # no message body, uploads or delivery flags and are safe to retain for a
        # reconnecting browser after the original queue row leaves the UI window.
        self._observed: dict[tuple[str, int], _ObservedMessage] = {}
        # Set when a message joins a session's queue, so the acknowledgement
        # watch below hands the loop straight back instead of finishing its
        # poll first. A person who sends again must not wait on it.
        self._queue_nudges: dict[str, asyncio.Event] = {}
        self._new_tasks: dict[str, asyncio.Task] = {}
        self._new_status: dict[str, InjectionStatus] = {}
        self._starts: dict[str, StartedChat] = {}
        self._delegation_tasks: dict[str, asyncio.Task] = {}
        self._delegations: dict[str, DelegationStatus] = {}
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._next_id = 1

    def status(self, session_key: str) -> InjectionStatus | None:
        return self._status.get(session_key)

    def session_busy(self, session_key: str) -> bool:
        """Whether a turn is running in this chat right now.

        Public because the pending row's wording depends on it: an accepted
        message a busy chat has not written yet is waiting its turn, not being
        ignored, and saying otherwise is a false alarm (#2016). The delivery loop
        already refuses to resend on the same reasoning.
        """
        return bool(self._session_busy(session_key))

    def discard(self, session_key: str, item_id: int) -> bool:
        """Forget one optimistic row the agent took and never wrote down.

        Only a row that is already accusing the agent qualifies: accepted, past
        the grace period, and in a chat with no turn running — exactly the rows
        that show the unacknowledged caption. A queued or running row is still
        with the delivery loop, and one in a busy chat may yet be read (#2016),
        so neither may be pulled out from under it.

        Nothing durable moves. The runtime's delivery receipts and the Mac
        callback ledger are untouched, so dismissing a row can never let the same
        message be delivered a second time — it only clears what the person is
        looking at. `_prune_history` keeps pending rows forever on purpose, so
        without this the only way to lose a dead row was to restart the web
        process (#2027).

        The row is marked `discarded` before it leaves the list because the
        resend watcher holds its own reference: a state that is no longer
        `accepted` is how that loop learns to stop.

        Its action id is remembered on the way out. A caller that retries behind a
        stable id was being held off by this very row, so removing it handed the
        next retry an empty queue to append to — and the row the person dismissed
        came straight back, new id and same text, within one Mac retry (#2031).
        """
        items = self._items.get(session_key)
        if not items or self.session_busy(session_key):
            return False
        target = next(
            (item for item in items if item.id == item_id and item.is_unacknowledged),
            None,
        )
        if target is None:
            return False
        target.state = "discarded"
        items[:] = [item for item in items if item is not target]
        if not items:
            self._items.pop(session_key, None)
        if target.client_action_id is not None:
            self._remember_discarded_action(target.client_action_id, session_key)
        self._snapshot(session_key)
        return True

    def _remember_discarded_action(self, action_id: str, session_key: str) -> None:
        """Note that this chat's person has thrown this action's row away.

        Written down twice on purpose. The durable record is the one that has to
        hold: it outlives the web process, and the caller retrying behind this id
        outlives the web process too. The map here is a cache in front of it, and
        the only record at all for a service built without a database, so its
        bound is a memory backstop rather than the lifetime of the decision.
        """
        self._on_dismissed(action_id, session_key)
        self._discarded_actions.pop(action_id, None)
        self._discarded_actions[action_id] = None
        while len(self._discarded_actions) > self._MAX_DISCARDED_ACTIONS:
            self._discarded_actions.pop(next(iter(self._discarded_actions)))

    def _action_was_dismissed(self, action_id: str) -> bool:
        return action_id in self._discarded_actions or self._was_dismissed(action_id)

    def _prune_observed_correlations(self, *, now: datetime | None = None) -> None:
        """Bound UI-only correlations by age and volume.

        Losing one can at worst leave an old optimistic row in a stale browser
        until reload; it can never cause another provider write. That is why a
        count backstop is correct here and deliberately *not* correct for the
        delivery receipts in providers/delivery.py.
        """
        current = now or datetime.now(UTC)
        for key, observed in tuple(self._observed.items()):
            if (current - observed.linked_at).total_seconds() > OBSERVED_CORRELATION_TTL_S:
                self._observed.pop(key, None)
        while len(self._observed) > MAX_OBSERVED_CORRELATIONS:
            self._observed.pop(next(iter(self._observed)))

    @staticmethod
    def _observed_matches(
        observed: _ObservedMessage, event: TranscriptEvent, event_at: datetime | None
    ) -> bool:
        return (
            observed.event_at == event_at
            and observed.event_fingerprint
            == _observed_event_fingerprint(event.text, event.image_media_types)
        )

    def observe_transcript(
        self, session_key: str, events: list[TranscriptEvent]
    ) -> dict[int, QueuedMessage | _ObservedMessage]:
        """Link durable user events to app-queued turns exactly once.

        Provider acceptance only means the send was handed off. The transcript
        is authoritative for when its optimistic row can disappear. Once that
        link is known, keep only compact reconciliation metadata so the delivery
        queue itself stays bounded while a slower browser can still remove its
        optimistic row.
        """
        self._prune_observed_correlations()
        items = self._items.get(session_key, [])
        links: dict[int, QueuedMessage | _ObservedMessage] = {}
        changed = False
        for event in events:
            if event.role != "user":
                continue
            event_ts = event.ts
            if event_ts is not None and event_ts.tzinfo is None:
                event_ts = event_ts.replace(tzinfo=UTC)
            # Claude queue-operation events are durable transcript evidence but
            # carry no timestamp. Their pre-send cursor is the temporal guard.
            if event_ts is None and not event.queued:
                continue
            observed_key = (session_key, event.seq)
            already_observed = self._observed.get(observed_key)
            if already_observed is not None:
                if self._observed_matches(already_observed, event, event_ts):
                    links[event.seq] = already_observed
                    continue
                # The provider rewrote this seq. Never let the old optimistic
                # action ID claim a different durable event.
                self._observed.pop(observed_key, None)
            match = next(
                (
                    item
                    for item in items
                    if item.state in ("running", "accepted")
                    and _delivery_text_matches(item.text, event.text)
                    and item.image_media_types == event.image_media_types
                    and (item.after_seq is None or event.seq > item.after_seq)
                    and (
                        event_ts is not None
                        and event_ts
                        >= item.created_at.replace(
                            microsecond=(item.created_at.microsecond // 1000) * 1000
                        )
                        or (
                            event_ts is None
                            and event.queued
                            and item.after_seq is not None
                        )
                    )
                ),
                None,
            )
            if match is None:
                continue
            match.state = "observed"
            match.observed_seq = event.seq
            match.observed_at = event_ts
            # The transcript carries the message, which is the strongest evidence
            # there is that the provider took the send — stronger than the call
            # returning. It can land while `provider.inject` is still awaited, and
            # `_finish_delivery` returns early for an already-observed item, so
            # announcing only from there would miss exactly this ordering (#2003).
            self._announce_handover(session_key, match)
            observed = _ObservedMessage(
                match.id,
                match.client_action_id,
                event_ts,
                _observed_event_fingerprint(event.text, event.image_media_types),
            )
            self._observed[observed_key] = observed
            self._prune_observed_correlations()
            links[event.seq] = match
            changed = True
        if changed:
            self._snapshot(session_key)
        return links

    def can_queue(self, session_key: str) -> bool:
        task = self._tasks.get(session_key)
        return session_key in self._revivals or (
            task is not None and not task.done()
        )

    async def wait_for_delivery(self, session_key: str) -> None:
        """Wait for the session's local provider-delivery task to finish.

        Callers that fence a control-plane transition can use this immediately
        after ``start``. Cancellation of the caller must not release that fence
        while the provider handoff continues in the background.
        """
        task = self._tasks.get(session_key)
        if task is None:
            return
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # More than one shutdown/cancellation request may arrive while
                # the provider is still accepting the turn. Remember all of
                # them, but do not let any release the caller's fence early.
                cancelled = True
        if not task.cancelled():
            task.result()
        if cancelled:
            raise asyncio.CancelledError

    def _begin_delivery(
        self,
        session_key: str,
        message: str,
        images: list[Path] | None = None,
        *,
        files: list[Path] | None = None,
        client_action_id: str | None = None,
        after_seq: int | None = None,
    ) -> QueuedMessage:
        """Track a provider delivery performed outside the FIFO injection loop."""
        item = QueuedMessage(
            self._next_id,
            message,
            tuple(images or []),
            tuple(files or []),
            client_action_id=client_action_id,
            state="running",
            after_seq=after_seq,
        )
        self._next_id += 1
        self._items.setdefault(session_key, []).append(item)
        self._on_sent(session_key)
        self._snapshot(session_key)
        return item

    def _note_send_failed(self, session_key: str) -> None:
        """Take back the working stamp a handoff that reached nothing put there.

        Only when nothing else is on its way. A sibling message still running,
        or accepted and waiting for its transcript event, is a real handoff, and
        this chat is genuinely working because of it — clearing on one failure
        would throw that away too (#1450).

        Call after the failing item's own state is already terminal, so the
        check below does not count it as in flight.
        """
        if any(
            item.state in ("running", "accepted")
            for item in self._items.get(session_key, [])
        ):
            return
        self._on_send_failed(session_key)

    def _action_already_handed_over(
        self, session_key: str, item: QueuedMessage
    ) -> bool:
        """Whether an earlier item already handed this exact action id over.

        `counts_as_work` is decided when a delivery is queued, but the stamp is
        applied when it runs, and those can be minutes apart. A durable retry
        queued while the first send was still in flight therefore carries a
        `True` that was true when written and false by the time it is read: the
        first send has since armed the fence, so this one gives the chat nothing.
        Asking again here, at the moment of stamping, is what keeps the stale
        answer from reaching the chat's working state (#2003).
        """
        if not item.client_action_id:
            return False
        return any(
            other is not item
            and other.client_action_id == item.client_action_id
            and other.handover_announced
            for other in self._items.get(session_key, ())
        )

    def _announce_handover(self, session_key: str, item: QueuedMessage) -> None:
        """Say once that this action id's send reached the provider (#2003).

        Whoever queued the delivery may have stopped waiting long before this —
        the Mac runner gives it ten seconds — so a durable retry cannot find this
        out by looking. It has to be pushed at the moment it becomes true.
        """
        if item.handover_announced or not item.client_action_id:
            return
        item.handover_announced = True
        self._on_handover(session_key, item.client_action_id)

    def _finish_delivery(
        self, session_key: str, item: QueuedMessage, result: InjectResult
    ) -> None:
        """Record handoff outcome without regressing an already observed item."""
        if item.state == "observed":
            item.reason = None
            # Already announced when the event was seen; this is the belt to that
            # braces, for an `observed` reached by any route this does not own.
            self._announce_handover(session_key, item)
            self._snapshot(session_key)
            return
        if result.accepted:
            if not result.transcript_expected:
                item.state = "complete"
            elif item.state != "observed":
                item.state = "accepted"
                item.accepted_at = datetime.now(UTC)
        else:
            item.state = "failed"
        item.reason = result.reason
        if item.state == "failed":
            self._note_send_failed(session_key)
        else:
            # The provider took it, so the fence behind this id is armed from here
            # on. Announced from the delivery rather than read back by the caller,
            # because this is a moment the caller may no longer be present for.
            self._announce_handover(session_key, item)
        self._snapshot(session_key)

    async def deliver_now(
        self,
        account: Account,
        session: Session,
        provider: SessionProvider,
        message: str,
        images: list[Path] | None = None,
        *,
        files: list[Path] | None = None,
        client_action_id: str | None = None,
        allow_over_budget: bool = False,
    ) -> InjectionReceipt:
        """Deliver one message to the active turn NOW, bypassing the FIFO loop.

        Steering a live turn is synchronous and must not queue, so unlike
        ``start`` this runs no background task. It owns the whole choreography the
        steer route used to hand-assemble: cursor read, optimistic-row tracking,
        the provider ``steer`` call, and upload lifecycle.
        """
        images = images or []
        files = files or []
        uploads = [*images, *files]
        item: QueuedMessage | None = None
        handoff_started = False
        try:
            _, after_seq = await provider.transcript_cursor(account, session)
            item = self._begin_delivery(
                session.key,
                message,
                images,
                files=files,
                client_action_id=client_action_id,
                after_seq=after_seq,
                )
            kwargs = {"images": images} if images else {}
            if allow_over_budget:
                kwargs["allow_over_budget"] = True
            handoff_started = True
            result = await provider.steer(account, session, message, **kwargs)
            handoff_started = False
        except BaseException as exc:  # noqa: BLE001 -- re-raised after compensation
            if item is not None:
                self._finish_delivery(
                    session.key, item, InjectResult(False, str(exc) or "steering failed")
                )
            cleanup_uploaded_files(images if handoff_started and files else uploads)
            raise
        self._finish_delivery(session.key, item, result)
        if not result.accepted:
            cleanup_uploaded_files(uploads)
        else:
            if images:
                self._defer_cleanup(
                    account,
                    provider,
                    session.session_id,
                    images,
                )
            if files:
                self._defer_cleanup(
                    account,
                    provider,
                    session.session_id,
                    files,
                    preserve_on_cancel=True,
                )
        return InjectionReceipt(result=result, item=item)

    async def start(
        self,
        account: Account,
        session: Session,
        provider: SessionProvider,
        message: str,
        images: list[Path] | None = None,
        *,
        files: list[Path] | None = None,
        effort: str | None = None,
        client_action_id: str | None = None,
        allow_over_budget: bool = False,
        counts_as_work: bool = True,
    ) -> InjectResult:
        revival = self._revivals.get(session.key)
        if revival is not None:
            await revival.wait()
        if not self.config.enabled:
            return InjectResult(False, "message injection is disabled")
        held = self.held(session.key)
        if held:
            return InjectResult(False, held)
        if Capability.INJECT not in session.capabilities and not self.can_queue(session.key):
            return InjectResult(False, "this session cannot be injected safely")
        message = message.strip()
        if not message:
            return InjectResult(False, "message is empty")
        if self.config.too_long(_submitted_message(message, files)):
            return InjectResult(False, "message is too long")
        if client_action_id is not None:
            waiting = next(
                (
                    queued
                    for queued in self._items.get(session.key, [])
                    if queued.client_action_id == client_action_id and queued.is_pending
                ),
                None,
            )
            if waiting is not None:
                # This exact action is already in this chat's queue, so queueing it
                # again would put the same message in front of the agent twice.
                # Callers with a stable action id retry on purpose — the Mac runner
                # re-posts a command's outcome every 30 seconds until the deck
                # confirms it — and a chat in a long turn does not confirm anything
                # until the turn ends. Without this the queue grew by one copy of
                # every outstanding outcome per retry, which is what the boss was
                # looking at in #2016.
                log.info(
                    "not queueing %s for %s again: it is already waiting",
                    client_action_id,
                    session.key,
                )
                return InjectResult(True)
            if self._action_was_dismissed(client_action_id):
                # Same reasoning one step later in the row's life. This action had
                # a row, the agent never wrote it down, and the person threw the
                # row away — so the message is already in front of the agent as far
                # as anything durable is concerned, and building a second row for
                # it would simply undo the dismissal (#2031).
                log.info(
                    "not queueing %s for %s again: its row was dismissed",
                    client_action_id,
                    session.key,
                )
                return InjectResult(True)
        item = QueuedMessage(
            self._next_id,
            message,
            tuple(images or []),
            tuple(files or []),
            client_action_id=client_action_id,
            effort=effort,
            allow_over_budget=allow_over_budget,
            counts_as_work=counts_as_work,
        )
        self._next_id += 1
        self._items.setdefault(session.key, []).append(item)
        self._snapshot(session.key, "queued")
        self._drain(account, session, provider)
        return InjectResult(True)

    def _drain(
        self, account: Account, session: Session, provider: SessionProvider
    ) -> None:
        """Make sure this chat's delivery loop is running for what was just queued."""
        nudge = self._queue_nudges.get(session.key)
        if nudge is not None:
            nudge.set()
        running = self._tasks.get(session.key)
        if running is None or running.done():
            task = asyncio.create_task(
                self._run(account, session, provider),
                name=f"inject:{session.key}",
            )
            self._tasks[session.key] = task

    async def resend(
        self,
        account: Account,
        session: Session,
        provider: SessionProvider,
        item_id: int,
        *,
        row_session_key: str | None = None,
    ) -> InjectResult:
        """Put one failed message back in the queue, under its own action id (#2662).

        A failed send used to be a dead end: the composer was already empty, the
        row disappeared, and the text was gone. This is the way back.

        The item keeps its ``client_action_id``, so the runtime's delivery
        receipts fence the second attempt exactly like the first: a send that
        did reach the agent replays as delivered or ``uncertain`` and is never
        written twice. That is what makes a press safe even when the first
        attempt's outcome was unclear. Uploads are not kept once a send fails,
        so an item that had them cannot come back this way.

        ``row_session_key`` is the leg the row was drawn from, when the chat has
        since rolled over to ``session``. The row lives in that leg's queue, so
        it is looked up there and moved to the leg that now does the chat's work:
        a retired leg is no place to deliver a message. The caller has already
        asked the old leg's runtime whether it holds a receipt for this action,
        because the new leg's fence cannot see one.
        """
        source_key = row_session_key or session.key
        revival = self._revivals.get(session.key)
        if revival is not None:
            await revival.wait()
        if not self.config.enabled:
            return InjectResult(False, "message injection is disabled")
        held = self.held(session.key)
        if held:
            return InjectResult(False, held)
        if Capability.INJECT not in session.capabilities and not self.can_queue(session.key):
            return InjectResult(False, "this session cannot be injected safely")
        source = self._items.get(source_key, [])
        item = next((queued for queued in source if queued.id == item_id), None)
        if item is None or item.state != "failed":
            return InjectResult(False, "this message is no longer waiting to be sent again")
        if not is_resendable(item):
            return InjectResult(
                False, "its attachments were not kept; send the message again with them"
            )
        item.state = "queued"
        item.reason = None
        item.accepted_at = None
        # A fresh try, not the automatic resend: the row must not say the first
        # send left no trace, and the resend after silence is still available.
        item.attempts = 0
        if source_key != session.key:
            source[:] = [queued for queued in source if queued is not item]
            if not source:
                self._items.pop(source_key, None)
            self._snapshot(source_key)
            self._items.setdefault(session.key, []).append(item)
        self._snapshot(session.key, "queued")
        self._drain(account, session, provider)
        return InjectResult(True)

    async def _run(
        self,
        account: Account,
        session: Session,
        provider: SessionProvider,
    ) -> None:
        uncertain_files: set[Path] = set()
        try:
            while True:
                items = self._items.get(session.key, [])
                item = next((queued for queued in items if queued.state == "queued"), None)
                if item is None:
                    break
                held = self.held(session.key)
                if held:
                    # Queued before the Work was paused: never sent, so dropping it is safe.
                    item.state = "failed"
                    item.reason = held
                    cleanup_uploaded_files([*item.images, *item.files])
                    self._snapshot(session.key)
                    continue
                transcript_offset = transcript_seq = 0
                cursor = getattr(provider, "transcript_cursor", None)
                if cursor is not None:
                    transcript_offset, transcript_seq = await cursor(account, session)
                item.after_seq = transcript_seq
                item.state = "running"
                item.attempts += 1
                if item.counts_as_work and not self._action_already_handed_over(
                    session.key, item
                ):
                    self._on_sent(session.key)
                self._snapshot(session.key)
                cleanup_files_now = True
                try:
                    kwargs = {"images": list(item.images)} if item.images else {}
                    if item.effort:
                        kwargs["effort"] = item.effort
                    if item.allow_over_budget:
                        kwargs["allow_over_budget"] = True
                    action_context = (
                        client_action_context(item.client_action_id)
                        if item.client_action_id
                        else nullcontext()
                    )
                    with action_context:
                        uncertain_files.update(item.files)
                        result = await provider.inject(
                            account,
                            session,
                            item.text,
                            timeout_s=self.config.timeout_s,
                            **kwargs,
                        )
                    if not result.accepted:
                        uncertain_files.difference_update(item.files)
                    if result.accepted and item.images:
                        await self._wait_for_durable_images(
                            account,
                            session,
                            provider,
                            item,
                            transcript_offset,
                            transcript_seq,
                        )
                    if result.accepted and item.files:
                        self._defer_cleanup(
                            account,
                            provider,
                            session.session_id,
                            list(item.files),
                            preserve_on_cancel=True,
                        )
                        uncertain_files.difference_update(item.files)
                        cleanup_files_now = False
                finally:
                    cleanup_uploaded_files(
                        [
                            *item.images,
                            *(
                                path
                                for path in item.files
                                if cleanup_files_now and path not in uncertain_files
                            ),
                        ]
                    )
                self._finish_delivery(session.key, item, result)
                # No cursor means no way to read the transcript, and the resend
                # is only ever made on transcript evidence.
                if item.state == "accepted" and cursor is not None:
                    await self._wait_for_acknowledgement(
                        account,
                        session,
                        provider,
                        item,
                        transcript_offset,
                        transcript_seq,
                    )
                if not result.accepted and item.state != "observed":
                    for pending in items:
                        if pending.state == "queued":
                            pending.state = "failed"
                            pending.reason = "not sent because the previous turn failed"
                            cleanup_uploaded_files([*pending.images, *pending.files])
                    self._snapshot(session.key)
                    break
        except asyncio.CancelledError:
            for item in self._items.get(session.key, []):
                if item.state in ("queued", "running"):
                    item.state = "failed"
                    item.reason = "injection cancelled"
                    cleanup_uploaded_files(
                        [
                            *item.images,
                            *(path for path in item.files if path not in uncertain_files),
                        ]
                    )
            self._note_send_failed(session.key)
            self._snapshot(session.key, "failed")
            raise
        except Exception as exc:  # noqa: BLE001 -- background actions must report failure
            log.exception("injection failed for %s", session.key)
            for item in self._items.get(session.key, []):
                if item.state in ("queued", "running"):
                    item.state = "failed"
                    item.reason = str(exc)
                    cleanup_uploaded_files(
                        [
                            *item.images,
                            *(path for path in item.files if path not in uncertain_files),
                        ]
                    )
            self._note_send_failed(session.key)
            self._snapshot(session.key, "failed")
        finally:
            self._tasks.pop(session.key, None)

    async def _wait_for_durable_images(
        self,
        account: Account,
        session: Session,
        provider: SessionProvider,
        item: QueuedMessage,
        byte_offset: int,
        seq: int,
    ) -> None:
        """Keep pending uploads available until their transcript event exists."""
        deadline = asyncio.get_running_loop().time() + self.config.timeout_s
        while True:
            events, byte_offset, seq = await provider.tail_transcript(
                account, session, byte_offset, seq
            )
            self.observe_transcript(session.key, events)
            if item.state == "observed":
                return
            if asyncio.get_running_loop().time() >= deadline:
                log.warning(
                    "timed out waiting for image transcript event in %s", session.key
                )
                return
            await asyncio.sleep(0.1)

    async def _wait_for_acknowledgement(
        self,
        account: Account,
        session: Session,
        provider: SessionProvider,
        item: QueuedMessage,
        byte_offset: int,
        seq: int,
    ) -> None:
        """Watch an accepted send land, and send it once more if it landed nowhere.

        Acceptance only means the provider took the message. A CLI can take one,
        write nothing, and go back to idle, and nothing errors — so the row used
        to sit there saying the agent never acknowledged it and wait for a person
        to decide whether to send it again (#1451).

        Resending on that alone would be wrong: an uncertain write turned into a
        retry is how a message gets delivered twice. So this only resends on
        positive evidence that the first send produced nothing at all — the
        transcript has not moved a single event since the pre-send cursor, the
        session has no turn running, and the message has no uploads to redo. Any
        event at all, a busy session, or a second silent attempt leaves today's
        behaviour exactly as it was: the row says so, and it is the person's call.

        The tail runs here rather than off a viewer's SSE stream on purpose —
        the transcript is only read for a session someone is watching, and a
        message that was dropped while nobody looked is the same message.
        """
        # Uploads were cleaned up once they were handed over, so a resend could
        # not carry them; a second silent attempt is not made at all, so there
        # is nothing to watch for and no reason to hold the queue another minute.
        if item.images or item.files or item.attempts >= MAX_DELIVERY_ATTEMPTS:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ACKNOWLEDGEMENT_GRACE_S
        nudge = asyncio.Event()
        self._queue_nudges[session.key] = nudge
        wrote_something = False
        try:
            while True:
                events, byte_offset, seq = await provider.tail_transcript(
                    account, session, byte_offset, seq
                )
                wrote_something = wrote_something or bool(events)
                self.observe_transcript(session.key, events)
                if item.state != "accepted":
                    return
                # Someone sent another message meanwhile: they are watching, the
                # queue must drain now, and this one keeps its manual ending.
                if nudge.is_set() or any(
                    queued.state == "queued"
                    for queued in self._items.get(session.key, [])
                ):
                    return
                if loop.time() >= deadline:
                    break
                try:
                    await asyncio.wait_for(nudge.wait(), ACKNOWLEDGEMENT_POLL_S)
                except TimeoutError:
                    pass
                else:
                    return
        finally:
            self._queue_nudges.pop(session.key, None)
        if wrote_something or self._session_busy(session.key):
            return
        log.warning(
            "resending an unacknowledged message to %s: the transcript has not "
            "moved since it was accepted",
            session.key,
        )
        item.state = "queued"
        item.accepted_at = None
        item.reason = None
        self._snapshot(session.key)

    async def start_new(
        self,
        account: Account,
        provider: SessionProvider,
        cwd: Path,
        message: str,
        images: list[Path] | None = None,
        *,
        files: list[Path] | None = None,
        model: str | None = None,
        effort: str | None = None,
        client_action_id: str | None = None,
        session_token: str | None = None,
        allow_over_budget: bool = False,
        origin: str = "new",
        source_session_key: str | None = None,
        display_message_override: str | None = None,
        fork_from: ForkFrom | None = None,
        sandbox: str | None = None,
        role: str | None = None,
        worker_key: str | None = None,
    ) -> InjectResult:
        """Start a session, optionally under a token the caller minted.

        A caller that must find this session again once it exists — the Poller Builder records
        which session is holding a run (#268) — cannot use the returned key, because there is none
        until the provider answers. Supplying the token instead gives it an exact handle: the same
        binding the injector already makes turns it into the session key as soon as one exists.

        A supplied token also gets a ``StartedChat`` record, which is what the browser can be sent
        to while the spawn runs (#765). ``origin``/``source_session_key`` say which composer asked,
        so the status strip can name the composer the message came from.
        """
        if not self.config.enabled:
            return InjectResult(False, "message injection is disabled")
        if not provider.can_start_session(account):
            return InjectResult(False, _cannot_start(provider, account))
        message = message.strip()
        if not message:
            return InjectResult(False, "message is empty")
        if self.config.too_long(_submitted_message(message, files)):
            return InjectResult(False, "message is too long")
        if not cwd.is_dir():
            return InjectResult(False, "working directory does not exist")
        running = self._new_tasks.get(account.key)
        if running is not None and not running.done():
            return InjectResult(False, "a new session is already starting for this account")
        self._new_status[account.key] = InjectionStatus("running")
        if session_token:
            self._record_start(
                StartedChat(
                    token=session_token,
                    account_key=account.key,
                    cwd=str(cwd),
                    message=display_message_override or display_message(message),
                    image_count=len(images or []),
                    model=model,
                    effort=effort,
                    origin=origin,
                    source_session_key=source_session_key,
                )
            )
        action_context = (
            client_action_context(client_action_id) if client_action_id else nullcontext()
        )
        with action_context:
            self._new_tasks[account.key] = asyncio.create_task(
                self._run_new(
                    account,
                    provider,
                    cwd,
                    message,
                    images or [],
                    files or [],
                    model,
                    effort,
                    allow_over_budget=allow_over_budget,
                    session_token=session_token,
                    fork_from=fork_from,
                    sandbox=sandbox,
                    role=role,
                    worker_key=worker_key,
                ),
                name=f"new-session:{account.key}",
            )
        return InjectResult(True)

    async def wait_for_new_start(self, account_key: str, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for this account's start in progress to finish.

        ``start_new`` refuses a second start while one runs; a caller with several to make
        in a row (the deck's Work starts, #2971) waits here rather than being refused.
        """
        running = self._new_tasks.get(account_key)
        if running is not None and not running.done():
            await asyncio.wait({running}, timeout=timeout)

    def new_start_answer(self, account_key: str) -> Awaitable[InjectResult | None]:
        """What the provider will answer the account's start in progress (#2984).

        Call it straight after an accepted ``start_new``: the start is picked now, so the answer
        is that start's even if it ends before anyone awaits it. The awaitable gives ``None``
        when the answer is unknown (it raised or was cancelled, so it may have launched); it
        waits until the spawn ends, which the provider's own timeout bounds.
        """
        running = self._new_tasks.get(account_key)

        async def answer() -> InjectResult | None:
            if running is None:
                return None
            await asyncio.wait({running})
            if running.cancelled() or running.exception() is not None:
                return None
            return running.result()

        return answer()

    async def _run_new(
        self,
        account: Account,
        provider: SessionProvider,
        cwd: Path,
        message: str,
        images: list[Path],
        files: list[Path],
        model: str | None = None,
        effort: str | None = None,
        *,
        allow_over_budget: bool = False,
        session_token: str | None = None,
        fork_from: ForkFrom | None = None,
        sandbox: str | None = None,
        role: str | None = None,
        worker_key: str | None = None,
    ) -> InjectResult | None:
        """Spawn the session; return the provider's answer, ``None`` if it raised."""
        result: InjectResult | None = None
        cleanup_images_now = True
        cleanup_files_now = True
        try:
            kwargs = {"images": images} if images else {}
            if model:
                kwargs["model"] = model
            if effort:
                kwargs["effort"] = effort
            if allow_over_budget:
                kwargs["allow_over_budget"] = True
            # Only a side chat sends these (#2485), so every other start keeps
            # calling providers exactly as before.
            if fork_from is not None:
                kwargs["fork_from"] = fork_from
            if sandbox is not None:
                kwargs["sandbox"] = sandbox
            # Only the deck's start of a Work agent names a role (#2895 step 5b).
            if role:
                kwargs["role"] = role
            # Only a start that took a spare CLI names its key (#3420).
            if worker_key:
                kwargs["worker_key"] = worker_key
            # Mint the session's identity before it exists: the token travels
            # into the instructions the provider injects, and is bound to the
            # real key as soon as the provider returns one. A caller may supply
            # its own so it can resolve the session later (#268).
            session_token = session_token or uuid.uuid4().hex
            # The durable reservation distinguishes a web restart before this
            # point (safe to resume) from one after it (provider outcome
            # uncertain, so replay must stay spent rather than duplicate).
            self._on_new_session_dispatch(session_token)
            cleanup_files_now = not bool(files)
            result = await provider.start_session(
                account,
                cwd,
                message,
                timeout_s=self.config.timeout_s,
                session_token=session_token,
                **kwargs,
            )
            cleanup_files_now = True
            if result.accepted and result.session_id:
                self._on_session_token(
                    session_token, f"{account.key}:{result.session_id}"
                )
            elif not result.accepted and not result.session_id:
                self._on_new_session_refused(session_token)
            if images and result.accepted and result.session_id:
                self._defer_cleanup(
                    account,
                    provider,
                    result.session_id,
                    images,
                )
                cleanup_images_now = False
            if files and result.accepted and result.session_id:
                self._defer_cleanup(
                    account,
                    provider,
                    result.session_id,
                    files,
                    preserve_on_cancel=True,
                )
                cleanup_files_now = False
            self._settle_new(
                account.key,
                session_token,
                InjectionStatus(
                    "complete" if result.accepted else "failed",
                    result.reason,
                    session_key=(
                        f"{account.key}:{result.session_id}"
                        if result.session_id
                        else None
                    ),
                ),
            )
        except asyncio.CancelledError:
            self._settle_new(
                account.key, session_token, InjectionStatus("failed", "session cancelled")
            )
            raise
        except Exception as exc:  # noqa: BLE001 -- background actions must report failure
            log.exception("new session failed for %s", account.key)
            self._settle_new(
                account.key, session_token, InjectionStatus("failed", str(exc))
            )
        finally:
            cleanup_uploaded_files(
                [
                    *(images if cleanup_images_now else ()),
                    *(files if cleanup_files_now else ()),
                ]
            )
            self._new_tasks.pop(account.key, None)
        return result

    def _defer_cleanup(
        self,
        account: Account,
        provider: SessionProvider,
        session_id: str,
        uploads: list[Path],
        *,
        preserve_on_cancel: bool = False,
    ) -> None:
        """Remove temporary uploads after the active turn finishes."""
        task = asyncio.create_task(
            self._wait_and_cleanup(
                account,
                provider,
                session_id,
                uploads,
                preserve_on_cancel=preserve_on_cancel,
            ),
            name=f"upload-cleanup:{session_id}",
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _wait_and_cleanup(
        self,
        account: Account,
        provider: SessionProvider,
        session_id: str,
        uploads: list[Path],
        *,
        preserve_on_cancel: bool,
    ) -> None:
        cleanup = True
        try:
            result = await provider.wait_for_session(
                account,
                session_id,
                timeout_s=self.config.timeout_s,
            )
            if preserve_on_cancel and not result.accepted:
                cleanup = False
        except asyncio.CancelledError:
            cleanup = not preserve_on_cancel
            raise
        except Exception as exc:  # noqa: BLE001 -- cleanup must outlive turn failures
            log.debug("upload cleanup wait failed for %s: %s", session_id, exc)
            cleanup = not preserve_on_cancel
        finally:
            if cleanup:
                cleanup_uploaded_files(uploads)

    async def start_delegation(
        self,
        account: Account,
        provider: SessionProvider,
        cwd: Path,
        message: str,
        *,
        sandbox: str = "workspace-write",
        model: str | None = None,
        effort: str | None = None,
        approval_policy: str = "on-request",
        parent_session_id: str | None = None,
        session_role: DelegationSessionRole = "delegated",
    ) -> tuple[InjectResult, str | None]:
        """Start tracked work and retain a pollable result.

        Machine callers keep the delegated role. A human-started coordinator may
        receive the primary role needed to run its own bounded review lanes; only
        delegated sessions are recorded in the delegated-session tree.
        """
        if not self.config.enabled:
            return InjectResult(False, "message injection is disabled"), None
        if not provider.can_start_session(account):
            return InjectResult(False, _cannot_start(provider, account)), None
        message = message.strip()
        if not message:
            return InjectResult(False, "message is empty"), None
        if self.config.too_long(message):
            return InjectResult(False, "message is too long"), None
        if not cwd.is_dir():
            return InjectResult(False, "working directory does not exist"), None
        if is_deploy_checkout(cwd):
            # A machine-dispatched agent editing the live checkout blocks every
            # unattended deploy tick and has silently reverted uncommitted files
            # there (#786). Workers belong in .worktrees/* under the repo.
            return (
                InjectResult(
                    False,
                    "refusing to delegate into the live deploy checkout — pass --cwd a"
                    " worktree under .worktrees/, or for a read-only review the review"
                    " mirror ~/.cache/agentdeck/review-mirrors/<owner>/<repo>.git",
                ),
                None,
            )
        if session_role == "delegated" and not self._delegation_ready():
            # Before the provider is asked for anything: a child started now could not
            # be recorded under the new rule. Nothing ran, so the caller may retry.
            return (
                InjectResult(
                    False, "delegation storage is not ready (cutover not recorded); retry shortly"
                ),
                None,
            )

        delegation_id = uuid.uuid4().hex
        session_token = uuid.uuid4().hex
        self._remember_delegation(
            DelegationStatus(
                delegation_id, "starting", account.key, session_token=session_token
            )
        )
        self._delegation_tasks[delegation_id] = asyncio.create_task(
            self._guard_delegation(
                delegation_id,
                self._run_delegation(
                    delegation_id,
                    account,
                    provider,
                    cwd,
                    message,
                    sandbox,
                    model,
                    effort,
                    approval_policy,
                    parent_session_id,
                    session_role,
                    session_token,
                ),
            ),
            name=f"delegation:{delegation_id}",
        )
        return InjectResult(True), delegation_id

    async def _run_delegation(
        self,
        delegation_id: str,
        account: Account,
        provider: SessionProvider,
        cwd: Path,
        message: str,
        sandbox: str,
        model: str | None,
        effort: str | None,
        approval_policy: str = "on-request",
        parent_session_id: str | None = None,
        session_role: DelegationSessionRole = "delegated",
        session_token: str | None = None,
    ) -> None:
        session_token = session_token or uuid.uuid4().hex
        started = await provider.start_session(
            account,
            cwd,
            message,
            timeout_s=self.config.timeout_s,
            sandbox=sandbox,
            model=model,
            effort=effort,
            approval_policy=approval_policy,
            delegated=session_role == "delegated",
            session_token=session_token,
        )
        if not started.accepted or not started.session_id:
            self._remember_delegation(
                DelegationStatus(
                    delegation_id,
                    "failed",
                    account.key,
                    reason=started.reason or "Codex did not return a session id",
                )
            )
            return
        session_key = f"{account.key}:{started.session_id}"
        self._on_session_token(session_token, session_key)
        if session_role == "delegated":
            self._on_delegation_started(session_key, parent_session_id)
        self._remember_delegation(
            DelegationStatus(
                delegation_id,
                "running",
                account.key,
                session_key=session_key,
            )
        )
        # Only the *startup* handshake above is a message-injection-shaped
        # wait. This one spans the delegated subagent's entire task, so it
        # runs on the delegation clock instead of the injection one.
        await self._watch_delegation(
            delegation_id,
            account,
            provider,
            started.session_id,
            timeout_s=self.config.delegation_timeout_s,
        )

    async def _watch_delegation(
        self,
        delegation_id: str,
        account: Account,
        provider: SessionProvider,
        session_id: str,
        *,
        timeout_s: float,
    ) -> None:
        """Wait for a started child to finish and record its final message.

        Only ever waits and reads, so a web process taking over another's
        delegation can run it without sending or starting anything (#2582).
        """
        session_key = f"{account.key}:{session_id}"
        completed = await provider.wait_for_session(
            account,
            session_id,
            timeout_s=timeout_s,
        )
        if not completed.accepted:
            self._remember_delegation(
                DelegationStatus(
                    delegation_id,
                    "failed",
                    account.key,
                    session_key=session_key,
                    reason=completed.reason or "Codex delegation failed",
                )
            )
            return
        final_message = None
        for _ in range(50):
            final_message = await provider.session_result(account, session_id)
            if final_message:
                break
            await asyncio.sleep(0.2)
        if not final_message:
            self._remember_delegation(
                DelegationStatus(
                    delegation_id,
                    "failed",
                    account.key,
                    session_key=session_key,
                    reason="Codex completed but its final message was unavailable",
                )
            )
            return
        self._remember_delegation(
            DelegationStatus(
                delegation_id,
                "complete",
                account.key,
                session_key=session_key,
                final_message=final_message,
            )
        )

    async def _guard_delegation(
        self, delegation_id: str, work: Coroutine[Any, Any, None]
    ) -> None:
        """Run one delegation's start or watch, keeping any failure for its caller."""
        try:
            await work
        except asyncio.CancelledError:
            # Only stop() cancels these, and that is the web process going away while
            # the child goes on in the runtime. The saved record stays unfinished so the
            # next web process takes the wait over; only this process's own copy says
            # it stopped (#2582).
            current = self._delegations.get(delegation_id)
            if current is not None:
                self._delegations[delegation_id] = replace(
                    current, state="failed", reason="delegation cancelled"
                )
            raise
        except Exception as exc:  # noqa: BLE001 -- retain failure for the caller
            log.exception("delegation failed for %s", delegation_id)
            current = self._delegations.get(delegation_id)
            if current is not None:
                self._remember_delegation(
                    replace(current, state="failed", reason=str(exc), final_message=None)
                )
        finally:
            self._delegation_tasks.pop(delegation_id, None)

    def start_delegation_takeover(
        self,
        accounts: Callable[[], Iterable[Account]],
        providers: Mapping[str, SessionProvider],
    ) -> None:
        """Keep picking up delegations whose web process exited before they finished."""
        if self._delegation_takeover is None or self._delegation_takeover.done():
            self._delegation_takeover = asyncio.create_task(
                self._take_over_delegations_forever(accounts, providers),
                name="delegation-takeover",
            )

    async def _take_over_delegations_forever(
        self,
        accounts: Callable[[], Iterable[Account]],
        providers: Mapping[str, SessionProvider],
    ) -> None:
        """Take over when a process that could own a delegation exits (#2865).

        A delegation is orphaned only by its owner's exit, so this waits on that exit
        (a pidfd per process) rather than looking every few seconds. Watched: the owner
        of every unfinished delegation, and every sibling web worker under the same
        supervisor — during a deploy the old worker can still start a delegation after
        this one's first pass, and its exit is what orphans it. A process that cannot be
        watched falls back to the old cadence.
        """
        self._delegation_store.prune_delegations(time.time() - DELEGATION_RETENTION_S)
        wake = asyncio.Event()
        exits = pid_watch.ExitWatches(lambda _pid: wake.set())
        try:
            while True:
                wake.clear()
                # A pass that raised, a refused claim, or an owner that died between the
                # pass and the watch leaves a delegation no exit will ever announce: keep
                # the old cadence until none is left (#2865).
                retry = True
                try:
                    self.take_over_delegations(accounts(), providers)
                    exits.watch(self._delegation_owner_pids() | _sibling_pids())
                    retry = self._orphaned_delegations()
                except Exception:  # noqa: BLE001 -- one bad row must not end the loop
                    log.exception("delegation takeover pass failed")
                if retry or exits.unwatched:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(wake.wait(), DELEGATION_TAKEOVER_INTERVAL_S)
                else:
                    await wake.wait()
        finally:
            exits.close()

    def _orphaned_delegations(self) -> bool:
        """Whether an unfinished delegation's owner is gone and this process has not
        taken it."""
        me = os.getpid()
        return any(
            record.get("owner_pid") != me
            and str(record["id"]) not in self._delegation_tasks
            and not _process_alive(record.get("owner_pid"))
            for record in self._delegation_store.unfinished_delegations()
        )

    def _delegation_owner_pids(self) -> set[int]:
        """Other processes that own an unfinished delegation this one has not taken."""
        me = os.getpid()
        owners = set()
        for record in self._delegation_store.unfinished_delegations():
            owner = record.get("owner_pid")
            # A recycled pid is no owner: the pass already took its delegations over.
            if isinstance(owner, int) and owner != me and _process_alive(owner):
                owners.add(owner)
        return owners

    def take_over_delegations(
        self, accounts: Iterable[Account], providers: Mapping[str, SessionProvider]
    ) -> None:
        """Watch every unfinished delegation whose watcher process has exited (#2582).

        A deploy runs the old and new web processes side by side for a while, so a
        delegation is taken over only once its owner is gone, never at startup.
        """
        me = os.getpid()
        by_key = {account.key: account for account in accounts}
        for record in self._delegation_store.unfinished_delegations():
            delegation_id = str(record["id"])
            owner = record.get("owner_pid")
            if owner == me or delegation_id in self._delegation_tasks or _process_alive(owner):
                continue
            if not self._delegation_store.claim_delegation(delegation_id, owner, me):
                continue
            status = DelegationStatus.from_record(record)
            self._delegations.pop(delegation_id, None)
            account = by_key.get(status.account_key)
            if account is None or account.provider_id not in providers:
                self._remember_delegation(
                    replace(status, state="failed", reason="account no longer configured")
                )
                continue
            session_key = status.session_key
            if session_key is None and status.session_token:
                session_key = self._session_key_for_token(status.session_token)
            prefix = f"{account.key}:"
            if session_key is None or not session_key.startswith(prefix):
                # Never start it again: the first start may have reached the provider,
                # and a second child is the duplicate delivery the deck must not make.
                self._remember_delegation(
                    replace(
                        status,
                        state="failed",
                        reason=(
                            "the web process restarted while this chat was starting;"
                            " look for it on the deck before relaunching"
                        ),
                    )
                )
                continue
            status = replace(status, state="running", session_key=session_key)
            self._remember_delegation(status)
            spent = time.time() - (status.created_at or time.time())
            remaining = max(
                self.config.delegation_timeout_s - spent, DELEGATION_RESUME_FLOOR_S
            )
            log.info("taking over delegation %s from process %s", delegation_id, owner)
            self._delegation_tasks[delegation_id] = asyncio.create_task(
                self._guard_delegation(
                    delegation_id,
                    self._watch_delegation(
                        delegation_id,
                        account,
                        providers[account.provider_id],
                        session_key.removeprefix(prefix),
                        timeout_s=remaining,
                    ),
                ),
                name=f"delegation:{delegation_id}",
            )

    async def stop(self) -> None:
        if self._delegation_takeover is not None:
            self._delegation_takeover.cancel()
            await asyncio.gather(self._delegation_takeover, return_exceptions=True)
            self._delegation_takeover = None
        tasks = [
            *self._tasks.values(),
            *self._new_tasks.values(),
            *self._delegation_tasks.values(),
            *self._cleanup_tasks,
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._new_tasks.clear()
        self._delegation_tasks.clear()
        self._cleanup_tasks.clear()

    def new_status(self, account_key: str) -> InjectionStatus | None:
        return self._new_status.get(account_key)

    def started_chat(self, token: str) -> StartedChat | None:
        """The start a deck-minted token names, while it is still worth showing."""
        return self._starts.get(token)

    def _record_start(self, start: StartedChat) -> None:
        self._starts[start.token] = start
        while len(self._starts) > self._MAX_STARTS:
            self._starts.pop(next(iter(self._starts)))

    def _settle_new(
        self, account_key: str, session_token: str | None, status: InjectionStatus
    ) -> None:
        """Publish one start's outcome to both readers of it.

        The per-account status is what the composer polls; the per-token record is what a
        status strip polls. They must not drift, so nothing writes only one of them.
        """
        self._new_status[account_key] = status
        start = self._starts.get(session_token) if session_token else None
        if start is not None:
            start.status = status

    def delegation_status(self, delegation_id: str) -> DelegationStatus | None:
        # The saved record first: during a deploy another web process may be the one
        # watching this delegation, and only the store sees what it wrote (#2582).
        record = self._delegation_store.load_delegation(delegation_id)
        if record is not None:
            return DelegationStatus.from_record(record)
        return self._delegations.get(delegation_id)

    def _remember_delegation(self, status: DelegationStatus) -> None:
        previous = self._delegations.pop(status.id, None)
        status = replace(
            status,
            session_token=status.session_token
            or (previous.session_token if previous is not None else None),
            created_at=status.created_at
            or (previous.created_at if previous is not None else None)
            or time.time(),
        )
        self._delegations[status.id] = status
        while len(self._delegations) > self._MAX_STATUSES:
            self._delegations.pop(next(iter(self._delegations)))
        try:
            self._delegation_store.save_delegation({**asdict(status), "owner_pid": os.getpid()})
        except Exception:  # noqa: BLE001 -- losing durability must not fail the delegation
            log.exception("could not save delegation %s", status.id)

    def _remember(self, session_key: str, status: InjectionStatus) -> None:
        self._status.pop(session_key, None)
        self._status[session_key] = status
        while len(self._status) > self._MAX_STATUSES:
            retired_key = next(iter(self._status))
            self._status.pop(retired_key)
            self._prune_history(retired_key, keep=0)

    def _prune_history(self, session_key: str, *, keep: int) -> None:
        """Forget terminal rows outside the UI window, never pending work.

        Observed rows have already moved their browser identity into
        `_observed`, so the heavy QueuedMessage is disposable alongside failures
        and completions. Mutate the list in place: a running delivery loop may
        still hold it, while callers such as the scheduler retain their own item
        reference.
        """
        items = self._items.get(session_key)
        if items is None or len(items) <= keep:
            return
        cutoff = len(items) - keep
        items[:] = [
            item for index, item in enumerate(items)
            if index >= cutoff or item.state not in ("complete", "failed", "observed")
        ]
        if not items:
            self._items.pop(session_key, None)

    def _snapshot(self, session_key: str, fallback: str = "complete") -> None:
        self._prune_history(session_key, keep=self._HISTORY_ITEMS)
        items = self._items.get(session_key, [])
        active = next((item for item in items if item.state == "running"), None)
        queued = any(item.state == "queued" for item in items)
        last = items[-1] if items else None
        if active is not None:
            state = "running"
        elif queued:
            state = "queued"
        elif last is not None:
            state = last.state
        else:
            state = fallback
        reason = last.reason if last is not None and last.state == "failed" else None
        self._remember(
            session_key,
            InjectionStatus(state, reason, tuple(items[-self._HISTORY_ITEMS:])),
        )
        self._on_change(session_key)

