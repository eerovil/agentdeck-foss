"""The deck chat's address (#3403): ``GET /meta`` and the first start behind it, the
view change it asks every open page to make (#3814), and the jump it makes its own page
take to another chat (#3816).

Once the deck chat exists, ``/meta`` is only a redirect to it; every later message
goes through the chat's own composer, whose send route starts a fresh leg after an
idle hour (``routes_actions._deck_chat_fresh_leg``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from .. import meta_chat
from ..attachments import cleanup_uploaded_files, display_message
from ..chat_jump import MAX_LISTED, work_matches
from ..providers import PROVIDERS
from ..view_commands import VIEW_TOPIC, UnknownProject
from . import start_spares
from .action_timing import bind_action, timing_span
from .chat_successor import blocking_start_is_live
from .deps import get_accounts, get_config, get_db, get_injector, get_state, get_templates
from .routes_actions import (
    _claim_start_spare,
    _reject_reserved_secret_command,
    _render_start_accepted,
    _render_start_refusal,
    _require_same_origin,
    _settle_start_spare,
    _turn_form,
)

router = APIRouter()

RESULT_ID = "new-session-result"
_EPOCH = datetime.min.replace(tzinfo=UTC)


def _starting_token(request: Request) -> str | None:
    """A first start that may still become the deck chat, so no second one begins.

    The same rule a Continue's fence uses (``blocking_start_is_live``): a start this
    process still sees running, or one whose record a web restart dropped while it is
    young enough that the runtime may still be spawning it. Only a start known to have
    failed, or one lost for longer than that, frees the way for another.
    """
    injector = get_injector(request)
    for token, kind, created_at in reversed(get_db(request).deck_chat_starts()):
        if kind == meta_chat.FIRST and blocking_start_is_live(injector, token, created_at):
            return token
    return None


@router.get("/meta", response_model=None)
async def deck_chat(request: Request) -> Response:
    state = get_state(request)
    session_key = meta_chat.deck_chat_session_key(state, get_db(request))
    if session_key is not None:
        meta_chat.name_deck_chat(state, session_key)
        path = state.chat_path_for_session(session_key) or f"/sessions/{session_key}"
        return RedirectResponse(path, status_code=303)
    account = meta_chat.resolve_account(
        get_accounts(request), get_config(request).meta_chat.account
    )
    return get_templates(request).TemplateResponse(
        request,
        "meta_chat.html",
        {"account": account, "starting_token": _starting_token(request)},
    )


@router.post("/meta/new", response_class=HTMLResponse)
async def start_deck_chat(request: Request) -> HTMLResponse:
    """Start the deck chat with its brief; refused while one already exists."""
    _require_same_origin(request)
    message, uploads, _form = await _turn_form(request)
    # A secret belongs in an existing chat's box, never in a first message (#1171).
    _reject_reserved_secret_command(message, uploads)

    def refuse(text: str, status: int = 422) -> HTMLResponse:
        cleanup_uploaded_files(uploads.paths)
        return _render_start_refusal(request, text, result_id=RESULT_ID, status_code=status)

    state = get_state(request)
    db = get_db(request)
    if meta_chat.deck_chat_session_key(state, db) is not None or _starting_token(request):
        return refuse("The deck chat already exists. Reload this page to open it.", 409)
    config = get_config(request).meta_chat
    account = meta_chat.resolve_account(get_accounts(request), config.account)
    if account is None:
        return refuse(
            "No account for the deck chat: set [meta_chat] account to a configured label."
        )
    provider = PROVIDERS[account.provider_id]
    undeliverable = provider.user_message_refusal(message)
    if undeliverable is not None:
        return refuse(undeliverable)
    try:
        cwd = Path(config.cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return refuse("The deck chat's folder ([meta_chat] cwd) is not usable.")
    # A CLI already booted for the deck chat takes the message at once (#3420). The
    # refusal above already answers a resend of this press, so it needs no action id.
    spare = _claim_start_spare(
        request,
        start_spares.DECK_CHAT,
        account_key=account.key,
        cwd=str(cwd),
        model=None,
        effort=None,
        action_id=None,
    )
    token = spare[0] if spare is not None else uuid.uuid4().hex
    db.record_deck_chat_start(token, meta_chat.FIRST)
    try:
        with timing_span(request, "queue"):
            result = await get_injector(request).start_new(
                account,
                provider,
                cwd,
                meta_chat.with_brief(message),
                list(uploads.images),
                files=[upload.path for upload in uploads.files],
                client_action_id=bind_action(request, provider=account.provider_id),
                session_token=token,
                allow_over_budget=True,
                origin="deck_chat",
                display_message_override=display_message(message),
                **({"worker_key": spare[1]} if spare is not None else {}),
            )
    except BaseException:
        _settle_start_spare(request, start_spares.DECK_CHAT, spare, account, accepted=False)
        cleanup_uploaded_files(uploads.paths)
        raise
    _settle_start_spare(
        request, start_spares.DECK_CHAT, spare, account, accepted=result.accepted
    )
    if not result.accepted:
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(status_code=422, detail=result.reason)
    return await _render_start_accepted(request, token, result_id=RESULT_ID)


class ProjectFilter(BaseModel):
    # The project to show alone, by the name on its tab; "" or "all" shows every project.
    project: str = ""


@router.post("/api/view/project-filter")
async def view_project_filter(request: Request, body: ProjectFilter) -> JSONResponse:
    """Switch the Work list's project tabs on every open page (#3814).

    The deck chat calls this when the owner asks, often by voice, to see one project or
    all of them. Every page stream carries the command (``view_commands``); ``pages`` is
    how many page streams were listening, so 0 means no page saw it.
    """
    _require_same_origin(request)
    service = getattr(request.app.state, "work_overview", None)
    overview = await service.current() if service is not None else None
    projects = sorted(
        {card.project for card in (overview.cards if overview else ()) if card.project},
        key=str.casefold,
    )
    try:
        command = request.app.state.view_commands.show_project(body.project, projects)
    except UnknownProject:
        return JSONResponse(
            {"error": f"no project named {body.project!r}", "projects": projects},
            status_code=404,
        )
    return JSONResponse(
        {
            "project": command.project or "all",
            "pages": get_state(request).bus.subscribers(VIEW_TOPIC),
        }
    )
@router.get("/api/view/chats")
async def view_chats(request: Request) -> JSONResponse:
    """The chats the deck chat can open on its page (#3816), most recently active first,
    each with its ``key``, ``title``, ``project``, the issues it works on, and whether it
    is working now."""
    state = get_state(request)
    deck_key = meta_chat.deck_chat_session_key(state, get_db(request))
    deck = state.chat_identity_key(deck_key) if deck_key is not None else None
    # Idle chats too, one row per chat (its newest leg): a Work's chat mostly rests.
    newest = sorted(
        (s for s in state.all_sessions() if s.parent_session_key is None),
        key=lambda session: session.last_activity or _EPOCH,
        reverse=True,
    )
    chats: dict[str, object] = {}
    for session in newest:
        chats.setdefault(state.chat_identity_key(session.key), session)
    chats.pop(deck, None)
    sessions = list(chats.values())
    return JSONResponse(
        {
            "chats": [
                {
                    "key": session.key,
                    "title": session.display_title,
                    "project": session.project_name,
                    "issues": [f"{ref.repository}#{ref.number}" for ref in session.issues],
                    "working": session.thinking,
                    "last_activity": (
                        session.last_activity.isoformat() if session.last_activity else None
                    ),
                }
                for session in sessions[:MAX_LISTED]
            ]
        }
    )


class OpenChat(BaseModel):
    # A chat's session key, from ``GET /api/view/chats``.
    chat: str = ""
    # Or a Work, as ``owner/repo#n``, ``repo#n`` or its number: the chat it opens into.
    work: str = ""


@router.post("/api/view/open-chat")
async def view_open_chat(request: Request, body: OpenChat) -> JSONResponse:
    """Open a chat on every page showing the deck chat (#3816).

    The deck chat calls this when the owner asks, often by voice, to go to another chat;
    voice mode carries on there. ``pages`` is how many pages showing the deck chat heard
    it, so 0 means none moved. A Work number several projects share answers 409 with
    their ids, and an unknown one 404.
    """
    _require_same_origin(request)
    state = get_state(request)
    db = get_db(request)
    if body.chat.strip():
        session = state.sessions.get(body.chat.strip())
        if session is None:
            return JSONResponse({"error": f"no chat {body.chat.strip()!r}"}, status_code=404)
        key = session.key
        path = state.chat_path_for_session(key)
        title = session.display_title
    elif body.work.strip():
        service = getattr(request.app.state, "work_overview", None)
        overview = await service.current() if service is not None else None
        cards = work_matches(overview.cards if overview else (), body.work)
        if not cards:
            return JSONResponse({"error": f"no Work {body.work.strip()!r}"}, status_code=404)
        if len(cards) > 1:
            return JSONResponse(
                {
                    "error": f"several Works are {body.work.strip()!r}",
                    "works": [card.work_id for card in cards],
                },
                status_code=409,
            )
        card = cards[0]
        if not card.chat_href:
            return JSONResponse({"error": f"{card.work_id} has no chat to open"}, status_code=404)
        key, path, title = card.chat_key, card.chat_href, card.title
    else:
        raise HTTPException(status_code=400, detail="name a chat or a Work")
    if key and meta_chat.is_deck_chat(state, db, key):
        return JSONResponse({"error": "that is the deck chat itself"}, status_code=409)
    jumps = request.app.state.chat_jumps
    jump = jumps.jump(path, title)
    return JSONResponse({"opened": jump.path, "title": jump.title, "pages": jumps.listening()})
