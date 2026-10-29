"""Full-page routes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import github_suspension, issue_post, live_prototype, meta_chat, voice_helper
from ..chat_transcript import chat_event_id, load_chat_transcript
from ..composer_phrases import composer_phrases
from ..composer_suggestions import composer_suggestions
from ..providers import PROVIDERS
from ..providers.base import DETAIL_WINDOW, SessionProvider
from .account_settings import choice_label, effective_new_session_defaults
from .action_timing import action_timing, timing_span
from .deps import (
    get_accounts,
    get_db,
    get_state,
    get_templates,
    require_access,
    resolve_session,
)
from .render import (
    SessionView,
    render_chat_notes,
    render_chat_transcript,
    render_session_fragments,
    session_labels,
    worker_exit_message,
)
from .routes_actions import (
    _inject_failure,
    session_can_write_handoff,
    session_model_preference_context,
    session_title_context,
    starting_chat_context,
)
from .routes_mac_runner import session_context as mac_runner_session_context
from .routes_rollback import rollback_context
from .routes_works import new_work_form
from .side_chat import side_chat_context

router = APIRouter()
logger = logging.getLogger(__name__)


def _show_new_chat_cwd_option(cwd: Path) -> bool:
    """Keep history shallow and omit linked Git worktrees."""
    try:
        resolved_cwd = cwd.expanduser().resolve()
        git_marker = resolved_cwd / ".git"
        if git_marker.is_file():
            return False
        relative_cwd = resolved_cwd.relative_to(Path.home().resolve())
    except (OSError, RuntimeError, ValueError):
        # Paths outside home, or paths that cannot otherwise be inspected safely,
        # retain the historical behavior instead of disappearing from the picker.
        return True
    if len(relative_cwd.parts) <= 1:
        return True
    return git_marker.is_dir()


def _new_chat_cwd_options(state, account_keys: set[str]) -> list[str]:
    """Directories the New-chat picker offers, one filesystem check per directory.

    ``_show_new_chat_cwd_option`` resolves the path and stats ``.git`` twice, and
    a deck's sessions share very few distinct directories — hundreds of chats in
    a handful of checkouts. Deduplicating first turns "a burst of realpaths per
    session" into "one per candidate directory", which is what the sorted set was
    already collapsing to anyway (#766).
    """
    candidates = frozenset(
        str(session.cwd)
        for session in state.all_sessions()
        if session.account_key in account_keys and session.cwd is not None
    )
    # The checks ran on every front-page open, about a tenth of a second on the event loop
    # for some fifty directories (#2841). Whether a directory is a checkout or a linked
    # worktree does not change while it is in use, so the last answer stands until the set
    # of chat directories itself changes.
    global _cwd_options_memo
    if _cwd_options_memo is not None and _cwd_options_memo[0] == candidates:
        return list(_cwd_options_memo[1])
    found = sorted(cwd for cwd in candidates if _show_new_chat_cwd_option(Path(cwd)))
    _cwd_options_memo = (candidates, tuple(found))
    return found


# The last ``_new_chat_cwd_options`` answer, keyed by the directories it checked.
_cwd_options_memo: tuple[frozenset[str], tuple[str, ...]] | None = None


def _advanced_summary(defaults: dict[str, str]) -> str:
    """The read-only line above the new-chat form's collapsed advanced block.

    ``choice_label`` answers with the sentinel ``"account"`` when the deck holds no
    explicit default, which reads as nonsense twice in one line — "account ·
    account" tells a reader nothing about what the chat will actually use (#1912).
    """
    parts = [part for part in (defaults["model"], defaults["effort"]) if part]
    if parts and all(part == "account" for part in parts):
        return "Account defaults"
    return " · ".join(
        "account default" if part == "account" else part for part in parts
    )


def _session_list_controls_context(
    request: Request, accounts, state, *, include_cwd_options: bool = True
) -> dict[str, object]:
    """Build the controls that accompany every full session list.

    ``include_cwd_options`` is False for a render that does not mount the session
    list — the in-page chat open, whose response is only ``.session-detail``. The
    directory picker lives in ``partials/session_list_controls.html`` alone, so
    scanning the deck's directories for it there is pure latency on the one
    action this page exists to make fast (#766).
    """
    new_chat_accounts = [
        account
        for account in accounts
        if PROVIDERS[account.provider_id].offers_new_session(account)
    ]
    new_chat_account_keys = {account.key for account in new_chat_accounts}
    # Model options for each provider present in the create form; the picker JS
    # shows only those matching the selected account's provider.
    new_chat_models = [
        {"provider_id": provider_id, "value": choice.value, "label": choice.label}
        for provider_id in sorted({account.provider_id for account in new_chat_accounts})
        for choice in PROVIDERS[provider_id].selectable_models
    ]
    new_chat_efforts = [
        {"provider_id": provider_id, "value": choice.value, "label": choice.label}
        for provider_id in sorted({account.provider_id for account in new_chat_accounts})
        for choice in PROVIDERS[provider_id].selectable_efforts
    ]
    initial_provider_id = (
        new_chat_accounts[0].provider_id if new_chat_accounts else None
    )
    # The same meters the sticky usage header reads, so the account buttons and the
    # header can never disagree about what an account has left (#1912). Rendered
    # with the page and never refreshed in place: this control holds a selection.
    from .render import _usage_rows

    new_chat_usage = {
        row["account"].key: row
        for row in _usage_rows(
            new_chat_accounts, state, getattr(request.app.state, "auth_watch", None)
        )
    }
    new_chat_defaults = {}
    for account in new_chat_accounts:
        provider = PROVIDERS[account.provider_id]
        defaults = effective_new_session_defaults(request, account, provider)

        new_chat_defaults[account.key] = {
            "model": choice_label(defaults.model, provider.selectable_models),
            "effort": choice_label(defaults.effort, provider.selectable_efforts),
        }
    cwd_options: list[str] = []
    default_cwd = ""
    if include_cwd_options:
        cwd_options = _new_chat_cwd_options(state, new_chat_account_keys)
        manual_cwd = get_db(request).load_manual_new_chat_cwd()
        # Membership first: an entry already in the list has been checked, and
        # re-resolving it is another round of filesystem work for nothing.
        if (
            manual_cwd
            and manual_cwd not in cwd_options
            and _show_new_chat_cwd_option(Path(manual_cwd))
        ):
            cwd_options.insert(0, manual_cwd)
        # A configured default always wins over the remembered last-used cwd.
        configured_default = request.app.state.config.inject.new_chat_default_cwd
        default_cwd = (
            configured_default or manual_cwd or (cwd_options[0] if cwd_options else "")
        )
        if (
            default_cwd
            and default_cwd not in cwd_options
            and _show_new_chat_cwd_option(Path(default_cwd))
        ):
            cwd_options.insert(0, default_cwd)

    return {
        "new_chat_accounts": new_chat_accounts,
        "new_chat_enabled": request.app.state.config.inject.enabled,
        "new_chat_cwds": cwd_options,
        "new_chat_default_cwd": default_cwd,
        "new_chat_models": new_chat_models,
        "new_chat_efforts": new_chat_efforts,
        # Match the first selected account before model_picker.js hydrates. A
        # deferred picker can arrive after first paint on a slow connection;
        # hiding these fields until then moved the whole session transcript.
        "new_chat_initial_has_models": any(
            model["provider_id"] == initial_provider_id for model in new_chat_models
        ),
        "new_chat_initial_has_efforts": any(
            effort["provider_id"] == initial_provider_id
            for effort in new_chat_efforts
        ),
        "new_chat_defaults": new_chat_defaults,
        "new_chat_usage": new_chat_usage,
        "new_chat_advanced_summary": (
            _advanced_summary(new_chat_defaults[new_chat_accounts[0].key])
            if new_chat_accounts
            else ""
        ),
    }


def _github_health() -> dict[str, object]:
    suspended = github_suspension.suspensions()
    return {
        "state": "suspended" if suspended else "ok",
        "suspended": [item.as_dict() for item in suspended],
    }


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    state = get_state(request)
    return JSONResponse(
        {
            "status": "ok",
            "accounts": len(get_accounts(request)),
            "sessions": len(state.sessions),
            "revision": getattr(request.app.state, "revision", None),
            # Not folded into `status`: the web process is up, and the deploy script
            # reads `status` as exactly that. This says whether GitHub still lets the
            # deck speak (#2682).
            "github": _github_health(),
        }
    )


@router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_access)])
async def settings_page(request: Request) -> HTMLResponse:
    """Deck-wide settings; anything account-scoped stays on /accounts/{key}."""
    from .settings_page import settings_context

    return get_templates(request).TemplateResponse(
        request, "settings.html", settings_context(request)
    )


def _process_chats(request: Request) -> dict:
    from ..host_processes import ChatOwner

    return {
        session.pid: ChatOwner(session.key, session.display_title, session.proc_start)
        for session in get_state(request).all_sessions()
        if session.pid
    }


async def _process_context(request: Request) -> dict:
    """Two /proc reads two seconds apart, off the event loop (#2663)."""
    from ..host_processes import sample_processes

    snapshot = await asyncio.to_thread(sample_processes, _process_chats(request))
    return {"snapshot": snapshot, "host": get_state(request).host_stats}


@router.get("/processes", response_class=HTMLResponse, dependencies=[Depends(require_access)])
async def processes_page(request: Request) -> HTMLResponse:
    """Who is using this host's CPU now. Measured only when opened or refreshed."""
    response = get_templates(request).TemplateResponse(
        request, "processes.html", await _process_context(request)
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "/partials/processes", response_class=HTMLResponse, dependencies=[Depends(require_access)]
)
async def processes_partial(request: Request) -> HTMLResponse:
    response = get_templates(request).TemplateResponse(
        request, "partials/process_tables.html", await _process_context(request)
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/pollers", response_class=HTMLResponse, dependencies=[Depends(require_access)])
async def pollers(request: Request) -> HTMLResponse:
    """The operator surface for this host's Poller Instances (#182).

    Everything shown is read from durable files and systemd, never from a live channel, so the
    page is honest about instances that are not running: an overdue instance reads as stale rather
    than as current.
    """
    from .. import project_source
    from .render import poller_context

    templates = get_templates(request)
    boards = await request.app.state.issues.resolve_instance_boards()
    shared_changes = await project_source.changed_sources(
        getattr(getattr(request.app.state.config, "poller", None), "root", project_source.ROOT)
    )
    request.app.state.poller_shared_changes = shared_changes
    return templates.TemplateResponse(
        request,
        "pollers.html",
        poller_context(request, boards=boards, shared_changes=shared_changes),
    )


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_access)])
async def dashboard(request: Request) -> Response:
    """The Work-first front page (#2219): Works in sections, the board in a line, and what no
    Work owns. No chat list; the chats outside every Work are at ``/chats`` (#2256)."""
    if request.query_params.get("chats") == "1":
        # `/?chats=1` opened the old deck's chat list (#1498): a stale link or history entry
        # lands on the chats view, never on the old deck (#2256).
        return RedirectResponse("/chats", status_code=307)
    from .work_view import StepTimes, front_row_html, row_renders

    times = StepTimes(getattr(request.state, "received_at", None))
    templates = get_templates(request)
    accounts = get_accounts(request)
    state = get_state(request)
    from .render import _usage_rows

    # The snapshot is the refresher's, so a page load costs no Run-trace reads unless nothing
    # has been built yet or it is stale — and nothing here builds the chat list or its
    # presentation. A stale one gets a moment to rebuild, so a reload does not paint old
    # cards and swap them a second later (#3108).
    work_overview = await request.app.state.work_overview.current(wait=FRESH_WAIT_S)
    times.lap("overview")
    rows = _usage_rows(accounts, state, request.app.state.auth_watch)
    new_work = new_work_form(request)
    times.lap("forms")
    controls = _session_list_controls_context(request, accounts, state)
    times.lap("controls")
    resp = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "work_overview": work_overview,
            # The rows its stream sends, from one shared render per revision (#3228).
            "row_html": front_row_html(work_overview, renders=row_renders(request.app.state)),
            "rows": rows,
            "host": state.host_stats,
            "new_work": new_work,
            **controls,
        },
    )
    times.lap("render")
    # Live dashboard — always revalidate so a deploy's HTML (and the inline
    # SSE-recovery JS) reaches the phone instead of a stale browser-cached copy.
    resp.headers["Cache-Control"] = "no-cache"
    _note_slow_page(resp, "front page", "/", times)
    return resp


