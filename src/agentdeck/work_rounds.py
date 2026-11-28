"""Rounds of a reopened Work (#4585).

It imports nothing of the deck's own: the release imports it, and ``tests/conftest.py`` imports
the release, so anything imported here sits on every test's path and makes every suite
selection the full suite (``scripts/select_suite.py``).

A long Work merges a pull request, is checked live, closes, and is reopened for its next
step, many times over. Each reopen, or a person's follow-up once the round was checked
live, starts a new **round**: the pull requests that had merged or closed by then go into
the round's ``earlier`` set (``work_rounds`` in the control database), and from then on the
merge release, the live check, the blockers and the Work page count only the others. The
old ones stay on the Work page under "Earlier rounds".

Two writers, one row per Work, joined as a union (``Db.start_work_round``): the Work router
on an ``issues`` ``reopened`` delivery, and the web on a Work-page message or New chat.
``started_at`` moves only when the set grows, so a repeat never ages the current round.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

#: Why a round started, as stored.
REOPENED = "reopened"
FOLLOW_UP = "follow-up"

#: The live-check results that end a round, so a person's next message starts the next one.
#: ``needs_person`` (Waiting for a try) and ``not_live`` are not among them: the person's
#: answer to a try belongs to the round it was asked in (#4585 design review).
ENDING_RESULTS = frozenset({"verified", "failed", "nothing_to_check"})

#: The release store keys about one round's merge, Merge press and live check (#4585). At a
#: new round each loses what it says about the earlier round's pull requests: a list or map
#: keyed by pull request loses those, and a flag or ask about none of the current ones goes.
#: Everything else in the entry (the message keys already sent, held and unrecorded verdicts,
#: the light-review answers, the waits ``_evaluate`` sets again) is per pull request or per
#: look, so it stays: dropping it could let an older pass merge.
RELEASE_ROUND_KEYS = frozenset(
    {
        "live_check_asked",
        "merged_now",
        "merged_pulls",
        "asked",
        "asked_cleared",
        "press_waits",
        "unlanded",
    }
)


def _ref(pull: str) -> tuple[str, int] | None:
    """``owner/repo#n`` as ``(owner/repo, n)``; ``None`` for anything else."""
    repository, _, number = pull.partition("#")
    if not repository or not number.isdigit():
        return None
    return repository.lower(), int(number)


def settled(
    pulls: Iterable[str],
    cached_pulls: Callable[[set], Mapping],
    state_of: Callable[[Any], str],
) -> list[str]:
    """The pull requests among ``pulls`` whose cached row ``state_of`` reads as merged or
    closed (``work_facts.pull_state``).

    One whose row is missing or whose state is unknown stays out, so a missed cache entry
    never hides an open pull request from the release (#4585 design D4)."""
    refs = {ref: pull.lower() for pull in pulls if (ref := _ref(pull)) is not None}
    if not refs:
        return []
    rows = cached_pulls(set(refs))
    found = []
    for ref, pull in refs.items():
        row = rows.get(ref)
        if row is not None and state_of(row) in {"merged", "closed"}:
            found.append(pull)
    return sorted(found)


def start(db: Any, work_id: str, earlier: Iterable[str], reason: str) -> bool:
    """Start a round of ``work_id`` with ``earlier`` settled; whether one started."""
    writer = getattr(db, "start_work_round", None)
    if writer is None:
        return False
    try:
        started = bool(writer(work_id, earlier, reason))
    except Exception:  # noqa: BLE001 - logged; no round is what a Work had before
        log.warning("work rounds: %s round not started", work_id, exc_info=True)
        return False
    if started:
        log.info("work rounds: %s started a new round (%s)", work_id, reason)
        # The new round has no pull requests yet: its published label comes off (#4794).
        from . import work_published

        work_published.request([work_id])
    return started


