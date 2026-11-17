"""What a chat page needs to offer side chats (#2485).

A side chat is a new chat started as a copy of another chat's history, cut after one
finished turn. A reply's button starts one to file new work from there (#3171): the
side chat writes the issue from the conversation so far and stops. The page needs two
things from the server: which of its legs can be cut (the per-reply button only
appears on those), and — when the page *is* a side chat — where it came from, so it
can link back and send a summary there.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request

from ..attachments import display_message
from ..chat_transcript import chat_event_id, load_chat_transcript
from ..db import SideChatFork
from ..providers import PROVIDERS
from ..providers.base import DETAIL_WINDOW
from .deps import (
    get_accounts,
    get_injector,
    get_state,
    get_templates,
    resolve_current_leg,
    resolve_session,
)
from .render import render_chat_transcript_leg, session_labels

SUMMARY_PROMPT = (
    "Summarise what this side chat concluded, for the original chat: only the "
    "conclusions and anything it should act on."
)


def new_work_brief(text: str, repository: str, label: str, chat_url: str = "") -> str:
    """The side chat's first message when it files new work from a reply (#3171).

    It must not ask anything: nobody is watching a side chat started this way, so a
    question would leave the work unfiled. Anything unclear goes on the issue instead.
    """
    filed_from = f"\n- End the body with: Filed from {chat_url}" if chat_url else ""
    return (
        "Start new work from here: file it as one GitHub issue, then stop.\n\n"
        f"The work, in my words:\n{text}\n\n"
        "- Write the issue from this conversation up to here, so an agent can start on it "
        "without this chat: a short title, then the problem, what was already decided, and "
        "what done looks like.\n"
        f"- File it with `gh issue create --repo {repository} --label {label}` and a title "
        "and body.\n"
        "- Ask me nothing and do not start the work. Anything unclear goes on the issue as "
        f"an open question.{filed_from}\n"
        "- Reply with the issue's link and nothing else."
    )


def _forkable(request: Request, leg_key: str) -> bool:
    try:
        account, session, provider = resolve_session(request, leg_key)
    except HTTPException:
        return False
    return provider.fork_refusal(account, session) is None


def _source(request: Request, leg_keys: tuple[str, ...]) -> dict[str, Any] | None:
    state = get_state(request)
    found = next(
        (source for key in leg_keys if (source := state.chat_fork_source(key)) is not None),
        None,
    )
    if found is None:
        return None
    source_key, seq = found
    source = state.sessions.get(source_key)
    source_legs = state.chat_session_keys(source_key)
    leg_index = source_legs.index(source_key) if source_key in source_legs else 0
    try:
        resolve_current_leg(request, source_key)
        reachable = True
    except HTTPException:
        reachable = False
    return {
        "title": source.display_title if source is not None else "the original chat",
        "url": (
            f"{state.chat_path_for_session(source_key)}"
            f"#event-{chat_event_id(leg_index, seq)}"
        ),
        "reachable": reachable,
        # Asked at the moment of sending, never baked in here: a side chat can stay
        # open while the original moves on to a new leg, and only that leg's
        # composer restores the draft.
        "target_url": f"/sessions/{source_key}/composer-target",
    }


def composer_target(request: Request, session_key: str) -> dict[str, str]:
    """Where text meant for ``session_key``'s chat waits to be sent, right now (#2485).

    The chat's working leg owns the composer, and the composer keeps unsent text
    under its own form path, so that is the key to write and the chat page the
    place to open.
    """
    _account, target, _provider = resolve_current_leg(request, session_key)
    return {
        "draft_key": f"agentdeck:message-draft:/sessions/{target.key}/inject",
        "url": get_state(request).chat_path_for_session(target.key),
    }


def side_chat_context(request: Request, session_key: str) -> dict[str, Any]:
    leg_keys = get_state(request).chat_session_keys(session_key)
    fork_leg_keys = [key for key in leg_keys if _forkable(request, key)]
    source = _source(request, leg_keys)
    # The box's own side chat (#4313). A page that is itself a side chat sends back through
    # its origin instead, so it gets no switch of its own.
    side_state = "none"
    if source is None:
        side_state = find_side_chat(request, session_key).state
    return {
        "fork_leg_keys": fork_leg_keys,
        "side_chat_source": source,
        "side_chat_summary_prompt": SUMMARY_PROMPT,
        "side_chat_box": source is None and (bool(fork_leg_keys) or side_state != "none"),
        "side_chat_state": side_state,
    }


# The chat's one side chat in its message box (#4313). The box's switch sends a message
# there instead of to the chat; the first one starts it as a copy of the chat cut at its
# newest finished turn, and every later one goes to that same side chat while it lasts.

SIDE = "side"
#: How long a side chat whose start this process no longer remembers still counts as
#: starting, so a second message cannot start a second one in the meantime.
_START_WAIT = timedelta(minutes=3)


@dataclass(frozen=True)
class SideChat:
    """Where the chat's side chat stands: ``none`` (send starts one), ``starting``, ``live``."""

    state: str
    fork: SideChatFork | None = None
    #: The side chat's working leg, once it is ``live``.
    session_key: str | None = None
    start: Any = None


