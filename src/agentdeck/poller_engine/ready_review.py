"""A live worker's head offered for review while it goes on to verify it (#2350).

Before this, a card's review and its final suite ran one after the other: the worker finished
*In review*, the outside reviewer read the head, and only a passed review queued the engine's
verify turn (#2253). On a project whose review comes from an outside reviewer, the worker now
says ``ready-for-review`` instead and keeps its turn. The reviewer reads that head while the
worker runs the suite on it, so the two gates overlap rather than queue.

The record kept here is the whole of what the engine knows about that offer:

* ``heads`` — the open pull requests' heads the worker offered, as ``repo#n -> sha``;
* ``generation`` — the lease that offered them, so a later worker never inherits the offer;
* ``rejected`` — set once the review of those heads came back not passing. From then on no
  verification job of this Run may run until the worker offers a new head, and the engine
  cancels any that does;
* ``cancelled`` / ``told`` — the job ids already cancelled and already explained to the worker,
  so a retried wake neither cancels nor explains one twice;
* ``offered`` — whether a full wake has shown the offer to the reviewer's extension yet. Until it
  has, the wake may not skip its live reads, because only a full wake launches a review.

``refused`` remembers every head set a review already rejected in this generation, so offering
the same unchanged code again is refused rather than re-reviewed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

__all__ = [
    "FINDINGS_MARKER",
    "clear",
    "current",
    "enabled",
    "fingerprint",
    "findings_head",
    "is_findings",
    "names_head",
    "needs_offer",
    "note_cancelled",
    "note_offered",
    "note_told",
    "observe_row",
    "offer",
    "reject",
    "rejected",
    "set_enabled",
]

_KEY = "ready_for_review"
# The wake's own reading of whether this project runs review beside verification.
_ENABLED = "ready_review_enabled"
# How many rejected head sets one generation remembers. A worker trading a handful of fix
# rounds with the reviewer is normal; one that has had this many is not waiting on this list.
_REFUSED_KEPT = 16

# The ChatGPT reviewer's handoff comments end with this marker (#2167); the heading is how the
# ones posted before the marker existed are recognised. The heading names the head it read.
FINDINGS_MARKER = "<!-- chatgpt-review -->"
_FINDINGS_HEADING_RE = re.compile(
    r"\AReview findings for PR #\d+(?:\s+at\s+`?(?P<head>[0-9a-fA-F]{7,40})`?)?"
)
_SHA_RE = re.compile(r"\A[0-9a-f]{7,40}\Z")


def set_enabled(state: dict, enabled: bool) -> None:
    """Record, once per wake, whether this project takes ``ready-for-review`` at all."""
    if enabled:
        state[_ENABLED] = True
    else:
        state.pop(_ENABLED, None)


def enabled(state: dict) -> bool:
    return bool(state.get(_ENABLED))


def fingerprint(heads: dict[str, str]) -> str:
    """One head set as a stable string: ``repo#1@sha,repo#2@sha``."""
    return ",".join(f"{ref}@{heads[ref]}" for ref in sorted(heads))


def current(rec: dict | None, *, generation: str = "") -> dict | None:
    """The offer this card holds, only if it is this generation's (when one is named)."""
    raw = rec.get(_KEY) if isinstance(rec, dict) else None
    if not isinstance(raw, dict):
        return None
    heads = raw.get("heads")
    if not isinstance(heads, dict) or not heads:
        return None
    if generation and str(raw.get("generation") or "") != generation:
        return None
    return raw


def rejected(rec: dict | None, *, generation: str = "") -> bool:
    ready = current(rec, generation=generation)
    return bool(ready and ready.get("rejected"))


