"""What a chat page needs to offer side chats (#2485).

A side chat is a new chat started as a copy of another chat's history, cut after one
finished turn. A reply's button starts one to file new work from there (#3171): the
side chat writes the issue from the conversation so far and stops. The page needs two
things from the server: which of its legs can be cut (the per-reply button only
appears on those), and — when the page *is* a side chat — where it came from, so it
can link back and send a summary there.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from ..chat_transcript import chat_event_id
from .deps import get_state, resolve_current_leg, resolve_session

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
    return {
        "fork_leg_keys": [key for key in leg_keys if _forkable(request, key)],
        "side_chat_source": _source(request, leg_keys),
        "side_chat_summary_prompt": SUMMARY_PROMPT,
    }
