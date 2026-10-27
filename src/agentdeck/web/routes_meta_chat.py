"""The deck chat's address (#3403): ``GET /meta`` and the first start behind it.

Once the deck chat exists, ``/meta`` is only a redirect to it; every later message
goes through the chat's own composer, whose send route starts a fresh leg after an
idle hour (``routes_actions._deck_chat_fresh_leg``).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import meta_chat
from ..attachments import cleanup_uploaded_files, display_message
from ..providers import PROVIDERS
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
