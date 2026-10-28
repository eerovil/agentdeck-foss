"""Report a Work that stopped on an AgentDeck bug to AgentDeck's own Poller (#2888).

``work_attention`` marks a Needs retry Work whose cause is AgentDeck's rather than the work's
(``OperatorAttention.bug``). Each overview pass hands its rows here; every such Work that has
stayed in Needs retry for ``SETTLE`` gets one request file in the folder of the Poller
instance that manages AgentDeck's issues (``poller_engine.bug_reports``), which files it.

Only a whole pass reports: a source that failed to read can put a Work in the wrong group, and
a Work that is really stuck is still there on the next whole pass. Writing is idempotent by
Work and cause, so the pass may offer the same Work every few seconds.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from .poller_engine import bug_reports
from .public_origin import public_url
from .work_attention import NEEDS_RETRY
from .work_status_log import why

log = logging.getLogger(__name__)

# The repository AgentDeck's own bugs are filed in.
HOME = "eerovil/agentdeck"
# How long a Work has to stay in Needs retry before it is reported: longer than a flap.
SETTLE = timedelta(minutes=2)


def track_entries(
    points: Mapping[str, Any] | None, entered: dict[str, datetime]
) -> dict[str, datetime] | None:
    """When each Work entered Needs retry, kept in ``entered`` from pass to pass (#3553).

    ``points`` is the status log's newest point per Work. A point's time is its last change
    of any kind, and a change of kind or tone inside Needs retry must not restart the wait,
    so only a move into Needs retry sets the time. A Work first seen already in it (a fresh
    process) counts from its newest point, which is never earlier than the real entry.
    None without a status log: ``due`` then waits on the chat's stop alone.
    """
    if points is None:
        return None
    for work_id in [work_id for work_id in entered if work_id not in points]:
        entered.pop(work_id)
    for work_id, point in points.items():
        if point.category != NEEDS_RETRY:
            entered.pop(work_id, None)
        elif point.prev_category != NEEDS_RETRY:
            entered[work_id] = point.at
        else:
            entered.setdefault(work_id, point.at)
    return entered


def due(
    rows: Sequence[Any], *, now: datetime, since: Mapping[str, datetime] | None = None
) -> list[Any]:
    """The rows to report: a bug cause, in Needs retry since at least ``SETTLE`` ago.

    ``since`` holds when each Work entered Needs retry (``track_entries``): when given, the
    Work must have been in it ``SETTLE``, not only stopped that long ago. A chat that stopped
    days ago and flaps into Needs retry for one pass is not reported (#3553).
    """
    found = []
    for row in rows:
        attention = getattr(row, "attention", None)
        if attention is None or attention.category != NEEDS_RETRY or not attention.bug:
            continue
        stopped = getattr(row, "stopped_at", None)
        if stopped is None or now - stopped < SETTLE:
            continue
        if since is not None:
            entered = since.get(row.card.work_id)
            if entered is None or now - entered < SETTLE:
                continue
        found.append(row)
    return found


def _instance(board: Any) -> tuple[str, str] | None:
    """``(state path, repository id)`` of the instance managing ``HOME``, or None."""
    managing = getattr(board, "managing", None)
    scopes = managing(HOME) if managing is not None else ()
    if not scopes:
        return None
    scope = scopes[0]
    owner, _, name = HOME.partition("/")
    # A manifest names its repositories by id, which is the bare name inside its own org.
    repo = name if (scope.org or "").lower() == owner.lower() else HOME
    return scope.state_path, repo


def _facts(card: Any, status: Any) -> dict:
    """The status log's facts for the issue, without any chat's words (#2888).

    A filed issue is read outside this host; a chat's question can hold anything, so only
    the facts about it are kept.
    """
    found = why(card, status)
    found["chats"] = [
        {key: value for key, value in chat.items() if key != "question"}
        for chat in found.get("chats", ())
    ]
    review = found.get("review")
    if isinstance(review, dict):
        review.pop("label", None)
    return found


def report(
    rows: Sequence[Any],
    board: Any,
    *,
    now: datetime,
    degraded: Sequence[str] = (),
    revision: str | None = None,
    sent: set[str] | None = None,
    since: Mapping[str, datetime] | None = None,
) -> int:
    """Leave a bug report for every row ``due`` finds; return how many were written.

    ``sent`` holds the request ids this process already wrote, so a Work that stays stuck is
    not written again on every pass after the Poller took its file; a restart writes it once
    more, and the Poller discards a report it has already taken.
    """
    if degraded:
        return 0
    rows = due(rows, now=now, since=since)
    if not rows:
        return 0
    target = _instance(board)
    if target is None:
        return 0
    state_path, repo = target
    written = 0
    for row in rows:
        card = row.card
        ident = bug_reports.request_id(card.work_id, row.attention.bug, "")
        if sent is not None and ident in sent:
            continue
        chats = getattr(card, "attention", ())
        try:
            path = bug_reports.write(
                state_path,
                repo=repo,
                work_id=card.work_id,
                cause=row.attention.bug,
                reason=row.attention.reason or row.reason,
                work_url=public_url(card.href) or card.source_url,
                session_key=chats[0].session_key if chats else "",
                why=_facts(card, row.status),
                revision=revision or "",
                now=now,
            )
        except OSError as exc:
            log.warning("bug report for %s not written: %s", card.work_id, exc)
            continue
        if path is not None:
            written += 1
            if sent is not None:
                sent.add(ident)
    return written
