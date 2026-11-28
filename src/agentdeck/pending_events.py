"""Pending events (#4303): deck notes a chat gets with its next message, without being woken.

Some deck notices tell a Work's chat something it should know but need nothing from it now,
such as a review pass on a head that is already tested. Waking the chat for each costs a
whole turn, and staying silent means its agent never hears it. A pending event is the middle
way: the deck stores the note, and the next message delivered to that chat — a person's, the
deck's, a steer into a running turn — carries every waiting note in front of it, once, in the
order they came.

"Once" rides on the delivery fence the send paths already have. A delivery takes its notes
before anything is written to the provider and keeps them under its action id: a retry of
that delivery, in this process or after a restart, carries the same notes (so the fence sees
the same text and replays instead of sending twice), and no other delivery can take them. A
clean refusal puts them back for the next message. A delivery whose outcome is unknown keeps
them: at-most-once, as for the message itself. A rolled-back message's notes are added
again as new ones, since the agent has forgotten them (#4618).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .note_text import NOTES_END as NOTES_END
from .note_text import NOTES_HEADING as NOTES_HEADING
from .note_text import split_notes as split_notes
from .note_text import with_notes as with_notes
from .note_text import without_notes as without_notes
from .providers.delivery import delivery_participated

log = logging.getLogger(__name__)

#: How long a delivered note is kept for the record. A waiting one is kept until it goes.
KEEP_DELIVERED = timedelta(days=7)


def carries_notes(message: str) -> bool:
    """Can ``message`` carry notes? Not a slash command: anything in front of the slash
    turns the command into plain text, and the command would never run."""
    return not message.lstrip().startswith("/")


def refused_before_write(outcome: object) -> bool:
    """Is ``outcome`` a delivery known to have written nothing, so its notes may go back?

    The one rule every send path asks (#4303). An ``InjectResult`` counts when refused
    without ``write_uncertain``; a runtime delivery answer (a mapping) when the runtime
    answered and its own participation rule says the write never began. A refusal that
    names a session may have written to it. Anything else —
    an exception, an unknown shape, an uncertain or possibly started write — keeps the
    notes claimed: at most once, as for the message itself."""
    if isinstance(outcome, Mapping):
        return (
            bool(outcome.get("action"))
            and outcome.get("action") != "uncertain"
            and not outcome.get("accepted")
            and not delivery_participated(outcome)
        )
    accepted = getattr(outcome, "accepted", None)
    return (
        accepted is False
        and not getattr(outcome, "write_uncertain", True)
        # A refusal naming a session (a start that began one) may have written to it.
        and not getattr(outcome, "session_id", None)
    )


class Store(Protocol):
    def add_pending_event(self, event_id: str, chat_id: str, text: str) -> bool: ...

    def claim_pending_events(
        self, chat_ids: Sequence[str], action_id: str, fits: Callable[[list[str]], bool]
    ) -> list[str]: ...

    def release_pending_events(self, action_id: str) -> None: ...

    def pending_events(self, chat_id: str) -> list[dict]: ...

    def prune_pending_events(self, delivered_before: datetime) -> None: ...


class PendingEvents:
    """The deck's pending notes, by chat, over the control database."""

    def __init__(self, store: Store, chats_of: Callable[[str], Sequence[str]]) -> None:
        self.store = store
        #: The chat ids a session's notes may be kept under: its chat's id, and the
        #: session key itself for a session no chat row names yet (the deck addresses
        #: such a session by its key, as scheduled messages do).
        self.chats_of = chats_of

    def add(self, chat_id: str, event_id: str, text: str) -> bool:
        """Keep ``text`` for ``chat_id``'s next message; whether it is held. Wakes nothing."""
        if not chat_id or not text.strip():
            return False
        held = self.store.add_pending_event(event_id, chat_id, text.strip())
        if held:
            log.info("pending event %s kept for chat %s", event_id, chat_id)
        return held

    def claim(
        self,
        session_key: str,
        action_id: str,
        fits: Callable[[list[str]], bool],
        *,
        retry_only: bool = False,
    ) -> list[str]:
        """The notes the delivery ``action_id`` to ``session_key`` carries, oldest first.

        ``retry_only`` takes no new notes: only those this delivery already took, which its
        retry must carry again, even when the retry can no longer bind to the chat."""
        try:
            chats = (
                []
                if retry_only
                else [chat for chat in dict.fromkeys(self.chats_of(session_key)) if chat]
            )
        except Exception:  # noqa: BLE001 - unread: the message goes out bare
            log.warning("pending events: no chat for %s", session_key, exc_info=True)
            chats = []
        notes = self.store.claim_pending_events(chats, action_id, fits)
        if notes:
            log.info(
                "delivery %s to %s carries %d pending note(s)", action_id, session_key, len(notes)
            )
        return notes

    def release(self, action_id: str) -> None:
        """The delivery ``action_id`` was cleanly refused: its notes wait for the next one."""
        self.store.release_pending_events(action_id)

    def waiting(self, chat_id: str) -> list[dict]:
        return self.store.pending_events(chat_id)

    def prune(self, now: datetime | None = None) -> None:
        self.store.prune_pending_events((now or datetime.now(UTC)) - KEEP_DELIVERED)
