"""The message box's side chat (#4313): send to it, and show it in the main chat's place.

Each chat has one side chat. The box's switch sends a message there instead of to the
chat: the first starts it as a copy of the chat cut at its newest finished turn, through
the same start a reply's side chat button makes (#2485); every later one is queued to that
same side chat. Its own talk is shown in the chat's place on the page, read from here and
then from its own stream.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..models import ForkFrom
from .action_timing import bind_action, supplied_action_id
from .deps import get_injector, get_state, get_templates, require_access, resolve_current_leg
from .routes_actions import _note_person_message, _require_same_origin, _turn_form
from .side_chat import SIDE, find_side_chat, moved_on, side_brief, side_view_context
from .uploads import cleanup_uploaded_files

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_access)])

# One send per chat at a time: finding the side chat and starting one are several awaits
# apart, so two sends at once would each start one.
_LOCKS: dict[str, asyncio.Lock] = {}


async def _view(request: Request, session_key: str, *, status_code: int = 200) -> HTMLResponse:
    context = await side_view_context(request, session_key, find_side_chat(request, session_key))
    response = get_templates(request).TemplateResponse(
        request, "partials/side_chat_view.html", context
    )
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/partials/sessions/{session_key}/side-chat", response_class=HTMLResponse)
async def side_chat_view(request: Request, session_key: str) -> HTMLResponse:
    """The side chat's own talk, for the main chat's page to show in its place."""
    _account, session, _provider = resolve_current_leg(request, session_key)
    return await _view(request, session.key)


@router.post("/sessions/{session_key}/side-chat", response_class=HTMLResponse)
async def side_chat_send(request: Request, session_key: str) -> HTMLResponse:
    """Send the box's message to the chat's side chat, starting it if there is none.

    A press carries the client's action ID, so a resent press reaches the side chat at
    most once: a start is found again by that ID, and a send is queued under it.
    """
    _require_same_origin(request)
    if supplied_action_id(request) is None:
        raise HTTPException(status_code=422, detail="The press carried no action ID; reload.")
    _account, main, _provider = resolve_current_leg(request, session_key)
    async with _LOCKS.setdefault(get_state(request).chat_identity_key(main.key), asyncio.Lock()):
        return await _send(request, main.key)


async def _send(request: Request, session_key: str) -> HTMLResponse:
    state = get_state(request)
    action_id = supplied_action_id(request) or ""
    message, uploads, _form = await _turn_form(request, attachments_alone=True)

    def refuse(text: str, status_code: int = 409) -> HTTPException:
        cleanup_uploaded_files(uploads.paths)
        return HTTPException(status_code=status_code, detail=text)

    if state.chat_fork_by_action(action_id) is not None:
        # This press already started the side chat: show it, and start nothing more.
        cleanup_uploaded_files(uploads.paths)
        return await _view(request, session_key, status_code=202)
    images = list(uploads.images)
    files = [upload.path for upload in uploads.files]
    account, main, provider = resolve_current_leg(request, session_key)
    side = find_side_chat(request, main.key)
    if side.state == "starting":
        raise refuse("The side chat is still starting; send this again once it is up.")
    newest = await asyncio.to_thread(provider.newest_fork, account, main)
    injector = get_injector(request)
    if side.state == "live" and side.session_key is not None and side.fork is not None:
        side_account, side_session, side_provider = resolve_current_leg(request, side.session_key)
        text = moved_on(request, side.fork, main.key, newest) + message
        refusal = side_provider.user_message_refusal(text)
        if refusal is not None:
            raise refuse(refusal, 422)
        result = await injector.start(
            side_account,
            side_session,
            side_provider,
            text,
            images,
            files=files,
            client_action_id=bind_action(
                request, session_key=side_session.key, provider=side_account.provider_id
            ),
            allow_over_budget=True,
        )
        if not result.accepted:
            # The write may have happened: say so with a 5xx, so the page keeps the action
            # id and sending the same words again is delivered once.
            status = 503 if result.write_uncertain or result.runtime_unreachable else 409
            raise refuse(result.reason or "The side chat did not take the message.", status)
        if newest is not None:
            state.note_side_chat_seen(side.fork.token, main.key, newest[0])
        # A person's message to the side chat, which a Work counts as its own (#4562).
        _note_person_message(request, side_session.key)
        return await _view(request, main.key, status_code=202)
    # Told it is a side chat, so it does not take itself for the chat it copies (#4556).
    message = side_brief(main.key) + message
    refusal = provider.fork_refusal(account, main) or provider.user_message_refusal(message)
    if refusal is not None:
        raise refuse(refusal)
    if newest is None:
        raise refuse("This chat has no finished reply to copy yet, so no side chat can start.")
    seq, cut = newest
    model, effort = provider.fork_settings(account, main)
    token = uuid.uuid4().hex
    owner = state.reserve_chat_fork(token, main.key, seq, action_id, kind=SIDE, cut_ref=cut)
    if owner is None:
        raise refuse("The deck could not record the side chat, so it was not started.")
    if owner != token:
        cleanup_uploaded_files(uploads.paths)
        return await _view(request, main.key, status_code=202)
    try:
        result = await injector.start_new(
            account,
            provider,
            main.cwd,
            message,
            images,
            files=files,
            model=model,
            effort=effort,
            client_action_id=bind_action(
                request, session_key=main.key, provider=account.provider_id
            ),
            session_token=token,
            allow_over_budget=True,
            origin="fork",
            source_session_key=main.key,
            fork_from=ForkFrom(main.session_id, cut),
        )
    except BaseException:
        # As a reply's side chat button does: nothing started, so a resend is free to try.
        state.release_chat_fork(token)
        cleanup_uploaded_files(uploads.paths)
        raise
    if not result.accepted:
        state.release_chat_fork(token)
        raise refuse(result.reason or "The side chat could not start.")
    logger.info("side chat of %s started from line %s", main.key, seq)
    return await _view(request, main.key, status_code=202)
