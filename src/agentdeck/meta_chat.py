"""The deck chat: one always-on chat for steering the deck itself (#3403).

The 🎙️ link in the top bar opens it (`GET /meta`). It is one chat forever: after
``[meta_chat] idle_reset_minutes`` without a message, the next message starts the
chat's next leg — a fresh provider session with a clean context, linked through the
same successor fence a Continue uses — so the page, its address and the transcript
never change. Each leg's first message carries ``brief()``, which the chat view hides
behind a 🎙️ mark, as it hides voice mode's line.

Which chat is the deck chat is kept as the tokens its starts were minted under
(`Db.deck_chat_starts`): the newest ``first`` start names the chat, and every
``reset`` start names a leg the transcript marks as a fresh start.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from .titles import title_evidence_signature

MARK = "🎙️"
TITLE = f"{MARK} Deck chat"
FIRST = "first"
RESET = "reset"
#: The reason a fresh-start seam carries, beside context management's rollover reasons.
IDLE_RESET = "idle_reset"

_BRIEF_HEAD = "(Deck chat: "
BRIEF = (
    _BRIEF_HEAD + "you are the owner's always-on helper for AgentDeck. Answer questions about "
    "chats, Works, pull requests and usage by reading $AGENTDECK_URL's API and gh. To "
    "get code changed, file an issue in the right repository (it starts a Work); never "
    "edit code yourself. When the owner answers a Work's question, GET "
    "$AGENTDECK_URL/api/works/<owner>/<repo>/<number>/question and follow each question's "
    "answer_with: POST a question_box answer as JSON to its answer_url, and send a chat "
    "message only where it says message. Never answer a question box by chat message, and "
    "never by issue comment; that leaves the box open and the Work stuck on Needs answer. "
    "Keep replies short; the owner is often listening, not reading."
)


def brief(previous_transcript_url: str | None = None) -> str:
    """The paragraph a leg's first message opens with."""
    earlier = (
        f" Earlier conversation, only if the owner refers back to it: {previous_transcript_url}"
        if previous_transcript_url
        else ""
    )
    return f"{BRIEF}{earlier})"


def with_brief(message: str, previous_transcript_url: str | None = None) -> str:
    return f"{brief(previous_transcript_url)}\n\n{message.strip()}"


def hide_brief(text: str) -> str:
    """``text`` with a leading brief replaced by the 🎙️ mark; the agent still has it."""
    if not text.startswith(_BRIEF_HEAD):
        return text
    head, sep, rest = text.partition(")\n\n")
    if not sep or "\n\n" in head:
        return text
    return f"{MARK} {rest.lstrip()}" if rest.strip() else MARK


def resolve_account(accounts: Iterable, label: str | None):
    """The Claude account ``[meta_chat] account`` names, else the first Claude account.

    The deck chat runs on Claude only: a label naming another provider's account is
    no account at all, so the start is refused with the missing-account sentence.
    """
    claude = [account for account in accounts if account.provider_id == "claude_code"]
    if label:
        return next((account for account in claude if account.label == label), None)
    return next(iter(claude), None)


def needs_fresh_leg(session, *, busy: bool, now: datetime, minutes: int) -> bool:
    """Whether a message now should start the next leg rather than reach ``session``.

    Only a leg at rest that has been quiet for the whole window: a working turn, a
    queued message or a leg with no recorded activity keeps the message where it is.
    """
    if busy or session.thinking or session.last_activity is None:
        return False
    return now - session.last_activity >= timedelta(minutes=minutes)


def deck_chat_session_key(state, db) -> str | None:
    """A session key of the deck chat (any leg), or ``None`` before it has one."""
    if db is None:
        return None
    for token, kind, _at in reversed(db.deck_chat_starts()):
        if kind != FIRST:
            continue
        key = state.session_key_for_token(token)
        if key is not None:
            return key
    return None


def is_deck_chat(state, db, session_key: str) -> bool:
    deck_key = deck_chat_session_key(state, db)
    if deck_key is None:
        return False
    return state.chat_identity_key(deck_key) == state.chat_identity_key(session_key)


def name_deck_chat(state, session_key: str) -> None:
    """Give the deck chat its title, unless the owner has named it by hand.

    Stored as a hand-typed name so the chat's own self-naming cannot replace it.
    """
    session = state.sessions.get(session_key)
    if session is None:
        return
    standing = state.generated_title_for(session_key)
    if standing is not None and standing.source == "user":
        return
    state.set_generated_title(session_key, TITLE, title_evidence_signature(session), source="user")


def reset_leg_reasons(state, db) -> dict[str, str]:
    """Leg key → ``IDLE_RESET`` for every leg a fresh start began."""
    if db is None:
        return {}
    reasons = {}
    for token, kind, _at in db.deck_chat_starts():
        if kind == RESET and (key := state.session_key_for_token(token)) is not None:
            reasons[key] = IDLE_RESET
    return reasons
