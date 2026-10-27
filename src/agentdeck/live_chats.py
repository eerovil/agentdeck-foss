"""Loading a chat the live window let go, when someone opens it (#2841).

``live_window.LiveWindow`` keeps only recent chats, and those still in use, in the web
process. Every chat and session route reads its chat from memory and answers "unknown"
otherwise, so a request that names a chat the deck knows but has let go first marks it
opened and reads that chat's own transcripts (``Collector.load_sessions``), and the route
finds it. Its account is not listed: the next full scan picks the chat up as opened.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote

from .work import WorkEvidence, WorkRef

log = logging.getLogger(__name__)

# The routes that address one chat: its page, transcript, pins, partials, stream and
# controls. A key that names no chat the deck knows matches nothing and costs nothing.
_SESSION_PATH = re.compile(r"^/(?:api/|partials/|events/)?sessions/([^/]+)")
_CHAT_PATH = re.compile(r"^/chats/([^/]+)")
# A Work's page and its controls (#3229): a finished Work's chats are let go, and its page
# then had no chat to show, open or message. The path arrives decoded, ``/`` and ``#`` too.
_WORK_PATH = re.compile(r"^/(?:partials/)?works/(github_issue:[^/#]+/[^/#]+#\d+)(?:/|$)")
# A chat that could not be found by a scan is not scanned for again this soon, so a
# page that keeps asking for it cannot keep the deck scanning.
RETRY_AFTER_S = 30.0
# The Work bindings that make a chat a member; an ``ambiguous`` one joins nothing.
_BOUND = frozenset(
    {WorkEvidence.DELEGATED.value, WorkEvidence.BACKFILLED.value, WorkEvidence.SIDE_CHAT.value}
)
_failed: dict[tuple[str, ...], float] = {}


def chat_legs(state: Any, path: str) -> tuple[tuple[str, ...], bool]:
    """The legs of the chat ``path`` names, and whether the deck must load it first.

    A Work's path names every chat of the Work, and loads the ones the deck holds none of.
    Empty when the window is off, the path names no chat, or the deck does not know the
    chat. Loading is due when the deck holds none of its legs and did not just fail to.
    """
    window = getattr(state, "live_window", None)
    if window is None or not window.enabled:
        return (), False
    work = _WORK_PATH.match(path)
    if work is not None:
        chats = work_chats(state, work.group(1))
        # The chats of the Work the deck holds none of are loaded; the others kept live.
        # A chat that just failed to load is not asked for again this soon (``load``).
        missing = tuple(leg for legs in chats if _due(state, legs)[1] for leg in legs)
        if missing:
            return missing, True
        return tuple(leg for legs in chats for leg in legs), False
    match = _SESSION_PATH.match(path)
    if match is not None:
        key = unquote(match.group(1))
        chat = state.chat_for_session(key)
        if chat is None:
            if key in state.sessions:
                return (key,), False
            # The session route answers a chat id too (#3240): a Work row whose chat
            # has no leg in memory links one, and it 404'd unloaded (#3293).
            chat = state.chat_by_id(key)
            if chat is None:
                return (), False
    else:
        match = _CHAT_PATH.match(path)
        if match is None:
            return (), False
        chat = state.chat_by_id(unquote(match.group(1)))
        if chat is None:
            return (), False
    return _due(state, tuple(chat.legs))


def work_chats(state: Any, work_id: str) -> tuple[tuple[str, ...], ...]:
    """The legs of each chat the Work's saved membership evidence names (``work.py``): an
    issue registration, a Run of the Work, or a delegated or side-chat Work binding.

    A provider-inferred ``Session.issue_url`` is the one kind left out: it is read from a
    transcript, so a chat the deck does not hold has none to read.
    """
    ref = WorkRef.from_work_id(work_id)
    if ref is None:
        return ()
    keys = {
        key
        for key, refs in list(state.session_issues.items())
        if any(WorkRef.from_issue(issue) == ref for issue in refs)
    }
    db = getattr(state, "db", None)
    runs = getattr(db, "session_run_work_ids", None)
    keys.update(key for key, run_work in (runs() if runs else ()) if run_work == ref.work_id)
    bindings = getattr(db, "session_work_bindings", None)
    keys.update(
        row.session_key
        for row in (bindings() if bindings else ())
        if row.work_id == ref.work_id and row.evidence in _BOUND
    )
    chats: dict[tuple[str, ...], None] = {}
    for key in sorted(keys):
        chats[tuple(state.chat_session_keys(key))] = None
    return tuple(chats)


def _due(state: Any, legs: tuple[str, ...]) -> tuple[tuple[str, ...], bool]:
    # Loaded only when the deck holds none of its legs. An older leg of a held chat is
    # a companion, which the scan that follows its publication lists; and a leg whose
    # transcript is gone must not cost a scan every time its chat is opened.
    if any(leg in state.sessions for leg in legs):
        return legs, False
    failed_at = _failed.get(legs)
    due = failed_at is None or time.monotonic() - failed_at >= RETRY_AFTER_S
    return legs, due


async def load(state: Any, collector: Any, legs: Iterable[str]) -> bool:
    """Mark these legs opened and read just their transcripts; whether one is now held."""
    legs = tuple(legs)
    state.live_window.request(legs)
    by_account: dict[str, set[str]] = {}
    for leg in legs:
        account_key, _, session_id = leg.rpartition(":")
        by_account.setdefault(account_key, set()).add(session_id)
    for account_key, session_ids in by_account.items():
        # Just this chat's own transcripts, not its whole account (D4).
        await collector.load_sessions(account_key, frozenset(session_ids))
    # Each chat of a Work's several is remembered by itself, so one whose transcript is
    # gone is not loaded again on every request while its Work's other chats are held.
    groups = {legs, *(tuple(state.chat_session_keys(leg)) for leg in legs)}
    for group in groups:
        if any(leg in state.sessions for leg in group):
            _failed.pop(group, None)
        else:
            _failed[group] = time.monotonic()
    found = any(leg in state.sessions for leg in legs)
    if not found:
        log.info("could not load chat %s on demand", legs[-1] if legs else "")
    return found
