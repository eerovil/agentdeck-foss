"""One agent's message to the chat running another Work (#3908).

An agent that filed an issue, or was told to change one, used to edit that issue or comment
on it. The Work router passes on only a person's ``/claude`` comment, and every agent writes
as the deck's own login, so the Work's agent never saw the change and built the old request.
This sends the text straight to the chat that owns the Work, through the scheduled-message
path the router uses: queued behind a running turn, at most once per id.

A caller that may retry names the message with an ``id`` of its own: a retry under the same id
is stored once, and the same words sent again later under a new id are a new message (#3909
review). With no id, every call is a new message. The text says it is from another agent,
not the owner, so the agent asks the owner when it conflicts with what they said.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from .providers import PROVIDERS
from .scheduled_messages import ScheduledMessage
from .work import WorkRef

#: The most text one message carries; longer belongs on the issue.
MAX_TEXT = 4000

HEADER = "AgentDeck message for this Work ({target}) from {sender}, another agent, not the owner:"
FOOTER = (
    "Act on it when it changes or corrects what this Work is asked to do, and say so on the "
    "issue. If it conflicts with what the owner told you, ask the owner in your question box. "
    "Reply to the sender only if it asks you something: `POST "
    "$AGENTDECK_URL/api/works/{reply}/message` with your answer as `text` and your own "
    "session key as `from`."
)


class MessageRefused(Exception):
    """Why a message was not sent, and the HTTP status that says so."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Sent:
    message_id: str
    chat_id: str
    #: ``False`` when the same message was already stored: nothing more was sent.
    new: bool


def is_agent_message(text: str) -> bool:
    """Whether a delivered row's text is one of these messages, not a person's."""
    return text.startswith(HEADER.split("{", 1)[0])


def on_delivered(state, assistant):
    """What a delivered scheduled message does to its chat's attention.

    A person's message, or the deck's on their behalf, is a boss turn: it opens a boss window
    and retires the chat's report and its open question to the owner. Another Work's agent is
    not the owner (#3909 review), so its message leaves the question and the report standing.
    """

    def delivered(session_key: str, text: str) -> None:
        if is_agent_message(text):
            return
        state.note_boss_turn(session_key, text)
        assistant.handle_boss_turn(session_key)

    return delivered


def message_refusal(state, chat_id: str, text: str) -> str | None:
    """Why the provider of ``chat_id``'s current leg would drop ``text``, or ``None``.

    When the deck cannot tell the provider, every provider is asked, so a message any of them
    would drop is never sent.
    """
    chat = state.chat_by_id(chat_id)
    session = state.current_session_for_chat(chat) if chat is not None else None
    session = session or state.sessions.get(chat_id)
    provider = PROVIDERS.get(getattr(session, "provider_id", ""))
    for candidate in (provider,) if provider is not None else PROVIDERS.values():
        refusal = candidate.user_message_refusal(text)
        if refusal is not None:
            return refusal
    return None


def message_id(sender_chat: str, ref: WorkRef, caller_id: str = "") -> str:
    """The row id: stable for one sender, Work and caller id; fresh when there is no id."""
    if not caller_id:
        return f"agent-message:{secrets.token_hex(12)}"
    digest = hashlib.sha256(f"{sender_chat}\n{ref.work_id}\n{caller_id}".encode()).hexdigest()
    return f"agent-message:{digest[:24]}"


def send(state, messages, ref: WorkRef, sender_key: str, text: str, caller_id: str = "") -> Sent:
    """Store ``text`` for the chat that owns ``ref``'s Work, from the chat of ``sender_key``."""
    from .work_router import owner_of_issue

    text = text.strip()
    if not text:
        raise MessageRefused(422, "the message is empty")
    if len(text) > MAX_TEXT:
        raise MessageRefused(
            422, f"the message is over {MAX_TEXT} characters; put the detail on the issue"
        )
    projection = state.work_projection()
    owner = owner_of_issue(state, ref.repository, ref.number, projection)
    if owner is None:
        raise MessageRefused(
            409,
            f"no chat on this deck runs {ref.repository}#{ref.number}; "
            "edit or comment on its issue instead",
        )
    chat_id, target = owner
    sender_chat = state.chat_identity_key(sender_key)
    if sender_chat == chat_id or sender_key == chat_id:
        raise MessageRefused(409, "that Work is this chat's own")
    joined = projection.works_chat_joined(sender_chat) or projection.works_for_chat(sender_chat)
    sender = f"the chat of {joined[0].repository}#{joined[0].number}" if joined else "a chat"
    reply = (
        f"{joined[0].repository}/{joined[0].number}" if joined else "<owner>/<repo>/<issue number>"
    )
    body = (
        f"{HEADER.format(target=target, sender=sender)}\n\n{text}\n\n{FOOTER.format(reply=reply)}"
    )
    refused = message_refusal(state, chat_id, body)
    if refused is not None:
        raise MessageRefused(422, f"its chat would not receive this text: {refused}")
    key = message_id(sender_chat, ref, caller_id.strip())
    if messages.exists(key):
        return Sent(key, chat_id, False)
    now = datetime.now(UTC)
    stored = messages.schedule(
        ScheduledMessage(id=key, chat_id=chat_id, text=body, due_at=now, created_at=now)
    )
    if stored is None and not messages.exists(key):
        raise MessageRefused(409, "the deck could not store the message; send it again")
    return Sent(key, chat_id, stored is not None)
