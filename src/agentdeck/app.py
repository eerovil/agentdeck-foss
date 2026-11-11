"""FastAPI application factory + lifespan wiring."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from jinja2 import BytecodeCache

from . import (
    agent_review,
    github_suspension,
    live_chats,
    mac_runner,
    review_history,
    review_refs,
    runtime_feed,
    services_link,
    session_copy,
    shutdown,
    wayfinder_map,
    work_endings,
    work_release,
    work_services,
    work_status_log,
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
from .config import AppConfig
from .db import open_db
from .deployment import deployment_status, skills_sha
from .github_webhook_log import WebhookLog
from .inject import InjectionService
from .issue_context import IssueResolver
from .issue_latest_comment import LatestCommentService
from .issue_marks import IssueMarksService
from .issue_refresh import IssueRefreshService
from .live_window import LiveWindow
from .poller_wake import KEEPALIVE_S as WEBHOOK_KEEPALIVE_S
from .poller_wake import PollerWakeService
from .providers import PROVIDERS, chat_target_for, delivery_target_for
from .providers.codex.runtime_client import runtime_socket_path
from .providers.usage import read_shared_cache, shared_cache_dir, warm_usage_state
from .public_origin import record_public_origin
from .push import PushService
from .quota_retry import QuotaRetryService
from .review_fallback import ReviewFallbackService
from .review_fallback import deck_hooks as review_fallback_hooks
from .review_fallback import person_check as review_fallback_person
from .review_fallback import reviewer_down as review_reviewer_down
from .review_fallback import starred_check as review_fallback_starred
from .review_fallback import tell_takeover as review_fallback_tell_takeover
from .run_execution import MAX_CONTINUATIONS
from .scheduled_messages import ScheduledMessageService
from .shared_file import add_drained_hook
from .staging_deploys import StagingDeployService
from .state import AppState
from .usage_move import UsageMoveService, start_moved_leg
from .view_commands import ViewCommands
from .voice_mode import deck_line
from .wake_on import WakeOnService, check_account_env, read_live, settled_head
from .web import render as render_mod
from .web import services_hooks
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
from .web.routes_services import router as services_router
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
    voice_mode_on,
)
from .webhook_status_cache import WebhookStatusCacheService
from .webhook_tailer import WebhookWatcher
from .work_message import on_delivered as work_message_delivered
from .work_router import configured_own_logins
from .work_router import owner_of_issue as work_owner_of_issue
from .work_router import owner_of_pull as work_owner_of_pull
from .work_signals import open_blockers
from .work_start import session_start_choice, work_accounts
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

    def resolve_answer_target(session_key: str):
        # The leg that asked, never the chat's current one: an answer belongs to it alone.
        session = state.sessions.get(session_key)
        return chat_target_for(session, collector.accounts) if session is not None else None

    scheduled_messages = ScheduledMessageService(
        db,
        injector,
        resolve_scheduled_target,
        resolve_answer_target=resolve_answer_target,
        on_change=lambda chat_id: state.sessions_changed((chat_id,)),
        # Another Work's agent's message (#3908) leaves the owner's open question standing.
        on_delivered=work_message_delivered(state, assistant),
        # While voice mode is on in the chat, the deck's own messages ask for a spoken
        # answer like the person's own (#3875).
        voice_line=lambda message, target: deck_line(
            message.id,
            message.text,
            voice_on=voice_mode_on(state, (message.chat_id, target.session.key)),
            language=config.speech.language,
            too_long=config.inject.too_long,
        ),
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
        start_choice=lambda session: session_start_choice(app, session.key),
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
    # Each issue's newest spec, review findings and review pass (#3855), so another deck's
    # Work shows its status from GitHub alone. Its own consumer name, like the one above.
    issue_marks = IssueMarksService(
        config.github_webhook.path,
        enabled=config.github_webhook.enabled,
        watcher=webhook_watcher,
        # A new verdict or spec reaches an open front page and Work page at once.
        on_change=lambda: state.bus.publish(WORK_INPUTS_TOPIC),
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

    async def wake_fetch(path: str) -> object:
        # One GitHub read of a timed-out merge or CI wait (#4253), off the event loop.
        from . import gh_cli

        code, stdout = await gh_cli.run(["gh", "api", path], timeout=30)
        if code != 0:
            raise RuntimeError(f"gh api {path} exited {code}")
        return json.loads(stdout or b"null")

    async def wake_ask_review(repository: str, number: int, note: str) -> str:
        # The deck's own ``review-request`` with ``no_verdict`` (#4253): "" when asked.
        from .review_refs import REVIEWER_OFF_TEXT
        from .work_router import AgainBusy, AgainRefused

        router_service = getattr(app.state, "work_router", None)
        if router_service is None or not router_service.enabled:
            return "this deck does not route Work events, so it starts no review"
        if getattr(router_service, "reviewer_off", False):
            return REVIEWER_OFF_TEXT
        try:
            await router_service.ask_again(repository, number, note, no_verdict=True)
        except AgainBusy:
            return ""  # a review is already coming: the wait keeps waiting for it
        except AgainRefused as exc:
            return str(exc) or "refused"
        return ""

    def wake_ready_head(request) -> bool:
        # A commit wait on the head of a ready or merged pull request this chat registered in a
        # repository the release merges (#4253), from cached rows alone.
        router_service = getattr(app.state, "work_router", None)
        if router_service is None or not router_service.enabled:
            return False
        refs = {
            ref.identity
            for ref in state.pull_requests_for(request.session_key)
            if ref.identity[0] == request.repository
        }
        refs = {ref for ref in refs if router_service.switched(ref[0])}
        rows = issues.cached_pulls(refs).values() if refs else ()
        return any(settled_head(row, request.sha) for row in rows)

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
        # A review wait's timeout says when no reviewer can start at all (#4064).
        reviewer_down=lambda: review_reviewer_down(config, config.build_accounts()),
        # The routine wakes the deck settles itself (#4253).
        live_read=lambda request: read_live(request, wake_fetch),
        ask_review=wake_ask_review,
        ready_head=wake_ready_head,
        release_notices=lambda chat_id, since: scheduled_messages.ids_since(
            chat_id, "work-release:", since
        ),
        # A time wait's check runs as its chat's own Claude account (#4256).
        check_env=lambda session_key: check_account_env(config, session_key),
    )
    # A Work hears its own GitHub events (#2895, ADR 0008), and the deck merges a switched
    # Work itself (#2895 step 2): the Work router, the merge release and the missed-start
    # backstop. They run in the services process (#4016), where a walk over every pull
    # request delays a merge rather than freezing pages; the web keeps stand-ins that read
    # the config, the release store and the review history here and send every act to the
    # services socket (``services_link``). The tests' stand-in builds them in this process
    # instead (``work_services.build`` below).
    services_in_web = services_link.runs_in_web()
    services_transport = (
        services_link.InProcessTransport(None)
        if services_in_web
        else services_link.SocketTransport(lambda: services_link.services_socket_path(config))
    )
    services_later = services_link.Later(services_transport)
    work_router = services_link.RouterLink(config, services_transport, state, services_later)
    work_release_service = services_link.ReleaseLink(
        config, services_transport, state, services_later
    )
    # The web's live chats, copied to the services process, which has no collector (#4001).
    session_copy_feed = (
        None
        if services_in_web
        else session_copy.SessionCopyFeed(
            state,
            scanned=collector.initial_scans_settled,
            shown=work_services.work_reviewer(config) in agent_review.PROVIDERS,
        )
    )
    # What the services process changed, republished on this bus (#4003).
    services_follower = (
        None
        if services_in_web
        else services_link.ServicesFollower(
            lambda: services_link.services_socket_path(config),
            state=state,
            scheduled_messages=scheduled_messages,
            wake_on=wake_on,
        )
    )

    def work_starred(work_id: str) -> bool:
        # A person's star (#3116). Unreadable is unstarred; case-blind, as a webhook may spell
        # the repository otherwise than the Work page did.
        return work_id.casefold() in {starred.casefold() for starred in db.starred_works()}

    # A Work's derived status reads its wake-ons (#2895 step 3): each write announces itself,
    # so the Work cards rebuild from a change rather than a clock.
    wake_changed = wake_on.registry.on_change

    def wake_registry_changed() -> None:
        if wake_changed is not None:
            wake_changed()
        state.bus.publish(WORK_INPUTS_TOPIC)

    wake_on.registry.on_change = wake_registry_changed
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
    # The Work list leaves out an unlabelled issue another deck would work (#3731).
    deck_claims = work_services.make_deck_claims(config)

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
    # An escalation goes into the history of each pull request it names (#3957). The
    # public copy's stand-in has no board at all (#3623).
    if chat_board_service.board is not None:
        chat_board_service.board.on_escalated = lambda *escalation: work_release.note_escalation(
            config.chat_board.repository,
            state.chat_review_heads(config.chat_board.repository),
            state.record_review_event,
            *escalation,
            full_head=lambda pull, sha: review_history.full_head(state.review_events(pull), sha),
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
            answered=lambda pull, head: review_history.answered(state.review_events(pull), head),
        ),
        # A starred Work's review chat is taken first (#3116).
        starred=review_fallback_starred(state, work_starred),
        # A pull request no Work owns goes after the Works', for an hour at most (#4152).
        person=review_fallback_person(state),
        # A chat ChatGPT left is taken over in the open: its Work's chat hears it (#4189).
        taken_over=review_fallback_tell_takeover(
            state, scheduled_messages, config.chat_board.repository
        ),
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
        status_log_pruner = asyncio.create_task(
            work_status_log.prune_forever(db), name="work-status-log-pruner"
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
        issue_marks.start()
        staging_deploys.start()
        chat_notes.start()
        poller_wake.start()
        wake_on.start()
        if work_services_host is not None:
            work_services_host.start()
        if session_copy_feed is not None:
            session_copy_feed.start()
        if services_follower is not None:
            services_follower.start()

        def learn_registrations() -> None:
            # A pull request registered with the old worker during a deploy was unknown here,
            # so its ready event found no Work and no review started (#3244): look again now.
            for ref in state.reload_pull_requests():
                work_router.note_registered(ref.repository, ref.number)

        forget_drained_hook = add_drained_hook(learn_registrations)
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
            status_log_pruner.cancel()
            with suppress(asyncio.CancelledError):
                await status_log_pruner
            relation_follower.cancel()
            with suppress(asyncio.CancelledError):
                await relation_follower
            await assistant.stop()
            await review_fallback.stop()
            await auto_dream.stop()
            await chat_board_service.stop()
            if work_services_host is not None:
                await work_services_host.stop()
            if services_follower is not None:
                services_follower.stop()
            if session_copy_feed is not None:
                await session_copy_feed.stop()
            await services_later.flush()
            await services_transport.aclose()
            await wake_on.stop()
            await poller_wake.stop()
            await latest_comments.stop()
            await issue_marks.stop()
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
    app.state.github_image_cache = config.github_webhook.store_path / "github-images"
    app.state.poller_wake = poller_wake
    app.state.wake_on = wake_on
    app.state.work_router = work_router
    # The Work list leaves out an unlabelled issue another deck would work (#3731).
    app.state.deck_claims = deck_claims
    # The Start button says when an open blocker holds the agent back (#4216).
    app.state.open_blockers = open_blockers
    app.state.work_release = work_release_service
    app.state.services_later = services_later
    app.state.session_copy = session_copy_feed
    # A deck-run Work's turn that ends without a next step is asked about once (#2960). Off
    # where the router is off (staging), so that deck never messages a real chat.
    app.state.work_endings = (
        work_endings.EndingLedger(
            config.github_webhook.store_path / work_endings.STORE_FILENAME
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

    # The moved services inside this process: only the tests' stand-in (#4016).
    work_services_host = None
    if services_in_web:
        work_services_host = work_services.build(
            config,
            state=state,
            db=db,
            issues=issues,
            hooks=services_hooks.in_process_hooks(
                app,
                scheduled_messages=scheduled_messages,
                chat_board=chat_board_service,
                collector=collector,
            ),
            watcher=webhook_watcher,
        )
        services_transport.services = work_services_host
    # A dropped ready event leaves the cached row a draft until the hourly refresh rewrites
    # it; that rewrite wakes the backstop, with no delivery needed (#3903 review).
    issues.on_pull_rows = work_router.wake_sweep
    app.state.work_services = work_services_host

    @app.exception_handler(services_link.ServicesUnavailable)
    async def services_unavailable(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(services_link.ServicesUncertain)
    async def services_uncertain(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=504)

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
    app.include_router(services_router)
    # Deliberately last: the local-file route is a catch-all for absolute paths
    # such as /tmp/report.md:12 and must never shadow AgentDeck's own routes.
    app.include_router(files_router)
    return app
