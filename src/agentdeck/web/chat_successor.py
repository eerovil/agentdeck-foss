"""Fencing a chat's one continuation, and stepping over a fence nothing will lift.

``chat_successors`` lets a chat have exactly one successor, so two presses cannot fork
it. Nothing deletes a reservation once its start has been dispatched, so a start that
failed later — or one whose in-memory record a web restart dropped — leaves the row
behind and every later continuation of that chat is refused, forever, with no way out
(#1620).

Every path that continues a chat needs that step-over, and until #2127 only one of them
had it: the Continue button, but not the poller delivery that moves a card to another
account when the first account runs out of quota. The usage move of a Work's chat (#3362)
is the third. The rule lives here so they cannot drift apart again.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from shlex import quote
from typing import Any

from fastapi import Request

from .deps import get_injector, get_state

# How long the status strip keeps calling a dropped token provisional. A start whose
# in-memory record is gone only means "gone" once the strip itself would have given up,
# so the fence borrows that window rather than inventing a second one.
LOST_RECHECKS = 90


def blocking_start_is_live(injector: Any, token: str, reserved_at: datetime) -> bool:
    """Whether the reservation fencing this chat still has a real start behind it.

    Only a start this process can still see running, or one too young for a restart to
    have been ruled out, has earned the fence.
    """
    if reserved_at.tzinfo is None:
        reserved_at = reserved_at.replace(tzinfo=UTC)
    start = injector.started_chat(token)
    if start is None:
        grace = timedelta(seconds=LOST_RECHECKS)
        return datetime.now(UTC) - reserved_at < grace
    if start.status.state == "failed":
        return False
    # Accepted, but the provider never named a session. The status strip calls that a
    # dead end too, and no later poll changes it.
    return start.status.state != "complete"


def reserve_chat_successor(
    request: Request,
    source_session_key: str,
    token: str,
    *,
    newest_run: tuple[str, str] | None = None,
) -> str | None:
    """Fence this chat's one continuation, stepping over a start that has died.

    A conflict is only real while the reservation holding it has a live start behind it.
    Once that start is gone the fence is a corpse, and refusing because of it is what
    leaves a chat permanently un-continuable (#1620).

    ``source_session_key`` is reserved against exactly as given. The store refuses a key
    that is not its chat's newest leg, and that refusal is load-bearing for a caller
    holding a *frozen* leg: a replayed context delivery must fence the leg it originally
    decided about, never whichever leg the chat has moved on to since. A caller that
    means "wherever this chat is now" resolves that itself before calling.

    ``newest_run`` fences the reservation on that Run still being its Work's newest (#2862).
    """
    return reserve_successor(
        get_state(request),
        lambda: get_injector(request),
        source_session_key,
        token,
        newest_run=newest_run,
    )


def reserve_successor(
    state: Any,
    injector: Callable[[], Any],
    source_session_key: str,
    token: str,
    *,
    newest_run: tuple[str, str] | None = None,
) -> str | None:
    """``reserve_chat_successor`` for a caller with no request, such as the usage move.

    ``injector`` is read only when a conflict has to be weighed.
    """
    reservation = state.reserve_chat_successor(token, source_session_key, newest_run=newest_run)
    if reservation != "conflict":
        return reservation
    pending = state.pending_chat_successor(source_session_key)
    if pending is None:
        return reservation
    blocking_token, reserved_at = pending
    if blocking_start_is_live(injector(), blocking_token, reserved_at):
        return reservation
    state.release_chat_successor(blocking_token)
    return state.reserve_chat_successor(token, source_session_key, newest_run=newest_run)


def transcript_continuation(transcript_url: str) -> str:
    """The first message of a chat continued from its predecessor's transcript."""
    return (
        "Continue the work from this AgentDeck chat in the new session:\n"
        f"{transcript_url}\n\n"
        "Before taking any substantive action, fetch and read the complete transcript "
        "with:\n"
        f"curl --fail --silent --show-error {quote(transcript_url)}\n\n"
        "Treat it as the prior conversation context and continue from where it ended."
        " A final departure message that links to this new chat and tells the old chat "
        "to stop applies only to that source chat, not to this session."
    )