# How long the front page waits for a stale Work overview's rebuild (#3108). A rebuild takes
# about 2-3 s on this host; one slower than this is pushed to the page when it lands.
FRESH_WAIT_S = 2.0

# A page open slower than this logs its steps, as the Work page's does (#2654, #2841).
SLOW_PAGE_MS = 1000.0


def _note_slow_page(resp: Response, page: str, what: str, times, *, header: bool = True) -> None:
    """Give a page its ``Server-Timing`` header, and log its steps when it was slow (#2841).

    ``lag`` is the time the request waited for the event loop before the handler ran.
    ``header`` is False where ``action_timing`` already writes the header for the request.
    """
    if header:
        resp.headers["Server-Timing"] = times.server_timing()
    if times.total_ms > SLOW_PAGE_MS:
        logger.warning("slow %s %s total=%.0fms %s", page, what, times.total_ms, times.summary())


@router.get(
    "/chats",
    response_class=HTMLResponse,
    dependencies=[Depends(require_access)],
)
async def chats_page(request: Request) -> HTMLResponse:
    """The chats no Work owns (#2256): the board strip and the chat list.

    The list is rendered once, here, and filtered by this page's own address (#2603): it
    stopped being the front page, so it no longer streams or filters in the browser. An
    old ``?chats=1`` is simply ignored. A Work's chats are left out; they are reached from
    the Work.
    """
    templates = get_templates(request)
    accounts = get_accounts(request)
    state = get_state(request)
    from ..guess import guessing_enabled
    from .render import (
        ChatListQuery,
        _usage_rows,
        board_strip_context,
        render_session_groups,
        session_list_context,
    )
    from .work_view import work_chat_ids

    presentation = state.session_presentation()
    session_context = session_list_context(
        accounts,
        presentation,
        injector=request.app.state.injector,
        assistant=request.app.state.assistant,
        query=ChatListQuery.from_params(request.query_params),
        work_chat_ids=work_chat_ids(request.app),
    )
    session_context["rendered_groups"] = render_session_groups(templates, session_context)
    resp = templates.TemplateResponse(
        request,
        "chats.html",
        {
            # The filters are remembered only while "Guess for me" is on (#3367).
            "remember_filters": guessing_enabled(request.app),
            "rows": _usage_rows(accounts, state, request.app.state.auth_watch),
            "host": state.host_stats,
            **session_context,
            **_session_list_controls_context(request, accounts, state),
            **board_strip_context(request, state),
        },
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@router.get(
    "/accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_access)],
)
async def accounts_page(request: Request) -> HTMLResponse:
    from .render import _usage_rows

    accounts = get_accounts(request)
    rows = _usage_rows(accounts, get_state(request), request.app.state.auth_watch)
    account_rows = []
    for row in rows:
        account = row["account"]
        provider = PROVIDERS[account.provider_id]
        defaults = effective_new_session_defaults(request, account, provider)
        account_rows.append(
            {
                **row,
                "default_model": choice_label(
                    defaults.model, provider.selectable_models
                ),
                "default_effort": choice_label(
                    defaults.effort, provider.selectable_efforts
                ),
            }
        )
    return get_templates(request).TemplateResponse(
        request, "accounts.html", {"account_rows": account_rows}
    )


@router.get(
    "/accounts/{account_key}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_access)],
)
async def account_page(request: Request, account_key: str) -> HTMLResponse:
    from .account_settings import account_settings_context
    from .render import _usage_rows

    account = next(
        (item for item in get_accounts(request) if item.key == account_key), None
    )
    if account is None:
        raise HTTPException(status_code=404, detail="unknown account")
    provider = PROVIDERS[account.provider_id]
    row = _usage_rows(
        [account], get_state(request), request.app.state.auth_watch
    )[0]
    return get_templates(request).TemplateResponse(
        request,
        "account.html",
        {
            **account_settings_context(request, account, provider),
            "row": row,
            "planned_reset_at": get_db(request).planned_resets().get(account.key),
            "auth": request.app.state.auth_watch.status(account.key),
            "auth_flow": request.app.state.auth_watch.flow(account.key),
        },
    )


@router.get(
    "/chats/{chat_id}/transcript.json",
    response_class=JSONResponse,
    dependencies=[Depends(require_access)],
    name="chat_transcript",
)
async def chat_transcript(request: Request, chat_id: str) -> JSONResponse:
    """The chat URL's transcript, so ``<session_url>/transcript.json`` works.

    A published chat URL is the one a worker hands on to whoever continues its
    card, and the reader appends this path to it. Without this route the
    canonical URL would be the one form of the link that could not be read.
    """
    state = get_state(request)
    chat = state.chat_by_id(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="unknown chat")
    session = state.current_session_for_chat(chat)
    if session is None:
        raise HTTPException(status_code=404, detail="chat has no available session")
    from ..models import Capability

    accounts_by_key = {account.key: account for account in get_accounts(request)}
    encoded_events = []
    readable_leg_found = False
    for leg_index, session_key in enumerate(state.chat_session_keys(session.key)):
        leg_session = state.sessions.get(session_key)
        if leg_session is None:
            continue
        account = accounts_by_key.get(leg_session.account_key)
        if account is None or Capability.TRANSCRIPT not in leg_session.capabilities:
            continue
        readable_leg_found = True
        encoded_events.extend(
            await _encoded_transcript_events(
                request,
                account,
                leg_session,
                PROVIDERS[account.provider_id],
                leg_index=leg_index,
            )
        )
    if not readable_leg_found:
        raise HTTPException(status_code=404, detail="transcript unavailable")
    return _transcript_response(request, session, encoded_events)


async def _encoded_transcript_events(
    request: Request,
    account,
    session,
    provider: SessionProvider,
    *,
    leg_index: int,
) -> list[dict]:
    """Serialize one concrete leg while keeping its native action targets."""
    events = await provider.read_transcript(account, session)
    encoded_events = []
    for event in events:
        encoded = jsonable_encoder(event)
        # ``seq`` stays native to this leg because that is what the provider
        # takes; the chat identity is what an anchor or a link needs.
        encoded["chat_event_id"] = chat_event_id(leg_index, event.seq)
        encoded["image_urls"] = [
            str(
                request.url_for(
                    "transcript_image",
                    session_key=session.key,
                    seq=event.seq,
                    image_index=index,
                )
            )
            for index in range(len(event.image_media_types))
        ]
        encoded_events.append(encoded)
    return encoded_events


def _transcript_response(
    request: Request, session, encoded_events: list[dict]
) -> JSONResponse:
    response = JSONResponse(
        {
            "session_key": session.key,
            "account_key": session.account_key,
            "working_directory": str(session.cwd) if session.cwd else None,
            "session_url": str(
                request.url_for("chat_detail", chat_id=session.chat_id)
                if session.chat_id
                else request.url_for("session_detail", session_key=session.key)
            ),
            "events": encoded_events,
        }
    )
    response.headers["Cache-Control"] = "no-cache"
    return response


@router.get(
    "/sessions/{session_key}/transcript.json",
    response_class=JSONResponse,
    dependencies=[Depends(require_access)],
)
async def session_transcript(request: Request, session_key: str) -> JSONResponse:
    """Expose the complete normalized transcript for agent-to-agent handoffs."""
    account, session, provider = resolve_session(request, session_key)
    from ..models import Capability

    if Capability.TRANSCRIPT not in session.capabilities:
        raise HTTPException(status_code=404, detail="transcript unavailable")
    state = get_state(request)
    chat_keys = state.chat_session_keys(session.key)
    leg_index = chat_keys.index(session.key) if session.key in chat_keys else 0
    encoded_events = await _encoded_transcript_events(
        request, account, session, provider, leg_index=leg_index
    )
    return _transcript_response(request, session, encoded_events)


@router.get(
    "/sessions/starting/{token}",
    dependencies=[Depends(require_access)],
)
async def starting_chat(request: Request, token: str) -> RedirectResponse:
    """Send a start's stable link to whatever it has become (#1799).

    This used to be a waiting page you landed on the moment you pressed Start. Nothing
    presses through to it now — a start runs in the background and its chat arrives in
    the chat list on its own — but the link itself outlives the page: a Continue handoff
    writes it into a message for a person to click later, and the machine callbacks
    answer with it. So it forwards to the chat once the token names one, and to the chat
    list until it does; a start that never binds simply lands there too, where a chat
    that is running would be visible anyway.
    """
    context = await starting_chat_context(request, token)
    destination = context["chat_url"] or (
        f"/sessions/{context['session_key']}" if context["session_key"] else "/"
    )
    return RedirectResponse(destination, status_code=303)


def _live_page_links(request: Request, session_key: str, repository: str = "") -> dict:
    """The chat's ▶ Live prototype and ◇ Diagram chips (#3827): one live page holds both.
    The Diagram chip follows the Work page's button (``live_prototype.diagram_link``): the
    live page while the file is there, then the merged copy."""
    record = live_prototype.read(get_state(request).chat_identity_key(session_key)) or {}
    page = live_prototype.page_url(session_key)
    has_diagram = bool(record.get("diagram"))
    link = live_prototype.diagram_link(record, session_key, repository)
    return {
        # A page that holds only a diagram has no prototype to open.
        "live_prototype_url": page if record and (record.get("url") or not has_diagram) else None,
        "live_diagram_url": link[1] if link else None,
        "live_diagram_external": bool(link and link[2]),
    }


async def _render_session_detail(
    request: Request,
    session_key: str,
) -> HTMLResponse:
    session_pane = request.query_params.get("pane") == "extra"
    detail_navigation = (
        request.headers.get("x-agentdeck-session-navigation") == "detail"
        or session_pane
    )
    from .work_view import StepTimes

    times = StepTimes(getattr(request.state, "received_at", None))
    account, session, provider = resolve_session(request, session_key)
    templates = get_templates(request)
    accounts = get_accounts(request)
    state = get_state(request)
    # The deck chat (#3403) is titled the first time any of its pages opens, which is
    # where its first start's "Open it" link lands.
    is_deck_chat = meta_chat.is_deck_chat(state, get_db(request), session.key)
    if is_deck_chat:
        meta_chat.name_deck_chat(state, session.key)
    with timing_span(request, "transcript"):
        chat_transcript = await load_chat_transcript(
            chat_keys=state.chat_session_keys(session.key),
            current_session_key=session.key,
            sessions=state.sessions,
            accounts_by_key={item.key: item for item in accounts},
            providers=PROVIDERS,
            account_labels=session_labels(accounts),
            window=DETAIL_WINDOW,
            rollover_reasons=state.chat_rollover_reasons(session.key),
        )
    times.lap("transcript")
    detail = chat_transcript.detail
    session = state.sessions.get(session.key) or session
    # Only the current leg has a live queue behind it; a historical leg's
    # colliding seq must never adopt a pending row (#1265).
    observed_messages = request.app.state.injector.observe_transcript(
        session.key, detail.events
    )
    # A failed transcript read never reaches this point, so it cannot clear the
    # chat's attention colour (#2599): only a transcript the boss could read
    # counts as opening the chat.
    state.mark_session_opened(session.key)
    from datetime import UTC, datetime

    from ..models import ACTIVITY_LOOKBACK, Capability, SessionStatus
    from .render import (
        _usage_rows,
        pending_injection_messages,
        resolve_activity_label,
        session_head_review_chip,
        session_issue_chips,
        session_pull_request_chips,
    )

    recent_events = detail.events[-ACTIVITY_LOOKBACK:]
    last_ev = recent_events[-1] if recent_events else None
    live = session.status == SessionStatus.LIVE
    progress_at = session.last_progress or session.last_activity
    age = (
        (datetime.now(UTC) - progress_at).total_seconds()
        if progress_at
        else 1e9
    )
    presentation = state.session_presentation()
    label = resolve_activity_label(
        has_question=bool(session.question),
        live=live,
        streaming=bool(session.thinking),
        last_event=last_ev,
        age_s=age,
        has_working_subagent=presentation.has_working_subagent(session),
        lifecycle_active=session.lifecycle_active,
        recent_events=recent_events,
    )
    effective_session = presentation.display(session)
    assistant = request.app.state.assistant
    # Read before the fragment view: the PR chips are a live fragment, so the
    # first paint and every later SSE push must render from the same context.
    # This read never shells out — an unresolved context refreshes behind the
    # page and arrives on the stream (see ``Assistant.session_context``, #766).
    with timing_span(request, "git_context"):
        git_context = assistant.session_context(session)
    pull_requests = session_pull_request_chips(git_context)
    interaction = provider.pending_interaction(account, session)
    fragment_view = SessionView(
        current=session,
        effective=effective_session,
        busy=label is not None,
        label=label,
        age=age,
        active_child_sessions=presentation.active_child_sessions(session),
        interaction=interaction,
        issues=session_issue_chips(session),
        pull_requests=pull_requests,
        head_review=session_head_review_chip(session, git_context, state),
    )
    times.lap("chips")
    labels = session_labels(accounts)
    account_label = labels.get(session.account_key)
    # A chat page carries no chat list and no New chat form (#2603), so the directory
    # picker that form needs is never scanned for here.
    controls_context = _session_list_controls_context(
        request, accounts, state, include_cwd_options=False
    )
    # The one Work that owns the chat, if any — for a shared chat, the one it was opened from
    # (#2567). The chat's header links up to it, however the chat was opened (#2512), and
    # opening the chat counts as looking at that Work: a phone's front-page row opens the
    # chat instead of the Work page. A chat page loaded on its own also sits beside the front
    # page and that Work — the columns opening it from the front page builds (#2346); only
    # the full page mounts them, through ``work_column_path``.
    from ..work_overview import work_href
    from .work_view import chat_map_strip, chat_work_actions, chat_work_id, note_work_opened

    work_id = chat_work_id(
        request.app,
        state.chat_identity_key(session.key),
        session_key=session.key,
        wanted=request.query_params.get("work", ""),
    )
    work_path = work_href(work_id) if work_id is not None else None
    # The Work's own buttons under the chat's Issue/PR chips (#2972), read once on load.
    work_card, work_buttons = chat_work_actions(
        request.app,
        work_id,
        (state.chat_path_for_session(session.key), f"/sessions/{session.key}"),
    )
    if work_id is not None:
        note_work_opened(request.app, work_id)
    # Continuing in the same account is allowed: the new chat re-reads the source
    # transcript, so it works whether or not the destination account matches.
    continue_chat_accounts = list(controls_context["new_chat_accounts"])
    inject_failure_message, inject_failure_resets_at = _inject_failure(
        request, session.key, request.app.state.injector.status(session.key)
    )
    # "Also post to issue" (#2416) is offered only when the chat belongs to exactly one Work.
    issue_post_target = issue_post.issue_post_target(state, session.key)
    times.lap("context")
    resp = templates.TemplateResponse(
        request,
        "session.html",
        {
            "detail_navigation": detail_navigation,
            "session_pane": session_pane,
            "session": effective_session,
            "session_fragments": render_session_fragments(templates, fragment_view),
            "inject_failure_message": inject_failure_message,
            "inject_failure_resets_at": inject_failure_resets_at,
            "worker_exit_message": worker_exit_message(
                effective_session.worker_exit_reason
            ),
            "detail": detail,
            "rendered_transcript": render_chat_transcript(
                templates,
                chat_transcript,
                observed_messages=observed_messages,
                store=getattr(request.app.state, "control_db", None),
                chat_id=state.chat_identity_key(session.key),
            ),
            "rendered_chat_notes": render_chat_notes(
                templates,
                getattr(request.app.state, "control_db", None),
                state.chat_identity_key(session.key),
            ),
            # Chat-level, so a switch while the page is open leaves the stream a
            # cursor that still says which leg the reader had reached (#1267).
            "transcript_after_seq": chat_transcript.current.event_id(
                chat_transcript.after_seq
            ),
            "work_path": work_path,
            "work_column_path": None if detail_navigation else work_path,
            "work_id": work_id,
            # Opened just now: the top bar's list leaves it out before the rebuild has (#3289).
            "seen_now": (work_id,) if work_id is not None else (),
            "work_card": work_card,
            "work_buttons": work_buttons,
            # A wayfinder ticket's chat (#3696): its map above the question form.
            "map_strip": chat_map_strip(request.app, work_id),
            "pending_messages": pending_injection_messages(
                request.app.state.injector.status(session.key)
            ),
            # A pending row in a chat that is mid-turn must not claim the agent
            # ignored it (#2016).
            "session_busy": request.app.state.injector.session_busy(session.key),
            "held_messages": request.app.state.scheduled_messages.visible(
                state.chat_identity_key(session.key)
            ),
            # which account (main/alt) this session belongs to
            "account_label": account_label,
            # The deck chat (#3403): its top-bar link reads as the current page.
            "is_deck_chat": is_deck_chat,
            "can_inject": (
                request.app.state.config.inject.enabled
                and (
                    Capability.INJECT in session.capabilities
                    or request.app.state.injector.can_queue(session.key)
                    # A send while the runtime restarts is held for it (#2847).
                    or provider.runtime_unreachable(account, session)
                )
            ),
            "inject_max_chars": request.app.state.config.inject.max_message_chars,
            "issue_post_target": issue_post_target,
            "issue_post_notice": issue_post.notice(
                getattr(request.app.state, "control_db", None), issue_post_target
            ),
            "composer_suggestions": [
                asdict(item) for item in composer_suggestions(account, session.cwd)
            ],
            "composer_phrases": composer_phrases(get_db(request)),
            "session_efforts": (
                provider.selectable_efforts
                if Capability.EFFORT in session.capabilities
                else ()
            ),
            **session_title_context(request, effective_session),
            **session_model_preference_context(request, effective_session, provider),
            "model_preference_available": bool(provider.selectable_models),
            "permission_prompts_available": (
                Capability.PERMISSION_PROMPTS in session.capabilities
            ),
            "permission_prompts_enabled": provider.permission_prompts_allowed(
                account, session
            ),
            "permission_prompt_error": None,
            "bypass_permissions_available": (
                Capability.BYPASS_PERMISSIONS in session.capabilities
            ),
            "bypass_permissions_enabled": provider.bypass_permissions_on(account, session),
            "bypass_permissions_error": None,
            **mac_runner_session_context(request, session.key),
            **side_chat_context(request, session.key),
            **rollback_context(request, session.key),
            "voice_log_saved": voice_helper.has_saved(
                get_state(request).chat_session_keys(session.key)
            ),
            **_live_page_links(
                request, session.key, git_context.repository if git_context else ""
            ),
            "new_work": new_work_form(
                request,
                session_key=session.key,
                issues=session.issues,
                repository=git_context.repository if git_context else None,
            ),
            "permission_prompts_next_turn": provider.permission_prompts_next_turn(
                account, session
            ),
            "continue_chat_accounts": (
                continue_chat_accounts
                if controls_context["new_chat_enabled"]
                and Capability.TRANSCRIPT in session.capabilities
                and session.cwd is not None
                else []
            ),
            "continue_chat_can_handoff": (
                request.app.state.config.inject.enabled
                and session_can_write_handoff(session, account.provider_id)
            ),
            "git_context": git_context,
            # topbar usage bars, rendered server-side so they paint immediately
            # (the per-session SSE stream then keeps them live over one socket).
            "rows": _usage_rows(accounts, state, request.app.state.auth_watch),
            "host": state.host_stats,
            "active_account_key": session.account_key,
            **controls_context,
        },
    )
    resp.headers["Cache-Control"] = "no-cache"
    # The same URL answers with and without the deck sidebar depending on this
    # header. Say so, so no cache can hand the sidebar-less body to a plain
    # visit — a chat page with no list under it has no way back (#2047).
    resp.headers["Vary"] = "X-AgentDeck-Session-Navigation"
    times.lap("render")
    if not detail_navigation:
        # The in-page open has its own timing header (``action_timing``); this is the full
        # page a phone or a fresh tab loads.
        _note_slow_page(
            resp, "chat page", session.key, times, header=action_timing(request) is None
        )
    return resp


@router.get(
    "/chats/{chat_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_access)],
    name="chat_detail",
)
async def chat_detail(
    request: Request,
    chat_id: str,
) -> HTMLResponse:
    state = get_state(request)
    chat = state.chat_by_id(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="unknown chat")
    session = state.current_session_for_chat(chat)
    if session is None:
        raise HTTPException(status_code=404, detail="chat has no available session")
    return await _render_session_detail(request, session.key)


@router.get(
    "/sessions/{session_key}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_access)],
    name="session_detail",
)
async def session_detail(
    request: Request,
    session_key: str,
) -> HTMLResponse:
    """Permanent alias from any concrete leg to its durable chat.

    A chat id answers here too: a Work row whose chat had no leg in the snapshot
    links ``/sessions/<chat id>``, which used to 404 as an unknown session (#3240).
    """
    state = get_state(request)
    chat = state.chat_for_session(session_key)
    if chat is None and session_key not in state.sessions:
        chat = state.chat_by_id(session_key)
    if chat is not None:
        session = state.current_session_for_chat(chat)
        if session is None:
            raise HTTPException(status_code=404, detail="chat has no available session")
        session_key = session.key
    return await _render_session_detail(request, session_key)
