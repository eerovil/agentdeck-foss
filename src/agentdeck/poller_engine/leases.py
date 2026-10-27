"""Generation leases (#176 slice 3).

Every dispatch to a worker carries a ``generation`` — an opaque token minted for
that card. A worker must present the exact current generation before it may
comment, edit, push, or post a terminal result; minting a fresh generation
therefore *invalidates* any worker still holding the old one. This is how the
engine cancels in-flight work (stop) or hands a card to a new dispatch without
racing it. A retry of one unacknowledged spawn keeps its generation (#2824).

Pure, stdlib-only operations over one card's state record.
"""

from __future__ import annotations

import uuid


def new_generation() -> str:
    """Mint a fresh, unique generation token."""
    return uuid.uuid4().hex


def current(rec: dict) -> str:
    """The card's current generation, or '' if none is held."""
    return rec.get("generation") or ""


def bump(rec: dict) -> str:
    """Mint and install a new generation on the card, returning it (invalidates the prior lease)."""
    generation = new_generation()
    rec["generation"] = generation
    return generation


def invalidate(rec: dict) -> str:
    """Supersede the current lease with a fresh one so any in-flight worker is rejected."""
    return bump(rec)


def holds(rec: dict, generation: str) -> bool:
    """True iff ``generation`` is exactly the card's current lease."""
    return bool(generation) and rec.get("generation") == generation


def outgoing(rec: dict) -> str:
    """The lease an outstanding continuation successor took over from, or ''.

    A phase successor is minted the moment the engine decides to hand the card on, and that
    mint invalidates the predecessor immediately.  The delivery, though, is only *queued*
    here: the host can refuse it a whole wake later, and the commonest refusal is
    ``active_turn`` — the worker this successor continues is still running.  On #1798 that
    left a live worker's own control command dead for seventy seconds while it pushed its
    branch and tried to post its result, because it had been fenced out in favour of a
    successor nobody had received yet (#1801).

    A continuation is handed to the conversation already doing the work, so until its
    delivery lands there is no second worker for the fence to protect against.  This reports
    the predecessor generation for exactly as long as that successor dispatch is outstanding;
    acknowledging the dispatch drops the record, and the grace with it.
    """
    dispatch = rec.get("dispatch")
    if not isinstance(dispatch, dict) or not dispatch.get("continuation"):
        return ""
    return str(dispatch.get("supersedes") or "")


def holds_or_outgoing(rec: dict, generation: str) -> bool:
    """True for the current lease, or the one an undelivered continuation replaced."""
    return holds(rec, generation) or (bool(generation) and generation == outgoing(rec))
