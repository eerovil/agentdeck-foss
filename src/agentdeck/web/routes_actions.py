"""State-changing web actions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.datastructures import FormData

from .. import issue_post as issue_posts
from .. import live_chats, meta_chat, shutdown
from ..attachments import ATTACHMENT_HEADER, ATTACHMENT_MARKER, display_message
from ..chat_answer_relay import answers_parked_card, leave_answer
from ..chat_cwd import cannot_resume, continued_chat_cwd, cwd_exists
from ..composer_phrases import (
    ComposerPhrase,
    ComposerPhraseError,
    normalize_composer_phrases,
)
from ..config import owned_session_base_url
from ..db import ISSUE_POST_ACTION, SessionRunBinding
from ..head_review import review_brief, reviewable_commit
from ..images import media_type_for_suffix, sniff_suffix, suffix_for_media_type
from ..inject import InjectionStatus, unsent_messages
from ..models import (
    OPEN_QUESTIONS_PREFIX,
    Capability,
    ForkFrom,
    HeadReviewLaunch,
    InjectResult,
    open_questions_id,
)
from ..providers import PROVIDERS
from ..providers.codex.runtime_client import runtime_socket_path
from ..public_origin import public_origin as deck_public_origin
from ..run_execution import MAX_CONTINUATIONS, SAFE_STATES
from ..run_trace import read_outcomes
from ..scheduled_messages import RUNTIME_HOLD, HeldAttachment, ScheduledMessage
from ..session_secrets import (
    SecretCommand,
    SecretCommandError,
    SecretRuntimeError,
    issue_runtime_secret,
    parse_secret_command,
    revoke_runtime_secret,
    secret_notice,
)
from ..titles import MAX_TITLE_CHARS, normalize_agent_title, title_evidence_signature
from . import start_spares
from .account_settings import (
    account_settings_context,
    choice_label,
    saved_new_session_defaults,
)
from .action_timing import action_timing, bind_action, supplied_action_id, timing_span
from .chat_successor import LOST_RECHECKS, transcript_continuation
from .chat_successor import reserve_chat_successor as _reserve_chat_successor
from .deps import (
    delegation_account,
    get_accounts,
    get_config,
    get_control_db,
    get_db,
    get_injector,
    get_state,
    get_templates,
    require_access,
    resolve_current_leg,
    resolve_session,
)
from .render import (
    SESSION_FRAGMENTS_BY_NAME,
    SessionView,
    humanize_refusal,
    session_head_review_chip,
    session_issue_chips,
    session_pull_request_chips,
    start_failure_message,
)
from .side_chat import composer_target as side_chat_composer_target
from .side_chat import new_work_brief
from .uploads import (
    MAX_MULTIPART_OVERHEAD_BYTES,
    SavedUploads,
    UploadedFile,
    UploadError,
    cleanup_uploaded_files,
    save_uploaded_files,
)

log = logging.getLogger(__name__)

# How each quota window reads inside a sentence. Deliberately not
# ``capacity.WINDOW_TITLES``, which is bar-row shorthand ("7d", "Week"): this one
# has to finish "…usage in its {} window".


# The alphabet the timing middleware mints its IDs from. A press may carry its
# own so a retried request settles on one row; anything else gets a fresh one.
_CLIENT_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

router = APIRouter(dependencies=[Depends(require_access)])

def _require_same_origin(request: Request) -> None:
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site action refused")
    origin = request.headers.get("origin")
    if not origin:
        return
    parsed = urlsplit(origin)
    if parsed.scheme != request.url.scheme or parsed.netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="origin mismatch")


def _render_status(
    request: Request,
    session_key: str,
    status,
    queued_item=None,
    issue_post=None,
    replace_items=None,
) -> HTMLResponse:
    templates = get_templates(request)
    failure_message, failure_resets_at = _inject_failure(request, session_key, status)
    if replace_items is None:
        replace_items = unsent_messages(status)
    return templates.TemplateResponse(
        request,
        "partials/inject_status.html",
        {
            "session_key": session_key,
            "inject_status": status,
            "queued_item": queued_item,
            "replace_items": replace_items,
            # A ticked send's post outcome (#2416), swapped into the notice above the composer.
            "issue_post": issue_post,
            "session_busy": request.app.state.injector.session_busy(session_key),
            "inject_failure_message": failure_message,
            "inject_failure_resets_at": failure_resets_at,
        },
    )


def _held_chat_id(request: Request, session_key: str) -> str:
    return get_state(request).chat_identity_key(session_key)


def _held_rows(request: Request, session_key: str):
    return request.app.state.scheduled_messages.visible(_held_chat_id(request, session_key))


def _render_held_messages(request: Request, session_key: str) -> HTMLResponse:
    return get_templates(request).TemplateResponse(
        request,
        "partials/held_messages.html",
        {"session_key": session_key, "held_messages": _held_rows(request, session_key)},
    )


async def _hold_for_runtime(
    request: Request,
    session,
    provider,
    form: FormData,
    message: str,
    uploads: SavedUploads,
    secret_command,
    post_target,
    client_action_id: str | None,
) -> HTMLResponse:
    """Keep a send for a chat whose runtime is restarting, and send it once it is back (#2847).

    The message and its uploads wait in the store the wake-on messages use, under the
    page's action id, so a replay of this request finds the same row and the delivery
    is fenced exactly like an ordinary send. A temporary secret is never held: the
    runtime itself takes the value, and the deck never writes it down.
    """
    if secret_command is not None:
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(
            status_code=503,
            detail="The runtime is restarting; send the secret again in a minute.",
        )
    try:
        effort = _selected_effort(form, provider)
    except HTTPException:
        cleanup_uploaded_files(uploads.paths)
        raise
    # No EFFORT check here: the runtime grants it, so a held chat never shows it.
    # The choice is kept, and the delivery applies it once the runtime is back.
    undeliverable = provider.user_message_refusal(message)
    if undeliverable is not None:
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(status_code=422, detail=undeliverable)
    # The delivery would refuse it anyway; say so now rather than 30 seconds later.
    submitted = display_message(message) if uploads.files else message
    if get_config(request).inject.too_long(submitted.strip()):
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(status_code=422, detail="message is too long")
    if not client_action_id:
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(
            status_code=503,
            detail="The runtime is restarting; reload the page and send again in a minute.",
        )
    attachments = tuple(HeldAttachment(path, "image") for path in uploads.images) + tuple(
        HeldAttachment(upload.path, "file") for upload in uploads.files
    )
    created_at = datetime.now(UTC)
    held = request.app.state.scheduled_messages.schedule(
        ScheduledMessage(
            id=client_action_id,
            chat_id=_held_chat_id(request, session.key),
            text=message,
            effort=effort,
            due_at=created_at,
            created_at=created_at,
            hold_reason=RUNTIME_HOLD,
            attachments=attachments,
        )
    )
    if held is None:
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(status_code=409, detail="This message id is already in use.")
    # A replay of a send already held: the first request's files are the kept ones. A
    # generic file is saved at a path keyed by its action id and content, so a replay can
    # land on the very file the held row keeps; only the replay's other copies go.
    kept = {item.path for item in held.attachments}
    cleanup_uploaded_files([path for path in uploads.paths if path not in kept])
    # A parked card's answer is handed to the Poller only once it is delivered, from the
    # delivery path (``app.py``): a held message may still be cancelled, expire or lose its
    # files, and the card must not resume on an answer its worker never got (#2847 review).
    posted = None
    if post_target is not None:
        if await _answers_parked_card(request, session.key):
            # The Poller posts a parked card's answer itself, after the relay.
            posted = issue_posts.Outcome(issue_posts.HELD, post_target, action_id=client_action_id)
        else:
            # GitHub does not need the runtime, so the post goes now, as for any send.
            posted = await _post_to_issue(
                request,
                session.key,
                post_target,
                display_message(message),
                len(uploads.paths),
                client_action_id,
                relay=False,
            )
        request.state.issue_post_outcome = posted
    response = get_templates(request).TemplateResponse(
        request,
        "partials/held_message_receipt.html",
        {
            "session_key": session.key,
            "held_messages": _held_rows(request, session.key),
            "issue_post": posted,
        },
    )
    response.status_code = 202
    response.headers["X-AgentDeck-Action-State"] = "held"
    if posted is not None:
        response.headers["X-AgentDeck-Issue-Post"] = posted.code
    return response


def _inject_failure(
    request: Request, session_key: str, status
) -> tuple[str | None, datetime | None]:
    del request, session_key
    reason = status.reason if status is not None and status.state == "failed" else None
    if not reason:
        return None, None
    return humanize_refusal(reason), None


def _account_provider(request: Request, account_key: str):
    account = next((item for item in get_accounts(request) if item.key == account_key), None)
    if account is None:
        raise HTTPException(status_code=404, detail="unknown account")
    return account, PROVIDERS[account.provider_id]


def _selected_effort(form: FormData, provider) -> str | None:
    raw_effort = form.get("effort")
    effort = raw_effort.strip() if isinstance(raw_effort, str) else ""
    if effort and not provider.is_valid_effort(effort):
        raise HTTPException(status_code=422, detail="invalid effort")
    return effort or None


# The composer result slots a start can be watched in. The poll carries the id
# back as a query parameter, so it is matched against this rather than trusted:
# a strip rendered under an id no composer targets is a form that cannot start a
# second chat.
_START_RESULT_IDS = {
    "new-session-result": "new-session-result",
    "continue-session-result": "continue-session-result",
    "poller-build-result": "poller-build-result",
    # A side chat started from a Work's page (#2388, ``routes_works.NEW_CHAT_RESULT_ID``).
    "work-new-chat-result": "work-new-chat-result",
    # The deck chat's fresh start, watched in its composer's own slot (#3403).
    "inject-result": "inject-result",
}


async def _render_start_accepted(
    request: Request,
    token: str,
    *,
    result_id: str = "new-session-result",
) -> HTMLResponse:
    """Answer an accepted start: 202, and the strip that watches it in place.

    The press does not navigate anywhere (#1799). Spawning runs in the background
    and the chat arrives in the chat list on its own — already working — so the
    only thing left where the button was is the outcome of the *start*: a chat to
    open, or the reason there is not one.
    """
    context = await starting_chat_context(request, token)
    response = get_templates(request).TemplateResponse(
        request,
        "partials/new_session_status.html",
        {**context, "result_id": result_id},
    )
    response.status_code = 202
    return response


def _note_boss_turn(request: Request, session_key: str, form: FormData, text: str) -> None:
    """Treat an accepted write as the boss engaging with this chat."""
    state = get_state(request)
    state.note_boss_turn(session_key, text)
    # The accepted write is logically the next user turn even when the provider's
    # transcript has not caught up yet. Retire the old turn's report and owed push
    # now, so neither outlives the reply the boss just gave.
    request.app.state.assistant.handle_boss_turn(session_key)


def _public_chat_url(request: Request, session_key: str) -> str:
    chat_path = get_state(request).chat_path_for_session(session_key)
    origin = deck_public_origin()
    return f"{origin}{chat_path}" if origin and chat_path else ""


async def _relay_chat_answer(
    request: Request, session_key: str, text: str, action_id: str | None
) -> str:
    """Hand an accepted message to the card's Poller when it answers a parked worker (#2348).

    The message is already in the chat, so nothing here may fail the send. The answer says
    whether the Poller took it (``relayed``), the chat is no parked card's (``not_parked``), or
    that could not be told (``unknown``), which a ticked post to the issue must not guess past.
    """
    state = get_state(request)
    try:
        left = await asyncio.to_thread(
            leave_answer,
            db=getattr(state, "db", None),
            issues=getattr(request.app.state, "issues", None),
            session_key=session_key,
            text=text,
            request_id=action_id,
            chat_url=_public_chat_url(request, session_key),
        )
    except Exception:  # noqa: BLE001 - the send already succeeded
        log.exception("could not hand %s's answer to its Poller card", session_key)
        return "unknown"
    return "relayed" if left is not None else "not_parked"


def _issue_post_refusal(uploads, status_code: int, detail: str) -> HTTPException:
    cleanup_uploaded_files(uploads.paths)
    return HTTPException(status_code=status_code, detail=detail)


def _issue_post_target(request: Request, session, form, message: str, secret_command, uploads):
    """The Work a ticked "Also post to issue" posts to, or None when the box was not ticked.

    Every refusal comes before anything is sent. The box names the issue the page showed; the
    server's own target must still be that issue, so a page left open while the chat's Works
    changed can never post somewhere the person did not see (#2416).
    """
    shown = str(form.get("post_to_issue") or "").strip()
    if not shown:
        return None
    untick = "Untick “Also post to issue” to"
    if secret_command is not None:
        raise _issue_post_refusal(
            uploads, 422, f"A secret is never posted to an issue. {untick} send it."
        )
    if supplied_action_id(request) is None:
        raise _issue_post_refusal(
            uploads, 422, "Posting to the issue needs the page's action id; reload and send again."
        )
    if not display_message(message).strip():
        raise _issue_post_refusal(uploads, 422, "Nothing to post: the message has no text.")
    state = get_state(request)
    expected = getattr(request.state, "issue_post_work", None)
    if expected is not None:
        # The Work page posts to its own issue, whichever of its chats takes the message.
        works = state.work_projection().works_for_chat(state.chat_identity_key(session.key))
        actual = expected if expected in works else None
    else:
        actual = issue_posts.issue_post_target(state, session.key)
    if actual is None or issue_posts.shown_target(shown) != actual:
        raise _issue_post_refusal(
            uploads, 409, "This chat's issue changed. Reload the page before posting to the issue."
        )
    return actual


async def _answers_parked_card(request: Request, session_key: str) -> bool:
    try:
        return await asyncio.to_thread(
            answers_parked_card,
            db=getattr(get_state(request), "db", None),
            issues=getattr(request.app.state, "issues", None),
            session_key=session_key,
        )
    except Exception:  # noqa: BLE001 - then the post is not made now either
        log.exception("could not tell whether %s answers a parked card", session_key)
        return True


async def _post_to_issue(
    request: Request,
    session_key: str,
    target,
    text: str,
    attachments: int,
    action_id: str,
    *,
    relay: bool = True,
):
    """Post one accepted, ticked message to its Work's issue, at most once (#2416).

    The ledger is read first, so a retried send reports what its first attempt did and never
    relays or posts again. A parked card's answer goes to the Poller instead (#2348), which
    posts it; when that cannot be told, nothing is posted rather than risk two comments.
    """
    db = get_control_db(request)
    pressed = db.work_action(action_id)
    if pressed is not None:
        if pressed.action != ISSUE_POST_ACTION:
            # The id is already some other press's; it can never also stand for this post.
            return issue_posts.Outcome(issue_posts.UNRECORDED, target, action_id=action_id)
        return issue_posts.from_press(pressed, target)
    relay = (
        await _relay_chat_answer(request, session_key, text, action_id)
        if relay
        else "not_parked"
    )
    if relay == "relayed":
        return issue_posts.Outcome(issue_posts.RELAYED, target, action_id=action_id)
    if relay == "unknown":
        return issue_posts.Outcome(issue_posts.UNKNOWN, target, action_id=action_id)
    if shutdown.is_shutting_down():
        return issue_posts.Outcome(issue_posts.RESTARTING, target, action_id=action_id)
    reserved = db.reserve_work_action(action_id, target.work_id, ISSUE_POST_ACTION)
    if reserved is None:
        return issue_posts.Outcome(issue_posts.UNRECORDED, target, action_id=action_id)
    if reserved is False:
        # Reserved by a concurrent attempt since the read above: report what it recorded.
        pressed = db.work_action(action_id)
        if pressed is None or pressed.action != ISSUE_POST_ACTION:
            return issue_posts.Outcome(issue_posts.UNRECORDED, target, action_id=action_id)
        return issue_posts.from_press(pressed, target)
    comment = issue_posts.body(
        text,
        action_id=action_id,
        chat_url=_public_chat_url(request, session_key),
        attachments=attachments,
    )
    return await shutdown.protect(
        f"issue-post:{action_id}",
        issue_posts.post(db, action_id=action_id, target=target, comment=comment),
    )


def _interaction_answer_text(interaction, answers: dict[str, list[str]]) -> str:
    """Mirror the normalized answer text that becomes a user transcript event."""
    return "; ".join(
        f"{question.prompt}: {', '.join(answers[question.id])}"
        for question in interaction.questions
        if question.id in answers
    )


@router.get("/sessions/{session_key}/transcript-images/{seq}/{image_index}")
async def transcript_image(
    request: Request, session_key: str, seq: int, image_index: int
) -> Response:
    """Serve one bounded embedded transcript image without inlining it in HTML."""
    account, session, provider = resolve_session(request, session_key)
    image = await provider.transcript_image(account, session, seq, image_index)
    if image is None:
        raise HTTPException(status_code=404, detail="transcript image not found")
    media_type, content = image
    expected_extension = suffix_for_media_type(media_type)
    if (
        expected_extension is None
        or len(content) > get_config(request).inject.max_image_bytes
        or sniff_suffix(content) != expected_extension
    ):
        raise HTTPException(status_code=404, detail="transcript image not found")
    return Response(content, media_type=media_type, headers={"Cache-Control": "private, no-store"})


@router.get("/sessions/{session_key}/pending-images/{item_id}/{image_index}")
async def pending_message_image(
    request: Request, session_key: str, item_id: int, image_index: int
) -> Response:
    """Serve one validated upload while its message is still pending."""
    resolve_session(request, session_key)
    status = get_injector(request).status(session_key)
    item = next(
        (
            queued
            for queued in status.items
            if queued.id == item_id
            and queued.is_pending
        ),
        None,
    ) if status else None
    if item is None or image_index < 0 or image_index >= len(item.images):
        raise HTTPException(status_code=404, detail="pending image not found")
    image = item.images[image_index]
    media_type = media_type_for_suffix(image.suffix)
    if media_type is None:
        raise HTTPException(status_code=404, detail="pending image not found")
    try:
        content = image.read_bytes()
    except OSError:
        raise HTTPException(status_code=404, detail="pending image not found") from None
    return Response(content, media_type=media_type, headers={"Cache-Control": "private, no-store"})


def _message_with_files(message: str, files: tuple[UploadedFile, ...]) -> str:
    if not files:
        return message
    rows = "\n".join(
        f"- {json.dumps({'name': upload.name, 'path': str(upload.path)})}"
        for upload in files
    )
    return (
        f"{message}{ATTACHMENT_MARKER}\n"
        f"{ATTACHMENT_HEADER}\n"
        f"{rows}\n</agentdeck-attachments>"
    )


async def _turn_form(request: Request) -> tuple[str, SavedUploads, FormData]:
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        raise HTTPException(status_code=415, detail="form content type required")
    config = get_config(request).inject
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > (
        config.max_file_total_bytes + MAX_MULTIPART_OVERHEAD_BYTES
    ):
        raise HTTPException(status_code=413, detail="upload request is too large")
    with timing_span(request, "form"):
        form = await request.form(
            max_files=config.max_images,
            max_fields=32,
            max_part_size=config.max_image_bytes,
        )
    raw = form.get("message")
    message = raw.strip() if isinstance(raw, str) else ""
    if not message or config.too_long(message):
        raise HTTPException(status_code=422, detail="invalid message")
    if ATTACHMENT_MARKER in message:
        raise HTTPException(status_code=422, detail="message contains reserved attachment text")
    try:
        with timing_span(request, "uploads"):
            timing = action_timing(request)
            action_id = (
                timing.client_action_id if timing is not None else uuid.uuid4().hex
            )
            uploads = await save_uploaded_files(form, config, action_id=action_id)
    except UploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    message = _message_with_files(message, uploads.files)
    return message, uploads, form


def _secret_command_or_422(
    message: str, uploads: tuple[Path, ...] = ()
) -> SecretCommand | None:
    try:
        return parse_secret_command(message)
    except SecretCommandError as exc:
        cleanup_uploaded_files(uploads)
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _reject_reserved_secret_command(message: str, uploads: SavedUploads) -> None:
    if _secret_command_or_422(message, uploads.paths) is not None:
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(
            status_code=422,
            detail="Enter temporary secrets from an existing session's message box",
        )


def _render_composer_phrase_settings(
    request: Request,
    *,
    rows: list[ComposerPhrase] | None = None,
    error: str | None = None,
    saved: str | None = None,
) -> HTMLResponse:
    from .settings_page import composer_phrase_context

    # The form carries a native method/action so a submit that beats deferred htmx
    # still saves; that post wants the whole page back, not a bare fragment.
    template = (
        "partials/composer_phrase_settings.html"
        if request.headers.get("hx-request") == "true"
        else "settings.html"
    )
    context = composer_phrase_context(request, rows=rows, error=error, saved=saved)
    return get_templates(request).TemplateResponse(request, template, context)


@router.post("/settings/composer-phrases", response_class=HTMLResponse)
async def set_composer_phrases(request: Request) -> HTMLResponse:
    """Edit the ⚡ steering vocabulary; only a boss-shaped write may change it."""
    from .settings_page import submitted_phrase_rows

    _require_same_origin(request)
    form = await request.form()
    db = get_db(request)
    if form.get("action") == "reset":
        db.clear_composer_phrases()
        return _render_composer_phrase_settings(
            request, saved="Reset · the built-in phrases are back."
        )
    rows = submitted_phrase_rows(form)
    try:
        phrases = normalize_composer_phrases((row.label, row.text) for row in rows)
    except ComposerPhraseError as exc:
        # Re-render what was typed, not what is stored: a refused save must not
        # silently swallow the edit it refused.
        return _render_composer_phrase_settings(request, rows=rows, error=str(exc))
    db.record_composer_phrases(phrases)
    return _render_composer_phrase_settings(
        request,
        saved=(
            f"Saved · {len(phrases)} phrase{'' if len(phrases) == 1 else 's'}"
            " in the ⚡ menu."
        ),
    )


@router.post("/settings/guessing", response_class=HTMLResponse)
async def set_guessing(request: Request) -> HTMLResponse:
    """The "Guess for me" switch (#3367): a ticked box is on, anything else off."""
    _require_same_origin(request)
    form = await request.form()
    enabled = "1" in form.getlist("enabled")
    get_db(request).record_guessing_enabled(enabled)
    template = (
        "partials/guess_settings.html"
        if request.headers.get("hx-request") == "true"
        else "settings.html"
    )
    from .settings_page import settings_context

    context = {**settings_context(request), "guessing_saved": True}
    return get_templates(request).TemplateResponse(request, template, context)


def _render_permission_prompts(
    request: Request,
    account,
    session,
    provider,
    *,
    error: str | None = None,
) -> HTMLResponse:
    return get_templates(request).TemplateResponse(
        request,
        "partials/session_permission_prompts.html",
        {
            "session": session,
            "permission_prompts_enabled": provider.permission_prompts_allowed(
                account, session
            ),
            "permission_prompt_error": error,
            "permission_prompts_next_turn": provider.permission_prompts_next_turn(
                account, session
            ),
        },
    )


@router.post(
    "/sessions/{session_key}/permission-prompts", response_class=HTMLResponse
)
async def set_session_permission_prompts(
    request: Request, session_key: str
) -> HTMLResponse:
    _require_same_origin(request)
    account, session, provider = resolve_current_leg(request, session_key)
    if Capability.PERMISSION_PROMPTS not in session.capabilities:
        raise HTTPException(status_code=403, detail="permission prompt override unavailable")
    form = await request.form()
    result = await provider.set_permission_prompts(
        account, session, form.get("enabled") == "true"
    )
    return _render_permission_prompts(
        request,
        account,
        session,
        provider,
        error=None if result.accepted else (result.reason or "could not update setting"),
    )


def _render_bypass_permissions(
    request: Request,
    account,
    session,
    provider,
    *,
    error: str | None = None,
) -> HTMLResponse:
    return get_templates(request).TemplateResponse(
        request,
        "partials/session_bypass_permissions.html",
        {
            "session": session,
            "bypass_permissions_enabled": provider.bypass_permissions_on(account, session),
            "bypass_permissions_error": error,
        },
    )


@router.post(
    "/sessions/{session_key}/bypass-permissions", response_class=HTMLResponse
)
async def set_session_bypass_permissions(
    request: Request, session_key: str
) -> HTMLResponse:
    _require_same_origin(request)
    account, session, provider = resolve_current_leg(request, session_key)
    if Capability.BYPASS_PERMISSIONS not in session.capabilities:
        raise HTTPException(status_code=403, detail="bypass permissions unavailable")
    form = await request.form()
    result = await provider.set_bypass_permissions(
        account, session, form.get("enabled") == "true"
    )
    return _render_bypass_permissions(
        request,
        account,
        session,
        provider,
        error=None if result.accepted else (result.reason or "could not update setting"),
    )


def session_model_preference_context(
    request: Request,
    session,
    provider,
    *,
    error: str | None = None,
    saved: str | None = None,
    open_editor: bool = False,
) -> dict:
    """What the ⚙ Model control needs, on the page and on its own re-render.

    The preference belongs to the logical chat, not to the leg being looked at, so
    that it outlives the boundary it exists to cross (#1665).
    """
    stored = get_db(request).chat_model_preference(
        get_state(request).chat_identity_key(session.key)
    )
    return {
        "session": session,
        "model_preference": stored,
        "model_preference_label": choice_label(stored, provider.selectable_models),
        "model_preference_choices": provider.selectable_models,
        "model_preference_error": error,
        "model_preference_saved": saved,
        "model_preference_open": open_editor,
    }


@router.post("/sessions/{session_key}/model-preference", response_class=HTMLResponse)
async def set_session_model_preference(
    request: Request, session_key: str
) -> HTMLResponse:
    _require_same_origin(request)
    account, session, provider = resolve_current_leg(request, session_key)
    if not provider.selectable_models:
        raise HTTPException(status_code=403, detail="model preference unavailable")
    form = await request.form()
    raw_model = form.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) else ""
    if model and not provider.is_valid_model(model):
        return get_templates(request).TemplateResponse(
            request,
            "partials/session_model_preference.html",
            session_model_preference_context(
                request,
                session,
                provider,
                error=f"{account.label} does not offer the model “{model}”.",
                open_editor=True,
            ),
        )
    get_db(request).record_chat_model_preference(
        get_state(request).chat_identity_key(session.key), model or None
    )
    return get_templates(request).TemplateResponse(
        request,
        "partials/session_model_preference.html",
        session_model_preference_context(
            request,
            session,
            provider,
            saved=(
                f"Saved · next leg starts on "
                f"{choice_label(model, provider.selectable_models)}."
                if model
                else "Saved · no preference; this chat follows its account."
            ),
            open_editor=True,
        ),
    )


def session_title_context(
    request: Request,
    session,
    *,
    typed: str | None = None,
    error: str | None = None,
    saved: str | None = None,
    open_editor: bool = False,
) -> dict:
    """What the ✎ Rename control needs, on the page and on its own re-render."""
    record = get_state(request).generated_title_for(session.key)
    return {
        "session": session,
        "title_value": record.title if typed is None and record is not None else (typed or ""),
        "title_placeholder": session.display_title,
        "title_stored": record is not None,
        "title_max_chars": MAX_TITLE_CHARS,
        "title_error": error,
        "title_saved": saved,
        "title_editor_open": open_editor,
    }


def _render_session_title(request: Request, session, **kwargs) -> HTMLResponse:
    # Re-read the session: the write we just made is what the placeholder and the
    # stored value have to reflect, not the copy resolved before it.
    session = get_state(request).sessions.get(session.key, session)
    return get_templates(request).TemplateResponse(
        request,
        "partials/session_title_editor.html",
        session_title_context(request, session, open_editor=True, **kwargs),
    )


@router.post("/sessions/{session_key}/title", response_class=HTMLResponse)
async def set_session_title(request: Request, session_key: str) -> HTMLResponse:
    """Name a chat by hand. A hand-typed name outranks the chat's own."""
    _require_same_origin(request)
    _account, session, _provider = resolve_current_leg(request, session_key)
    form = await request.form()
    state = get_state(request)
    if form.get("action") == "clear":
        state.clear_generated_title(session.key, source="user")
        return _render_session_title(
            request, session, saved="Cleared · this chat names itself again."
        )
    typed = str(form.get("title") or "")
    title = normalize_agent_title(session, typed)
    if title is None:
        # Re-render what was typed: a refused save must not swallow the edit.
        return _render_session_title(
            request, session, typed=typed, error="Give the chat a name first."
        )
    state.set_generated_title(
        session.key, title, title_evidence_signature(session), source="user"
    )
    return _render_session_title(request, session, saved="Saved · this chat is renamed.")


@router.post("/sessions/{session_key}/inject", response_class=HTMLResponse)
async def inject_message(request: Request, session_key: str) -> HTMLResponse:
    _require_same_origin(request)
    account, session, provider = resolve_current_leg(request, session_key)
    session_key = session.key
    client_action_id = bind_action(
        request, session_key=session.key, provider=account.provider_id
    )
    config = get_config(request).inject
    injector = get_injector(request)
    hold = False
    if not config.enabled or (
        Capability.INJECT not in session.capabilities and not injector.can_queue(session.key)
    ):
        # The deck's own chat whose runtime is restarting keeps the message and sends
        # it once the runtime is back (#2847); nothing else is fixed by waiting.
        hold = config.enabled and provider.runtime_unreachable(account, session)
        if not hold:
            # A chat the deck does not run loses its send path with its directory; say
            # that rather than a bare "unavailable" (#2524).
            nowhere_to_run = _nowhere_to_run(session) if config.enabled else None
            raise nowhere_to_run or HTTPException(
                status_code=403, detail="message injection unavailable"
            )
    queued_behind_turn = session.thinking or injector.can_queue(session.key)
    message, uploads, form = await _turn_form(request)
    images = list(uploads.images)
    files = [upload.path for upload in uploads.files]
    secret_command = _secret_command_or_422(message, uploads.paths)
    post_target = _issue_post_target(request, session, form, message, secret_command, uploads)
    issued = None
    if hold:
        return await _hold_for_runtime(
            request,
            session,
            provider,
            form,
            message,
            uploads,
            secret_command,
            post_target,
            client_action_id,
        )
    if secret_command is not None:
        # #1171: a secret queues behind a running turn like any other message.
        # Handing a credential over is most often needed *while* the agent works,
        # and the queue never carries the value — only the name and a one-use
        # ticket, which is what the notice below is built from.
        if uploads.paths:
            cleanup_uploaded_files(uploads.paths)
            raise HTTPException(
                status_code=422, detail="A secret command cannot include attachments"
            )
        if not client_action_id:
            raise HTTPException(status_code=422, detail="secret action id required")
        # Run every local admission check against a value-free stand-in before
        # the runtime starts retaining the value.
        message = secret_notice(secret_command.name, "pending-ticket")
    try:
        effort = _selected_effort(form, provider)
    except HTTPException:
        cleanup_uploaded_files(uploads.paths)
        raise
    if effort and Capability.EFFORT not in session.capabilities:
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(status_code=422, detail="effort unavailable")
    undeliverable = provider.user_message_refusal(message)
    if undeliverable is not None:
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(status_code=422, detail=undeliverable)
    nowhere_to_run = _nowhere_to_run(session)
    if nowhere_to_run is not None:
        cleanup_uploaded_files(uploads.paths)
        raise nowhere_to_run
    if secret_command is None and post_target is None:
        fresh = await _deck_chat_fresh_leg(
            request, account, session, provider, message, images, files, effort,
            client_action_id,
        )
        if fresh is not None:
            return fresh
    try:
        if secret_command is not None:
            try:
                issued = await issue_runtime_secret(
                    runtime_socket_path(),
                    session_key=session.key,
                    action_id=client_action_id,
                    name=secret_command.name,
                    value=secret_command.value,
                )
            except SecretRuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from None
            message = secret_notice(issued.name, issued.ticket)
        with timing_span(request, "queue"):
            result = await injector.start(
                account,
                session,
                provider,
                message,
                images,
                files=files,
                effort=effort,
                client_action_id=client_action_id,
                allow_over_budget=True,
            )
    except BaseException:
        cleanup_uploaded_files(uploads.paths)
        raise
    if not result.accepted:
        cleanup_uploaded_files(uploads.paths)
        if issued is not None:
            with contextlib.suppress(SecretRuntimeError):
                await revoke_runtime_secret(runtime_socket_path(), issued.ticket)
        raise HTTPException(status_code=403, detail=result.reason)
    _note_boss_turn(request, session.key, form, message)
    status = injector.status(session.key)
    queued_item = status.items[-1] if status and status.items else None
    posted = None
    if post_target is not None:
        # Before the render, so the receipt can say what became of the post; ``status`` is
        # still the one captured when the send was accepted. Never a secret: refused above.
        with timing_span(request, "issue_post"):
            posted = await _post_to_issue(
                request,
                session.key,
                post_target,
                display_message(message),
                len(uploads.paths),
                client_action_id,
            )
        request.state.issue_post_outcome = posted
    with timing_span(request, "render"):
        response = _render_status(request, session.key, status, queued_item, posted)
    if secret_command is None and post_target is None:
        # After the render, so the receipt shows the send as it was accepted. Never a secret:
        # its notice names a ticket, and the value must not reach an issue.
        await _relay_chat_answer(request, session.key, display_message(message), client_action_id)
    response.status_code = 202
    response.headers["X-AgentDeck-Action-State"] = (
        "queued" if queued_behind_turn else "accepted"
    )
    if posted is not None:
        # An ASCII code only: header values are latin-1, and the words live in the receipt.
        response.headers["X-AgentDeck-Issue-Post"] = posted.code
    if secret_command is not None:
        response.headers["Cache-Control"] = "no-store"
    return response


async def _deck_chat_fresh_leg(
    request: Request,
    account,
    session,
    provider,
    message: str,
    images: list[Path],
    files: list[Path],
    effort: str | None,
    client_action_id: str | None,
) -> HTMLResponse | None:
    """Start the deck chat's next leg when its current one has been idle too long (#3403).

    ``None`` means deliver as usual: not the deck chat, not idle long enough, or another
    start already holds the chat's successor fence. The new leg runs on the same
    account and directory with a clean context; its brief names the old leg's
    transcript so earlier talk stays reachable on request.
    """
    state = get_state(request)
    db = get_db(request)
    minutes = get_config(request).meta_chat.idle_reset_minutes
    if not meta_chat.needs_fresh_leg(
        session,
        busy=get_injector(request).session_busy(session.key),
        now=datetime.now(UTC),
        minutes=minutes,
    ) or not meta_chat.is_deck_chat(state, db, session.key):
        return None
    if session.cwd is None or not cwd_exists(session.cwd):
        return None
    defaults = saved_new_session_defaults(request, account, provider)
    # A CLI already booted for this leg takes the message at once (#3420). The
    # successor fence below refuses a resend of this press, so the spare needs no
    # action id of its own.
    spare = _claim_start_spare(
        request,
        start_spares.DECK_CHAT,
        account_key=account.key,
        cwd=str(Path(session.cwd).expanduser().resolve()),
        model=defaults.model or None,
        effort=effort or defaults.effort,
        action_id=None,
    )
    token = spare[0] if spare is not None else uuid.uuid4().hex
    worker_key = {"worker_key": spare[1]} if spare is not None else {}
    reservation = _reserve_chat_successor(
        request, state.chat_newest_leg_key(session.key), token
    )
    if reservation in (None, "conflict", "superseded"):
        _settle_start_spare(request, start_spares.DECK_CHAT, spare, account, accepted=False)
        return None
    transcript_url = str(request.url_for("session_transcript", session_key=session.key))
    # Before the start, so the seam reads as a fresh start the moment the leg links.
    db.record_deck_chat_start(token, meta_chat.RESET)
    with timing_span(request, "queue"):
        result = await get_injector(request).start_new(
            account,
            provider,
            Path(session.cwd),
            meta_chat.with_brief(message, transcript_url),
            images,
            files=files,
            model=defaults.model or None,
            effort=effort or defaults.effort,
            client_action_id=client_action_id,
            session_token=token,
            allow_over_budget=True,
            origin="deck_chat",
            source_session_key=session.key,
            display_message_override=display_message(message),
            **worker_key,
        )
    _settle_start_spare(
        request, start_spares.DECK_CHAT, spare, account, accepted=result.accepted
    )
    if not result.accepted:
        state.release_chat_successor(token)
        cleanup_uploaded_files([*images, *files])
        raise HTTPException(status_code=403, detail=start_failure_message(result.reason))
    _note_boss_turn(request, session.key, {}, message)
    return await _render_start_accepted(request, token, result_id="inject-result")


@router.post(
    "/sessions/{session_key}/held/{message_id}/cancel",
    response_class=HTMLResponse,
)
async def cancel_held_message(
    request: Request, session_key: str, message_id: str
) -> HTMLResponse:
    _require_same_origin(request)
    _account, session, _provider = resolve_current_leg(request, session_key)
    chat_id = _held_chat_id(request, session.key)
    if not request.app.state.scheduled_messages.cancel(message_id, chat_id):
        raise HTTPException(status_code=409, detail="This message is already sending.")
    return _render_held_messages(request, session.key)


@router.post(
    "/sessions/{session_key}/pending/{item_id}/dismiss",
    response_class=HTMLResponse,
)
async def dismiss_pending_message(
    request: Request, session_key: str, item_id: int
) -> HTMLResponse:
    """Remove one sent-but-never-acknowledged row from the chat (#2027).

    The answer is always what that row should look like now: nothing when the
    dismissal took, and the row re-rendered when it did not. A refusal therefore
    explains itself in the caption — the chat started a turn, or the agent
    finally wrote the message down — instead of leaving a press that did nothing.
    A row that is simply gone answers as removed, because that is the truth.

    Resolved with `resolve_session`, not `resolve_current_leg`, so it acts on the
    leg the row was rendered from. The page names a leg in the URL and draws its
    pending rows from that leg's queue; `resolve_current_leg` hops to whichever
    leg the chat has since moved its work to. In a chat that has rolled over those
    are different keys, so the dismissal found nothing, fell into the "already
    gone" branch below, and answered "removed" for a row it had never touched —
    which is how a removed message came back on the next render (#2031). Nothing
    durable moves here, so there is no live worker to need in the first place.
    """
    _require_same_origin(request)
    _account, session, _provider = resolve_session(request, session_key)
    injector = get_injector(request)
    if injector.discard(session.key, item_id):
        return HTMLResponse("")
    status = injector.status(session.key)
    item = next(
        (queued for queued in status.items if queued.id == item_id and queued.is_pending),
        None,
    ) if status else None
    if item is None:
        return HTMLResponse("")
    response = get_templates(request).TemplateResponse(
        request,
        "partials/pending_message.html",
        {
            "item": item,
            "session_key": session.key,
            "session_busy": injector.session_busy(session.key),
        },
    )
    response.status_code = 409
    return response


async def _cross_leg_refusal(
    injector, row_account, row_session, row_provider, session, item_id: int
) -> InjectResult | None:
    """Why a row from a retired leg must not be resent, or None when it may.

    A failed send is normally a clean one, but a send whose connection broke
    after the runtime began writing it fails here while the runtime holds a
    receipt. On the same leg that receipt fences the resend; on the next leg it
    is invisible, so ask the old leg's runtime first.
    """
    if row_session.key == session.key:
        return None
    status = injector.status(row_session.key)
    item = next(
        (queued for queued in status.items if queued.id == item_id), None
    ) if status else None
    spent = getattr(row_provider, "delivery_spent", None)
    if item is None or not item.client_action_id or spent is None:
        return None
    if await spent(row_account, row_session, item.client_action_id):
        return InjectResult(
            False, "it may already have reached the earlier part of this chat"
        )
    return None


@router.post(
    "/sessions/{session_key}/pending/{item_id}/resend",
    response_class=HTMLResponse,
)
async def resend_unsent_message(
    request: Request, session_key: str, item_id: int
) -> HTMLResponse:
    """Send one "Not sent" message again, under its own action id (#2662).

    Answers with the composer's status slot, as a send does, and redraws the row
    in place: queued again, or still "Not sent" with the reason the resend was
    refused. A refusal is the slot's own failure text rather than an error
    status, so the press always shows what became of it.

    The row is found on the leg the URL names, as a dismissal's is: that is the
    queue the page drew it from, and after a rollover ``resolve_current_leg``
    names a different one. It is delivered to the leg the chat works on now.
    Moving legs moves the runtime fence too, so a message the old leg's runtime
    holds any receipt for is refused rather than risked twice.
    """
    _require_same_origin(request)
    row_account, row_session, row_provider = resolve_session(request, session_key)
    account, session, provider = resolve_current_leg(request, session_key)
    injector = get_injector(request)
    result = await _cross_leg_refusal(
        injector, row_account, row_session, row_provider, session, item_id
    )
    if result is None:
        result = await injector.resend(
            account, session, provider, item_id, row_session_key=row_session.key
        )
    status = injector.status(session.key)
    item = next(
        (queued for queued in status.items if queued.id == item_id), None
    ) if status else None
    if not result.accepted:
        status = InjectionStatus(
            "failed", result.reason, status.items if status else (), session.key
        )
    return _render_status(
        request, session.key, status, replace_items=[item] if item else []
    )


@router.get("/partials/sessions/{session_key}/inject-status", response_class=HTMLResponse)
async def inject_status(request: Request, session_key: str) -> HTMLResponse:
    _account, session, _provider = resolve_current_leg(request, session_key)
    session_key = session.key
    status = get_injector(request).status(session_key)
    return _render_status(request, session_key, status)


@router.post("/sessions/{session_key}/steer", response_class=HTMLResponse)
async def steer_turn(request: Request, session_key: str) -> HTMLResponse:
    _require_same_origin(request)
    account, session, provider = resolve_current_leg(request, session_key)
    session_key = session.key
    client_action_id = bind_action(
        request, session_key=session.key, provider=account.provider_id
    )
    config = get_config(request).inject
    if not config.enabled:
        raise HTTPException(status_code=403, detail="active-turn steering unavailable")
    message, uploads, form = await _turn_form(request)
    images = list(uploads.images)
    files = [upload.path for upload in uploads.files]
    _reject_reserved_secret_command(message, uploads)
    undeliverable = provider.user_message_refusal(message)
    if undeliverable is not None:
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(status_code=422, detail=undeliverable)
    active_turn = Capability.STEER in session.capabilities
    if not active_turn:
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(status_code=403, detail="active-turn steering unavailable")
    injector = get_injector(request)
    with timing_span(request, "runtime"):
        receipt = await injector.deliver_now(
            account,
            session,
            provider,
            message,
            images,
            files=files,
            client_action_id=client_action_id,
            allow_over_budget=True,
        )
    if not receipt.result.accepted:
        raise HTTPException(status_code=409, detail=receipt.result.reason)
    _note_boss_turn(request, session.key, form, message)
    return HTMLResponse(
        '<div id="steer-result" class="inject-result complete">'
        "Sent to active turn.</div>"
    )


@router.post("/sessions/{session_key}/interrupt", response_class=HTMLResponse)
async def interrupt_turn(request: Request, session_key: str) -> HTMLResponse:
    _require_same_origin(request)
    account, session, provider = resolve_current_leg(request, session_key)
    session_key = session.key
    bind_action(request, session_key=session.key, provider=account.provider_id)
    if Capability.INTERRUPT not in session.capabilities:
        raise HTTPException(status_code=403, detail="turn interruption unavailable")
    with timing_span(request, "runtime"):
        result = await provider.interrupt(account, session)
    if not result.accepted:
        raise HTTPException(status_code=409, detail=result.reason)
    return HTMLResponse(
        '<div id="inject-result" class="inject-result running" aria-live="polite"'
        ' aria-label="Stopping active turn"><span class="send-spinner"'
        ' aria-hidden="true"></span></div>'
    )


def _render_related_links(
    request: Request,
    session,
    *,
    refusal: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """Re-render the row the commit chip lives in, optionally carrying a refusal.

    The same fragment the stream pushes, built from the same context callable, so a
    press leaves behind exactly what the next tick would have rendered — plus the
    one thing the stream has no business knowing, which is why a press was refused.
    """
    state = get_state(request)
    assistant = request.app.state.assistant
    git_context = assistant.session_context(session)
    pull_requests = session_pull_request_chips(git_context)
    # One read for both chip sets; the card's pull request is what lets a chat that
    # registered none of its own still show its card's review (#2043).
    issue_board = request.app.state.issues.read_board() if session.issues else None
    view = SessionView(
        current=session,
        effective=state.session_presentation().display(session),
        issues=session_issue_chips(session, issue_board),
        pull_requests=pull_requests,
        head_review=session_head_review_chip(
            session, git_context, state, get_injector(request)
        ),
    )
    fragment = SESSION_FRAGMENTS_BY_NAME["related-links"]
    templates = get_templates(request)
    html = templates.get_template(fragment.template).render(
        **fragment.context(view),
        head_review_refusal=refusal,
    )
    return HTMLResponse(html, status_code=status_code)


@router.post("/sessions/{session_key}/review-head", response_class=HTMLResponse)
async def review_session_head(request: Request, session_key: str) -> HTMLResponse:
    """Start a review of this chat's current commit and keep its verdict (#1127).

    One press, one review. A commit that already has a verdict is not re-reviewed
    and a commit already being reviewed is not reviewed twice: the answer to "has
    this commit been reviewed" does not change by asking again, and each extra
    review would spend an agent to say the same thing.

    The review runs as its own top-level coordinator in this chat's working
    directory, so it can run review-fix-pr's bounded lanes while the session being
    looked at keeps its context and turn. Every refusal comes back as the chip row
    itself, with the reason in it.
    """
    _require_same_origin(request)
    _account, session, _provider = resolve_current_leg(request, session_key)
    session_key = session.key
    state = get_state(request)
    git_context = request.app.state.assistant.session_context(session)
    commit = reviewable_commit(git_context)
    if commit is None:
        return _render_related_links(
            request,
            session,
            refusal=(
                "no commit to review — the deck cannot name this chat's repository "
                "and commit"
            ),
            status_code=422,
        )
    repo, sha = commit
    if git_context is not None and git_context.dirty:
        return _render_related_links(
            request,
            session,
            refusal=(
                "commit or discard the checkout's uncommitted changes before starting "
                "review-fix-pr"
            ),
            status_code=409,
        )
    if state.review_verdict(repo, sha) is not None:
        return _render_related_links(
            request, session, refusal="this commit already has a verdict", status_code=409
        )
    launch = state.head_review_launch(repo, sha)
    running = (
        get_injector(request).delegation_status(launch.delegation_id)
        if launch is not None
        else None
    )
    if running is not None and running.state in ("starting", "running"):
        return _render_related_links(
            request, session, refusal="a review of this commit is already running", status_code=409
        )
    if session.cwd is None:
        return _render_related_links(
            request,
            session,
            refusal="this chat has no working directory to review in",
            status_code=422,
        )
    try:
        account = delegation_account(request, None)
    except HTTPException as exc:
        # No account can start a chat right now — an unavailable runtime, or more
        # than one candidate and no default. Said in the row, not as a status code
        # the person never sees.
        return _render_related_links(
            request,
            session,
            refusal=f"no account can start a review chat: {exc.detail}",
            status_code=422,
        )
    provider = PROVIDERS[account.provider_id]
    bind_action(request, session_key=session.key, provider=account.provider_id)
    with timing_span(request, "queue"):
        result, delegation_id = await get_injector(request).start_delegation(
            account,
            provider,
            session.cwd,
            review_brief(
                repo=repo,
                sha=sha,
                base_url=owned_session_base_url(get_config(request)),
                branch=git_context.branch if git_context is not None else None,
            ),
            # review-fix-pr may fix, commit and push. Keep the filesystem writable and
            # let providers surface the narrow approval they need rather than creating
            # an agent whose assigned skill cannot complete.
            sandbox="workspace-write",
            approval_policy="on-request",
            # The person pressed Review: this top-level chat coordinates
            # review-fix-pr and needs its bounded review lanes.
            session_role="primary",
        )
    if not result.accepted or delegation_id is None:
        return _render_related_links(
            request,
            session,
            refusal=humanize_refusal(result.reason or "the review could not be started"),
            status_code=422,
        )
    state.note_head_review_launch(
        HeadReviewLaunch(
            repo=repo, sha=sha, delegation_id=delegation_id, started_at=datetime.now(UTC)
        )
    )
    return _render_related_links(request, session, status_code=202)


@router.get("/partials/sessions/{session_key}/interaction", response_class=HTMLResponse)
async def pending_interaction(
    request: Request,
    session_key: str,
    known_interaction_id: str | None = None,
) -> Response:
    # The detail page renders this section server-side and keeps it live over SSE
    # (the `interaction` event, pushed only when the pending interaction actually
    # changes — see routes_sse). This endpoint is the on-demand render used by the
    # answer POST's swap target.
    account, session, provider = resolve_current_leg(request, session_key)
    session_key = session.key
    interaction = provider.pending_interaction(account, session)
    interaction_id = interaction.id if interaction is not None else ""
    if known_interaction_id is not None and known_interaction_id == interaction_id:
        return Response(status_code=204, headers={"Cache-Control": "no-cache"})
    view = SessionView(current=session, effective=session, interaction=interaction)
    response = HTMLResponse(
        SESSION_FRAGMENTS_BY_NAME["interaction"].render(get_templates(request), view)
    )
    response.headers["Cache-Control"] = "no-cache"
    return response


@router.post("/sessions/{session_key}/interaction", response_class=HTMLResponse)
async def answer_interaction(request: Request, session_key: str) -> HTMLResponse:
    _require_same_origin(request)
    with timing_span(request, "form"):
        form = await request.form()
    session, remaining = await _answer_interaction(
        request,
        session_key,
        interaction_id=form.get("interaction_id"),
        decision=form.get("decision"),
        values=lambda question_id: [
            *form.getlist(f"answer__{question_id}"), form.get(f"other__{question_id}")
        ],
    )
    with timing_span(request, "render"):
        view = SessionView(current=session, effective=session, interaction=remaining)
        return HTMLResponse(
            SESSION_FRAGMENTS_BY_NAME["interaction"].render(
                get_templates(request), view
            )
        )


class InteractionAnswer(BaseModel):
    """An agent's answer to a chat's question box, in ``GET /api/.../interaction``'s ids."""

    interaction_id: str
    # Question id → the chosen option values (or labels) and any free text.
    answers: dict[str, str | list[str]] = Field(default_factory=dict)
    decision: str | None = None


@router.post("/api/sessions/{session_key}/interaction")
async def answer_interaction_json(
    request: Request, session_key: str, body: InteractionAnswer
) -> JSONResponse:
    """The question box's own answer, for an agent relaying the owner's (#3481).

    Before this an agent either scraped the page's form or sent the answer as a chat
    message, which the chat reads but which leaves the box open, so its Work kept
    showing Needs answer.
    """
    _require_same_origin(request)
    answers = {
        key: [value] if isinstance(value, str) else value
        for key, value in body.answers.items()
    }
    session, remaining = await _answer_interaction(
        request,
        session_key,
        interaction_id=body.interaction_id,
        decision=body.decision,
        values=lambda question_id: answers.get(question_id, []),
    )
    return JSONResponse(
        {
            "session_key": session.key,
            "answered": True,
            "remaining": remaining.answer_json() if remaining is not None else None,
        }
    )


async def _answer_interaction(
    request: Request,
    session_key: str,
    *,
    interaction_id: object,
    decision: object,
    values: Callable[[str], Iterable[object]],
):
    """Answer the chat's pending interaction; (its session, the questions still open)."""
    account, session, provider = resolve_current_leg(request, session_key)
    session_key = session.key
    bind_action(request, session_key=session.key, provider=account.provider_id)
    if Capability.INTERACT not in session.capabilities:
        raise HTTPException(status_code=403, detail="interaction unavailable")
    interaction = provider.pending_interaction(account, session)
    if interaction is None:
        raise HTTPException(status_code=409, detail="interaction is no longer pending")
    # A chat's open questions change as questions are asked and answered (#3451). Each
    # answer names its own question, so one that has gone in the meantime is dropped
    # rather than refusing the ones still open.
    if interaction_id != interaction.id and not (
        interaction.dismissible
        and isinstance(interaction_id, str)
        and interaction_id.startswith(OPEN_QUESTIONS_PREFIX)
    ):
        raise HTTPException(status_code=409, detail="interaction changed")
    dismissed = (
        decision.removeprefix("dismiss:")
        if interaction.dismissible and isinstance(decision, str)
        and decision.startswith("dismiss:")
        else None
    )
    if dismissed is not None and all(q.id != dismissed for q in interaction.questions):
        raise HTTPException(status_code=409, detail="that question is no longer open")
    answers: dict[str, list[str]] = {}
    for question in interaction.questions if dismissed is None else ():
        given = [
            value.strip()
            for value in values(question.id)
            if isinstance(value, str) and value.strip()
        ]
        if given:
            answers[question.id] = given
    if interaction.dismissible:
        if dismissed is None and not answers:
            raise HTTPException(status_code=422, detail="answer at least one question")
    elif interaction.kind == "question" and any(
        question.id not in answers for question in interaction.questions
    ):
        raise HTTPException(status_code=422, detail="answer every question")
    if get_config(request).inject.too_long(
        "".join(value for values in answers.values() for value in values)
    ):
        raise HTTPException(status_code=422, detail="answers are too long")
    answer_text = _interaction_answer_text(interaction, answers)
    with timing_span(request, "runtime"):
        result = await provider.answer_interaction(
            account,
            session,
            interaction.id,
            answers=answers,
            decision=decision if isinstance(decision, str) else None,
        )
    if not result.accepted:
        raise HTTPException(status_code=422, detail=result.reason)
    if answer_text:
        _note_boss_turn(request, session.key, {}, answer_text)
    remaining = None
    if interaction.dismissible:
        # The questions not answered here stay in the box (#3451).
        closed = set(answers) | ({dismissed} if dismissed else set())
        left = tuple(q for q in interaction.questions if q.id not in closed)
        if left:
            remaining = replace(
                interaction, id=open_questions_id(q.id for q in left), questions=left
            )
    return session, remaining


@router.post("/sessions/new", response_class=HTMLResponse)
async def new_session(request: Request) -> HTMLResponse:
    _require_same_origin(request)
    message, uploads, form = await _turn_form(request)
    images = list(uploads.images)
    files = [upload.path for upload in uploads.files]
    _reject_reserved_secret_command(message, uploads)
    account_key = form.get("account_key")
    raw_cwd = form.get("cwd")
    if not all(isinstance(value, str) for value in (account_key, raw_cwd)):
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(status_code=422, detail="invalid new-session request")
    try:
        account, provider = _account_provider(request, account_key)
    except BaseException:
        cleanup_uploaded_files(uploads.paths)
        raise
    # Blank = provider/account default (no --model). A non-blank value must be one
    # the provider actually offers, so an unknown slug is a 422 rather than a raw
    # arg handed to a CLI/app-server.
    raw_model = form.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) else ""
    if model and not provider.is_valid_model(model):
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(status_code=422, detail="invalid model")
    try:
        effort = _selected_effort(form, provider)
    except HTTPException:
        cleanup_uploaded_files(uploads.paths)
        raise
    undeliverable = provider.user_message_refusal(message)
    if undeliverable is not None:
        cleanup_uploaded_files(uploads.paths)
        # The sentence, not a bare 422: the person has to know to reword before
        # the same message can start the chat it was meant to start.
        return _render_start_refusal(
            request, undeliverable, result_id="new-session-result"
        )
    saved_defaults = saved_new_session_defaults(request, account, provider)
    model = model or saved_defaults.model or ""
    effort = effort or saved_defaults.effort
    try:
        cwd = Path(raw_cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        cleanup_uploaded_files(uploads.paths)
        raise HTTPException(status_code=422, detail="invalid working directory") from None
    # Minted here rather than inside the injector so the answer can name the chat
    # that does not exist yet, and send the browser straight to its page (#765).
    session_token = uuid.uuid4().hex
    # A CLI already booted for exactly this start takes the message at once (#3420).
    # Only a press carrying the client's own action id may take one: that id is what
    # sends a resend to the same chat.
    spare = _claim_start_spare(
        request,
        start_spares.NEW_CHAT,
        account_key=account.key,
        cwd=str(cwd),
        model=model or None,
        effort=effort,
        action_id=supplied_action_id(request),
    )
    if spare is not None:
        session_token = spare[0]
    try:
        client_action_id = bind_action(request, provider=account.provider_id)
        with timing_span(request, "queue"):
            result = await get_injector(request).start_new(
                account,
                provider,
                cwd,
                message,
                images,
                files=files,
                model=model or None,
                effort=effort,
                client_action_id=client_action_id,
                session_token=session_token,
                allow_over_budget=True,
                **({"worker_key": spare[1]} if spare is not None else {}),
            )
    except BaseException:
        _settle_start_spare(request, start_spares.NEW_CHAT, spare, account, accepted=False)
        cleanup_uploaded_files(uploads.paths)
        raise
    _settle_start_spare(
        request, start_spares.NEW_CHAT, spare, account, accepted=result.accepted
    )
    if not result.accepted:
        cleanup_uploaded_files(uploads.paths)
        status_code = 409 if "already starting" in (result.reason or "") else 422
        raise HTTPException(status_code=status_code, detail=result.reason)
    # This UI route is the one intentional source of the shared form default.
    # Machine delegations use /api/delegations and must not change it.
    get_db(request).record_manual_new_chat_cwd(str(cwd))
    if spare is None:
        # The remembered directory may have moved: the next spare boots there.
        _nudge_start_spares(request)
    with timing_span(request, "render"):
        return await _render_start_accepted(request, session_token)


@router.get("/sessions/{session_key}/composer-target")
async def session_composer_target(request: Request, session_key: str) -> dict[str, str]:
    """Where a side chat's Send to original puts its text, resolved at send time (#2485)."""
    return side_chat_composer_target(request, session_key)


@router.post("/sessions/{session_key}/fork", response_class=HTMLResponse)
async def fork_session(request: Request, session_key: str) -> HTMLResponse:
    """Start a side chat: a new chat holding this session's history up to one turn (#2485).

    ``session_key`` is the leg the chosen reply belongs to, never "wherever the chat is
    now": transcript lines are numbered per leg, so only that leg can be cut at ``seq``.
    The side chat runs on the same account, folder, model and effort, because the
    provider's cache only helps a request that starts the way the source's did. It has
    the same permissions as any new chat (#2750): read-only left it unable to act.
    """
    _require_same_origin(request)
    message, uploads, form = await _turn_form(request)
    images = list(uploads.images)
    files = [upload.path for upload in uploads.files]
    _reject_reserved_secret_command(message, uploads)

    def reject(text: str, *, status_code: int = 422) -> HTMLResponse:
        cleanup_uploaded_files(uploads.paths)
        return _render_start_refusal(
            request, text, result_id="fork-session-result", status_code=status_code
        )

    try:
        account, session, provider = resolve_session(request, session_key)
    except BaseException:
        cleanup_uploaded_files(uploads.paths)
        raise
    raw_seq = form.get("seq")
    try:
        seq = int(raw_seq) if isinstance(raw_seq, str) else 0
    except ValueError:
        seq = 0
    if seq < 1:
        return reject("Choose the reply the side chat should start after.")
    refusal = provider.fork_refusal(account, session)
    if refusal is not None:
        return reject(refusal, status_code=409)
    ref = await asyncio.to_thread(provider.fork_ref, account, session, seq)
    if ref is None:
        return reject(
            "That reply does not end a finished turn, so a side chat cannot start there.",
            status_code=409,
        )
    if "repository" in form:
        # Start new work from this reply (#3171): the side chat files it and stops.
        from .. import new_work
        from .routes_works import _new_work_targets

        target = new_work.target_for(_new_work_targets(request), form.get("repository"))
        if target is None:
            return reject("Choose a repository a Poller on this host manages.")
        message = new_work_brief(
            message,
            target.repository,
            target.ownership_label,
            _public_chat_url(request, session.key),
        )
        if get_config(request).inject.too_long(message):
            return reject("The work's description is too long. Shorten it and try again.")
    undeliverable = provider.user_message_refusal(message)
    if undeliverable is not None:
        return reject(undeliverable)
    model, effort = provider.fork_settings(account, session)
    session_token = uuid.uuid4().hex
    owner = get_state(request).reserve_chat_fork(
        session_token, session.key, seq, supplied_action_id(request)
    )
    if owner is None:
        return reject("The side chat could not be recorded. Try again.", status_code=503)
    if owner != session_token:
        # The same press arrived again: send it to the start the first one made.
        cleanup_uploaded_files(uploads.paths)
        return await _render_start_accepted(request, owner, result_id="fork-session-result")
    try:
        client_action_id = bind_action(request, provider=account.provider_id)
        with timing_span(request, "queue"):
            result = await get_injector(request).start_new(
                account,
                provider,
                session.cwd,
                message,
                images,
                files=files,
                model=model,
                effort=effort,
                client_action_id=client_action_id,
                session_token=session_token,
                allow_over_budget=True,
                origin="fork",
                source_session_key=session.key,
                fork_from=ForkFrom(session.session_id, ref),
            )
    except BaseException:
        # Nothing started, so a resend of this press must be free to try again.
        get_state(request).release_chat_fork(session_token)
        cleanup_uploaded_files(uploads.paths)
        raise
    if not result.accepted:
        get_state(request).release_chat_fork(session_token)
        status_code = 409 if "already starting" in (result.reason or "") else 422
        return reject(start_failure_message(result.reason), status_code=status_code)
    with timing_span(request, "render"):
        return await _render_start_accepted(request, session_token, result_id="fork-session-result")


def _claim_start_spare(
    request: Request,
    name: str,
    *,
    account_key: str,
    cwd: str,
    model: str | None,
    effort: str | None,
    action_id: str | None,
) -> tuple[str, str] | None:
    """The ``(session token, worker key)`` of the spare CLI this start takes, or None."""
    spares = getattr(request.app.state, "start_spares", None)
    if spares is None:
        return None
    return spares.claim(
        name,
        account_key=account_key,
        cwd=cwd,
        model=model,
        effort=effort,
        action_id=action_id,
    )


def _settle_start_spare(
    request: Request, name: str, spare: tuple[str, str] | None, account, *, accepted: bool
) -> None:
    """Ask for the next spare once the start that took one has answered (#3420)."""
    spares = getattr(request.app.state, "start_spares", None)
    if spares is None or spare is None:
        return
    if accepted:
        spares.replenish_after(name, get_injector(request).new_start_answer(account.key))
    else:
        spares.release(name)


def _nudge_start_spares(request: Request) -> None:
    spares = getattr(request.app.state, "start_spares", None)
    if spares is not None:
        spares.nudge()


def _render_start_refusal(
    request: Request,
    message: str,
    *,
    result_id: str,
    status_code: int = 422,
    link: str | None = None,
    link_label: str = "",
) -> HTMLResponse:
    """Answer a refused start with the reason, in the control's own result slot.

    The status stays a refusal — this route is only ever posted by its own form —
    but the body carries a sentence instead of a JSON `detail` htmx would drop on
    the floor, leaving the person with nothing but the number (#746). A refusal
    that names somewhere else to go carries the link too, so the sentence is not
    a dead end (#1582).
    """
    response = get_templates(request).TemplateResponse(
        request,
        "partials/start_refusal.html",
        {
            "result_id": result_id,
            "refusal_message": message,
            "refusal_link": link,
            "refusal_link_label": link_label,
        },
    )
    response.status_code = status_code
    return response


_LATER_RUN_WORKING = (
    "The poller has already restarted this card's work in another chat, so "
    "continuing this one would run it twice. Open the card's current chat instead."
)
_RUN_LEFT_BEHIND = (
    "The poller stopped or replaced the run this chat worked for, so a chat continued "
    "from it would not be stopped when the card runs again. Ask on the card to restart "
    "the work instead."
)
_RUN_UNKNOWN = (
    "Could not tell which poller run this chat works for, so continuing it might run "
    "the card twice. Try again in a moment, or ask on the card."
)


def _continue_refusal(
    request: Request, session_key: str
) -> tuple[str | None, SessionRunBinding | None]:
    """Why continuing this chat would put a second worker on its card, and the Run it serves.

    A Continue carries a Work's chat on under the Run it served, and the Run's outcome
    follows it there, so the next Run's admission parks it. That holds only while the Run
    is the Work's newest: a leg naming no Run counts only for the newest Run (#2862). So a
    chat whose Run was replaced is refused, and so is one whose Run was stopped, though a
    stopped Run's later continuation is still followed. A later Run with no recorded
    outcome has just been placed, and an unreadable outcome store is not proof it stopped,
    so both count as working; with no later Run, an unreadable store refuses nothing.

    This is the early answer with a reason to show. What makes it hold is the reservation:
    the caller fences it on the returned Run still being the newest, in the same
    transaction, and a Run's admission checks for reservations again after freezing.
    A chat whose Run cannot be read is refused rather than taken for a free one.
    """
    db = get_state(request).budget_db
    try:
        binding = db.chat_run_binding(session_key, max_legs=MAX_CONTINUATIONS)
    except Exception:  # noqa: BLE001 -- unreadable or too long is unknown, never free
        log.exception("could not read the Run of chat %s", session_key)
        return _RUN_UNKNOWN, None
    if binding is None:
        return None, None
    runs = [route.run_id for route in db.latest_delivery_routes_by_run(binding.work_id)]
    if binding.run_id not in runs:
        return (_RUN_LEFT_BEHIND if runs else None), binding
    later = set(runs[: runs.index(binding.run_id)])  # newest Run first
    outcomes = read_outcomes(get_config(request).history.db_path, binding.work_id)
    if outcomes is None:
        return (_LATER_RUN_WORKING if later else None), binding
    if later - {o.run_id for o in outcomes if o.execution_state in SAFE_STATES}:
        return _LATER_RUN_WORKING, binding
    stopped = any(o.run_id == binding.run_id and o.stop_requested_at for o in outcomes)
    return (_RUN_LEFT_BEHIND if later or stopped else None), binding


def recorded_continue_cwd_exists(cwd: Path) -> bool:
    """Whether a chat's recorded working directory is still present on this host."""
    return cwd_exists(cwd)


def _nowhere_to_run(session) -> HTTPException | None:
    """Refuse a send into a chat whose directory is gone and that cannot move.

    Waking an idle owned worker starts its CLI again. Once its directory is gone —
    a finished issue worktree removed after the chat is the usual way — the
    runtime resumes the same chat in the repository that held it (#2524), so the
    send goes through. A chat whose runtime cannot do that, or that has no
    repository above its directory, would be accepted and never answered: the
    optimistic row would wait forever on "Sent; waiting for transcript". That one
    is refused, and Continue in new chat is the way on.

    Only for a chat with no turn in flight. A running process does not need its
    directory looked up again, and a stale ``thinking`` flag must not cost
    someone a message they could have sent.
    """
    if not cannot_resume(session):
        return None
    return HTTPException(
        status_code=409,
        detail=(
            f"this chat's working directory ({session.cwd}) no longer exists and "
            "it cannot be resumed anywhere else; use Continue in new chat"
        ),
    )


def session_can_write_handoff(session, provider_id: str) -> bool:
    """Whether this source can both answer and call the loopback completion route.

    Codex read-only delegations have an injectable runtime but no network at all,
    including loopback. Kanban workers use workspace-write and remain eligible.
    Claude does not apply Codex's filesystem/network sandbox.
    """
    if Capability.INJECT not in session.capabilities:
        return False
    return not (
        provider_id == "codex"
        and session.is_delegated
        and session.worker_type != "kanban"
    )


def _resolved_continue_cwd(source_session) -> tuple[Path | None, str]:
    """Where the continuation starts, or the refusal that stands in for it.

    While the recorded directory is still there, use it. Once a worker worktree is
    gone, continue in its repository; there is no directory choice to make (#1512).
    """
    recorded = source_session.cwd
    if recorded_continue_cwd_exists(recorded):
        return recorded, ""
    repository = continued_chat_cwd(recorded)
    if repository is None:
        return None, (
            f"This chat's working directory {recorded} no longer exists, and no "
            "containing repository exists on this host. The transcript is still "
            "readable; start a new chat somewhere that does exist."
        )
    return repository, ""


@router.post("/sessions/{session_key}/continue", response_class=HTMLResponse)
async def continue_session(request: Request, session_key: str) -> Response:
    """Continue through the transcript, or ask this chat to hand work over."""
    _require_same_origin(request)
    source_account, source_session, source_provider = resolve_current_leg(
        request, session_key
    )
    session_key = source_session.key
    if Capability.TRANSCRIPT not in source_session.capabilities:
        raise HTTPException(status_code=404, detail="transcript unavailable")
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        raise HTTPException(status_code=415, detail="form content type required")
    form = await request.form()
    return await _continue_chat(
        request, source_account, source_session, source_provider, form
    )


async def _continue_chat(
    request: Request,
    source_account,
    source_session,
    source_provider,
    form: Mapping[str, Any],
    *,
    refusal_id: str = "continue-session-result",
) -> Response:
    """Start the next leg of a chat, or refuse in ``refusal_id``'s slot."""
    refuse = partial(_render_start_refusal, request, result_id=refusal_id)
    mode = form.get("mode")
    handoff = form.get("handoff")
    handoff_token = form.get("session_token")
    machine_callback = mode == "handoff-complete"

    def reject(message: str, *, status_code: int = 422) -> Response:
        if machine_callback:
            return Response(message, status_code=status_code, media_type="text/plain")
        return refuse(message, status_code=status_code)

    if mode not in (None, "transcript", "handoff", "handoff-complete"):
        return reject("Choose how the new chat should receive its context.")
    if mode == "handoff" and not session_can_write_handoff(
        source_session, source_account.provider_id
    ):
        return reject("This chat cannot write a handoff from this deck.")
    if mode == "handoff-complete" and (
        not isinstance(handoff, str) or not handoff.strip()
    ):
        return reject("The old chat did not provide a handoff.")
    if mode == "handoff-complete" and (
        not isinstance(handoff_token, str)
        or len(handoff_token) != 32
        or any(char not in "0123456789abcdef" for char in handoff_token)
    ):
        return reject("The handoff has no valid destination token.")
    if source_session.cwd is None:
        return reject(
            "This chat has no recorded working directory, so there is nowhere to "
            "continue it."
        )
    account_key = form.get("account_key")
    if not isinstance(account_key, str):
        return reject("Pick the account the new chat should run under.")
    try:
        account, provider = _account_provider(request, account_key)
    except HTTPException:
        return reject(
            f"The account {account_key} is no longer configured on this deck. "
            "Reload the page and pick one that is."
        )
    raw_model = form.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) else ""
    if model and not provider.is_valid_model(model):
        return reject(f"{account.label} does not offer the model “{model}”.")
    try:
        effort = _selected_effort(form, provider)
    except HTTPException:
        return reject(f"{account.label} does not offer the selected effort.")
    cwd, cwd_refusal = _resolved_continue_cwd(source_session)
    if cwd is None:
        return reject(cwd_refusal)
    refusal, served_run = _continue_refusal(
        request, get_state(request).chat_newest_leg_key(source_session.key)
    )
    if refusal:
        return reject(refusal, status_code=409)
    saved_defaults = saved_new_session_defaults(request, account, provider)
    model = model or saved_defaults.model or ""
    effort = effort or saved_defaults.effort
    transcript_url = str(
        request.url_for("session_transcript", session_key=source_session.key)
    )
    relocation = (
        ""
        if cwd == source_session.cwd
        else (
            f"\n\nThat chat ran in {source_session.cwd}, which no longer exists on "
            f"this host; you are starting in {cwd} instead, so re-establish "
            "anything the old directory held before acting on it."
        )
    )
    if mode == "handoff":
        private_origin = owned_session_base_url(get_config(request)).rstrip("/")
        public_origin = str(request.base_url).rstrip("/")
        completion_path = request.url_for(
            "continue_session", session_key=source_session.key
        ).path
        handoff_token = uuid.uuid4().hex
        handoff_file = f"/tmp/agentdeck-handoff-{handoff_token}.md"
        destination_url = f"{public_origin}{starting_chat_path(handoff_token)}"
        handoff_request = (
            "Write a concise self-contained handoff for a new chat and save the exact text "
            f"to {handoff_file} without changing the repository. Include objective, "
            "decisions, state, files/branch, tests, next steps and blockers; assume no "
            "transcript access. Form-POST it exactly once (never retry an uncertain write) to "
            f"{private_origin}{completion_path} with Origin {private_origin} and fields "
            f"mode=handoff-complete, account_key={account.key!r}, model={model!r}, "
            f"effort={(effort or '')!r}, cwd={str(cwd)!r}, and handoff loaded from "
            f"{handoff_file}, plus session_token={handoff_token}. A non-2xx response means "
            "failure: report it and give no link. On 2xx, reply with the same handoff ending "
            f"[Continue in new chat]({destination_url}). Then stop."
        )
        client_action_id = bind_action(
            request,
            session_key=source_session.key,
            provider=source_account.provider_id,
        )
        with timing_span(request, "queue"):
            result = await get_injector(request).start(
                source_account,
                source_session,
                source_provider,
                handoff_request,
                client_action_id=client_action_id,
                allow_over_budget=True,
            )
        if not result.accepted:
            return reject(start_failure_message(result.reason))
        response = get_templates(request).TemplateResponse(
            request, "partials/handoff_status.html", {}
        )
        response.status_code = 202
        return response
    if mode == "handoff-complete":
        message = (
            "Continue the work from this handoff. Do not fetch the old chat transcript; "
            "this handoff is the complete context:\n\n"
            f"{handoff.strip()}{relocation}"
        )
    else:
        message = transcript_continuation(transcript_url) + relocation
    client_action_id = bind_action(request, provider=account.provider_id)
    session_token = handoff_token if machine_callback else uuid.uuid4().hex
    if machine_callback:
        control_db = get_state(request).budget_db
        reserved = control_db.reserve_continued_chat_start(
            session_token, source_session.key
        )
        # This process still knows a reserved start it already queued. After a
        # restart that in-memory record is absent: a merely-reserved attempt did
        # not reach provider I/O and is safe to resume. Once dispatched, outcome
        # is uncertain and the stable waiting URL is the only safe response.
        if reserved == "dispatched" or (
            reserved == "reserved"
            and get_injector(request).started_chat(session_token) is not None
        ):
            return Response(
                starting_chat_path(session_token), status_code=202, media_type="text/plain"
            )
        if reserved is None:
            return reject("The handoff could not reserve its destination.", status_code=503)
    # A new leg belongs at the end of the chat's chain, and only there — the
    # chain is linear, so `reserve_chat_successor` refuses any other source.
    # Which leg is *working* is a different question, right for delivering a
    # message and wrong for growing the chain: a chat handed back to an account
    # it used before works on an earlier leg while its chain ends elsewhere
    # (#1501). Reserving from the working leg refused every such chat, forever,
    # and told the person to open a chat it gave them no way to reach (#1582).
    chat_reservation = _reserve_chat_successor(
        request,
        get_state(request).chat_newest_leg_key(source_session.key),
        session_token,
        newest_run=(served_run.work_id, served_run.run_id) if served_run else None,
    )
    if chat_reservation == "superseded":
        if machine_callback:
            get_state(request).budget_db.release_continued_chat_start(session_token)
        return reject(_LATER_RUN_WORKING, status_code=409)
    if chat_reservation == "conflict":
        conflict_message = "A new chat for this conversation is already starting."
        if machine_callback:
            get_state(request).budget_db.release_continued_chat_start(session_token)
            return reject(conflict_message, status_code=409)
        return _render_start_refusal(
            request,
            conflict_message,
            result_id=refusal_id,
            status_code=409,
            link=get_state(request).chat_path_for_session(source_session.key),
            link_label="Reload this chat",
        )
    if chat_reservation is None:
        if machine_callback:
            get_state(request).budget_db.release_continued_chat_start(session_token)
        return reject("The chat link could not be reserved.", status_code=503)
    with timing_span(request, "queue"):
        result = await get_injector(request).start_new(
            account,
            provider,
            cwd,
            message,
            model=model or None,
            effort=effort,
            client_action_id=client_action_id,
            session_token=session_token,
            allow_over_budget=True,
            origin="continue",
            source_session_key=source_session.key,
            # A Work's agent keeps its Work instructions on the new leg (#3362).
            role="work" if source_session.owned_role == "work" else None,
        )
    if not result.accepted:
        get_state(request).release_chat_successor(session_token)
        if machine_callback:
            get_state(request).budget_db.release_continued_chat_start(session_token)
        status_code = 409 if "already starting" in (result.reason or "") else 422
        return reject(start_failure_message(result.reason), status_code=status_code)
    if mode == "handoff-complete":
        return Response(
            starting_chat_path(session_token), status_code=202, media_type="text/plain"
        )
    destination_url = (
        f"{str(request.base_url).rstrip('/')}{starting_chat_path(session_token)}"
    )
    departure_message = (
        "Source chat only: this work is continuing in a new chat: "
        f"[Continue in new chat]({destination_url}). Do no more work in this "
        "chat. Reply with the same link so it remains visible here, then stop."
    )
    injector = get_injector(request)
    departure = None
    source_cwd_exists = recorded_continue_cwd_exists(source_session.cwd)
    if source_cwd_exists and Capability.STEER in source_session.capabilities:
        try:
            departure = (
                await injector.deliver_now(
                    source_account,
                    source_session,
                    source_provider,
                    departure_message,
                    allow_over_budget=True,
                )
            ).result
        except Exception:  # noqa: BLE001 -- destination already started; outcome uncertain
            log.exception(
                "continued chat %s started but steering source %s failed uncertainly",
                session_token,
                source_session.key,
            )
    elif source_cwd_exists:
        departure = await injector.start(
            source_account,
            source_session,
            source_provider,
            departure_message,
            allow_over_budget=True,
        )
    if (
        source_cwd_exists
        and departure is not None
        and not departure.accepted
        and Capability.STEER in source_session.capabilities
    ):
        # The active turn may have ended between session discovery and steering.
        # A definite refusal means no write occurred, so queueing the same message
        # as a new turn is safe; an exception above stays uncertain and is never retried.
        departure = await injector.start(
            source_account,
            source_session,
            source_provider,
            departure_message,
            allow_over_budget=True,
        )
    if departure is not None and not departure.accepted:
        log.warning(
            "continued chat %s started but source %s could not record the link: %s",
            session_token,
            source_session.key,
            departure.reason,
        )
    return await _render_start_accepted(
        request, session_token, result_id="continue-session-result"
    )


def _render_account_defaults(
    request: Request, account, provider, *, saved_message: str | None = None
) -> HTMLResponse:
    return get_templates(request).TemplateResponse(
        request,
        "partials/account_new_chat_defaults.html",
        {
            **account_settings_context(request, account, provider),
            "defaults_saved": saved_message,
        },
    )


@router.post("/accounts/{account_key}/new-chat-defaults", response_class=HTMLResponse)
async def set_account_new_chat_defaults(
    request: Request, account_key: str
) -> HTMLResponse:
    """Persist provider-validated defaults used by both New and Continue chat."""
    _require_same_origin(request)
    account, provider = _account_provider(request, account_key)
    form = await request.form()
    raw_model = form.get("model")
    raw_effort = form.get("effort")
    model = raw_model.strip() if isinstance(raw_model, str) else ""
    effort = raw_effort.strip() if isinstance(raw_effort, str) else ""
    if model and not provider.is_valid_model(model):
        raise HTTPException(status_code=422, detail="invalid model")
    if effort and not provider.is_valid_effort(effort):
        raise HTTPException(status_code=422, detail="invalid effort")
    get_db(request).record_account_new_chat_defaults(
        account.key, model or None, effort or None
    )
    # The next spare CLI boots with the new defaults (#3420).
    _nudge_start_spares(request)
    return _render_account_defaults(
        request, account, provider, saved_message="Saved for New chat"
    )


def _render_planned_reset(
    request: Request, account, *, error: str | None = None, saved: str | None = None
) -> HTMLResponse:
    return get_templates(request).TemplateResponse(
        request,
        "partials/account_planned_reset.html",
        {
            "account": account,
            "planned_reset_at": get_db(request).planned_resets().get(account.key),
            "planned_reset_error": error,
            "planned_reset_saved": saved,
        },
    )


@router.post("/accounts/{account_key}/planned-reset", response_class=HTMLResponse)
async def set_account_planned_reset(request: Request, account_key: str) -> HTMLResponse:
    """Record when a person will reset this account's usage; empty or Clear removes it (#3379)."""
    from .. import planned_reset

    _require_same_origin(request)
    account, _provider = _account_provider(request, account_key)
    form = await request.form()
    raw = form.get("at")
    text = "" if form.get("clear") else (raw if isinstance(raw, str) else "")
    try:
        at = planned_reset.apply(get_db(request), get_state(request), account.key, text)
    except ValueError as exc:
        return _render_planned_reset(request, account, error=str(exc))
    return _render_planned_reset(request, account, saved="Saved" if at else "Cleared")


async def _discover_started_session(request: Request, session_key: str) -> None:
    """Make a freshly started session resolvable before anything navigates to it.

    The periodic scan loop may not have discovered it yet, in which case its detail page
    404s "unknown session" until the next scan lands (issue #4). Reading just this chat's
    transcript, as opening a chat does (#2841), makes it resolvable at once: rescanning
    the whole account here was most of the wait a person saw, 3-6 s live (#3420).
    """
    state = get_state(request)
    if state.sessions.get(session_key) is not None:
        return
    await live_chats.load(state, request.app.state.collector, (session_key,))


def starting_chat_path(token: str) -> str:
    """The stable link for a start that has no session key yet.

    Nothing navigates here on a press any more (#1799), but the URL still has to
    exist: a Continue handoff puts it in a message an agent writes for a person to
    click later, and the machine callbacks answer with it. It is a redirect now —
    to the chat once the token names one, and to the chat list until then.
    """
    return f"/sessions/starting/{token}"


# How many 1s re-asks the status strip spends on a start this process cannot
# name before giving up. A web restart drops the in-memory ``StartedChat``
# while the spawn keeps going, and the token binding lands in the shared store
# as soon as the provider names a session — so a start that looks lost usually
# resolves to ``ready`` within the seconds a spawn takes (4-13s live). The
# window is generous because it costs one tiny poll a second, and its end is
# the honest dead end for a token nothing will ever bind (#792).
#
# The successor fence borrows this same window, which is why it lives in
# ``chat_successor`` alongside it rather than being restated there.
_LOST_RECHECKS = LOST_RECHECKS


async def starting_chat_context(request: Request, token: str, checks: int = 0) -> dict:
    """Everything the start-status strip and its poll need about one start (#765).

    ``state`` is deliberately four-valued. ``lost`` is not an error: a web restart drops the
    in-memory record while the runtime keeps spawning, so a start this process can no longer
    describe may still have become a perfectly good chat. When the record is gone but the
    token might still be bound, ``lost`` stays provisional for ``_LOST_RECHECKS`` polls —
    ``checks`` counts them — before the strip settles on the dead-end copy (#792).
    """
    start = get_injector(request).started_chat(token)
    session_key = get_state(request).session_key_for_token(token)
    if session_key is None and start is not None:
        session_key = start.status.session_key
    if session_key is not None:
        await _discover_started_session(request, session_key)
        state = "ready"
    elif start is None:
        state = "lost"
    elif start.status.state == "failed":
        state = "failed"
    elif start.status.state == "complete":
        # Accepted, but the provider never named a session — nothing to open.
        state = "lost"
    else:
        state = "starting"
    account = (
        next(
            (item for item in get_accounts(request) if item.key == start.account_key),
            None,
        )
        if start
        else None
    )
    source_key = start.source_session_key if start else None
    chat_url = (
        get_state(request).chat_path_for_session(session_key) if session_key else ""
    )
    # Only the record-is-gone flavour of ``lost`` is worth watching: a start
    # whose record says "complete, no session" has already answered, and no
    # later poll will change it.
    keep_looking = state == "lost" and start is None and checks < _LOST_RECHECKS
    # Once this page has settled on a dead end, the successor reservation it
    # fenced is a corpse: leaving it there refuses every later Continue on the
    # source chat with "a new chat is already starting" (#1620). Releasing only
    # touches a reservation nothing has claimed.
    if state == "failed" or (state == "lost" and not keep_looking):
        get_state(request).release_chat_successor(token)
        # The same corpse for a side chat: a resent press would be answered with
        # this dead start instead of trying again (#2485).
        get_state(request).release_chat_fork(token)
    return {
        "token": token,
        "start": start,
        "lost_keep_looking": keep_looking,
        "next_checks": checks + 1,
        "account_label": account.label if account else (start.account_key if start else ""),
        "session_key": session_key,
        "chat_url": chat_url,
        "starting_state": state,
        "starting_message": (
            start_failure_message(start.status.reason)
            if state == "failed" and start is not None
            else ""
        ),
        # Cleared only once a chat exists, so a refused start still finds its first
        # message waiting in the composer it was typed into (#559).
        "draft_key": (
            "agentdeck:message-draft:/sessions/new"
            if start is not None and start.origin == "new"
            else (
                f"agentdeck:message-draft:/sessions/{source_key}/fork"
                if start is not None and start.origin == "fork" and source_key
                else ""
            )
        ),
        "back_url": f"/sessions/{source_key}" if source_key else "/",
    }


@router.get("/partials/starting-chat/{token}", response_class=HTMLResponse)
async def starting_chat_status(
    request: Request,
    token: str,
    checks: int = 0,
    result_id: str = "new-session-result",
) -> HTMLResponse:
    """Re-render the strip in the composer's own result slot.

    ``result_id`` travels with the poll because the strip replaces that slot: a
    reply carrying the wrong id would leave the form with no target for the next
    start it accepts.
    """
    context = await starting_chat_context(request, token, checks=max(0, checks))
    return get_templates(request).TemplateResponse(
        request,
        "partials/new_session_status.html",
        {**context, "result_id": _START_RESULT_IDS.get(result_id, "new-session-result")},
    )


@router.post("/pollers/{instance}/pause", response_class=HTMLResponse)
async def poller_pause(instance: str, request: Request) -> HTMLResponse:
    """Pause a Poller Instance and re-render the list (#182)."""
    return await _poller_set_paused(instance, request, paused=True)


@router.post("/pollers/{instance}/resume", response_class=HTMLResponse)
async def poller_resume(instance: str, request: Request) -> HTMLResponse:
    """Resume a Poller Instance and re-render the list (#182)."""
    return await _poller_set_paused(instance, request, paused=False)


async def _poller_set_paused(instance: str, request: Request, *, paused: bool) -> HTMLResponse:
    """Write the pause straight into the instance's state; the next wake honours it (#2596)."""
    from .. import poller_ops
    from .render import poller_context

    form = await request.form()
    try:
        poller_ops.set_paused(
            _poller_root(request), instance, paused, by=str(form.get("by") or "operator")
        )
    except OSError as exc:
        # An instance whose state cannot be written is a fact to show, not a 500.
        logging.getLogger(__name__).warning("poller control failed for %s: %s", instance, exc)

    return _poller_page(request, poller_context(request))


@router.post("/pollers/{instance}/extensions/chatgpt-nudge", response_class=HTMLResponse)
async def poller_chatgpt_nudge(instance: str, request: Request) -> HTMLResponse:
    """Click "Run now" on this project's ChatGPT PR-review task, by hand (#1584).

    There is nothing for a wake to do here, because the click program is the same one the
    extension launches and it runs detached. The press therefore starts it here and reports back
    through a receipt, exactly as a wake's does.
    """
    from .. import poller_nudge
    from .render import poller_context

    error = ""
    try:
        poller_nudge.press(_poller_root(request), instance)
    except poller_nudge.NudgeError as exc:
        # A refused press is a sentence on the page, not a 500.
        error = str(exc)
        logging.getLogger(__name__).warning("chatgpt nudge refused for %s: %s", instance, error)
    return _poller_page(
        request, poller_context(request, nudge_errors={instance: error} if error else {})
    )


@router.post("/pollers/{instance}/settings/worker-accounts", response_class=HTMLResponse)
async def poller_worker_accounts(instance: str, request: Request) -> HTMLResponse:
    """Change which account a Poller Instance's workers run on (#901).

    Unlike a control, this is not an intent for the next wake to perform: it edits the instance's
    Overlay, which the next wake simply reads. That is safe precisely because the Overlay is not
    fingerprinted — a host may re-point or re-credential a project without re-approving it.

    Only accounts this deck actually has are accepted. An operator who names an account the host
    does not know would produce an Overlay that loads fine and then dispatches into nothing.
    """
    _require_same_origin(request)
    from .. import poller_settings
    from .render import poller_context

    form = await request.form()
    root = _poller_root(request)
    # What is already configured counts as known, even when this deck has no such account: an
    # Overlay may legitimately name one, and a save must not silently drop it.
    known = {account.key for account in (request.app.state.accounts or [])}
    known |= set(poller_settings.read_worker_accounts(root, instance).order)
    account = str(form.get("account") or "").strip()
    fallbacks = [str(value).strip() for value in form.getlist("fallbacks") if str(value).strip()]

    error = ""
    unknown = [key for key in [account, *fallbacks] if key and key not in known]
    if unknown:
        error = f"this host has no account named {', '.join(sorted(unknown))}"
    else:
        try:
            poller_settings.set_worker_accounts(
                root,
                instance,
                account=account,
                fallbacks=fallbacks,
                by=str(form.get("by") or "operator"),
            )
        except poller_settings.SettingsError as exc:
            error = str(exc)

    if error:
        logging.getLogger(__name__).warning(
            "poller worker accounts refused for %s: %s", instance, error
        )
    return _poller_page(
        request, poller_context(request, settings_errors={instance: error} if error else {})
    )


@router.post("/pollers/build", response_class=HTMLResponse)
async def build_poller_project(request: Request) -> HTMLResponse:
    """Launch a Deck-owned Poller Builder session (#181).

    The Builder is an ordinary session running a playbook, so this action does only the part that
    is new: pick the first offered account with usage left, and open the conversation with a brief
    that names the draft paths and the fallbacks. Ownership, resume across deploys, cancel and the
    operator's answers all come from the session model that already exists (#111).

    What it also does is *record the run* (#268). The launch plan — the drafts, the cwd, the
    operator's order and who won it — used to live only in this request, which left the Builder
    invisible the moment the 202 returned and left nothing for a later handover to advance.
    """
    _require_same_origin(request)
    from .. import poller_builder

    form = await request.form()
    project = str(form.get("project") or "").strip()
    manifest = str(form.get("manifest") or "").strip()
    overlay = str(form.get("overlay") or "").strip()
    order = [key for key in form.getlist("account_key") if isinstance(key, str) and key]
    if not project or not manifest or not overlay or not order:
        raise HTTPException(status_code=422, detail="a project, both draft paths and an account")
    if not poller_builder.is_project_id(project):
        # The id names the run's file under the poller root, so it may only be a plain name.
        raise HTTPException(status_code=422, detail="a project id must be a plain name")

    accounts = {account.key: account for account in get_accounts(request)}
    state = get_state(request)
    plan = poller_builder.plan_launch(
        order,
        {key: state.usage.get(key) for key in order},
        labels={key: account.label for key, account in accounts.items()},
    )
    if not plan.ok:
        # Every offered account is full: holding is the honest answer, not starting anyway.
        raise HTTPException(status_code=409, detail=plan.detail)
    account = accounts.get(plan.account_key)
    if account is None:
        raise HTTPException(status_code=422, detail="unknown account")
    provider = PROVIDERS.get(account.provider_id)
    if provider is None:
        raise HTTPException(status_code=422, detail="unknown provider")

    brief = poller_builder.BuilderBrief(
        project=project,
        repo_path=manifest,
        overlay_path=overlay,
        fallbacks=plan.fallbacks,
    )
    try:
        cwd = Path(str(form.get("cwd") or Path(manifest).parent)).expanduser().resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=422, detail="invalid working directory") from None
    client_action_id = bind_action(request, provider=account.provider_id)
    session_token = uuid.uuid4().hex
    result = await get_injector(request).start_new(
        account,
        provider,
        cwd,
        brief.render(),
        [],
        client_action_id=client_action_id,
        session_token=session_token,
    )
    if not result.accepted:
        raise HTTPException(status_code=422, detail=result.reason)
    # Recorded only once the launch is accepted: a run pointing at a leg that never started would
    # offer a handover from an account that never held anything. A root that cannot be written
    # loses the record, not the interview — the session is already running either way.
    try:
        poller_builder.record_launch(
            _poller_root(request),
            project,
            manifest=manifest,
            overlay=overlay,
            cwd=str(cwd),
            order=order,
            account_key=account.key,
            session_token=session_token,
        )
    except OSError as exc:
        log.warning("could not record the Builder run for %s: %s", project, exc)
    return await _render_start_accepted(
        request, session_token, result_id="poller-build-result"
    )


def _poller_root(request: Request) -> str:
    settings = getattr(get_config(request), "poller", None)
    return getattr(settings, "root", "~/.local/share/agentdeck/poller")


@router.post("/pollers/builder/{project}/handover", response_class=HTMLResponse)
async def poller_builder_handover(project: str, request: Request) -> HTMLResponse:
    """Hand a Builder run to the next account the operator listed (#268).

    Handover is a deliberate act, and it is always *a new session with a continuation brief* —
    never a resume, because the operator's order may cross providers. Three things make it safe to
    press: the claim is durable before the successor is started, so an uncertain press reads as
    done rather than as a second leg; a leg still working is refused, because the deck lets a live
    worker be steered even over budget and preempting a turn would lose it; and when nobody is
    left the run holds visibly instead of quietly looking finished.
    """
    _require_same_origin(request)
    from .. import poller_builder
    from .render import poller_context

    root = _poller_root(request)
    run = poller_builder.read_run(root, project)
    if run is None:
        raise HTTPException(status_code=404, detail="no Builder run for this project")

    form = await request.form()
    intent_id = str(form.get("intent") or "")
    state = get_state(request)
    leg = run.leg
    token = leg.session_token if leg else ""
    session_key = state.session_key_for_token(token) if token else None
    session = state.sessions.get(session_key) if session_key else None
    if session is not None and session.thinking:
        raise HTTPException(
            status_code=409, detail="the current leg is still working — let its turn finish"
        )

    # Deliberately fails closed: a claim that cannot be written durably must not be followed by a
    # spawn, because then nothing on disk would stop the next press starting a second leg.
    claimed = poller_builder.claim_handover(root, project, intent_id)
    if claimed is None:
        # The same press, not a second one. Re-render what the run actually says now.
        return _poller_page(request, poller_context(request))

    successor = claimed.next_account(
        {key: state.usage.get(key) for key in claimed.order}
    )
    if not successor:
        # Holding visibly, because a run that quietly looked finished is the worst outcome.
        poller_builder.hold(
            root,
            project,
            "no remaining account is configured — the run waits here, resumably",
        )
        return _poller_page(request, poller_context(request))

    accounts = {account.key: account for account in get_accounts(request)}
    account = accounts.get(successor)
    provider = PROVIDERS.get(account.provider_id) if account else None
    if account is None or provider is None:
        poller_builder.note(root, project, f"{successor} is no longer a known account")
        return _poller_page(request, poller_context(request))

    transcript_url = (
        str(request.url_for("session_transcript", session_key=session_key))
        if session_key
        else ""
    )
    brief = poller_builder.ContinuationBrief(
        project=claimed.project,
        repo_path=claimed.manifest,
        overlay_path=claimed.overlay,
        fallbacks=claimed.order[claimed.order.index(successor) + 1 :],
        transcript_url=transcript_url,
    )
    try:
        cwd = Path(claimed.cwd).expanduser()
    except (OSError, RuntimeError):
        cwd = Path(claimed.cwd)
    session_token = uuid.uuid4().hex
    # The handover is a switch, so the successor is the same chat's next leg, fenced before
    # the spawn exactly as an operator's Continue is (#1267). A run whose current leg already
    # has a successor keeps it: the handover still starts, on a chat of its own, because a
    # refused fork must not also refuse the operator's deliberate press.
    linked = bool(session_key) and state.reserve_chat_successor(
        session_token, session_key
    ) in {"new", "reserved"}
    result = await get_injector(request).start_new(
        account,
        provider,
        cwd,
        brief.render(),
        [],
        client_action_id=bind_action(request, provider=account.provider_id),
        session_token=session_token,
    )
    if not result.accepted and linked:
        state.release_chat_successor(session_token)
    try:
        if not result.accepted:
            # The claim stays spent, but the run's next intent id has moved on, so a deliberate
            # second press can retry — the leg it would have conflicted with never started.
            poller_builder.note(
                root, project, f"handover to {successor} was refused: {result.reason}"
            )
        else:
            poller_builder.add_leg(
                root, project, account_key=successor, session_token=session_token
            )
    except OSError as exc:
        # The successor is already running: losing its record is a fact to show, not a 500.
        log.warning("could not record the handover of %s: %s", project, exc)
    return _poller_page(request, poller_context(request))


@router.post("/pollers/builder/{project}/forget", response_class=HTMLResponse)
async def poller_builder_forget(project: str, request: Request) -> HTMLResponse:
    """Drop a run the operator is finished with — the Builder stops at a draft, and only they
    know when it has been taken up. Nothing on disk but the record itself is touched: the drafts
    are the point of the whole exercise."""
    _require_same_origin(request)
    from .. import poller_builder
    from .render import poller_context

    poller_builder.forget_run(_poller_root(request), project)
    return _poller_page(request, poller_context(request))


def _poller_page(request: Request, context: dict) -> HTMLResponse:
    """The instance list fragment, which is what every poller control re-renders."""
    return get_templates(request).TemplateResponse(
        request, "partials/poller_instances.html", context
    )