def offer(rec: dict, *, heads: dict[str, str], generation: str, now: datetime) -> str:
    """Offer these heads for review. Returns ``"offered"``, ``"already"`` or ``"refused"``.

    ``refused`` is a head set a review already rejected in this generation: the worker has to
    push something new first. The same set offered again while it is still waiting is
    ``already``, and changes nothing.
    """
    heads = {str(ref): str(sha).lower() for ref, sha in heads.items()}
    wanted = fingerprint(heads)
    raw = rec.get(_KEY) if isinstance(rec.get(_KEY), dict) else {}
    same_generation = str(raw.get("generation") or "") == generation
    refused = [str(item) for item in raw.get("refused") or ()] if same_generation else []
    if wanted in refused:
        return "refused"
    unchanged = fingerprint(raw.get("heads") or {}) == wanted
    if same_generation and not raw.get("rejected") and unchanged:
        return "already"
    rec[_KEY] = {
        "heads": heads,
        "generation": generation,
        "at": now.astimezone(UTC).isoformat(),
        "refused": refused,
    }
    return "offered"


def reject(rec: dict, *, by: str, now: datetime) -> bool:
    """Mark the current offer rejected. Returns whether this call was the one that did."""
    ready = current(rec)
    if ready is None or ready.get("rejected"):
        return False
    ready["rejected"] = True
    ready["rejected_by"] = by
    ready["rejected_at"] = now.astimezone(UTC).isoformat()
    refused = [str(item) for item in ready.get("refused") or ()]
    refused.append(fingerprint(ready["heads"]))
    ready["refused"] = refused[-_REFUSED_KEPT:]
    return True


def names_head(ready: dict, head: str) -> bool:
    """Whether ``head`` (full or abbreviated, any case) is one of the offered heads."""
    wanted = str(head or "").strip().lower()
    if not _SHA_RE.match(wanted):
        return False
    return any(str(sha).lower().startswith(wanted) for sha in (ready.get("heads") or {}).values())


def is_findings(instruction: object) -> bool:
    """Whether a ``/claude`` instruction is the outside reviewer's findings handoff."""
    text = str(instruction or "")
    return FINDINGS_MARKER in text or bool(_FINDINGS_HEADING_RE.match(text.lstrip()))


def findings_head(instruction: object) -> str:
    """The head a findings handoff says it read, or ``""`` when it does not say."""
    match = _FINDINGS_HEADING_RE.match(str(instruction or "").lstrip())
    return (match.group("head") or "").lower() if match else ""


def note_cancelled(rec: dict, job_id: str) -> None:
    _note(rec, "cancelled", job_id)


def note_told(rec: dict, job_id: str) -> None:
    _note(rec, "told", job_id)


def _note(rec: dict, field: str, job_id: str) -> None:
    ready = current(rec)
    if ready is None or not job_id:
        return
    seen = [str(item) for item in ready.get(field) or ()]
    if job_id not in seen:
        seen.append(job_id)
    ready[field] = seen


def needs_offer(rec: dict) -> bool:
    """Whether an offer is still waiting for a full wake to show it to the reviewer."""
    ready = current(rec)
    return bool(ready and not ready.get("rejected") and not ready.get("offered"))


def note_offered(rec: dict) -> None:
    ready = current(rec)
    if ready is not None and not ready.get("rejected"):
        ready["offered"] = True


def clear(rec: dict) -> None:
    rec.pop(_KEY, None)


def observe_row(rec: dict) -> dict | None:
    """What the reviewer's extension may see of the offer on a held card.

    ``heads`` is what it may review now — empty once rejected, so nothing of the rejected code
    is reviewed again. ``reports`` is the round count the worker's report will bring when it
    finishes this turn: a review asked for now is then already the answer for that round, and
    finishing *In review* on the same head does not ask for the same review twice (#1840).
    """
    generation = str(rec.get("generation") or "")
    ready = current(rec, generation=generation) if generation else None
    heads = [] if ready is None or ready.get("rejected") else sorted(
        {str(sha).lower() for sha in ready["heads"].values()}
    )
    counted = rec.get("counted_rounds")
    already = isinstance(counted, list) and generation in counted
    reports = int(rec.get("worker_rounds") or 0) + (0 if already else 1)
    return {"heads": heads, "reports": reports}
