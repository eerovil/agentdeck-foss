"""Per-session attention triage for the dashboard.

Deckhand answers one question for the operator: **which agents need me now?**
Runtime signals, session-authored reports, and safe finished-turn fallbacks are
decided deterministically in ``triage``. The service here orchestrates that loop
and owns all mutable/persisted state; ``triage`` stays pure.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from . import poller_ops
from .config import AppConfig
from .git_context import GitContext, GitContextResolver, GitContexts
from .models import (
    IssueRef,
    PendingInteraction,
    Session,
)
from .providers import pending_interaction_for
from .state import AppState
from .triage import (
    CAUSE_PENDING_INTERACTION,
    AssistantInsight,
    AssistantView,
    DeckhandStatus,
    WorkItemPill,
    blocked_issue_handoff,
    card_priority,
    pill_review_commit,
    resolve_deckhand_status,
    resolve_work_item_pill,
    stopped_card,
)

log = logging.getLogger(__name__)

# v11 keeps only what cannot be read back from the deck (#2599): the git contexts
# behind the pull request labels. Whether a chat needs attention is worked out again
# on every pass. A v10 payload contributes the same field; everything else an older
# payload holds (its inbox, view, working set, last look, and the push debt Deckhand
# kept before #3348) is ignored.
_CHECKPOINT_VERSION = 11
# Normal startups still wait for a coherent deck, but one broken account must
# not prevent healthy accounts from producing attention indefinitely.
_INITIAL_SCAN_GRACE_S = 60.0
# How long a pass waits after a chat is opened before it runs (#2841). Opening a chat wakes
# Deckhand to clear the chat's card, and the pass runs on the web process's event loop: run
# at once, it held the page that opened the chat for a third of a second before a byte of
# it went out. The card still clears within this beat.
_OPEN_SETTLE_S = 0.3
# Activity at or before this moment never raises a card (#2599). Attention is
# "stopped and not opened since", and most chats on a long-lived deck were never
# opened at all — workers, delegated sessions, chats from before the read marker
# existed. Without a floor every one of them would light up at once. Set to the
# day the rule shipped, so only activity after it can ask for attention.
ATTENTION_CUTOFF = datetime(2026, 10, 3, tzinfo=UTC)


class AssistantService:
    """Debounce session changes into per-session attention triage."""

    def __init__(
        self,
        config: AppConfig,
        state: AppState,
        *,
        context_resolver: GitContextResolver | None = None,
    ) -> None:
        self.config = config.assistant
        self.state = state
        self.accounts = config.build_accounts()
        # Where this host keeps its Poller Instances, so triage can read which cards a
        # poller is still going to release on its own (#552). Reading their durable state
        # is the only channel a oneshot poller has — see ``poller_ops``.
        self._poller_root = config.poller.root
        self.context_resolver = context_resolver or GitContextResolver()
        # The one writer of session git context; see ``GitContexts``. Everything
        # outside this service reads it through the ``contexts`` view below.
        self._contexts = GitContexts()
        # The current answer to "which chats need me", rebuilt by every pass. It
        # is not persisted: ``state`` stays "idle" until this process has made one
        # pass.
        self.view = AssistantView()
        self._task: asyncio.Task | None = None
        self._session_watch_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._last_run = 0.0
        # ``AppState.sessions_revision`` as of the last pass that looked.
        self._known_sessions_revision = -1
        self._known_attention_inputs: tuple[tuple[object, ...], ...] | None = None
        # Which accounts were still missing their initial scan on the last pass.
        # ``None`` until a pass has looked, so a fresh loop always forces one
        # refresh: an empty first scan need not move ``sessions_revision``, yet it
        # is what proves whether saved cards really disappeared (#1707). It lives
        # here rather than in ``_loop``'s frame so it can be seeded like every
        # other change-detection signal the loop reads.
        self._known_unscanned_accounts: frozenset[str] | None = None
        # One in-flight background context resolve per session, so opening the
        # same chat twice cannot queue two `git status` runs (see
        # ``session_context``).
        self._context_tasks: dict[str, asyncio.Task] = {}
        # A newer chat to resolve once its running resolve ends (#2865).
        self._context_reruns: dict[str, Session] = {}
        self._force = True
        # When a pass woken by a chat being opened may run (``_OPEN_SETTLE_S``), in loop time.
        self._settle_until = 0.0
        # Every checkpoint payload is numbered as it is taken, and a write of an older
        # one than was last written is dropped: a pass writes its payload from a
        # thread, which must not overwrite a newer pass's payload (#3208).
        self._checkpoint_lock = threading.Lock()
        self._checkpoint_taken = 0
        self._checkpoint_written = 0
        checkpoint = state.db.load_assistant_checkpoint() if state.db else None
        self._restore_checkpoint(checkpoint)

    @property
    def contexts(self) -> Mapping[str, GitContext]:
        """Read-only view of the resolved git contexts, for the web layer."""
        return self._contexts.view

    @property
    def visible_insights(self) -> tuple[AssistantInsight, ...]:
        """Every chat that needs attention now, most urgent first."""
        return self.view.insights

    # --- persistence ---------------------------------------------------

    def _restore_checkpoint(self, payload: dict[str, Any] | None) -> None:
        """Load the git contexts a v10 or v11 payload holds; older ones are ignored."""
        version = payload.get("version") if payload else None
        if version not in {10, _CHECKPOINT_VERSION}:
            return
        try:
            # The last complete PR facts, so a cold process starts out knowing
            # what a warm one knew. ``GitContexts`` decides what is worth restoring.
            self._contexts.restore(payload.get("contexts", {}))
        except (KeyError, TypeError, ValueError):
            log.warning("Ignoring invalid Deckhand checkpoint")

    def _checkpoint_payload(self) -> dict[str, Any]:
        return {
            "version": _CHECKPOINT_VERSION,
            "contexts": self._contexts.as_json(),
        }

    def _take_checkpoint(self) -> tuple[dict[str, Any], int]:
        self._checkpoint_taken += 1
        return self._checkpoint_payload(), self._checkpoint_taken

    def _write_checkpoint(self, payload: dict[str, Any], number: int) -> None:
        assert self.state.db is not None
        with self._checkpoint_lock:
            if number < self._checkpoint_written:
                return  # a newer payload is already on disk
            self._checkpoint_written = number
            self.state.db.record_assistant_checkpoint(payload)

    # --- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        if self.config.enabled and self._task is None:
            # A fresh loop has looked at nothing yet, so its first pass forces a
            # refresh however many times this service is started (#1707).
            self._known_unscanned_accounts = None
            self._task = asyncio.create_task(self._loop(), name="attention-triage")
            self._session_watch_task = asyncio.create_task(
                self._watch_sessions(), name="attention-triage-sessions"
            )

    async def stop(self) -> None:
        tasks = tuple(
            t
            for t in (
                self._task,
                self._session_watch_task,
                *self._context_tasks.values(),
            )
            if t is not None
        )
        self._task = None
        self._session_watch_task = None
        self._context_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_sessions(self) -> None:
        """Wake the cheap eligibility check as soon as collection changes."""
        with self.state.bus.subscribe(
            "sessions", "attention_reports", "session_scans", "session_opened", "jobs"
        ) as subscription:
            while True:
                await subscription.get()
                # A report rewords a card, and opening a chat clears its card.
                # Neither moves the session revision, so bypass the periodic
                # throttle to show either one straight away.
                changed = subscription.changed_topics()
                if {"attention_reports", "session_opened"} & changed:
                    self._force = True
                if "session_opened" in changed:
                    self._settle_until = asyncio.get_running_loop().time() + _OPEN_SETTLE_S
                self._wake.set()

    def _initial_scans_complete(self) -> bool:
        """Whether every account has supplied one authoritative session set.

        Account scans land independently. Refreshing Deckhand after the first one
        ranks a partial deck, so old sessions can enter the bounded triage window
        and briefly reappear as finished cards during every web restart. Keep the
        restored checkpoint until the initial deck is available or the startup
        grace expires; after that, refresh preserves the unread accounts' records.
        """
        return all(
            self.state.session_scan_revision(account.key) > 0
            for account in self.accounts
        )

    def request_refresh(self) -> bool:
        if not self.config.enabled:
            return False
        self._force = True
        self._wake.set()
        return True

    # --- session selection & evidence ---------------------------------

    def _interaction(self, session: Session) -> PendingInteraction | None:
        return pending_interaction_for(session, self.accounts)

    def _attention_owner_keys(
        self,
        presentation=None,
        *,
        parents: dict[str, str | None] | None = None,
        current_of: Mapping[str, str] | None = None,
    ) -> frozenset[str]:
        """Current top-level sessions own Deckhand handoffs; children do not."""
        current_of = current_of if current_of is not None else self._current_legs()
        parents = parents or self._attention_parents(current_of=current_of)
        return frozenset(
            session.key
            for session in self.state.sessions.values()
            if current_of.get(session.key) == session.key
            and self._attention_root(session.key, parents) == session.key
        )

    def _current_legs(self) -> dict[str, str]:
        """Every session's chat's current leg, from one walk of the deck (#2841).

        A pass asked ``chat_current_session_key`` for every session three or four times
        over — for the parent graph, the owners, the eligible chats and the working ones —
        on the web process's event loop, where it held a chat page that was being opened.
        The answer cannot change inside a pass, which never yields while it reads state.
        """
        chats = self.state.chat_snapshot()
        return {
            key: self.state.chat_current_session_key(key, chats)
            for key in self.state.sessions
        }

    def _eligible_sessions(
        self, presentation=None, *, parents: dict[str, str | None] | None = None
    ) -> list[Session]:
        """Every current top-level session participates in the stop-edge rule."""
        owner_keys = self._attention_owner_keys(presentation, parents=parents)
        return [
            session
            for session in self.state.sessions.values()
            if session.key in owner_keys
        ]

    def _reported_attention(self, session_key: str):
        return self.state.attention_report_for(session_key)

    def _job_working_keys(self, session_keys) -> set[str]:
        """Sessions doing their own queued or running heavy work."""
        activity = getattr(self.state, "_job_activity", None)
        return set(activity(session_keys)) if activity is not None else set()

    def _attention_parents(
        self, *, current_of: Mapping[str, str] | None = None
    ) -> dict[str, str | None]:
        """Resolve the session parent graph without building the UI projection."""
        sessions = list(self.state.sessions.values())
        builder = getattr(self.state, "_parents_by_key", None)
        if builder is not None:
            chats = self.state.chat_snapshot()
            parent_index = self.state._parent_index(sessions)
            # A chat rollover changes the concrete key that owns attention.
            # Normalize every old leg identity to the current leg before parent
            # links are resolved, just as SessionPresentation does for its tree.
            for session in sessions:
                current = (
                    current_of[session.key]
                    if current_of is not None and session.key in current_of
                    else self.state.chat_current_session_key(session.key, chats)
                )
                parent_index[session.key] = current
                parent_index[session.session_id] = current
            parents = dict(builder(sessions, parent_index, chats))
            # The dashboard floats a delegated child whose recorded parent is
            # temporarily absent so the chat remains reachable. Deckhand must
            # not mistake that layout fallback for ownership: the durable
            # parent record still makes it a child, even while there is no
            # visible ancestor to receive its activity.
            parent_index = {
                identity: session.key
                for session in sessions
                for identity in (session.key, session.session_id)
            }
            recorded_parents = self.state.recorded_delegation_parents
            if recorded_parents:
                for session in sessions:
                    if parents.get(session.key) is not None:
                        continue
                    keys = self.state.chat_session_keys(session.key, chats)
                    recorded = next(
                        (
                            recorded_parents[key]
                            for key in reversed(keys)
                            if key in recorded_parents
                        ),
                        None,
                    )
                    if recorded is not None:
                        current_parent = self.state.chat_current_session_key(
                            recorded, chats
                        )
                        parents[session.key] = parent_index.get(
                            current_parent, current_parent
                        )
            return parents

        # Small protocol-compatible fallback for focused test states.
        parents: dict[str, str | None] = {session.key: None for session in sessions}
        for parent, children in getattr(self.state, "children", {}).items():
            for child in children:
                parents[child.key] = parent
        for child, parent in getattr(
            self.state, "recorded_delegation_parents", {}
        ).items():
            if child in parents:
                parents[child] = parent
        return parents

    def _unscanned_recorded_owner_keys(
        self,
        unscanned_accounts: frozenset[str],
        parents: dict[str, str | None],
        owners: frozenset[str],
    ) -> set[str]:
        """Owners whose absent descendants belong to an unread account."""
        if not unscanned_accounts:
            return set()
        prefixes = tuple(f"{account}:" for account in unscanned_accounts)
        chats = self.state.chat_snapshot()
        identity_to_current = {
            identity: self.state.chat_current_session_key(session.key, chats)
            for session in self.state.sessions.values()
            for identity in (session.key, session.session_id)
        }
        affected: set[str] = set()
        for links in (
            self.state.recorded_delegation_parents,
            self.state.delegation_parents,
        ):
            for child, parent_identity in links.items():
                if not child.startswith(prefixes):
                    continue
                parent = identity_to_current.get(parent_identity)
                if parent is None:
                    parent = self.state.chat_current_session_key(
                        parent_identity, chats
                    )
                root = self._attention_root(parent, parents)
                if root in owners:
                    affected.add(root)
        return affected

    @staticmethod
    def _attention_root(
        session_key: str, parents: dict[str, str | None]
    ) -> str | None:
        """Walk a session to its top-level owner; reject orphaned/cyclic chains."""
        if session_key not in parents:
            return None
        current = session_key
        seen: set[str] = set()
        while (parent := parents.get(current)) is not None:
            if current in seen or parent not in parents:
                return None
            seen.add(current)
            current = parent
        return current

    def _working_owner_keys(
        self,
        presentation=None,
        *,
        parents: dict[str, str | None] | None = None,
        owners: frozenset[str] | None = None,
        current_of: Mapping[str, str] | None = None,
    ) -> set[str]:
        """Top-level owners working themselves or through any descendant."""
        current_of = current_of if current_of is not None else self._current_legs()
        sessions = [
            session
            for session in self.state.sessions.values()
            if current_of.get(session.key) == session.key
        ]
        parents = parents or self._attention_parents(current_of=current_of)
        owners = owners or self._attention_owner_keys(parents=parents, current_of=current_of)
        activity = getattr(self.state, "_effective_activity", None)
        if activity is not None:
            effective_working, _, _ = activity(sessions, parents)
        else:
            jobs = self._job_working_keys({session.key for session in sessions})
            effective_working = {
                session.key
                for session in sessions
                if session.thinking or session.key in jobs
            }
        working: set[str] = set()
        for session in sessions:
            root = self._attention_root(session.key, parents)
            if root not in owners:
                continue
            if (
                session.key in effective_working
                or self.state.session_awaiting_reply(session)
            ):
                working.add(root)
        return working

    @staticmethod
    def _matches_registration(session: Session, context: GitContext) -> bool:
        """Does a cached context describe exactly this session's registered PRs?

        A chat that just registered (or dropped) a PR must not keep reading a
        context resolved before that edit, even outside the triage window.
        """
        return {ref.identity for ref in session.pull_requests} == {
            (pull.repository.lower(), pull.number) for pull in context.pull_requests
        }

    def session_context(self, session: Session) -> GitContext | None:
        """The chat's git/PR context now, refreshed *behind* the page (#766).

        Opening a chat used to await ``ensure_session_context``, which shells out
        to ``git status`` and ``gh pr view`` for a session Deckhand has not
        triaged — the common case for anything but the newest cards. On a busy
        host that is the single largest and most variable cost of opening a chat:
        measured 0 ms on a warm cache and 7.0 s when ``git status`` reached
        ``GitContextResolver.COMMAND_TIMEOUT_S``, with the whole page and the
        whole SSE prime waiting behind it.

        Nothing on the page needs the answer synchronously: the PR chips
        (``pull-requests``) and the repo/branch line (``assistant-session``) are
        both live fragments, so a context that lands a tick later simply appears.
        Read what is known, kick the resolve off behind the render, and let the
        stream deliver the difference.
        """
        existing = self._contexts.get(session.key)
        if existing is not None and self._context_is_current(session, existing):
            return existing
        self._refresh_session_context_soon(session)
        return existing

    def _context_is_current(self, session: Session, context: GitContext) -> bool:
        """Is a held context still worth returning without re-resolving?

        Registration alone used to answer this, and it only asks *which* pull
        requests the chat claims — never what state they are in. So a context
        resolved while the pull request was open kept being returned after it
        merged: no triage pass re-resolves git context any more (#1450), and this
        was the only path that could, so the pill froze for the life of the
        process (#1697). The webhook streams had the merge on file within seconds;
        nobody looked. Asking the cache costs one local read and no GitHub quota.
        """
        if not self._matches_registration(session, context):
            return False
        return self.context_resolver.reconcile_cached_pulls(context) is None

    def _refresh_session_context_soon(self, session: Session) -> None:
        """Resolve this session's context off the request, at most once at a time.

        Every open of the same chat would otherwise start another ``git status``
        while the first is still running, which is exactly the pile-up the
        blocking call used to prevent by making the caller wait.
        """
        key = session.key
        running = self._context_tasks.get(key)
        if running is not None and not running.done():
            # The running resolve read the chat as it was when it started. A newer
            # registration is resolved again once it ends, rather than dropped: the
            # page no longer re-reads on a tick that would have hidden it (#2865).
            self._context_reruns[key] = session
            return
        try:
            task = asyncio.create_task(
                self.ensure_session_context(session), name=f"git-context:{key}"
            )
        except RuntimeError:
            # No running loop (a synchronous caller in a test/CLI context). The
            # next triage sweep resolves it; a page must not fail over metadata.
            return
        self._context_tasks[key] = task
        task.add_done_callback(self._finish_session_context_task)

    def _finish_session_context_task(self, task: asyncio.Task) -> None:
        for key, running in list(self._context_tasks.items()):
            if running is task:
                del self._context_tasks[key]
                rerun = self._context_reruns.pop(key, None)
                if rerun is not None and not task.cancelled():
                    self._refresh_session_context_soon(rerun)
        if task.cancelled():
            return
        # Retrieve the exception so a background resolve can never surface as an
        # unretrieved-task warning; the resolve itself already swallows failures.
        error = task.exception()
        if error is not None:
            log.debug("Background git-context resolve failed: %s", error)

    async def ensure_session_context(self, session: Session) -> GitContext | None:
        """Resolve git/PR metadata when a chat outside the triage window opens."""
        existing = self._contexts.get(session.key)
        if existing is not None and self._context_is_current(session, existing):
            return existing
        try:
            resolved = await self.context_resolver.resolve([session])
        except Exception as exc:  # noqa: BLE001 -- metadata must not break chat pages
            log.debug("Deckhand context resolve failed for %s: %s", session.key, exc)
            return existing
        if self._contexts.update(resolved):
            self.request_refresh()
            # The chat's page reads its chips from this context, and waits on its
            # chat's topic rather than re-reading it on a tick (#2865).
            wake = getattr(self.state, "wake_chat_pages", None)
            if wake is not None:
                wake(resolved)
        return self._contexts.get(session.key)

    async def refresh(self, *, unscanned_accounts: frozenset[str] = frozenset()) -> None:
        """Work out which chats need attention now.

        A chat needs attention when it is not working and its newest activity —
        its own or a delegated child's — is newer than the last time it was
        opened (#2599). That is read from the deck every pass; nothing records a
        card, and nothing but opening the chat or the chat working again clears
        one. Looking is a field read on state the collector has already computed, so
        every eligible chat is looked at every time.
        """
        # The loop supplies the accounts whose initial scan is still unknown.
        # Their chats get no card until they are read, and their git contexts
        # are kept rather than erased by absence.
        unread_prefixes = tuple(f"{key}:" for key in unscanned_accounts)
        # Which leg each chat is on, once for the whole pass (#2841).
        current_of = self._current_legs()
        parents = self._attention_parents(current_of=current_of)
        owner_keys = self._attention_owner_keys(parents=parents, current_of=current_of)
        chats = self.state.chat_snapshot()
        current_sessions = [
            session
            for session in self.state.sessions.values()
            if current_of.get(session.key) == session.key
        ]
        unread_owner_keys = {
            root
            for session in current_sessions
            if session.account_key in unscanned_accounts
            and (root := self._attention_root(session.key, parents)) in owner_keys
        }
        unread_owner_keys |= self._unscanned_recorded_owner_keys(
            unscanned_accounts, parents, owner_keys
        )
        sessions = [
            session
            for session in self.state.sessions.values()
            if session.key in owner_keys
            and session.account_key not in unscanned_accounts
            and session.key not in unread_owner_keys
        ]
        held = set(self._contexts.view)
        deferred_keys = {key for key in held if key.startswith(unread_prefixes)}
        deferred_keys |= unread_owner_keys & held
        self._contexts.retain({session.key for session in sessions} | deferred_keys)
        self._freshen_contexts_from_cache()
        now = datetime.now(UTC)

        by_key = {session.key: session for session in sessions}
        # Working is two facts, not one. The provider's own view — is a turn
        # open — and the deck's: has it just handed this chat something that has
        # not come back. The second is authoritative about the *start* of a turn
        # in a way the first cannot be, because the deck caused it rather than
        # read about it afterwards (#1450).
        working_now = self._working_owner_keys(
            parents=parents, owners=owner_keys, current_of=current_of
        ) & set(by_key)
        activity = self._owner_activity(
            (
                session
                for session in current_sessions
                if session.account_key not in unscanned_accounts
            ),
            parents,
            by_key,
        )

        cards: list[tuple[AssistantInsight, datetime]] = []
        for key, session in by_key.items():
            if key in working_now:
                continue
            stamp = activity.get(key)
            if stamp is None or stamp <= ATTENTION_CUTOFF:
                continue
            opened_at = self.state.chat_last_opened(key, chats)
            if opened_at is not None and stamp <= opened_at:
                continue
            report = self._reported_attention(key)
            # ``stopped_card`` reads chat state only, so the words always say
            # what is true now. Nothing about GitHub reaches it, so a merged pull
            # request can recolour a card but never create one.
            card = stopped_card(
                session,
                self._interaction(session),
                reported_state=report.state if report is not None else None,
                reported_detail=report.detail if report is not None else "",
            )
            cards.append((card, stamp))
        self._unmute_blocked_targets([card for card, _stamp in cards])
        # Waiting before finished, and the longest-waiting first within each: the
        # card most likely to be forgotten is the one that has sat there longest.
        cards.sort(key=lambda item: (card_priority(item[0]), item[1].timestamp()))
        view = AssistantView(
            state="ready",
            insights=tuple(card for card, _stamp in cards),
            analyzed_at=now,
            error=(
                "Initial session scan incomplete: " + ", ".join(sorted(unscanned_accounts))
                if unscanned_accounts else None
            ),
        )
        self._commit_view(view)
        # Encoding and writing the checkpoint was a tenth of a pass on the event loop, so this
        # pass writes it from a thread instead (#2841). The payload is taken here, on the loop;
        # a write that lands after a newer one is dropped (``_write_checkpoint``).
        if self.state.db and self.view.state == "ready":
            await asyncio.to_thread(self._write_checkpoint, *self._take_checkpoint())

    def _owner_activity(
        self,
        sessions: Iterable[Session],
        parents: dict[str, str | None],
        owners: Mapping[str, Session],
    ) -> dict[str, datetime]:
        """Each eligible chat's newest activity, its delegated children included.

        A parent that hands work to a child and waits is not finished when it
        goes quiet; it is finished when the child's answer has come back. So the
        child's activity counts as the parent's, exactly as its working does.
        """
        activity: dict[str, datetime] = {}
        for session in sessions:
            root = self._attention_root(session.key, parents)
            if root not in owners:
                continue
            stamp = max(
                (
                    value
                    for value in (session.last_activity, session.last_progress)
                    if value is not None
                ),
                default=None,
            )
            if stamp is not None and (root not in activity or stamp > activity[root]):
                activity[root] = stamp
        return activity

    def _commit_view(self, view: AssistantView) -> None:
        changed = view != self.view
        self.view = view
        if changed:
            self.state.assistant_changed()

    def _note_refresh_failure(self, exc: BaseException) -> None:
        """Say that triage is failing, instead of looking calm (#1861).

        A pass that throws leaves ``self.view`` exactly as the last good pass
        left it, so the deck keeps presenting stale cards as current. The cards
        are still the best we have, so they stay, but the view carries the error.
        Cleared by the next pass that gets through, which builds its own view.
        """
        log.exception("attention triage loop error: %s", exc)
        message = (
            "Attention triage is failing, so these rows may be stale: "
            f"{type(exc).__name__}: {exc}"
        )
        if self.view.error == message:
            return
        self.view = replace(self.view, error=message)
        self.state.assistant_changed()

    def _unmute_blocked_targets(self, cards: list[AssistantInsight]) -> None:
        """End a mute on a chat that has become blocked on the boss (#823).

        Reuses the #590 insight: only the boss can answer a pending interaction,
        so muting a chat that is waiting on the boss is a
        contradiction — and it is the exact shape of the #541 incident. Every
        other mute-ending signal is a boss write; this one is the target's own
        state changing underneath the lease.
        """
        for card in cards:
            if card.cause == CAUSE_PENDING_INTERACTION:
                self.state.release_mute_lease(card.session_key)

    def handle_boss_turn(self, session_key: str) -> None:
        """Retire the old turn's attention after an accepted boss write.

        The provider transcript lags behind the deck's accepted write, so clear
        the old turn's report and open question synchronously. The chat is
        working from here, which takes its card away on the next pass.
        """
        self.state.clear_attention_report(session_key)
        self.state.drop_open_question(session_key)
        self.request_refresh()

    def deckhand_statuses(
        self, sessions: Iterable[Session], presentation=None
    ) -> dict[str, DeckhandStatus]:
        """One status colour per given session that needs attention; absent = none."""
        live_by_key = {insight.session_key: insight for insight in self.view.insights}
        statuses: dict[str, DeckhandStatus] = {}
        for session in sessions:
            insight = live_by_key.get(session.key)
            if insight is None:
                continue
            statuses[session.key] = resolve_deckhand_status(
                self.state.sessions.get(session.key, session),
                insight,
                merged=self._contexts.has_merged_pr(session.key),
            )
        return statuses

    def freshen_pull_requests(self) -> None:
        """Adopt webhook-written pull-request state now, on a delivery (#2042).

        The sweep below already existed, but only a Deckhand pass ran it, and a pass
        is due once a minute — so a merge the deck had on file within seconds still
        took up to a minute to reach a pill. This is the same sweep, driven by the
        delivery instead of the clock.

        Deliberately *not* ``request_refresh()``: that forces a whole analysis pass,
        which spends model tokens, and a pull request moving is no reason to re-triage
        every chat. This path is local cache peeks only — no GitHub call, no
        subprocess, no model call — and it publishes nothing unless a context really
        moved, so a delivery that changes no pill changes no pixel either.
        """
        if self._freshen_contexts_from_cache():
            self.state.bus.publish("assistant")

    def _freshen_contexts_from_cache(self) -> bool:
        """Adopt pull-request state the webhook streams already recorded (#1697).

        The deck list draws its work-item pills from ``self._contexts``, and until
        now nothing re-read a held context: a chat whose pull request merged kept
        showing an open pill for the life of the web process, because git context
        is resolved on demand for the chat being viewed and never by this sweep.
        The merge was on file in the shared cache within seconds of the webhook
        delivery, at no cost. This is the read that adopts it — one local peek per
        registered pull request, no GitHub call and no subprocess, so a quiet sweep
        costs the same as a busy one.

        Returns whether any context moved, so a caller outside the analysis pass can
        tell a real change from a no-op instead of publishing on every sweep (#2042).
        """
        moved = self.context_resolver.reconcile_cached_pulls_many(dict(self._contexts.view))
        if not moved:
            return False
        self._contexts.update(moved)
        return True

    def work_item_pills(self, sessions: Iterable[Session]) -> dict[str, WorkItemPill]:
        """One work-item pill per given session; absent key = no work item.

        Reads only state the deck already holds — the resolved git context, the
        poller's durable board snapshot, and the session's own issue status — so
        drawing the row costs no GitHub call. The board is read once for the
        whole batch, and ``read_board`` reuses its result while the engine's
        files are untouched, so a row costs no file read of its own either.
        """
        board = self._board_snapshot()
        pills: dict[str, WorkItemPill] = {}
        for session in sessions:
            context = self._contexts.get(session.key)
            card = self._card(board, session)
            pill = resolve_work_item_pill(
                session,
                context,
                board_status=(card.status or None) if card is not None else None,
                reviewed_sha=self._reviewed_head_sha(context, card),
            )
            if pill is not None:
                pills[session.key] = pill
        return pills

    def _reviewed_head_sha(
        self, context: GitContext | None, card: poller_ops.BoardCard | None
    ) -> str:
        """The short commit this row may claim passed review, or "" (#2121).

        One condition throughout: whatever says a review passed has to be about
        the commit the row speaks for *now*. A head that has moved drops the
        claim rather than carrying a stale one forward, because a row saying
        "reviewed" about a commit nobody would merge is worse than a row saying
        nothing.

        Two things can say it, and they answer different halves of the question.

        The deck's own record (``poller_review_verdicts``, the same one the chat
        page's commit chip reads, #1127) is keyed by commit, so it answers "was
        this exact commit reviewed". It is the only source for a chat whose card
        the poller does not run — and, being commit-keyed, it goes quiet the
        moment a rebase moves the head, even though nothing anybody reviewed
        changed.

        The poller's own published reading of its release bits covers exactly
        that gap. The engine carries a clean verdict across a rebase by comparing
        the diff rather than the commit (``release.review_pass_fresh``), and
        since #2121 it writes down what it concluded and which heads it
        concluded it against. That is a fact this process cannot re-derive: the
        digests it turns on come from GitHub reads the engine has already paid
        for and a row must never make. So the row reads the engine's answer
        instead of guessing at it, and a rebased branch keeps its tick from the
        first wake that re-reads the card.

        Neither costs a GitHub call: the verdict is cached in this process, and
        the board snapshot is already loaded once for the whole batch.

        The verdict word is compared case-insensitively because only one of the
        two intake routes constrains it: a session-started review is a
        ``Literal["pass", "changes-needed"]``, while the poller host hands its
        own verdict over as free text (``ReviewVerdictBody.verdict``). The
        engine spells it lowercase today, but nothing at this end enforces that,
        and a row that silently never ticked would be a hard bug to see.
        """
        commit = pill_review_commit(context)
        if commit is None:
            return ""
        sha = commit[1]
        # Two independently-sourced spellings of one commit meet here — the head
        # GitHub gave the git context, and the head GitHub gave the engine — and
        # a commit is the same commit whichever case it arrives in.
        if card is not None and any(
            sha.casefold() == head.casefold() for head in card.review_passed_heads
        ):
            return sha[:7]
        verdict = self.state.review_verdict(*commit)
        if verdict is None or verdict.verdict.strip().casefold() != "pass":
            return ""
        return sha[:7]

    def _board_snapshot(self) -> poller_ops.BoardView | None:
        """The poller's cards, or None when they cannot be read.

        A broken or absent poller must not break a row: an unreadable board
        leaves every pill exactly as it was before this read existed.
        """
        try:
            return poller_ops.read_board(self._poller_root)
        except Exception as exc:  # noqa: BLE001 -- a poller must not break the list
            log.debug("Poller board unreadable: %s", exc)
            return None

    @staticmethod
    def _card(
        board: poller_ops.BoardView | None, session: Session
    ) -> poller_ops.BoardCard | None:
        """The poller's card for the issue this chat was dispatched for, if any.

        Only the dispatched issue counts. A chat can register other issues it
        merely touched, and those say nothing about what this chat is doing.

        The row takes two things from this card — the column its pill says, and
        the engine's own reading of whether the review bit is green (#2121) — so
        it is looked up once per row rather than once per question.
        """
        if board is None:
            return None
        issue = IssueRef.parse(session.issue_url or "")
        if issue is None:
            return None
        return board.card(issue.repository, issue.number)

    # --- loop ----------------------------------------------------------

    def _attention_input_signature(self) -> tuple[tuple[object, ...], ...]:
        """Cheap raw-session inputs that can change a Deckhand answer.

        Active transcript text and progress move continuously but cannot create
        a handoff until the turn stops. Excluding that churn lets the five-second
        wake reject the common case without building the whole-deck projection.
        The working/finished edge and every resting evidence field remain here.
        """
        rows = []
        job_working = self._job_working_keys(self.state.sessions)
        for session in self.state.sessions.values():
            awaiting_reply = self.state.session_awaiting_reply(session)
            has_job = session.key in job_working
            working = session.thinking or awaiting_reply or has_job
            resting = (
                (
                    session.title,
                    session.last_prompt,
                    session.last_role,
                    session.last_text,
                    session.question,
                    session.capabilities,
                )
                if not working
                else ()
            )
            active_blocked_handoff = session.thinking and blocked_issue_handoff(session)
            rows.append(
                (
                    session.key,
                    session.chat_id,
                    session.status,
                    session.thinking,
                    awaiting_reply,
                    has_job,
                    session.turn_failed,
                    session.cwd,
                    session.kind,
                    session.worker_type,
                    session.is_delegated,
                    session.parent_session_key,
                    session.subagent_count,
                    tuple(
                        (agent.agent_id, agent.status)
                        for agent in session.subagents
                    ),
                    session.issues,
                    session.pull_requests,
                    session.issue_url,
                    session.issue_status_kind,
                    session.show_when_idle,
                    active_blocked_handoff,
                    resting,
                )
            )
        rows.append(
            (
                "@delegation",
                tuple(sorted(self.state.delegation_parents.items())),
                tuple(sorted(self.state.recorded_delegation_parents.items())),
            )
        )
        return tuple(sorted(rows, key=lambda row: row[0]))

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        scan_deadline = loop.time() + _INITIAL_SCAN_GRACE_S
        while True:
            # Woken by a change only (ADR 0007, #2865): ``_watch_sessions`` sets the
            # event for every topic a pass reads. The one clock is the startup grace
            # below, and it is a deadline: while an account is unscanned the loop wakes
            # exactly when that grace runs out, and never again for it.
            unscanned_now = any(
                self.state.session_scan_revision(account.key) == 0 for account in self.accounts
            )
            grace = scan_deadline - loop.time() if unscanned_now else None
            # A mute lease running out is the other clock: nothing else notices it, and
            # only this loop drops the lease and republishes the row (#2865).
            mute_deadline = getattr(self.state, "mute_lease_deadline", None)
            mute_end = mute_deadline() if mute_deadline is not None else None
            timeouts = [
                wait
                for wait in (
                    grace if grace is not None and grace > 0 else None,
                    (mute_end - datetime.now(UTC)).total_seconds()
                    if mute_end is not None
                    else None,
                )
                if wait is not None
            ]
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=max(0.0, min(timeouts)) if timeouts else None,
                )
            except TimeoutError:
                pass
            self._wake.clear()
            expire = getattr(self.state, "expire_mute_leases", None)
            if expire is not None:
                expire()  # publishes the unmuted rows itself
            settle = self._settle_until - loop.time()
            if settle > 0:
                # Let the page that opened a chat go out before this pass holds the loop.
                await asyncio.sleep(settle)
            unscanned = frozenset(
                account.key for account in self.accounts
                if self.state.session_scan_revision(account.key) == 0
            )
            if unscanned and loop.time() < scan_deadline:
                continue
            if unscanned != self._known_unscanned_accounts:
                # An empty successful scan may not change sessions_revision, but
                # it now proves whether saved cards really disappeared.
                self._force = True
                self._known_unscanned_accounts = unscanned
            # Every one of the signals below is derived from the session
            # projection, so when no session has changed since the last pass they
            # all answer exactly as they did then (#1310). There is no periodic
            # full pass any more (#2865): a pass reads nothing the clock moves —
            # a chat stops working when its provider's liveness says so, which is
            # itself a session change — and ``_force`` covers the inputs that are
            # not sessions (a report, an open, a scan).
            revision = self.state.sessions_revision
            if not self._force and revision == self._known_sessions_revision:
                continue
            self._known_sessions_revision = revision
            attention_inputs = self._attention_input_signature()
            attention_inputs_changed = attention_inputs != self._known_attention_inputs
            self._known_attention_inputs = attention_inputs
            if not self._force and not attention_inputs_changed:
                continue
            self._force = False
            self._last_run = loop.time()
            try:
                if unscanned:
                    await self.refresh(unscanned_accounts=unscanned)
                else:
                    await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- keep the loop alive
                self._note_refresh_failure(exc)
