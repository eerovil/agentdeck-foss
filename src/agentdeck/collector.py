"""Collector: keeps AppState in sync with each account's session sources.

Per account we run up to four cooperating tasks:
- a **scan loop** (incremental known-session refresh after safe source changes,
  with a slow full reconciliation fallback) — bounded work without giving up
  an authoritative account pass,
- a **source watcher** — coalesces filesystem changes and wakes the scan loop,
- a **liveness sweep** (cheap ``sweep_liveness``) — run when the source watch sees
  a write, a live terminal chat's process exits (a pidfd each), or the provider's
  next clock deadline falls due; ``liveness_interval_s`` only spaces sweeps (#2865),
- a **usage poller** (provider-supplied) — the OAuth limit reader.

One additional host sampler publishes aggregate CPU and memory usage. The heavy-command
queue is read when the runtime's change feed says it moved, and the deploy pill when its
state directory changes; the runtime feed's changes are also passed on the bus under
``runtime_feed.RUNTIME_TOPIC`` (ADR 0007).

Providers without filesystem sources retain periodic scanning at
``scan_interval_s``. Watched providers never scan faster than that interval.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from watchfiles import Change, awatch

from . import github_suspension, live_window, mac_runner, pid_watch, runtime_feed, watch_retry
from .config import AppConfig
from .deployment import deploy_state_dir, deployment_status
from .host_stats import HostStatsSampler
from .job_view import read_job_queue
from .models import Account, Session, SessionStatus
from .poller_engine import github_authority
from .providers import PROVIDERS, SessionProvider
from .providers.runtime_socket_client import runtime_socket_path
from .providers.usage import shared_cache_dir
from .state import AppState, LetGoSessionChanged

log = logging.getLogger(__name__)

# The queue is read when the runtime's change feed says it moved (#2865); this is
# the old cadence, kept only for a runtime that predates the feed or is unreachable.
JOBS_INTERVAL_S = 2.0
# How often the deck asks the Mac what is waiting for approval (#2864). Once here for every
# open tab, instead of every tab asking every ten seconds; the badge rides the event stream.
MAC_WAITING_INTERVAL_S = 10.0
# Filesystem watching is authoritative for transcript-backed providers, but a
# bounded fallback still heals missed events, replaced watch roots, and provider
# sources that were absent at startup.
SCAN_RECONCILE_S = 300.0


class Collector:
    def __init__(self, config: AppConfig, state: AppState):
        self.config = config
        self.state = state
        self.accounts: list[Account] = config.build_accounts()
        self._tasks: list[asyncio.Task] = []
        self._initial_scan_attempts: set[str] = set()
        self._initial_scan_successes: set[str] = set()
        # Set once every account has had its first scan attempt (#2760).
        self._initial_scans_done = asyncio.Event()
        self._work_backfill_checked = False
        self._scan_wakes: dict[str, asyncio.Event] = {}
        self._scan_changes: dict[str, set[tuple[Change, Path]]] = {}
        self._force_full_scans: set[str] = set()
        # One scan of an account at a time, so a scan run for a chat being opened
        # (``scan_now``) cannot land under an older loop scan that lacks it (#2841).
        self._scan_locks: dict[str, asyncio.Lock] = {}
        self._companion_scans: set[asyncio.Task] = set()
        self._host_sampler = HostStatsSampler()
        # Set to sweep an account's liveness now: a source write, a watched exit.
        self._liveness_wakes: dict[str, asyncio.Event] = {}
        self._runtime_feed_hold: runtime_feed.FeedSubscription | None = None
        self._runtime_feed_unlisten: Callable[[], None] | None = None

    def _provider(self, account: Account) -> SessionProvider:
        return PROVIDERS[account.provider_id]

    def _scan_lock(self, account_key: str) -> asyncio.Lock:
        return self._scan_locks.setdefault(account_key, asyncio.Lock())

    async def _full_scan(self, provider: SessionProvider, account: Account) -> list[Session]:
        """One whole-account scan under the live window, published to the state."""
        token = live_window.CURRENT.set(self.state.live_window)
        try:
            sessions = await provider.scan_sessions(account)
        finally:
            live_window.CURRENT.reset(token)
        await self._reconcile_open_questions(provider, account, sessions)
        self.state.replace_account_sessions(account.key, sessions)
        self.state.sessions_scanned(account.key)
        # Liveness reads what a scan applied: a chat already live at startup needs its
        # exit watch and deadline now, not after the next write anywhere (#2865).
        if liveness := self._liveness_wakes.get(account.key):
            liveness.set()
        # Legs and relatives of held chats that no scan has listed yet: their accounts
        # scan again now, so a chat comes back whole rather than in five minutes.
        missing = self.state.publish_live_companions()
        for account_key in sorted({key.rsplit(":", 1)[0] for key in missing}):
            task = asyncio.create_task(
                self.scan_now(account_key), name=f"live-companions-{account_key}"
            )
            self._companion_scans.add(task)
            task.add_done_callback(self._companion_scans.discard)
        return sessions

    async def load_sessions(self, account_key: str, session_ids: frozenset[str]) -> bool:
        """Read just these chats of one account and admit them (#2841).

        Opening a chat the live window let go: the provider finds each chat's own
        transcript by name rather than listing the whole account, and the state adds
        them beside the account's other chats. A provider without that targeted read
        falls back to a whole-account scan.
        """
        account = next((a for a in self.accounts if a.key == account_key), None)
        if account is None:
            return False
        provider = self._provider(account)
        if not getattr(provider, "supports_targeted_scan", False):
            return await self.scan_now(account_key)
        async with self._scan_lock(account.key):
            token = live_window.CURRENT.set(self.state.live_window)
            try:
                sessions = await provider.scan_sessions(account, only=session_ids)
                await self._reconcile_open_questions(provider, account, sessions)
                self.state.add_account_sessions(account.key, sessions)
            except Exception as exc:  # noqa: BLE001 — the page answers "not found" instead
                log.warning("on-demand load failed for %s: %s", account.key, exc)
                return False
            finally:
                live_window.CURRENT.reset(token)
        return True

    async def scan_now(self, account_key: str) -> bool:
        """Scan one account straight away; whether it could (#2841).

        Opening a chat the live window let go asks for it: the chat is marked as
        opened first, so this scan lists it.
        """
        account = next((a for a in self.accounts if a.key == account_key), None)
        if account is None:
            return False
        async with self._scan_lock(account.key):
            try:
                await self._full_scan(self._provider(account), account)
            except Exception as exc:  # noqa: BLE001 — the page answers "not found" instead
                log.warning("on-demand scan failed for %s: %s", account.key, exc)
                return False
        return True

    async def _reconcile_open_questions(
        self,
        provider: SessionProvider,
        account: Account,
        sessions: list[Session],
    ) -> None:
        # A chat may leave historical legs in provider scans. Questions describe
        # the conversation, so only the current leg may advance or derive one;
        # reconciling an abandoned leg would move the cursor backwards to that
        # leg and replay its local sequence range on every scan.
        # One chat lookup for the whole pass rather than three per session
        # (#1310). Deliberately re-read after the transcript reads below: a
        # snapshot is only valid for a synchronous pass, and a leg may be linked
        # while those awaits are in flight.
        chats = self.state.chat_snapshot()
        current_sessions = [
            session
            for session in sessions
            if self.state.chat_current_session_key(session.key, chats) == session.key
        ]
        transcript_reads = [
            (session, cursor)
            for session in current_sessions
            if (cursor := self.state.open_question_cursor(session.key, chats))
            is not None
        ]
        event_batches = await asyncio.gather(
            *(
                provider.read_transcript(account, session, after_seq=cursor)
                for session, cursor in transcript_reads
            )
        )
        events_by_session = {
            session.key: events
            for (session, _cursor), events in zip(
                transcript_reads, event_batches, strict=True
            )
        }
        chats = self.state.chat_snapshot()
        for session in current_sessions:
            self.state.reconcile_open_question(
                session, events_by_session.get(session.key, ()), chats
            )

    async def _scan_loop(self, account: Account) -> None:
        provider = self._provider(account)
        interval = self.config.polling.scan_interval_s
        loop = asyncio.get_running_loop()
        force_full = True
        pending_changes: frozenset[tuple[Change, Path]] = frozenset()
        while True:
            scan_started = loop.time()
            scan_ok = False
            try:
                async with self._scan_lock(account.key):
                    sessions = None
                    if not force_full:
                        sessions = await provider.scan_changed_sessions(
                            account, pending_changes, self.state
                        )
                    if sessions is None:
                        sessions = await self._full_scan(provider, account)
                    else:
                        await self._reconcile_open_questions(provider, account, sessions)
                        self.state.merge_account_sessions(account.key, sessions)
                await self._record_context_observations(provider, account, sessions)
                scan_ok = True
                self._initial_scan_successes.add(account.key)
                self._settle_delegated_work()
                self._link_work_side_chats()
            except LetGoSessionChanged as exc:
                # A chat the live window let go wrote again: the full scan this
                # forces lists it once more (#2841).
                log.debug("scan for %s: %s changed; scanning in full", account.key, exc)
            except Exception as exc:  # noqa: BLE001 — a bad scan must not kill the loop
                log.warning("scan failed for %s: %s", account.key, exc)
            finally:
                self._initial_scan_attempts.add(account.key)
                if self.initial_scans_finished():
                    self._initial_scans_done.set()
            # Preserve the old prompt retry after a transient provider failure.
            # Waiting for a source event here can strand a recovered runtime for
            # five minutes when the failed input did not touch the filesystem.
            if not scan_ok:
                force_full = True
                pending_changes = frozenset()
                await asyncio.sleep(interval)
                continue
            wake = self._scan_wakes.get(account.key)
            if wake is None:
                force_full = True
                pending_changes = frozenset()
                await asyncio.sleep(interval)
                continue
            try:
                await asyncio.wait_for(wake.wait(), timeout=SCAN_RECONCILE_S)
            except TimeoutError:
                force_full = True
                pending_changes = frozenset()
            else:
                # A busy transcript can emit many writes per second. Preserve
                # the old maximum scan frequency while coalescing all changes
                # that arrive during that interval into one provider decision.
                await asyncio.sleep(max(0.0, interval - (loop.time() - scan_started)))
                # No await may separate clear from the drain: a later source write
                # then sets the event for the following pass instead of being lost.
                wake.clear()
                pending_changes = frozenset(self._scan_changes.pop(account.key, ()))
                force_full = (
                    account.key in self._force_full_scans or not pending_changes
                )
                self._force_full_scans.discard(account.key)

    async def _record_context_observations(
        self,
        provider: SessionProvider,
        account: Account,
        sessions: list[Session],
    ) -> None:
        """Persist current exact-leg readings only while maintenance is enforced."""
        if (
            self.config.context_management.mode != "enforce"
            or not self.state.session_scan_is_current(account.key)
        ):
            return
        observe = getattr(provider, "context_observations", None)
        if observe is None:
            return
        threshold = self.config.context_management.compact_rearm_below_tokens
        for observation in await observe(account, sessions):
            self.state.budget_db.record_context_observation(
                account.provider_id,
                account.key,
                observation.worker_key,
                observation.source_session_id,
                observation.tokens,
                observed_at=observation.observed_at,
                source_version=observation.source_version,
                rearm_below_tokens=threshold,
            )

    async def _watch_loop(
        self,
        account_key: str,
        paths: list[str],
        wake: asyncio.Event,
        watch_filter: Callable[[Change, str], bool] | None = None,
        *,
        recursive: bool = True,
        restore_periodic: bool = True,
        wake_liveness: bool = False,
    ) -> None:
        try:
            async for changes in awatch(
                *paths,
                debounce=500,
                step=100,
                watch_filter=watch_filter,
                recursive=recursive,
            ):
                if self._scan_wakes.get(account_key) is wake:
                    self._scan_changes.setdefault(account_key, set()).update(
                        (change, Path(path)) for change, path in changes
                    )
                wake.set()
                # The same writes are what the liveness sweep reads (#2865).
                if wake_liveness and (liveness := self._liveness_wakes.get(account_key)):
                    liveness.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- periodic scans remain available
            log.warning("source watcher failed for %s: %s", account_key, exc)
        else:
            log.warning("source watcher stopped for %s", account_key)
        finally:
            if restore_periodic and self._scan_wakes.get(account_key) is wake:
                self._scan_wakes.pop(account_key, None)
                self._scan_changes.pop(account_key, None)
                # Release a scan loop already waiting on this watcher; on its
                # next pass it sees the missing wake and resumes periodic scans.
                wake.set()

    async def _account_watch_loop(
        self, account: Account, wake: asyncio.Event
    ) -> None:
        """Watch source trees while rebuilding them after root-level replacement."""
        provider = self._provider(account)
        try:
            while True:
                topology_changed = asyncio.Event()

                def topology_filter(
                    change: Change,
                    path: str,
                    p: SessionProvider = provider,
                    a: Account = account,
                ) -> bool:
                    # Topology is about a watch root appearing or being
                    # replaced, never about its contents. Claude's
                    # ``history.jsonl`` is both a direct child of the account
                    # root and a watched source, so accepting ``modified`` here
                    # made every prompt the CLI records rebuild the watches and
                    # force a full scan (#1487) — the frequent case, since an
                    # atomic replacement still arrives as deleted+added.
                    return change is not Change.modified and p.watch_root_change(
                        a, Path(path)
                    )

                tasks = [
                    asyncio.create_task(
                        self._watch_loop(
                            account.key,
                            [str(path) for path in provider.watch_roots(account)],
                            topology_changed,
                            topology_filter,
                            recursive=False,
                            restore_periodic=False,
                        )
                    )
                ]
                source_paths = provider.watch_paths(account)
                if source_paths:
                    tasks.append(
                        asyncio.create_task(
                            self._watch_loop(
                                account.key,
                                [str(path) for path in source_paths],
                                wake,
                                restore_periodic=False,
                                wake_liveness=True,
                            )
                        )
                    )
                topology_wait = asyncio.create_task(topology_changed.wait())
                watched = [*tasks, topology_wait]
                try:
                    done, _pending = await asyncio.wait(
                        watched, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for task in watched:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*watched, return_exceptions=True)
                if topology_wait in done and topology_wait.result():
                    self._force_full_scans.add(account.key)
                    wake.set()
                    continue
                return
        finally:
            if self._scan_wakes.get(account.key) is wake:
                self._scan_wakes.pop(account.key, None)
                wake.set()
            # With no watch left, the liveness sweep returns to its old cadence.
            if liveness := self._liveness_wakes.get(account.key):
                liveness.set()

    def _link_work_side_chats(self) -> None:
        """Link side chats started from a Work's page once a scan names them (#2388)."""
        try:
            self.state.finalize_work_side_chats()
        except Exception:  # noqa: BLE001 — a failed link is retried on the next scan
            log.exception("linking Work side chats failed")

    def _settle_delegated_work(self) -> None:
        """Run the one-time delegated Work backfill after every account's first good scan.

        A parent's provider-inferred issue only exists once its account is scanned,
        so running earlier would save a child as unplaced for good (#2207). The latch
        is set only once the backfill's durable marker exists, so a failure is retried
        on a later scan. After that, each scan decides delegations whose parent was
        not resolvable yet.
        """
        if not self._work_backfill_checked:
            if not all(account.key in self._initial_scan_successes for account in self.accounts):
                return
            try:
                self.state.backfill_delegated_work()
            except Exception:  # noqa: BLE001 — a failed backfill must not kill the scan loop
                log.exception("delegated Work backfill failed; retrying on a later scan")
            self._work_backfill_checked = self.state.delegated_work_backfill_done()
            if not self._work_backfill_checked:
                return
        try:
            self.state.finalize_pending_delegations()
        except Exception:  # noqa: BLE001
            log.exception("deciding pending delegations failed")

    def initial_scans_finished(self) -> bool:
        """Whether every configured account has completed one scan attempt."""

        return all(
            account.key in self._initial_scan_attempts for account in self.accounts
        )

    async def wait_initial_scans(self, timeout: float) -> bool:
        """Wait until every account has had its first scan attempt; False on ``timeout``.

        A collector that was never started has nothing to wait for (#2760).
        """
        if not self._tasks or self.initial_scans_finished():
            return True
        try:
            await asyncio.wait_for(self._initial_scans_done.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def _liveness_loop(self, account: Account) -> None:
        """Sweep an account's liveness when something it reads changed (#2865).

        The sweep reads transcripts and the CLI's session registry (the account's
        source watch wakes it), worker state from the runtime (the providers' feed
        watches project that themselves), whether a terminal chat's process still runs
        (a pidfd per live chat wakes it), and clocks ("thinking ends N s after the last
        write"), which the provider names as its next deadline. ``liveness_interval_s``
        is now only the minimum spacing between sweeps, and the old cadence for an
        account with no working source watch or a process that cannot be watched; the
        five-minute reconcile scan is the catch-up for anything a watch missed.
        """
        provider = self._provider(account)
        interval = self.config.polling.liveness_interval_s
        wake = self._liveness_wakes.setdefault(account.key, asyncio.Event())
        exits = pid_watch.ExitWatches(lambda _pid: wake.set())
        loop = asyncio.get_running_loop()
        backoff = watch_retry.Backoff(first=interval)
        try:
            while True:
                swept_at = loop.time()
                deadline: float | None = None
                retry_s: float | None = None
                try:
                    current = self.state.account_sessions(account.key)
                    # The sweep refreshes state's own session objects and publishes
                    # "sessions" once through AppState if anything changed, so the
                    # mutation and its notification stay a single transition.
                    provider.sweep_liveness(account, current, self.state)
                    current = self.state.account_sessions(account.key)
                    deadline = provider.next_liveness_deadline(account, current)
                    exits.watch(
                        session.pid
                        for session in current
                        if session.pid is not None
                        and session.parent_session_key is None
                        and session.status == SessionStatus.LIVE
                    )
                    backoff.succeeded()
                except Exception as exc:  # noqa: BLE001
                    # A failed sweep is tried again, not left for the next write (#2865).
                    retry_s = backoff.failed()
                    log.debug("liveness sweep failed for %s: %s", account.key, exc)
                timeout = None if deadline is None else max(0.0, deadline - time.time())
                if retry_s is not None:
                    timeout = retry_s if timeout is None else min(timeout, retry_s)
                if exits.unwatched or self._scan_wakes.get(account.key) is None:
                    timeout = interval if timeout is None else min(timeout, interval)
                if timeout is None:
                    await wake.wait()
                else:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(wake.wait(), timeout)
                # Consumed before the spacing sleep, so a wake that lands during it
                # stays set and earns the following sweep rather than being erased.
                wake.clear()
                # A busy transcript writes many times a second: sweep at most once
                # per interval, gathering every change that lands meanwhile.
                await asyncio.sleep(max(0.0, interval - (loop.time() - swept_at)))
        finally:
            exits.close()
            if self._liveness_wakes.get(account.key) is wake:
                self._liveness_wakes.pop(account.key, None)

    def _make_usage_task(self, account: Account) -> asyncio.Task | None:
        provider = self._provider(account)
        poller = provider.make_usage_poller(
            account,
            self.state,
            self.state.bus,
            interval_s=self.config.polling.usage_interval_s,
            cache_dir=shared_cache_dir(self.config.usage.shared_cache_dir),
        )
        if poller is None:
            return None
        return asyncio.create_task(poller.run(), name=f"usage:{account.key}")

    async def _deployment_loop(self) -> None:
        """Publish ``deployment`` when the deploy timer's state files change.

        The pill was render-time only, so a tab left open watched a healthy
        pill while the unattended deploy refused every two minutes (#786). It
        is re-read when something it shows can have moved (#2865): a write in
        the deploy state directory, a write to ``github-authority.toml``, or a
        GitHub call that changed what this process holds as suspended. A page
        renders the pill from a fresh read, which is the catch-up for a missed
        file event; only a genuine change is published.
        """
        # The same slot carries the GitHub-suspended pill (#2682), so its change is
        # published by this loop too.
        loop = asyncio.get_running_loop()
        changed = asyncio.Event()

        def suspension_moved() -> None:
            # ``gh`` calls run on worker threads too.
            with contextlib.suppress(RuntimeError):  # the loop is already closed
                loop.call_soon_threadsafe(changed.set)

        remove = github_suspension.add_listener(suspension_moved)
        watcher = asyncio.create_task(
            self._watch_files(
                [deploy_state_dir(), github_authority.accounts_path().parent], changed
            ),
            name="deployment:files",
        )
        try:
            last = (deployment_status(), github_suspension.suspensions())
            while True:
                await changed.wait()
                changed.clear()
                try:
                    current = (deployment_status(), github_suspension.suspensions())
                except Exception as exc:  # noqa: BLE001 -- a bad read must not kill the loop
                    log.debug("deployment status read failed: %s", exc)
                    continue
                if current != last:
                    last = current
                    self.state.deployment_changed()
        finally:
            remove()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def _watch_files(self, targets: list[Path], changed: asyncio.Event) -> None:
        """Set ``changed`` on any write directly in these directories (#2865).

        Watched without recursion, so an atomic replace of a file in one is seen and
        nothing below it is. A directory that does not exist yet is watched from its
        nearest existing ancestor, and the watch moves down once it appears.
        """
        targets = [Path(target).expanduser() for target in targets]

        def nearest(path: Path) -> Path:
            while not path.exists() and path != path.parent:
                path = path.parent
            return path

        def relevant(_change: Change, raw: str) -> bool:
            path = Path(raw)
            return any(
                path.parent == target or path == target or path in target.parents
                for target in targets
            )

        async def watch() -> None:
            roots = sorted({nearest(target) for target in targets})
            async for _changes in awatch(
                *(str(root) for root in roots),
                debounce=200,
                step=100,
                recursive=False,
                watch_filter=relevant,
            ):
                changed.set()
                if sorted({nearest(target) for target in targets}) != roots:
                    return

        # One watch error used to stop the pills updating until a restart; it is retried,
        # and the re-read after the back-off catches up a write made meanwhile.
        await watch_retry.retry_forever(watch, name="deploy state", caught_up=changed.set)

    async def _jobs_loop(self) -> None:
        """Publish ``jobs`` when the runtime's heavy-command queue changes (#1301).

        The queue is owned by the persistent runtime, so this asks over the same
        Unix socket ``agentdeck job`` uses rather than reading the durable file:
        the runtime is the only process that knows a row still has a live child
        behind it. A failed read preserves the last successful view; unknown may
        not invent a job completion or a Deckhand stop edge.
        """
        client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(runtime_socket_path())),
            base_url="http://agentdeck-runtime",
            timeout=httpx.Timeout(5.0),
        )
        changes = runtime_feed.shared().subscribe(runtime_feed.JOBS)
        try:
            while True:
                failed = True
                try:
                    view = await read_job_queue(client)
                    if view is not None:
                        self.state.set_job_queue(view)
                        failed = False
                except Exception as exc:  # noqa: BLE001 -- a bad read must not kill the loop
                    log.debug("job queue read failed: %s", exc)
                # Read again when the runtime says the queue moved (#2865); on the old
                # cadence while it cannot say so, or after a read that failed.
                await changes.wait(JOBS_INTERVAL_S, retry=failed)
        finally:
            changes.close()
            with contextlib.suppress(Exception):
                await client.aclose()

    async def _host_loop(self) -> None:
        while True:
            try:
                self.state.set_host_stats(self._host_sampler.sample())
            except Exception as exc:  # noqa: BLE001 -- host stats are optional UI data
                log.debug("host stats sample failed: %s", exc)
            await asyncio.sleep(self.config.polling.host_interval_s)

    async def _mac_waiting_loop(self) -> None:
        """Keep the top bar's Mac-approvals count, so no browser has to ask (#2864)."""
        while True:
            try:
                if mac_runner.configured():
                    self.state.set_mac_waiting(await mac_runner.waiting_count())
            except Exception as exc:  # noqa: BLE001 -- a badge never stops the deck
                log.debug("mac waiting read failed: %s", exc)
            await asyncio.sleep(MAC_WAITING_INTERVAL_S)

    def _start_runtime_feed(self) -> None:
        """Hold the runtime's change feed open and pass every change on the bus (#2865).

        Views that read runtime rows (the Work overview's Run outcomes and job rows)
        subscribe to ``runtime_feed.RUNTIME_TOPIC`` rather than rebuilding on a timer.
        The held subscription matches nothing: it only keeps the feed connected while
        the collector runs, whichever other consumer comes and goes.
        """
        feed = runtime_feed.shared()
        bus = self.state.bus
        self._runtime_feed_unlisten = feed.add_listener(
            lambda kinds: bus.publish(runtime_feed.RUNTIME_TOPIC, kinds)
        )
        self._runtime_feed_hold = feed.subscribe(lambda _item: False)

    async def start(self) -> None:
        self._start_runtime_feed()
        self._tasks.append(asyncio.create_task(self._host_loop(), name="usage:host"))
        self._tasks.append(asyncio.create_task(self._mac_waiting_loop(), name="mac:waiting"))
        self._tasks.append(
            asyncio.create_task(self._deployment_loop(), name="deployment:watch")
        )
        if self.config.jobs.enabled:
            self._tasks.append(
                asyncio.create_task(self._jobs_loop(), name="jobs:watch")
            )
        for account in self.accounts:
            provider = self._provider(account)
            try:
                await provider.start_account(account, self.state)
            except Exception as exc:  # noqa: BLE001 -- scans remain useful without controls
                log.warning("provider runtime failed for %s: %s", account.key, exc)
            if provider.watch_paths(account) or provider.watch_roots(account):
                wake = asyncio.Event()
                self._scan_wakes[account.key] = wake
                self._tasks.append(
                    asyncio.create_task(
                        self._account_watch_loop(account, wake),
                        name=f"watch:{account.key}",
                    )
                )
            self._tasks.append(
                asyncio.create_task(self._scan_loop(account), name=f"scan:{account.key}")
            )
            self._tasks.append(
                asyncio.create_task(self._liveness_loop(account), name=f"liveness:{account.key}")
            )
            usage = self._make_usage_task(account)
            if usage is not None:
                self._tasks.append(usage)
        log.info("collector started for %d account(s)", len(self.accounts))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        if self._runtime_feed_hold is not None:
            self._runtime_feed_hold.close()
            self._runtime_feed_hold = None
        if self._runtime_feed_unlisten is not None:
            self._runtime_feed_unlisten()
            self._runtime_feed_unlisten = None
        self._liveness_wakes.clear()
        self._scan_wakes.clear()
        self._scan_changes.clear()
        self._force_full_scans.clear()
        for account in self.accounts:
            try:
                await self._provider(account).stop_account(account)
            except Exception as exc:  # noqa: BLE001 -- finish stopping other accounts
                log.debug("provider runtime stop failed for %s: %s", account.key, exc)
