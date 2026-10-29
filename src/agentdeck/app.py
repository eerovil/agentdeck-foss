"""FastAPI application factory + lifespan wiring."""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from jinja2 import BytecodeCache

from . import (
    agent_review,
    github_suspension,
    live_chats,
    mac_runner,
    review_refs,
    runtime_feed,
    shutdown,
    wayfinder_map,
    work_endings,
    work_release,
)
from .action_context import client_action_context
from .assistant import AssistantService
from .auth_watch import AuthWatch
from .auto_dream import AutoDream, settled
from .board_watch import BoardWatch
from .chat_board import TOPIC as CHAT_BOARD_TOPIC
from .chat_board import ChatBoardService
from .chat_jump import ChatJumps
from .chat_notes import TOPIC as CHAT_NOTES_TOPIC
from .chat_notes import ChatNotesService
from .collector import Collector
from .config import AppConfig, owned_session_base_url
from .db import open_db
from .deployment import deployment_status, skills_sha
from .github_webhook_log import WebhookLog
from .inject import InjectionService
from .issue_context import IssueResolver
from .issue_latest_comment import LatestCommentService
from .issue_refresh import IssueRefreshService
from .live_window import LiveWindow
from .poller_ops import (
    read_board,
    work_hold_labels,
    work_start_labels,
    work_unassigned_elsewhere,
)
from .poller_wake import KEEPALIVE_S as WEBHOOK_KEEPALIVE_S
from .poller_wake import PollerWakeService
from .providers import PROVIDERS, delivery_target_for
from .providers.codex.runtime_client import runtime_socket_path
from .providers.usage import read_shared_cache, shared_cache_dir, warm_usage_state
from .public_origin import record_public_origin
from .push import PushService
from .quota_retry import QuotaRetryService
from .review_fallback import ReviewFallbackService
from .review_fallback import deck_hooks as review_fallback_hooks
from .review_fallback import starred_check as review_fallback_starred
from .run_execution import MAX_CONTINUATIONS
from .scheduled_messages import ScheduledMessageService
from .shared_file import add_drained_hook
from .staging_deploys import StagingDeployService
from .state import AppState
from .usage_move import UsageMoveService, start_moved_leg
from .usage_move import transcript_url as usage_move_transcript_url
from .view_commands import ViewCommands
from .wake_on import WakeOnService
from .web import render as render_mod
from .web.action_timing import ActionTiming, identify_action
from .web.compression import RangeSafeGZipMiddleware
from .web.routes_actions import router as actions_router
from .web.routes_api import router as api_router
from .web.routes_auth import router as auth_router
from .web.routes_chat_board import router as chat_board_router
from .web.routes_chat_notes import router as chat_notes_router
from .web.routes_files import router as files_router
from .web.routes_live_prototype import router as live_prototype_router
from .web.routes_mac_runner import router as mac_runner_router
from .web.routes_meta_chat import router as meta_chat_router
from .web.routes_pages import router as pages_router
from .web.routes_partials import router as partials_router
from .web.routes_push import router as push_router
from .web.routes_pwa import cache_stamp
from .web.routes_pwa import router as pwa_router
from .web.routes_rollback import router as rollback_router
from .web.routes_speech import router as speech_router
from .web.routes_sse import router as sse_router
from .web.routes_webhook import router as webhook_router
from .web.routes_works import router as works_router
from .web.shared_fragments import SharedFragments
from .web.start_spares import StartSpares
from .web.static_files import RevalidatingStaticFiles
from .web.uploads import sweep_abandoned_uploads, sweep_abandoned_uploads_periodically
from .web.work_view import (
    WORK_INPUTS_TOPIC,
    WORKS_TOPIC,
    WorkOverviewService,
    needs_you_menu,
    owner_in_chat,
    paused_hold,
    start_signoff_cutoff,
)
from .webhook_status_cache import WebhookStatusCacheService
from .webhook_tailer import WebhookWatcher
from .work_backstop import MissedStartBackstop
from .work_claims import start_issue
from .work_router import (
    SwitchedRepositories,
    WorkRouterService,
    base_only_between,
    configured_own_logins,
    no_code_between,
    pull_files,
    work_busy,
    work_pulls,
)
from .work_router import owner_of_issue as work_owner_of_issue
from .work_router import owner_of_pull as work_owner_of_pull
from .work_signals import (
    acknowledge_comment,
    latest_comment,
    read_dependencies,
    read_map,
    read_map_body,
    read_parent,
    write_map_body,
)
from .work_start import start_action_id, start_work_agent, work_accounts
from .worktree_diff import WorktreeDiffReader

log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent / "web"
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"

VERSION = "0.3.1"


class _ProcessBytecodeCache(BytecodeCache):
    """Compiled templates kept for the process, so a second app compiles none again.

    The deck runs one app per process; a test run builds hundreds, and compiling every
    template afresh for each was most of a page test's time (#3052). A bucket is
    checked against its template's source, so an edited template still recompiles.
    """

    def __init__(self) -> None:
        self._code: dict[str, bytes] = {}

    def load_bytecode(self, bucket) -> None:
        code = self._code.get(bucket.key)
        if code is not None:
            bucket.bytecode_from_string(code)

    def dump_bytecode(self, bucket) -> None:
        self._code[bucket.key] = bucket.bytecode_to_string()


_TEMPLATE_BYTECODE = _ProcessBytecodeCache()

# How often the web checks for session tokens and card registrations the runtime wrote (#2144)
# when the runtime cannot say they moved: one from before its change feed, or none reachable
# (#2865). A worker resolves its own key by token within seconds of starting.
RUNTIME_RELATION_FOLLOW_S = 5.0
# How long the release's start sweep waits for the first chat scans, which tell each Work's
# pull requests (#2982); past it the sweep runs anyway and a Work it cannot place waits.
START_SCAN_WAIT_SECONDS = 300.0


