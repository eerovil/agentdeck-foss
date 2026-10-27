"""Roll a chat back to before one of the person's messages (#3474).

``POST /sessions/{leg}/rollback`` with the message's transcript line ``seq``. That
message and everything after it leave the chat, and the agent's next turn continues
as if they were never sent: the provider resumes the same session from the line
before it (Claude's ``--resume-session-at``). Only the conversation goes back. Files
the agent changed and messages it already sent stay, and the confirm box says so.

The agent no longer remembers anything it asked for in the removed part, so the
chat's wake-ons and ``agentdeck job``s registered from that message on are cancelled
without a word to it. The answer carries the removed message's text, which the page
puts back into the message box.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request

from .. import deck_notices
from ..attachments import display_message
from .deps import get_state, resolve_session
from .routes_actions import _require_same_origin

log = logging.getLogger(__name__)

router = APIRouter()

# One rollback at a time per chat: two pressed at once would each hide their own range
# while the runtime kept only the last cut, leaving one removed part in the agent's memory.
_CHAT_LOCKS: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

BUSY = "The agent is working. Stop it first, then roll back."
NOT_A_CUT = (
    "Only your own messages can be rolled back to, and not the chat's first one: "
    "there is nothing before it to go back to."
)


def _refuse(status_code: int, text: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=text)


@router.post("/sessions/{session_key}/rollback")
async def rollback_session(request: Request, session_key: str) -> dict:
    _require_same_origin(request)
    form = await request.form()
    raw_seq = form.get("seq")
    try:
        seq = int(raw_seq) if isinstance(raw_seq, str) else 0
    except ValueError:
        seq = 0
    if seq < 1:
        raise _refuse(422, "Choose the message to roll back to.")
    account, session, provider = resolve_session(request, session_key)
    state = get_state(request)
    if state.chat_current_session_key(session.key) != session.key:
        raise _refuse(
            409, "This message is from before the chat moved account, so it cannot be rolled back."
        )
    refusal = provider.rewind_refusal(account, session)
    if refusal is not None:
        raise _refuse(409, refusal)
    if session.thinking:
        raise _refuse(409, BUSY)
    async with _CHAT_LOCKS[state.chat_identity_key(session.key)]:
        return await _rollback(request, account, session, provider, seq)


async def _rollback(request: Request, account, session, provider, seq: int) -> dict:
    """The rollback itself, under its chat's lock, so each press sees the last one's result."""
    state = get_state(request)
    at = await asyncio.to_thread(provider.rewind_ref, account, session, seq)
    message = next(
        (
            event
            for event in await provider.read_transcript(account, session, after_seq=seq - 1)
            if event.seq == seq and event.role == "user"
        ),
        None,
    )
    if at is None or message is None:
        raise _refuse(409, NOT_A_CUT)
    if _sent_by_deck(request, session.key, message):
        raise _refuse(409, NOT_A_CUT)
    since = message.ts.timestamp() if message.ts is not None else None
    # The runtime cancels the jobs the agent forgot in the same call that rolls it back,
    # so no request can fail between the two and leave one to report to it later.
    result = await provider.rewind(
        account, session, at, chat_keys=state.chat_session_keys(session.key), since=since
    )
    if not result.accepted:
        busy = "worker_busy" in (result.reason or "")
        raise _refuse(409, BUSY if busy else f"The chat could not be rolled back: {result.reason}")
    # After the process ended, so the count holds every line the removed part wrote.
    _offset, last = await provider.transcript_cursor(account, session)
    if not state.record_transcript_rollback(session.key, seq, max(seq, last)):
        raise _refuse(
            503,
            "The agent was rolled back, but the deck could not record which messages to "
            "hide. Try again.",
        )
    waits = _cancel_waits(request, session.key, since)
    return {"text": display_message(message.text), "cancelled_waits": waits}


def _sent_by_deck(request: Request, session_key: str, message) -> bool:
    """Whether the deck sent this prompt, by the rule the page greys such turns out by."""
    if message.answer or message.notice:
        return True
    store = getattr(request.app.state, "control_db", None)
    chat_id = get_state(request).chat_identity_key(session_key)
    texts = deck_notices.candidate_texts([message])
    records = store.deck_message_ids(chat_id, texts) if store is not None and texts else {}
    return deck_notices.notice_kind(message.text, records, message.ts) is not None


def _cancel_waits(request: Request, session_key: str, since: float | None) -> int:
    """Drop the chat's wake-ons registered from the removed message on."""
    service = getattr(request.app.state, "wake_on", None)
    if service is None or since is None:
        return 0
    chat_id = get_state(request).chat_identity_key(session_key)
    return service.registry.remove(
        item.id for item in service.registry.for_chat(chat_id) if item.registered_at >= since
    )


def rollback_context(request: Request, session_key: str) -> dict:
    """The chat page's rollback dialog names the one leg it can roll back, or none."""
    state = get_state(request)
    current = state.chat_current_session_key(session_key)
    try:
        account, session, provider = resolve_session(request, current)
    except HTTPException:
        return {"rollback_leg_key": None}
    if provider.rewind_refusal(account, session) is not None:
        return {"rollback_leg_key": None}
    return {
        "rollback_leg_key": session.key,
        "rollback_floor": provider.rewind_floor(account, session),
    }
