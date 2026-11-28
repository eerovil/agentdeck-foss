"""Keep a waiting Work chat's prompt cache warm (#4737).

A chat that waits on the deck (a wake-on, a queued job, a review, the merge queue) wakes to a
cold cache once the wait outlasts the cache's one hour, and pays to write its whole context
again. A call that reads the cache renews it, so the deck sends such a chat one short ping
before the hour is up, for up to four hours after its last real turn.

What makes a ping safe to send:

* It may resume a chat whose process was stopped (#4752): a resumed chat still reads its
  cache inside the hour, and on an account with few places an idle process is stopped within
  minutes, so a ping that needed a running one mostly never went. The runtime decides,
  under the chat's lock: the ping joins no turn, starts no new chat, takes a free place or
  one an idle chat gives up, and is refused when the chat moved on since it was judged here
  or when its next start would build a prompt no cache holds.
* It is sent at most once per cache lifetime. The row stored for it, under an id made of the
  chat and the call whose cache it renews, is written before the send and never retried.
* It goes around the message queue. Nothing waits on it, nothing reports it, and a chat with
  anything else on its way gets no ping: that message warms the cache itself.
* A ping that found the cache already gone ends the series. Each further one would pay for
  a full write, which is the cost the ping exists to avoid.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from .deck_notices import KEEP_WARM_TEXT
from .models import Session, SessionStatus
from .wake_on import KIND_NEEDS_YOU, KIND_TIME, KIND_WATCH

logger = logging.getLogger(__name__)

#: The only cache worth a ping: a five-minute one is gone long before any wait ends.
CACHE_LIFETIME = timedelta(hours=1)
#: A ping goes this long after the call whose cache it renews, and no later than
#: ``PING_BEFORE``: past that the pass that would send it may land after the hour.
PING_AFTER = timedelta(minutes=45)
PING_BEFORE = timedelta(minutes=50)
#: How long after a chat's last real turn its cache is kept warm.
KEEP_FOR = timedelta(hours=4)
#: How soon after a ping the Work pass looks again: by then its answer is in the transcript,
#: and the answer's own call opens the next window.
LOOK_AGAIN = timedelta(minutes=2)
#: A chat whose only wait is a time set further than this after its last real turn gets no
#: ping (#4752): the last ping goes within ``KEEP_FOR`` of that turn and its cache lasts one
#: lifetime more, so that wake is cold whatever was sent before it.
FAR_WAIT = KEEP_FOR + CACHE_LIFETIME

ID_PREFIX = "keep-warm:"

#: The derived states in which a Work waits on the deck, which wakes its chat by itself.
DECK_WAIT_KINDS = frozenset({"waiting", "review", "review_queued", "queued"})
#: The states that say nothing about what a chat waits on: its Work is over.
_OVER_KINDS = frozenset({"closed", "finished", "scheduled"})


def message_id(chat_id: str, at: datetime) -> str:
    """The one row a ping renewing the cache of the call at ``at`` is stored under."""
    return f"{ID_PREFIX}{chat_id}:{int(at.timestamp())}"


def waiting_chats(cards: Iterable[Any], owner: Callable[[str], str | None]) -> tuple[str, ...]:
    """The chats that wait on the deck and on nothing else, in card order.

    ``owner`` names the chat that owns a Work. A chat owning several is pinged only when one
    of them waits on the deck and none waits on a person: a paused Work, a question, a
    review handed to the owner or a turn that just ended is a person's, and so is every state
    not named here.
    """
    waits: dict[str, bool] = {}
    for card in cards:
        kind = getattr(getattr(card, "derived", None), "kind", None)
        if kind is None or kind in _OVER_KINDS:
            continue
        chat = owner(card.work_id)
        if not chat:
            continue
        on_deck = kind in DECK_WAIT_KINDS and not getattr(card, "paused", False)
        waits[chat] = waits.get(chat, True) and on_deck
    return tuple(chat for chat, on_deck in waits.items() if on_deck)


def woken_by_more_than_a_wait(cards: Iterable[Any], owner: Callable[[str], str | None]) -> set[str]:
    """The chats the deck may wake for something no registered wait names: a review, a
    place in the merge queue, a test run or another queued job. Such a chat is pinged
    whatever its waits say."""
    chats: set[str] = set()
    for card in cards:
        kind = getattr(getattr(card, "derived", None), "kind", None)
        tests = getattr(getattr(card, "verification", None), "status", "")
        running = tests in {"queued", "running"} or bool(getattr(card, "jobs", ()))
        if (kind in DECK_WAIT_KINDS and kind != "waiting") or running:
            chat = owner(card.work_id)
            if chat:
                chats.add(chat)
    return chats


def waits_only_on_a_far_time(waits: Iterable[Any], last_turn: datetime) -> bool:
    """Whether all a chat waits on is a clock time more than ``FAR_WAIT`` after its last
    real turn. Counted from that turn, not from now: the pings stop ``KEEP_FOR`` after it,
    so a wait three hours away is as cold as one next week when the turn was hours ago.

    A time wait with a check may end at any run of it, and any other kind at any event, so
    one of either keeps the pings. A watch and a needs-you wait keep no chat waiting.
    """
    kept = [wait for wait in waits if wait.kind not in {KIND_WATCH, KIND_NEEDS_YOU}]
    horizon = (last_turn + FAR_WAIT).timestamp()
    return bool(kept) and all(
        wait.kind == KIND_TIME and not wait.check_command and wait.deadline > horizon
        for wait in kept
    )


def window(session: Session) -> tuple[datetime, datetime] | None:
    """When this chat's next ping may go, as (from, before); None when it gets none.

    Counted from the time of the chat's newest model call, the one whose cache a ping would
    renew, never from a computed expiry.
    """
    call = session.cache_call
    last = session.last_message_at
    if call is None or last is None or call.lifetime != CACHE_LIFETIME:
        return None
    if session.status is SessionStatus.REMOTE or session.thinking or session.question:
        return None  # a stopped process is no reason: the runtime may resume it (#4752)
    if session.turn_failed or session.turn_cut_off or session.automatic_retry_at is not None:
        return None  # stopped by a failure or its account's limit: its wake is the retry
    if session.keep_warm_turn and call.missed:
        return None  # the last ping wrote the cache anew: the series is over
    opens = call.at + PING_AFTER
    if opens >= last + KEEP_FOR:
        return None
    return opens, call.at + PING_BEFORE


def at_limit(usage: Any) -> bool:
    """Whether an account's last usage reading says a window is used up, or there is none.

    No reading means no ping: a ping is a saving, and an account whose state is not known
    is not one to spend on. A reading with no numeric meter, and one marked stale after a
    failed read, are not known either.
    """
    if usage is None or getattr(usage, "stale", False):
        return True  # no reading, or an old one kept after a failed read
    meters = usage.percentages()
    return not meters or max(meters) >= 100.0


def has_pending(status: Any) -> bool:
    """Whether a chat's message queue holds anything still on its way.

    A queue's status keeps its finished rows too, so a chat that was ever sent a message
    has items for good: only a pending one says a message is coming.
    """
    return any(item.is_pending for item in getattr(status, "items", None) or ())


class KeepWarmService:
    """Sends the pings a Work pass finds due, and says when the next one falls due."""

    def __init__(
        self,
        store: Any,
        resolve_target: Callable[[str], Any],
        *,
        queued: Callable[[str], bool],
        at_limit: Callable[[str], bool],
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._resolve_target = resolve_target
        self._queued = queued
        self._at_limit = at_limit
        self._on_change = on_change or (lambda _chat_id: None)
        self._tasks: set[asyncio.Task] = set()
        self._ended: set[str] = set()
        #: Whether the last pass that judged left a window to come back for.
        self.owed = False

    def tend(self, chats: Collection[str], now: datetime) -> datetime | None:
        """Ping each chat whose window is open; when the next pass is owed, if any."""
        next_at: datetime | None = None
        for chat_id in chats:
            try:
                target = self._resolve_target(chat_id)
                if target is None:
                    continue
                session = target.session
                span = window(session)
                self._note_series_end(chat_id, session)
                if span is None:
                    continue
                opens, closes = span
                if now < opens:
                    next_at = opens if next_at is None else min(next_at, opens)
                elif now < closes:
                    self._ping(chat_id, target)
                    # Look again once the answer is in: its call opens the next window, and
                    # no pass may come by itself for a chat nobody is watching.
                    again = now + LOOK_AGAIN
                    next_at = again if next_at is None else min(next_at, again)
            except Exception:  # noqa: BLE001 -- a ping is never worth a failed pass
                logger.warning("keep-warm check of %s failed", chat_id, exc_info=True)
        self.owed = next_at is not None
        return next_at

    def last_turn(self, chat_id: str) -> datetime | None:
        """When this chat's last real turn was; None for a chat that is not there."""
        target = self._resolve_target(chat_id)
        return target.session.last_message_at if target is not None else None

    def _note_series_end(self, chat_id: str, session: Session) -> None:
        """Log once when a ping came too late: the pings stop until the next real turn."""
        call = session.cache_call
        if session.keep_warm_turn and call is not None and call.missed:
            if chat_id not in self._ended:
                self._ended.add(chat_id)
                logger.info(
                    "keep-warm ping of %s found its cache gone (read %d, wrote %d);"
                    " no more pings until its next real turn",
                    chat_id,
                    call.read,
                    call.written,
                )
        else:
            self._ended.discard(chat_id)

    def _ping(self, chat_id: str, target: Any) -> None:
        session = target.session
        if self._at_limit(session.account_key) or self._queued(session.key):
            return
        if self._store.chat_has_undelivered_message(chat_id):
            return  # a real message is on its way, and warms the cache itself
        key = message_id(chat_id, session.cache_call.at)
        # Before the send, and only once: a pass repeated or a crash in between sends nothing.
        if not self._store.record_keep_warm(key, chat_id, KEEP_WARM_TEXT):
            return
        task = asyncio.get_running_loop().create_task(
            self._send(key, chat_id, target), name=f"keep-warm:{chat_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, key: str, chat_id: str, target: Any) -> None:
        try:
            result = await target.provider.keep_warm(
                target.account, target.session, KEEP_WARM_TEXT, delivery_id=key
            )
            reason = None if result.accepted else (result.reason or "refused")
        except Exception as exc:  # noqa: BLE001 -- never retried: the row stays the record
            reason = f"{type(exc).__name__}: {exc}"
        if reason is not None:
            self._store.fail_keep_warm(key, reason)
            logger.info("keep-warm ping of %s was not sent: %s", chat_id, reason)
            return
        logger.info("keep-warm ping sent to %s", chat_id)
        self._on_change(chat_id)


def tend_waiting_chats(
    app: Any, cards: Sequence[Any], *, degraded: Sequence[str] = (), now: datetime
) -> datetime | None:
    """One Work pass's keep-warm step: ping what is due, and return the next due moment.

    Nothing on a pass that failed a read (``degraded``), as ``ask_for_next_steps``: a job
    queue that did not answer shows a waiting Work as ended, and the reverse.
    """
    service = getattr(app.state, "keep_warm", None)
    if service is None:
        return None
    if degraded:
        # Not judged, and not forgotten: a window may open before the next pass comes.
        return now + LOOK_AGAIN if service.owed else None
    from .work import WorkRef
    from .work_router import owner_of_issue

    state = app.state.app_state
    projection = state.work_projection()

    def owner(work_id: str) -> str | None:
        ref = WorkRef.from_work_id(work_id)
        found = (
            owner_of_issue(state, ref.repository, ref.number, projection)
            if ref is not None
            else None
        )
        return found[0] if found else None

    registry = getattr(getattr(app.state, "wake_on", None), "registry", None)
    sooner = woken_by_more_than_a_wait(cards, owner)
    chats = [
        chat
        for chat in waiting_chats(cards, owner)
        if registry is None
        or chat in sooner
        or not waits_only_on_a_far_time(registry.for_chat(chat), service.last_turn(chat) or now)
    ]
    return service.tend(chats, now)