def load(db: Any, work_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """Each Work's round; ``{}`` when unread, which counts everything, as before rounds."""
    reader = getattr(db, "load_work_rounds", None)
    if reader is None:
        return {}
    try:
        return reader(work_ids)
    except Exception:  # noqa: BLE001 - unread is no round, never a held merge
        log.warning("work rounds: unread", exc_info=True)
        return {}


def current(pulls: Iterable[str], round_: Any) -> tuple[str, ...]:
    """``pulls`` without the round's earlier ones, in the order given."""
    earlier = getattr(round_, "earlier", None) or frozenset()
    return tuple(pull for pull in pulls if pull.lower() not in earlier)


def is_earlier(url: str, round_: Any) -> bool:
    """Whether the pull request at ``url`` (a URL or ``owner/repo#n``) is from an earlier
    round."""
    earlier = getattr(round_, "earlier", None)
    if not earlier:
        return False
    parts = url.rstrip("/").split("/")
    if len(parts) >= 4 and parts[-2] == "pull" and parts[-1].isdigit():
        return f"{parts[-4]}/{parts[-3]}#{parts[-1]}".lower() in earlier
    return url.lower() in earlier


def started_epoch(round_: Any) -> float | None:
    """When ``round_`` started, as epoch seconds; ``None`` for no round."""
    started = getattr(round_, "started_at", None)
    if not isinstance(started, datetime):
        return None
    return (started if started.tzinfo else started.replace(tzinfo=UTC)).timestamp()


def drop_release_keys(entry: dict, round_: Any) -> bool:
    """Drop what a release entry says about an earlier round's merge and live check (#4585).

    Whether anything changed. ``round_seen`` records the round the entry was cleared for, so
    each round clears it once, at the first read of the entry after the round started; what
    the release writes after that is the new round's."""
    started = started_epoch(round_)
    if started is None:
        return False
    seen = entry.get("round_seen")
    if isinstance(seen, (int, float)) and seen >= started:
        return False

    def earlier(pull: Any) -> bool:
        return isinstance(pull, str) and is_earlier(pull, round_)

    entry.pop("merged_now", None)
    merged = entry.get("merged_pulls")
    if isinstance(merged, list):
        entry["merged_pulls"] = [pull for pull in merged if not earlier(pull)]
    unlanded = entry.get("unlanded")
    if isinstance(unlanded, dict):
        entry["unlanded"] = {pull: head for pull, head in unlanded.items() if not earlier(pull)}
    live = entry.get("live_check_asked")
    if not isinstance(live, dict) or not any(not earlier(pull) for pull in live.get("pulls") or ()):
        entry.pop("live_check_asked", None)
    asked = entry.get("asked")
    pressed = asked.get("pulls", asked) if isinstance(asked, dict) else None  # pre-#3320 too
    if not isinstance(pressed, dict) or not any(not earlier(pull) for pull in pressed):
        for key in ("asked", "asked_cleared", "press_waits"):
            entry.pop(key, None)
    cleared = entry.get("asked_cleared")
    if isinstance(cleared, dict) and earlier(cleared.get("pull")):
        entry.pop("asked_cleared", None)
    for key in RELEASE_ROUND_KEYS:
        if key in entry and not entry[key]:
            del entry[key]
    entry["round_seen"] = started
    return True


def follow_up_due(
    outputs: Iterable[Any],
    *,
    asked_pending: bool,
    merged_now: bool,
    result: str | None,
    checked_at: datetime | None,
) -> bool:
    """Whether a person's message from the Work page starts a new round (#4585).

    Only once the current round is over: every pull request of it settled and one merged,
    the release following no merge (``merged_now``) and waiting on no live check, and a
    live-check result that ends a round (``ENDING_RESULTS``) recorded after the newest
    merge. A merge with no known time starts nothing. So "how's it going?" while the deploy
    or the check runs, or the answer to a Waiting-for-a-try, stays in the round it is about.
    """
    outputs = list(outputs)
    merged = [output for output in outputs if output.state == "merged"]
    if not merged or not all(output.settled for output in outputs):
        return False
    if asked_pending or merged_now or result not in ENDING_RESULTS or checked_at is None:
        return False
    times = [output.merged_at for output in merged]
    if any(when is None for when in times):
        return False
    return _aware(checked_at) > max(_aware(when) for when in times)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