def find_side_chat(request: Request, session_key: str) -> SideChat:
    """The side chat of ``session_key``'s chat, from its newest ``chat_forks`` mark.

    A side chat whose transcript is gone (no longer scanned) counts as none, so the next
    message starts a new one (decision 8).
    """
    state = get_state(request)
    forks = state.side_chat_forks(state.chat_session_keys(session_key))
    if not forks:
        return SideChat("none")
    fork = forks[0]
    start = get_injector(request).started_chat(fork.token)
    key = fork.session_key or state.session_key_for_token(fork.token)
    if key is None and start is not None:
        key = start.status.session_key
    if key:
        try:
            _account, leg, _provider = resolve_current_leg(request, key)
        except HTTPException:
            # Named, but not on this deck (yet): a start just answered is scanned in a
            # moment, while one whose chat has gone frees the place for a new one.
            if start is not None and start.status.state not in ("failed", "complete"):
                return SideChat("starting", fork, start=start)
            if start is not None and start.status.state == "complete" and _recent(fork):
                return SideChat("starting", fork, start=start)
            return SideChat("none", fork)
        return SideChat("live", fork, leg.key, start)
    if start is not None:
        if start.status.state == "failed" or start.status.state == "complete":
            return SideChat("none", fork, start=start)
        return SideChat("starting", fork, start=start)
    return SideChat("starting", fork) if _recent(fork) else SideChat("none", fork)


def _recent(fork: SideChatFork) -> bool:
    try:
        reserved = datetime.fromisoformat(fork.reserved_at)
    except ValueError:
        return False
    if reserved.tzinfo is None:
        reserved = reserved.replace(tzinfo=UTC)
    return datetime.now(UTC) - reserved < _START_WAIT


def moved_on(
    request: Request, fork: SideChatFork, main_key: str, newest: tuple[int, str] | None
) -> str:
    """The line a side message opens with when the chat has finished turns since (decision 9).

    The side chat keeps its own history; the line only says where to read what it missed.
    ``newest`` is the main chat's newest finished turn, ``(line, cut)``, as ``newest_fork``
    gives it.
    """
    if newest is None:
        return ""
    same_leg = fork.seen_session_key == main_key
    if same_leg and fork.seen_seq is not None and newest[0] <= fork.seen_seq:
        return ""
    after = ""
    if same_leg and fork.seen_seq:
        legs = get_state(request).chat_session_keys(main_key)
        index = legs.index(main_key) if main_key in legs else 0
        after = f" --after {chat_event_id(index, fork.seen_seq)}"
    return (
        "(From the deck: the main chat has moved on since you last heard from it. If you"
        " need its newer turns, read them with `~/agentdeck/.venv/bin/agentdeck chat-read"
        f" {main_key}{after}`.)\n\n"
    )


async def side_view_context(request: Request, session_key: str, side: SideChat) -> dict[str, Any]:
    """What ``partials/side_chat_view.html`` shows for ``session_key``'s side chat.

    Only the side chat's own talk: a Claude side chat's file starts with the history it
    was copied from, which the main chat already shows, so those lines are left out. The
    live tail is the side chat's own stream from the newest line read here.
    """
    context: dict[str, Any] = {
        "main_key": session_key,
        "side_state": side.state,
        "side_key": side.session_key,
        "rendered": "",
        "after_seq": "0",
        "own_events": 0,
        "side_url": "",
        "start_message": "",
        "side_display_state": "",
    }
    if side.state == "starting":
        start = side.start
        context["start_message"] = display_message(start.message) if start is not None else ""
        return context
    if side.state != "live" or side.session_key is None or side.fork is None:
        return context
    state = get_state(request)
    accounts = get_accounts(request)
    shown = state.sessions.get(side.session_key)
    context["side_display_state"] = shown.display_state if shown is not None else ""
    transcript = await load_chat_transcript(
        chat_keys=state.chat_session_keys(side.session_key),
        current_session_key=side.session_key,
        sessions=state.sessions,
        accounts_by_key={item.key: item for item in accounts},
        providers=PROVIDERS,
        account_labels=session_labels(accounts),
        window=DETAIL_WINDOW,
        rollover_reasons=state.chat_rollover_reasons(side.session_key),
    )
    legs = []
    for leg in transcript.legs:
        events = leg.detail.events
        if leg.index == 0 and side.fork.cut_ref:
            provider = PROVIDERS[leg.account.provider_id]
            copied = await asyncio.to_thread(
                provider.fork_copy_end, leg.account, leg.session, side.fork.cut_ref
            )
            events = [event for event in events if event.seq > copied]
        legs.append(replace(leg, detail=replace(leg.detail, events=events)))
    templates = get_templates(request)
    store = getattr(request.app.state, "control_db", None)
    chat_id = state.chat_identity_key(side.session_key)
    injector = request.app.state.injector
    context.update(
        # The current leg is linked to the side sends it delivers, as the chat's own page
        # does (only it has a live queue, #1265), so each pending card goes with its line.
        rendered="".join(
            render_chat_transcript_leg(
                templates,
                leg,
                observed_messages=(
                    injector.observe_transcript(leg.session_key, leg.detail.events)
                    if leg is legs[-1]
                    else None
                ),
                store=store,
                chat_id=chat_id,
            )
            for leg in legs
        ),
        after_seq=transcript.current.event_id(transcript.after_seq),
        own_events=sum(len(leg.detail.events) for leg in legs),
        side_url=state.chat_path_for_session(side.session_key),
    )
    return context