def _build_id() -> str:
    """Short git SHA of the running tree, shown in the footer so you can tell at
    a glance (on the phone) whether an install picked up the latest code. Falls
    back to 'dev' outside a checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() or "dev"
    except Exception:
        return "dev"


def _revision() -> str | None:
    """Full SHA of the tree this process started from, served by ``/healthz`` so
    ``scripts/staging.sh`` can tell a fresh web worker from the one it replaces (#2628)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def create_app(config: AppConfig) -> FastAPI:
    db = open_db(
        config.history.db_path,
        config.history.usage_retention_days,
        config.context_management.retention_days,
    )
    state = AppState(db=db, budget_db=db)
    # Keep only recent chats, and those still in use, live in memory (#2841).
    state.live_window = LiveWindow(config.polling.live_window_days)
    start_signoff_cutoff(db)  # before any Work is classified (#2510)
    # Show "stale" usage only when the data is genuinely old — at least a few
    # poll cycles behind — rather than after a single rate-limited poll (issue #6).
    state.usage_stale_after_s = max(3 * config.polling.usage_interval_s, 300.0)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.bytecode_cache = _TEMPLATE_BYTECODE
    render_mod.register_filters(templates)
    templates.env.globals["app_version"] = VERSION
    # The ChatGPT chat board's links, only where the deck has the real board (#3623).
    templates.env.globals["chat_board_shown"] = review_refs.reviewer_available()
    templates.env.globals["build_id"] = _build_id()
    # Voice mode uses the host's speech service when one is configured (#3409).
    templates.env.globals["host_speech"] = bool(config.speech.socket)
    templates.env.globals["speech_language"] = config.speech.language
    templates.env.globals["voice_idle_seconds"] = config.speech.idle_listen_seconds
    # Who decides a phrase heard while waiting woke voice mode (#3476).
    templates.env.globals["voice_wake"] = (
        config.speech.wake if config.speech.wake_socket else "whisper"
    )
    # The deploy timer may advance the persistent runtime after the web process
    # has already started. Keep this callable request-scoped instead of freezing
    # the two durable release markers at app startup.
    templates.env.globals["deployment_status"] = deployment_status
    # Beside it in the same topbar slot: an identity GitHub has suspended (#2682).
    templates.env.globals["github_suspensions"] = github_suspension.suspensions
    templates.env.globals["github_suspension_sentence"] = github_suspension.sentence
    # Same shape for the job-queue pill (#1301): the collector keeps the last
    # read of the runtime's queue on AppState and the template asks for it, so
    # one partial serves both the page load and the SSE push.
    templates.env.globals["job_queue"] = state.job_queue
    # Whether the top bar shows its Mac-approvals icon at all. A callable, not a
    # value: the address lives in the environment, so freezing it here would
    # hide the icon on a deck configured after this process started.
    templates.env.globals["mac_runner_configured"] = mac_runner.configured
    templates.env.globals["mac_runner_url"] = mac_runner.approval_url
    # The count the collector keeps (#2864), read at render so a push and a page load agree.
    templates.env.globals["mac_runner_waiting"] = lambda: state.mac_waiting
    # Content hash appended to static asset URLs so a changed file is fetched
    # fresh — a request for app.css?v=<new> misses any stale SW/HTTP cache keyed
    # to the old URL and falls through to the network.
    asset_version = cache_stamp()
    templates.env.globals["asset_ver"] = asset_version
    collector = Collector(config, state)
    injector = InjectionService(
        config.inject,
        on_change=lambda session_key: state.sessions_changed((session_key,)),
        on_sent=state.note_session_sent,
        on_send_failed=state.clear_session_sent,
        on_handover=lambda session_key, action_id: mac_runner.note_handover(
            db, session_key, action_id
        ),
        on_delegation_started=state.mark_delegated_session,
        delegation_ready=state.delegation_cutover_ready,
        on_session_token=state.bind_session_token,
        on_new_session_dispatch=db.mark_continued_chat_dispatched,
        on_new_session_refused=db.release_chat_successor,
        session_busy=state.session_has_a_turn_running,
        on_dismissed=db.record_dismissed_delivery,
        was_dismissed=db.delivery_was_dismissed,
        delegation_store=db,
        session_key_for_token=state.session_key_for_token,
    )
    # Web Push keys, subscriptions and mode are operational configuration, not history.
    push = PushService(config.push, db)

    assistant = AssistantService(config, state)

    def resolve_scheduled_target(chat_id: str):
        chat = state.chat_by_id(chat_id)
        session = (
            state.current_session_for_chat(chat)
            if chat is not None
            else state.sessions.get(chat_id)
        )
        return (
            delivery_target_for(session, collector.accounts)
            if session is not None
            else None
        )

    def note_scheduled_boss_turn(session_key: str, message: str) -> None:
        state.note_boss_turn(session_key, message)
        assistant.handle_boss_turn(session_key)

    scheduled_messages = ScheduledMessageService(
        db,
        injector,
        resolve_scheduled_target,
        on_change=lambda chat_id: state.sessions_changed((chat_id,)),
        on_delivered=note_scheduled_boss_turn,
        # A due row waiting on its chat (no target yet, runtime restarting) is retried on
        # the next chat change rather than on a one-second tick (#2865).
        bus=state.bus,
    )
    quota_retries = QuotaRetryService(
        lambda: tuple(state.sessions.values()),
        state.chat_current_session_key,
        scheduled_messages.schedule,
        scheduled_messages.exists,
        bus=state.bus,
    )
    def usage_move_repository(session) -> str | None:
        """The repository of the one Work this chat is the agent of, or None (#3362).

        A chat a poller Run holds is left to the poller's own account fallover.
        """
        try:
            if db.chat_run_binding(session.key, max_legs=MAX_CONTINUATIONS) is not None:
                return None
            chat_id = state.chat_identity_key(session.key)
            refs = state.work_projection().works_chat_joined(chat_id)
        except Exception:  # noqa: BLE001 - an unreadable Work never moves a chat
            log.exception("usage move: could not read the Work of %s", session.key)
            return None
        return refs[0].repository if len(refs) == 1 else None

    usage_moves = UsageMoveService(
        lambda: tuple(state.sessions.values()),
        state.chat_current_session_key,
        usage_move_repository,
        lambda repository: work_accounts(app, repository),
        lambda: shared_cache_dir(config.usage.shared_cache_dir),
        db,
        lambda move: start_moved_leg(app, move),
        lambda key: usage_move_transcript_url(owned_session_base_url(config), key),
        bus=state.bus,
    )
    # The deck's own issue reader (#821): one shared, rate-limited GitHub read
    # layer that every in-process consumer goes through over the local API,
    # instead of each one running `gh` itself.
    issues = IssueResolver(poller_root=config.poller.root)
    # Publishes ``board`` when a poller instance republishes its files (#2865).
    board_watch = BoardWatch(config.poller.root, state.bus)
    # The board strip reads the shared GitHub cache and never refreshes it, so
    # somebody has to keep it warm. That is the deck's own job, because the
    # front page must stay fresh with no background agent behind it (#1115).
    issue_refresh = IssueRefreshService(
        issues, interval_s=config.poller.issue_refresh_interval_s
    )
    # First webhook consumer (#1642): pull_request deliveries write through into the
    # shared status cache and issues deliveries invalidate the repo's list, so the
    # pills stay fresh without spending GitHub quota. Rides the same streams the
    # webhook route stores; without them it has nothing to tail and stays off.
    # A write that moves what a pill shows also wakes Deckhand's own cheap sweep, so
    # the pill follows the delivery instead of the next 60-second pass (#2042).
    # One watch on the webhook streams for every consumer below (#2865): an append wakes
    # each of them to read from its own committed offset, and nothing re-reads on a timer.
    # The keep-alive moves the poller wake's heartbeat on a quiet stream and reads nothing.
    webhook_watcher = WebhookWatcher(config.github_webhook.path, keepalive_s=WEBHOOK_KEEPALIVE_S)
    webhook_status_cache = WebhookStatusCacheService(
        config.github_webhook.path,
        enabled=config.github_webhook.enabled,
        on_pull_change=assistant.freshen_pull_requests,
        on_tree_change=lambda: state.bus.publish(WORK_INPUTS_TOPIC),
        # A wayfinder map's drawn reads changed (#3627): its open Work pages build again.
        on_map_change=lambda: (wayfinder_map.bump_generation(), state.bus.publish(WORKS_TOPIC)),
        watcher=webhook_watcher,
    )
    # Each issue's newest comment id, so a deck link to an issue opens at it (#2424). Its
    # own consumer name, so its first start replays the streams already on disk.
    latest_comments = LatestCommentService(
        config.github_webhook.path,
        enabled=config.github_webhook.enabled,
        watcher=webhook_watcher,
    )
    # Second webhook consumer (#1649): a delivery worth acting on starts the poller
    # instance that declares its repository, ahead of the 30-second timer. The timer
    # stays the backstop and this holds no durable state — it seeks to the end of the
    # streams at startup, so a deploy never replays old deliveries as a burst of wakes.
    poller_wake = PollerWakeService(
        config.github_webhook.path,
        config.poller.root,
        enabled=config.github_webhook.enabled,
        watcher=webhook_watcher,
    )
    # A chat that registered a wait on a merge, a CI result or a deploy gets one message
    # when its delivery lands, or when the wait times out (#2775). The message rides the
    # scheduled-message path, so its delivery id is stable across a crash and a replay.
    started_revision = _revision()
    wake_on = WakeOnService(
        config.github_webhook.path,
        schedule=scheduled_messages.schedule,
        message_exists=scheduled_messages.exists,
        # The revision this process started from — the value /healthz serves — never a
        # later read of the checkout, which a deploy moves before it reloads the web.
        revision=lambda: started_revision,
        enabled=config.github_webhook.enabled,
        watcher=webhook_watcher,
        # A chat's registered pull requests: their replaced heads end its commit waits (#3271).
        pull_requests=state.pull_requests_for,
        # A needs-you wait wakes its chat only while a page of it is open (#3464).
        present=lambda request: owner_in_chat(state, request),
        # A review chat whose reviewer stopped cuts its pull requests' review and merge waits
        # short (#3316, #3611).
        review_chats=config.chat_board.repository if config.chat_board.enabled else "",
        # A deck-deploy wait also waits for the deploy's bundled skills (#3584).
        skills_sha=skills_sha,
    )
    # A Work hears its own GitHub events (#2895, ADR 0008): a person's comment or review, the
    # reviewer's verdict, and a registered pull request ready at a new head (which starts the
    # reviewer), for every repository a poller manifest declares. Messages ride the
    # scheduled-message path under the delivery's own id.
    # Who reviews a Work's new ready head (#3640): a ChatGPT review chat, or a deck-started
    # Claude or Codex reviewer in a throwaway checkout of the head.
    work_reviewer = agent_review.effective(
        config.review.reviewer, review_chats=review_refs.reviewer_available()
    )
    agent_reviewer = (
        agent_review.AgentReviewer(
            kind=work_reviewer,
            max_reviewers=config.review.max_reviewers,
            ledger=config.review.ledger_path,
            digest=review_refs.target_digest,
            **agent_review.deck_hooks(
                config,
                injector=injector,
                state=state,
                accounts=config.build_accounts(),
                providers=PROVIDERS,
            ),
        )
        if work_reviewer in agent_review.PROVIDERS
        else None
    )

    async def start_work_review(
        pull: str, work: str, *, note: str = "", again: bool = False
    ) -> object:
        if agent_reviewer is not None:
            return await agent_reviewer.start(pull, work, note=note, again=again)
        if work_reviewer == agent_review.OFF:
            raise review_refs.ReviewOff(review_refs.REVIEWER_OFF_TEXT)
        from .web.routes_chat_board import note_review_head, request_review

        outcome = await request_review(
            app, pull, work=work, **({"note": note, "again": True} if again else {})
        )
        note_review_head(app, outcome)
        return outcome

    async def ready_pull_head(repository: str, number: int) -> str:
        from .review_refs import pull_facts

        facts = await pull_facts(repository, number) or {}
        if str(facts.get("state") or "").upper() != "OPEN" or facts.get("isDraft"):
            return ""
        return str(facts.get("headRefOid") or "")

    def work_event_refusal(chat_id: str, text: str) -> str | None:
        # The provider the message will reach: the chat's current leg, or, when the deck
        # cannot tell, every provider, so a message any of them would drop is never sent.
        chat = state.chat_by_id(chat_id)
        session = state.current_session_for_chat(chat) if chat is not None else None
        session = session or state.sessions.get(chat_id)
        provider = PROVIDERS.get(getattr(session, "provider_id", ""))
        for candidate in (provider,) if provider is not None else PROVIDERS.values():
            refusal = candidate.user_message_refusal(text)
            if refusal is not None:
                return refusal
        return None

    # The deck's own release (#2895 step 2): a switched Work merges here,
    # from its verdicts, pull request and label events, finished jobs and idle chats.
    switched_repositories = SwitchedRepositories(config.poller.root)

    def release_paused(work_id: str) -> bool:
        # Read live from the deck's own pause table (#2559), never the Work overview's
        # snapshot, which nothing rebuilds while no dashboard watches. A label pause is the
        # issue's label, which the release reads itself.
        db = getattr(app.state, "control_db", None)
        return bool(db is not None and work_id in db.paused_works())

    def work_finished(work_id: str) -> bool:
        # Someone pressed Finish (#2510). Unreadable is unfinished.
        db = getattr(app.state, "control_db", None)
        if db is None:
            return False
        wanted = work_id.casefold()
        return any(
            key.casefold() == wanted and finished is not None
            for key, (_closed, finished) in db.work_signoffs().items()
        )

    def work_starred(work_id: str) -> bool:
        # A person's star (#3116), read live like the pause above. Unreadable is unstarred.
        db = getattr(app.state, "control_db", None)
        # Case-blind: a webhook may spell the repository otherwise than the Work page did.
        if db is None:
            return False
        return work_id.casefold() in {starred.casefold() for starred in db.starred_works()}


    from .work import WorkRef
    from .work_overview import carries_label

    def paused_label_for(work_id: str) -> str:
        """The paused label the Work's project manifest names, "" for none."""
        repository = work_id.partition(":")[2].rpartition("#")[0]
        return str(work_release_service.ports.policy(repository).paused_label or "")

    def cached_record(repository: str, number: int) -> dict | None:
        """The issue's cached record; None when it has none or the cache cannot be read."""
        try:
            return issues.cached_issues([(repository, number)]).get((repository, number))
        except Exception:  # noqa: BLE001 -- unreadable reads as no record, as it did before
            log.debug("issue cache unreadable for %s#%s", repository, number, exc_info=True)
            return None

    def cached_issue(work_id: str) -> dict | None:
        ref = WorkRef.from_work_id(work_id)
        if ref is None:
            return None
        return cached_record(ref.repository, ref.number)

    def router_paused(work_id: str) -> bool:
        # Paused on this deck, or by the label on the Work's cached issue (#2956): the way the
        # Work card reads it, so a pull request event pauses no less than a comment does.
        if release_paused(work_id):
            return True
        return carries_label(cached_issue(work_id), paused_label_for(work_id))

    async def router_resume(work_id: str) -> bool:
        """Resume a paused Work an automatic event reached (#2956): both of its pauses."""
        from . import gh_cli

        db = getattr(app.state, "control_db", None)
        if db is not None and work_id in db.paused_works() and not db.resume_work(work_id):
            return False
        wanted = paused_label_for(work_id)
        ref = WorkRef.from_work_id(work_id)
        record = cached_issue(work_id)
        if ref is not None and carries_label(record, wanted):
            # Spelled as the issue carries it, which may differ in case from the manifest's.
            label = next(
                str(name).strip()
                for name in record["labels"]
                if str(name).strip().casefold() == wanted.strip().casefold()
            )
            # As the repository owner's non-human authority, like the Work page's Resume.
            argv = [
                "gh", "issue", "edit", str(ref.number), "--repo", ref.repository,
                "--remove-label", label,
            ]
            try:
                code, _out, err = await gh_cli.run_once(argv, timeout=30, repo=ref.repository)
            except (OSError, TimeoutError):
                log.warning(
                    "work-router: removing %s from %s failed", label, work_id, exc_info=True
                )
                return False
            if code != 0:
                log.warning(
                    "work-router: removing %s from %s: %s",
                    label, work_id, err.decode("utf-8", "replace").strip(),
                )
                return False
            await reread_work_issue(ref.repository, ref.number)
        service = getattr(app.state, "work_overview", None)
        if service is not None:
            try:
                # The injector holds a paused Work's chats by this snapshot (``paused_hold``).
                await service.refresh()
            except Exception:  # noqa: BLE001 -- resumed; the next pass shows it
                log.warning(
                    "work overview refresh after resuming %s failed", work_id, exc_info=True
                )
        if work_release_service.handles(work_id):
            try:
                # A verdict the release held as "paused" before this resume merges now, as
                # the Work page's Resume makes it look again (``_wake_release``, #2956 review).
                await work_release_service.evaluate(work_id)
            except Exception:  # noqa: BLE001 -- resumed; the release catches up later
                log.warning("work release of %s after resuming failed", work_id, exc_info=True)
        return True

    def release_chat(work_id: str) -> str | None:
        ref = work_id.partition(":")[2]
        repository, _, number = ref.rpartition("#")
        owner = work_owner_of_issue(state, repository, int(number)) if number.isdigit() else None
        return owner[0] if owner else None

    def release_review_chat(pull: str, head: str) -> str | None:
        repository = config.chat_board.repository
        return work_release.review_chat_of(
            repository, state.chat_review_heads(repository), pull, head
        )

    async def release_digest(ref: str, head: str) -> str:
        from .review_refs import target_digest

        repository, _, number = ref.partition("#")
        digest, _why = await target_digest(repository, int(number), head)
        return digest

    async def release_green_heads(work_id: str) -> set[str]:
        from .poller_engine.verification import credited_head_for_work_alone
        from .web.work_view import runtime_jobs

        rows = await runtime_jobs(app, [work_id])
        heads = {credited_head_for_work_alone(row, work_id=work_id, jobs=rows) for row in rows}
        return {head for head in heads if head}

    async def release_verifying(work_id: str) -> bool:
        from .web.work_view import runtime_queue

        return work_release.verifies(await runtime_queue(app, work_id=work_id), work_id)

    async def release_failed_heads(work_id: str) -> set[str]:
        from .poller_engine.verification import failed_head_for_work_alone
        from .web.work_view import runtime_jobs

        rows = await runtime_jobs(app, [work_id])
        heads = {failed_head_for_work_alone(row, work_id=work_id, jobs=rows) for row in rows}
        return {head for head in heads if head}

    async def release_moved_heads(work_id: str) -> set[str]:
        from .poller_engine.verification import moved_head_for_work_alone
        from .web.work_view import runtime_jobs

        rows = await runtime_jobs(app, [work_id])
        heads = {moved_head_for_work_alone(row, work_id=work_id) for row in rows}
        return {head for head in heads if head}

    async def release_unclean_heads(work_id: str) -> set[str]:
        from .poller_engine.verification import unclean_head_for_work_alone
        from .web.work_view import runtime_jobs

        rows = await runtime_jobs(app, [work_id])
        heads = {unclean_head_for_work_alone(row, work_id=work_id) for row in rows}
        return {head for head in heads if head}

    def release_tell(chat_id: str, message_id: str, text: str) -> bool:
        from datetime import UTC, datetime

        from .scheduled_messages import ScheduledMessage

        now = datetime.now(UTC)
        scheduled = scheduled_messages.schedule(
            ScheduledMessage(id=message_id, chat_id=chat_id, text=text, due_at=now, created_at=now)
        )
        # ``None`` is a replay the store absorbed or a row it refused; only it can say which.
        return scheduled is not None or scheduled_messages.exists(message_id)

    work_release_service = work_release.WorkReleaseService(
        work_release.ReleaseStore(
            Path(config.github_webhook.path).expanduser().parent / work_release.STORE_FILENAME
        ),
        work_release.Ports(
            press_is_review=work_reviewer == agent_review.OFF,
            switched=switched_repositories,
            paused=release_paused,
            starred=work_starred,
            policy=work_release.Policies(config.poller.root),
            pulls=lambda work_id: work_pulls(state, work_id),
            chat=release_chat,
            read_pull=work_release.read_pull,
            digest=release_digest,
            green_heads=release_green_heads,
            failed_heads=release_failed_heads,
            moved_heads=release_moved_heads,
            unclean_heads=release_unclean_heads,
            merge=work_release.merge,
            update_branch=work_release.update_branch,
            tell=release_tell,
            # A chat of the Work mid-turn may still push (#2481): read from the live sessions.
            busy=lambda work_id: work_busy(state, work_id),
            issue_labels=work_release.issue_labels,
            permission=work_release.permission,
            verifying=release_verifying,
            review_chat=release_review_chat,
            refusal=work_event_refusal,
        ),
        enabled=config.github_webhook.enabled,
    )
    # A Work's derived status reads its wake-ons (#2895 step 3): each write announces itself,
    # so the Work cards rebuild from a change rather than a clock.
    wake_changed = wake_on.registry.on_change

    def wake_registry_changed() -> None:
        if wake_changed is not None:
            wake_changed()
        state.bus.publish(WORK_INPUTS_TOPIC)

    wake_on.registry.on_change = wake_registry_changed
    # The release's own record feeds the cards too (Ready for merge, #2960): announced the same
    # way, so a merge or a recorded pass rebuilds them.
    work_release_service.store.on_change = lambda: state.bus.publish(WORK_INPUTS_TOPIC)
    # A verdict the release takes ends its chat's review waits without a second message (#3183).
    work_release_service.on_review_answer = wake_on.registry.answer_reviews
    # Each commit's deployments, so a Work whose heads all run on a branch staging copy says
    # it is ready to test there (#3180). Its own consumer name: the first start replays the
    # streams on disk, and a stored change rebuilds the Work cards.
    staging_deploys = StagingDeployService(
        config.github_webhook.path,
        enabled=config.github_webhook.enabled,
        watcher=webhook_watcher,
        on_change=lambda: state.bus.publish(WORK_INPUTS_TOPIC),
    )

    # A comment the deck's own account posts on a chat's issue or pull request shows in that
    # chat as a note its agent never sees (#3227). Its own consumer name; a first start
    # skips the history.
    chat_notes = ChatNotesService(
        config.github_webhook.path,
        store=db,
        owner_of_issue=lambda repository, number: work_owner_of_issue(state, repository, number),
        owner_of_pull=lambda repository, number: work_owner_of_pull(state, repository, number),
        own_logins=configured_own_logins,
        on_change=lambda: state.bus.publish(CHAT_NOTES_TOPIC),
        scanned=lambda: collector.wait_initial_scans(START_SCAN_WAIT_SECONDS),
        enabled=config.github_webhook.enabled,
        watcher=webhook_watcher,
    )

    async def load_work_chats(repository: str, number: int) -> None:
        # A finished Work's chats are let go (#2841); its issue's news still reaches them.
        legs, missing = live_chats.chat_legs(
            state, f"/works/github_issue:{repository.lower()}#{number}"
        )
        if missing:
            await live_chats.load(state, collector, legs)

    async def reread_work_issue(repository: str, number: int) -> None:
        if await issues.refresh_issue_records([(repository, number)], limit=1):
            state.bus.publish(WORK_INPUTS_TOPIC)

    def router_review_chat(pull: str, head: str):
        # The newest review chat about this head and its board row (#3584): whether a ready
        # press at a head under review waits on that review or asks for it again.
        chat = release_review_chat(pull, head)
        if chat is None:
            return None
        number = chat.rpartition("#")[2]
        board = chat_board_service.board if chat_board_service.enabled else None
        row = board.rows.get(int(number)) if board is not None and number.isdigit() else None
        return chat, row

    def router_wait_review(chat_id: str, pull: str, asked: bool) -> None:
        # The same wait ``review-request`` registers, on the chat's working leg.
        from .wake_on import parse_request

        chat = state.chat_by_id(chat_id)
        session = state.current_session_for_chat(chat) if chat is not None else None
        session_key = session.key if session is not None else chat_id
        wake_on.register(session_key, chat_id, asked=asked, **parse_request(review=pull))

    unassigned_elsewhere: list = [0.0, frozenset()]

    def deck_claims(repository: str, assignee: str) -> bool:
        # Two decks on one repository: the issue's assignee says which one works it (#3731).
        # The Work list asks once per issue, so the overlays are read at most every 30 s.
        from . import work_claims
        from .poller_engine.github_authority import deck_person

        person = deck_person()
        if person and not assignee and time.monotonic() - unassigned_elsewhere[0] > 30.0:
            unassigned_elsewhere[:] = [
                time.monotonic(),
                work_unassigned_elsewhere(config.poller.root),
            ]
        return work_claims.mine(
            assignee,
            person=person,
            takes_unassigned=repository.lower() not in unassigned_elsewhere[1],
        )

    async def interrupt_chat(chat_id: str) -> bool:
        # A Work whose issue went to another deck stops its agent's turn (#3731).
        chat = state.chat_by_id(chat_id)
        session = state.current_session_for_chat(chat) if chat is not None else None
        account = next(
            (item for item in app.state.accounts if session and item.key == session.account_key),
            None,
        )
        if session is None or account is None:
            return False
        result = await PROVIDERS[account.provider_id].interrupt(account, session)
        return bool(result.accepted)

    async def start_unblocked_ticket(repository: str, number: int) -> tuple[str, str]:
        # The Start button's own write and label (#3764): the first ownership label.
        gh = shutil.which("gh")
        labels = read_board(config.poller.root).ownership_labels(repository)
        if gh is None or not labels:
            return "failed", "no gh or no start label"
        return await start_issue(gh, repository, number, labels[0], deck_claims)

    work_router = WorkRouterService(
        config.github_webhook.path,
        switched=switched_repositories,
        release=work_release_service,
        owner_of_issue=lambda repository, number: work_owner_of_issue(state, repository, number),
        owner_of_pull=lambda repository, number: work_owner_of_pull(state, repository, number),
        schedule=scheduled_messages.schedule,
        exists=scheduled_messages.exists,
        start_review=start_work_review,
        reviewer_off=work_reviewer == agent_review.OFF,
        refusal=work_event_refusal,
        ready_head=ready_pull_head,
        enabled=config.github_webhook.enabled,
        watcher=webhook_watcher,
        # The ownership label on an issue no chat owns starts its Work's agent (step 5b).
        start_work=lambda repository, number, **kwargs: start_work_agent(
            app, repository, number, **kwargs
        ),
        start_labels=lambda repository: work_start_labels(config.poller.root).get(
            repository.lower(), ()
        ),
        # Removing it from a Work's issue approves the spec its agent wrote (#3134).
        spec_only_label=lambda repository: work_release_service.ports.policy(
            repository
        ).spec_only_label,
        # A paused Work is resumed by its own automatic news (#2956): paused on this deck, or
        # by the label its cached issue carries.
        paused=router_paused,
        starred=work_starred,
        resume=router_resume,
        # A person's comment reaches a Work's agent only from a Work-tier login (#2895 review).
        authorized=work_release_service.holds_work,
        # A merge re-reads its Work's issue, in case the close delivery was dropped (#2947).
        reread_issue=reread_work_issue,
        # A person's comment passed on to the Work's chat gets a 👀 on GitHub (#3109).
        acknowledge=acknowledge_comment,
        # Someone else closing a Work's issue is news to its chat unless the Work is finished
        # or the chat is mid-turn, as it is when it closes its own issue (#3188).
        finished=work_finished,
        # The closing comment a restart kept the router from seeing is read from GitHub.
        latest_comment=latest_comment,
        busy=lambda work_id: work_busy(state, work_id),
        # The reviewer's verdict handed to a chat ends its review waits quietly (#3183).
        on_verdict=wake_on.registry.answer_reviews,
        # A head whose diff an earlier review read is not reviewed again (#3314).
        digest=release_digest,
        base_only=base_only_between,
        # A no-code review's fix that changed only doc files is not reviewed again (#3322).
        no_code=no_code_between,
        # A person's comment on a finished Work's issue reaches its let-go chat (#3405).
        load_work=load_work_chats,
        # A ready press at a head under review says where that review is (#3584).
        review_chat=router_review_chat,
        wait_review=router_wait_review,
        # A wayfinder map's chat wakes when its last ticket closes (#3627).
        read_issue=read_map,
        read_parent=read_parent,
        # ... and a ticket starts once its last blocker closes (#3764).
        read_dependencies=read_dependencies,
        start_ticket=start_unblocked_ticket,
        hold_labels=lambda repository: work_hold_labels(config.poller.root).get(
            repository.lower(), ()
        ),
        # ... and the deck keeps its lists from its tickets' answers (#3695).
        read_body=read_map_body,
        write_body=write_map_body,
        mine=deck_claims,
        interrupt=interrupt_chat,
    )
    # A verdict about a head the branch left only by merging its base still stands (#3314).
    work_release_service.ports.same_change = work_router.reviewed.same_change
    work_release_service.ports.verdict_taken = work_router.reviewed.note_verdict

    async def release_pull_files(pull: str) -> set[str] | None:
        repository, _, number = pull.rpartition("#")
        return await pull_files(repository, int(number)) if number.isdigit() else None

    work_release_service.ports.pull_files = release_pull_files
    # A labelled Work whose "label added" delivery was lost is started every 15 minutes
    # from the cached issue lists, the same way the router would have (#2951).
    def work_start_spent(work_id: str) -> bool:
        # A start refused before anything ran is settled ``failed`` and is not spent (#2971).
        db = getattr(app.state, "control_db", None)
        press = db.work_action(start_action_id(work_id)) if db is not None else None
        return press is not None and press.outcome != "failed"

    missed_start_backstop = MissedStartBackstop(
        start_labels=lambda: work_start_labels(config.poller.root),
        hold_labels=lambda: work_hold_labels(config.poller.root),
        backlog=issues.cached,
        reread=lambda repository, number: issues.refresh_issue_records(
            [(repository, number)], limit=1
        ),
        record=cached_record,
        owned=lambda repository, number: work_owner_of_issue(state, repository, number)
        is not None,
        paused=release_paused,
        spent=work_start_spent,
        start_work=lambda repository, number, **kwargs: start_work_agent(
            app, repository, number, wait=True, **kwargs
        ),
        mine=deck_claims,
        enabled=config.github_webhook.enabled,
    )
    # Third webhook consumer (#1807): the issue-as-chat board for the review-chat repository.
    # It reads the same streams, keeps its rows on disk so a deploy serves the page
    # without waiting, repairs them against GitHub on start and when relied on an hour
    # stale (#1872, #2865), and publishes on the bus only when a delivery actually
    # changed a row — which is what lets its page have a live channel without a self-poll.
    chat_board_service = ChatBoardService(
        config.github_webhook.path,
        repository=config.chat_board.repository,
        store_path=config.chat_board.store_path,
        closed_tail=config.chat_board.closed_tail,
        enabled=config.chat_board.enabled and config.github_webhook.enabled,
        on_change=lambda: state.bus.publish(CHAT_BOARD_TOPIC),
        watcher=webhook_watcher,
    )
    # And Claude reviewing in ChatGPT's place while the operator's timed switch is on (#2736).
    review_fallback = ReviewFallbackService(
        config.chat_board, chat_board_service if config.chat_board.enabled else None
    )
    review_fallback.bind(
        **review_fallback_hooks(
            config,
            injector=injector,
            state=state,
            accounts=config.build_accounts(),
            providers=PROVIDERS,
            releases=work_release_service.handles,
            # A chat whose every head already has a verdict starts no reviewer (#3584).
            answered=work_router.reviewed.answered,
        ),
        # A starred Work's review chat is taken first (#3116).
        starred=review_fallback_starred(state, work_starred),
    )

    # Leftover weekly usage goes to /dream before the week resets (#3336).
    def auto_dream_readings():
        cache_dir = shared_cache_dir(config.usage.shared_cache_dir)
        readings = []
        for account in collector.accounts:
            snapshot = read_shared_cache(account, cache_dir)
            if snapshot is not None:
                readings.append((account.key, account.label, snapshot))
        return readings

    async def auto_dream_start(run) -> tuple[bool, str]:
        account = next((item for item in collector.accounts if item.key == run.account_key), None)
        provider = PROVIDERS.get(account.provider_id) if account is not None else None
        if account is None or provider is None:
            return False, f"no account {run.account_key} on this deck"
        # The account's saved New chat model and effort, as the New chat box uses them.
        saved = db.load_account_new_chat_defaults(account.key)
        model = saved.get("model")
        effort = saved.get("effort")
        result = await injector.start_new(
            account,
            provider,
            Path(__file__).resolve().parents[2],
            run.message,
            model=model if model and provider.is_valid_model(model) else None,
            effort=effort if effort and provider.is_valid_effort(effort) else None,
            session_token=uuid.uuid4().hex,
            allow_over_budget=True,
        )
        if not result.accepted:
            return False, str(result.reason or "")
        # Accepted only queues the spawn: record the run once the provider answered it.
        return await settled(injector.new_start_answer(account.key))

    auto_dream = AutoDream(readings=auto_dream_readings, start=auto_dream_start)

    async def follow_runtime_relations_forever() -> None:
        # The runtime binds tokens and registers cards for the Poller's direct deliveries
        # (#2144); this is how the web learns about them without a restart. Read when the
        # runtime's change feed says they moved (#2865), and once at start as the catch-up.
        with runtime_feed.shared().subscribe(runtime_feed.RELATIONS) as changes:
            while True:
                state.follow_runtime_relations()
                await changes.wait(RUNTIME_RELATION_FOLLOW_S)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sweep_abandoned_uploads()
        relation_follower = asyncio.create_task(
            follow_runtime_relations_forever(), name="runtime-relation-follower"
        )
        upload_sweeper = asyncio.create_task(
            sweep_abandoned_uploads_periodically(), name="upload-ttl-sweeper"
        )
        # Proxy channel to the long-lived runtime service (deck-owned Claude
        # workers). Lazy per-request use; unreachable → the route returns 503.
        app.state.runtime_http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(runtime_socket_path())),
            base_url="http://agentdeck-runtime",
            timeout=httpx.Timeout(30.0, read=None),
        )
        # Warm the usage bars from the last persisted snapshot before the live
        # poller starts, so a web restart shows aged bars instead of blank "no
        # usage data yet" until the first poll lands (minutes, under 429 backoff).
        warm_usage_state(
            state,
            app.state.accounts,
            shared_cache_dir(config.usage.shared_cache_dir),
        )
        push.start()
        await collector.start()
        # After the collector, so the provider clients a taken-over wait uses exist (#2582).
        injector.start_delegation_takeover(lambda: collector.accounts, PROVIDERS)
        # After the collector too: a spare is asked of the runtime through those clients.
        app.state.start_spares.start()
        scheduled_messages.start()
        quota_retries.start()
        usage_moves.start()
        issue_refresh.start()
        webhook_status_cache.start()
        latest_comments.start()
        staging_deploys.start()
        chat_notes.start()
        poller_wake.start()
        wake_on.start()
        work_router.start()

        def learn_registrations() -> None:
            # A pull request registered with the old worker during a deploy was unknown here,
            # so its ready event found no Work and no review started (#3244): look again now.
            for ref in state.reload_pull_requests():
                work_router.note_registered(ref.repository, ref.number)

        forget_drained_hook = add_drained_hook(learn_registrations)
        missed_start_backstop.start()
        work_release_service.start(
            state.bus, scanned=lambda: collector.wait_initial_scans(START_SCAN_WAIT_SECONDS)
        )
        chat_board_service.start()
        review_fallback.start()
        if config.auto_dream.enabled:
            auto_dream.start()
        await assistant.start()
        board_watch.start()
        app.state.work_overview.start()
        try:
            yield
        finally:
            forget_drained_hook()
            await app.state.start_spares.stop()
            await app.state.work_overview.stop()
            await board_watch.stop()
            upload_sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await upload_sweeper
            relation_follower.cancel()
            with suppress(asyncio.CancelledError):
                await relation_follower
            await assistant.stop()
            await review_fallback.stop()
            await auto_dream.stop()
            await chat_board_service.stop()
            await missed_start_backstop.stop()
            await work_router.stop()
            await work_release_service.stop()
            await wake_on.stop()
            await poller_wake.stop()
            await latest_comments.stop()
            await staging_deploys.stop()
            await chat_notes.stop()
            await webhook_status_cache.stop()
            await webhook_watcher.stop()
            await issue_refresh.stop()
            await quota_retries.stop()
            await usage_moves.stop()
            await scheduled_messages.stop()
            await injector.stop()
            await collector.stop()
            # The coordinator owns the delivery-specific stop protocol: refuse anything new,
            # drain already-started writes, then close the runtime channel they were using.
            # Ordinary SSE/HTTP connections still get uvicorn's short grace independently.
            await shutdown.drain_then_close(app.state.runtime_http.aclose)
            db.close()

    app = FastAPI(title="agentdeck", version=VERSION, lifespan=lifespan)
    # Session pages are dominated by server-rendered transcript/sidebar text.
    # Compress that HTML and the static shell at the application boundary so
    # the same policy applies through loopback and the Tailscale HTTP/2 proxy.
    # Starlette deliberately skips text/event-stream responses.
    app.add_middleware(RangeSafeGZipMiddleware, minimum_size=500, compresslevel=5)
    app.state.config = config
    app.state.revision = started_revision
    app.state.app_state = state
    app.state.accounts = config.build_accounts()
    app.state.templates = templates
    app.state.collector = collector
    app.state.injector = injector
    app.state.scheduled_messages = scheduled_messages
    app.state.quota_retries = quota_retries
    app.state.usage_moves = usage_moves
    app.state.assistant = assistant
    app.state.issues = issues
    app.state.issue_refresh = issue_refresh
    app.state.webhook_status_cache = webhook_status_cache
    app.state.staging_deploys = staging_deploys
    app.state.chat_notes = chat_notes
    app.state.github_image_cache = (
        Path(config.github_webhook.path).expanduser().parent / "github-images"
    )
    app.state.poller_wake = poller_wake
    app.state.wake_on = wake_on
    app.state.work_router = work_router
    # The Work list leaves out an unlabelled issue another deck would work (#3731).
    app.state.deck_claims = deck_claims
    app.state.missed_start_backstop = missed_start_backstop
    app.state.work_release = work_release_service
    # A deck-run Work's turn that ends without a next step is asked about once (#2960). Off
    # where the router is off (staging), so that deck never messages a real chat.
    app.state.work_endings = (
        work_endings.EndingLedger(
            Path(config.github_webhook.path).expanduser().parent / work_endings.STORE_FILENAME
        )
        if config.github_webhook.enabled
        else None
    )
    app.state.chat_board = chat_board_service
    app.state.review_fallback = review_fallback
    app.state.push = push
    # One store since #2601; the three names are the ones callers already read.
    app.state.db = db
    app.state.control_db = db
    app.state.budget_db = db
    # One render of a shared fragment per event, however many boards are
    # attached. Without it the marginal browser paid to rebuild HTML another
    # browser had just built byte for byte (#1357).
    app.state.shared_fragments = SharedFragments(state.bus)
    # The dashboard's Work cards (#2147): rebuilt off the request path, published on change.
    app.state.work_overview = WorkOverviewService(app)
    # The deck chat's page moved to another chat by asking (#3816).
    app.state.chat_jumps = ChatJumps(state.bus)
    # View changes the deck chat asks every open page to make (#3814).
    app.state.view_commands = ViewCommands(state.bus)
    # Claude CLIs booted ahead of the next New chat and deck chat (#3420).
    app.state.start_spares = StartSpares(app)
    # The top bar's new Needs you list (#3239), from the last overview built: a page render
    # never builds one, and a push renders the same snapshot it was published for.
    templates.env.globals["needs_you_menu"] = lambda seen=(): needs_you_menu(
        app.state.work_overview.overview, seen
    )
    # A paused Work's chats take no new turn from the deck (#2559).
    injector.held = lambda session_key: paused_hold(app, session_key)
    # Claude login health, and the recovery flow when it is broken (#298). Web-process
    # owned: a login is a minute of a human's attention, not a durable agent turn.
    app.state.auth_watch = AuthWatch()
    # Ephemeral signing key for directory-scoped preview bundle capabilities. File
    # links point at a stable wrapper URL; each web-process lifetime has new tokens.
    app.state.preview_secret = secrets.token_bytes(32)
    # Reads a session checkout's pending change on request only — nothing here
    # runs on a timer, so a chat whose diff nobody opens costs nothing.
    app.state.worktree_diff = WorktreeDiffReader()
    # The raw per-repository webhook streams (#1601). Storing a delivery is all this
    # does; nothing reads these files yet and no poller cycle is triggered by one. The
    # streams rotate themselves as they fill (#1622), so a chatty repository costs a
    # bounded amount of disk without anything else having to run.
    app.state.github_webhook_log = (
        WebhookLog(
            config.github_webhook.path,
            secret_env=config.github_webhook.secret_env,
            rotate_bytes=config.github_webhook.rotate_bytes,
            keep_rotations=config.github_webhook.keep_rotations,
        )
        if config.github_webhook.enabled
        else None
    )

    # A real request is the only place this deck learns the address people
    # actually reach it on, and sessions started by the runtime never see one
    # (#1838). Recording it here is a no-op after the first request of each
    # address, and loopback calls are ignored.
    @app.middleware("http")
    async def remember_public_origin(request, call_next):
        record_public_origin(request.base_url)
        return await call_next(request)

    @app.middleware("http")
    async def load_chats_on_demand(request, call_next):
        """Bring an older chat back into memory before a route asks for it (#2841).

        The live window keeps only recent chats, and every chat and session route
        answers "unknown" for one that is not in memory. A path naming a chat the
        deck knows but has let go marks it opened and reads just its transcripts first,
        so the route finds it. Any chat opened stays live for an hour after its last open.
        """
        app_state = request.app.state.app_state
        legs, missing = live_chats.chat_legs(app_state, request.scope.get("path", ""))
        if missing:
            await live_chats.load(app_state, request.app.state.collector, legs)
        elif legs:
            # Opening a chat keeps it live for an hour, even once its Work finishes.
            app_state.live_window.request(legs)
        return await call_next(request)

    @app.middleware("http")
    async def measure_direct_actions(request, call_next):
        action, session_key = identify_action(request)
        if action is None:
            return await call_next(request)
        timing = ActionTiming.from_request(request, action, session_key)
        request.state.action_timing = timing
        with client_action_context(timing.client_action_id):
            response = await call_next(request)
        server_timing, _ = timing.finish(response.status_code)
        response.headers["Server-Timing"] = server_timing
        response.headers["X-AgentDeck-Action-ID"] = timing.client_action_id
        return response

    # Registered last, so it runs first: when the request reached the app. A Work page
    # reports the gap to its handler's start as loop lag (#2654).
    @app.middleware("http")
    async def stamp_received(request, call_next):
        request.state.received_at = time.perf_counter()
        return await call_next(request)

    app.mount(
        "/static",
        RevalidatingStaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )
    app.include_router(pages_router)
    app.include_router(api_router)
    app.include_router(actions_router)
    app.include_router(partials_router)
    app.include_router(sse_router)
    app.include_router(pwa_router)
    app.include_router(push_router)
    app.include_router(auth_router)
    app.include_router(webhook_router)
    app.include_router(chat_board_router)
    app.include_router(mac_runner_router)
    app.include_router(works_router)
    app.include_router(chat_notes_router)
    app.include_router(speech_router)
    app.include_router(meta_chat_router)
    app.include_router(rollback_router)
    app.include_router(live_prototype_router)
    # Deliberately last: the local-file route is a catch-all for absolute paths
    # such as /tmp/report.md:12 and must never shadow AgentDeck's own routes.
    app.include_router(files_router)
    return app
