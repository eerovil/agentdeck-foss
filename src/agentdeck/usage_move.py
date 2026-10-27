"""Move a Work's chat to another account when its usage limit stops it (#3362).

The reset retry (#3305) waits for the full account to come back. A Work's agent need not:
its project already lists the accounts its Works may run on, so when the limit stops a
turn and the reset is far off, the chat continues on the next listed account with room,
the way the chat page's Continue in new chat does. The new leg joins the same chat, so
the Work, its pull requests and its wake-ons follow it, and the reset retry is cancelled
on its own once the chat's working leg has moved.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .models import Account, Session
from .quota_retry import message_id_for

log = logging.getLogger(__name__)

#: A reset nearer than this is waited for: the new chat reads the whole transcript again,
#: which costs more than a short wait.
MIN_WAIT = timedelta(minutes=30)
#: How long a move that proved it started nothing waits before it is tried again.
RETRY_AFTER = timedelta(minutes=1)

#: What one attempt came to: a chat started, nothing started (try again), or unknown.
STARTED, RETRY, SPENT = "started", "retry", "spent"


@dataclass(frozen=True)
class Move:
    """One chat the limit stopped, and the account it continues on."""

    session: Session
    account: Account
    token: str
    message: str


def move_token(session: Session) -> str | None:
    """The one session token this limit stop's move may use, or None.

    Keyed like the reset retry (session key and reset time), so a restart reaches the same
    reservation and never starts a second chat.
    """
    retry_id = message_id_for(session)
    if retry_id is None:
        return None
    return hashlib.sha256(f"usage-move\n{retry_id}".encode()).hexdigest()[:32]


def pick_account(
    candidates: Iterable[Account],
    current: str,
    usage_cache: Path | None,
    planned_resets: Mapping[str, datetime] | None = None,
) -> Account | None:
    """The listed account to move to, or None when none of the others has room now."""
    from .delivery_scheduling import choose_account
    from .poller_engine import github as usage

    others = {account.key: account for account in candidates if account.key != current}
    if not others:
        return None
    notes: list[str] = []
    chosen = choose_account(
        list(others), usage_cache, {}, notes, cold=True, planned_resets=planned_resets
    )
    for note in notes:
        log.info("usage move: %s", note)
    # ``choose_account`` still names the account that clears first when all refuse; a move
    # there would only stop again, so the reset retry is the better wait.
    if not chosen or usage.account_refusal_detail(usage_cache, chosen)[0]:
        return None
    return others[chosen]


def first_message(transcript: str, source: str, reset: datetime, target: Account) -> str:
    """The new leg's first message: why it moved, then the transcript continuation."""
    from .web.chat_successor import transcript_continuation

    label = source.partition(":")[2] or source
    clock = reset.astimezone().strftime("%H:%M")
    return (
        f"The previous account ({label}) reached its usage limit until {clock}. "
        f"AgentDeck moved this Work's chat to {target.key}.\n\n"
        f"{transcript_continuation(transcript)}"
    )


