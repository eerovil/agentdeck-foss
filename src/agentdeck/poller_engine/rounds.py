"""How many rounds of work a worker has handed back on one card (#1840)."""

from __future__ import annotations

__all__ = ["count_round"]

# How many counted generations to remember. A card takes a handful of rounds; the list exists
# only so the count survives being observed twice, not as a history of the card.
_COUNTED_ROUNDS_KEPT = 32


def count_round(rec: dict, generation: str) -> bool:
    """Count one worker terminal report, once, whoever observes it first (#1840).

    A round is what the worker hands back, and it is not always a commit: answering a review's
    blocker with evidence, or declining a finding with a reason, ends a round while leaving the
    pull request head untouched. Anything watching heads to decide whether to look again is
    blind to exactly those rounds, so this count is the edge it reads instead.

    The identity is the **generation whose terminal marker landed**, not the act of writing the
    board. Those are different things, and the difference is a whole lost round: the marker can
    land, the board write fail, reconciliation finish the card from the marker, and the worker's
    retry take the idempotent repeat path — three observers of one durable report, none of them
    the board write that counting used to hang off. Each of them calls this, the first one wins,
    and the rest are free. A comment that never landed is not a report and never gets here.

    Monotonic and never reset, because a consumer compares it against what it last acted on
    rather than against zero. Returns whether this call was the one that counted.
    """
    if not isinstance(rec, dict) or not generation:
        return False
    raw = rec.get("counted_rounds")
    counted = [item for item in raw if isinstance(item, str)] if isinstance(raw, list) else []
    if generation in counted:
        return False
    counted.append(generation)
    rec["counted_rounds"] = counted[-_COUNTED_ROUNDS_KEPT:]
    rec["worker_rounds"] = int(rec.get("worker_rounds") or 0) + 1
    return True