class UsageMoveService:
    """Watch normalized sessions and move each limit-stopped Work chat at most once."""

    def __init__(
        self,
        sessions: Callable[[], Iterable[Session]],
        current_key: Callable[[str], str],
        work_repository: Callable[[Session], str | None],
        accounts_for: Callable[[str], list[Account] | None],
        usage_cache: Callable[[], Path | None],
        db: Any,
        start: Callable[[Move], Awaitable[str]],
        transcript_url: Callable[[str], str],
        *,
        bus=None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._current_key = current_key
        self._work_repository = work_repository
        self._accounts_for = accounts_for
        self._usage_cache = usage_cache
        self._db = db
        self._start = start
        self._transcript_url = transcript_url
        self._bus = bus
        self._clock = clock
        # When the last pass decided: a retry due by then has had its chance.
        self._last_pass: datetime | None = None
        self._task: asyncio.Task | None = None
        self._skipped: set[str] = set()
        self._retry_after: dict[str, datetime] = {}

    def _decide(self, session: Session, now: datetime) -> Move | None:
        reset = session.automatic_retry_at
        token = move_token(session)
        if token is None or reset is None or token in self._skipped:
            return None
        if now < self._retry_after.get(token, now):
            return None
        if session.owned_role != "work" or session.cwd is None or not session.cwd.is_dir():
            return None
        if self._current_key(session.key) != session.key:
            return None
        if reset - now < MIN_WAIT:
            return None
        repository = self._work_repository(session)
        if repository is None:
            return None
        candidates = self._accounts_for(repository)
        if not candidates:
            return None
        account = pick_account(
            candidates, session.account_key, self._usage_cache(), self._db.planned_resets()
        )
        if account is None:
            # Said once per limit stop; the next stop asks again.
            self._skipped.add(token)
            log.info(
                "usage move: %s waits for its reset at %s: no other listed account has room",
                session.key,
                reset.isoformat(),
            )
            return None
        message = first_message(
            self._transcript_url(session.key), session.account_key, reset, account
        )
        return Move(session, account, token, message)

    async def reconcile(self, *, now: datetime | None = None) -> int:
        """Move every newly stopped Work chat that can move; return how many started."""
        now = now or self._clock()
        self._last_pass = now
        moves = []
        for session in tuple(self._sessions()):
            move = self._decide(session, now)
            if move is None:
                continue
            # At most once. A ``reserved`` row is an attempt that never reached the provider
            # (``start_moved_leg`` marks it dispatched only at the start itself), so it is
            # safe to resume; a ``dispatched`` one may have started and is never repeated. An
            # unreadable store decides nothing, so the next pass asks again.
            reservation = self._db.reserve_continued_chat_start(move.token, session.key)
            if reservation is None:
                continue
            self._skipped.add(move.token)
            if reservation in ("new", "reserved"):
                moves.append(move)
        outcomes = await asyncio.gather(*(self._attempt(move) for move in moves))
        return outcomes.count(STARTED)

    async def _attempt(self, move: Move) -> str:
        try:
            outcome = await self._start(move)
        except Exception:  # noqa: BLE001 - the start may have begun; never repeat it
            log.exception("usage move: %s did not report its start", move.session.key)
            return SPENT
        if outcome == STARTED:
            log.info(
                "usage move: %s stopped on its usage limit; continued on %s",
                move.session.key,
                move.account.key,
            )
        elif outcome == RETRY:
            # Nothing started, so the same limit stop may still move, a little later.
            self._skipped.discard(move.token)
            # Timed from the refusal itself: a slow one must not leave a deadline already past.
            self._retry_after[move.token] = self._clock() + RETRY_AFTER
        return outcome

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="usage-moves")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _until_retry(self) -> float | None:
        """Seconds until the earliest pending retry (0 when one is overdue), or None.

        A retry due by the last pass's decision had its chance there (or its chat has moved
        on), so it is dropped rather than woken for again. One that fell due while that pass
        was still waiting on a start has not, so it is kept and woken for at once.
        """
        tried = self._last_pass
        self._retry_after = {
            token: at
            for token, at in self._retry_after.items()
            if token not in self._skipped and (tried is None or at > tried)
        }
        due = self._retry_after.values()
        return max((min(due) - self._clock()).total_seconds(), 0.0) if due else None

    async def _run(self) -> None:
        sessions = self._bus.subscribe("sessions") if self._bus is not None else None
        try:
            while True:
                try:
                    await self.reconcile()
                except Exception:  # noqa: BLE001 - one bad pass must not end the watch
                    log.exception("usage move: a pass failed")
                if sessions is None:
                    return
                # A move that started nothing is due again at its own time, whether or not
                # any chat changes before then.
                try:
                    await asyncio.wait_for(sessions.get(), timeout=self._until_retry())
                except TimeoutError:
                    continue
                while sessions.get_nowait() is not None:
                    pass
        finally:
            if sessions is not None:
                sessions.close()


def transcript_url(base_url: str, session_key: str) -> str:
    """The loopback transcript a moved chat reads, as the Continue button links it."""
    return f"{base_url.rstrip('/')}/sessions/{quote(session_key, safe=':')}/transcript.json"


async def start_moved_leg(app: Any, move: Move) -> str:
    """Start ``move``'s new leg as the next leg of the same chat.

    ``STARTED`` once the provider started it (or may have), ``RETRY`` when it is proven that
    nothing started and both fences are free again, ``SPENT`` when the outcome is unknown.
    """
    from .providers import PROVIDERS
    from .web.chat_successor import reserve_successor

    state = app.state.app_state
    injector = app.state.injector
    db = app.state.db
    session, account = move.session, move.account
    provider = PROVIDERS[account.provider_id]
    source = state.chat_newest_leg_key(session.key)
    reservation = reserve_successor(state, lambda: injector, source, move.token)
    if reservation not in ("new", "reserved"):
        log.warning("usage move: %s could not reserve its next leg: %s", session.key, reservation)
        return RETRY

    def nothing_started(why: str | None) -> str:
        state.release_chat_successor(move.token)
        db.release_continued_chat_start(move.token)
        log.warning("usage move: %s was refused on %s: %s", session.key, account.key, why)
        return RETRY

    # From here the start may write, so a later pass or a restart must not repeat it. A fence
    # that did not land is no fence: start nothing.
    if not db.mark_continued_chat_dispatched(move.token):
        state.release_chat_successor(move.token)
        log.warning("usage move: %s could not record its start; not starting", session.key)
        return RETRY
    # The account's saved new-chat defaults, as a Continue with nothing picked uses.
    stored = db.load_account_new_chat_defaults(account.key)
    model, effort = stored.get("model"), stored.get("effort")
    result = await injector.start_new(
        account,
        provider,
        Path(session.cwd),
        move.message,
        model=model if model and provider.is_valid_model(model) else None,
        effort=effort if effort and provider.is_valid_effort(effort) else None,
        session_token=move.token,
        allow_over_budget=True,
        origin="continue",
        source_session_key=session.key,
        role="work",
    )
    if not result.accepted:
        return nothing_started(result.reason)
    # The queued spawn answers later; a clean refusal there (``at_capacity``) started nothing
    # either, as Work start reads it (#2984). An unknown answer may have launched.
    answer = await injector.new_start_answer(account.key)
    if answer is not None and not answer.accepted and not answer.session_id:
        return nothing_started(answer.reason)
    return STARTED
