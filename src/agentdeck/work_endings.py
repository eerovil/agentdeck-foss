"""A deck-run Work's turn that ends without a next step is asked about once (#2960).

A Work the deck runs itself rests in one of a few states, each with an owner: a question in
the question box (the person), a wake-on or a test run (an event), a ready pull request (the
reviewer, which the deck starts by itself), a reviewed and tested one (the person's Merge), or
a closed issue. A turn that ends in none of them — the agent forgot to close the issue, asked
in plain text, or only reported — used to rest as "Stopped" in Needs answer, where it asked a
person for nothing in particular. That is the agent ending its turn wrongly, never a state.

So the deck asks once: one message to the Work's chat, naming the ways to end a turn. If the
answering turn ends the same way, or nothing starts within ``work_derived.NUDGE_GRACE``, the
Work goes to Needs retry and is reported as a bug. Every ending is asked about, an old one
too: the owner wants the deck to act on those rather than list them (#2960, part 2).

The ledger holds, per Work, when the deck asked and for which turn end. A Work that rests in
a proper state again is forgotten, so its next wrong ending is asked about afresh. The message
is idempotent by its id (``work-ending:<work id>:<turn end>``), and the ledger is written
before the message is scheduled, so a crash in between loses the nudge rather than repeating
it; the Work then reads Needs retry after the grace, never asks twice.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

STORE_FILENAME = "work-endings.json"

NUDGE_TEXT = (
    "AgentDeck: your last turn on {ref} ended without a next step, so the Work has nothing to"
    " wait on. End every turn in one of these ways:\n"
    "- you need a person's decision: ask it in the question box (your structured question"
    " tool), not in the text of your reply;\n"
    "- you wait on CI, a merge or a deploy: register a wake-on for it;\n"
    "- you wait on long local work (a render, a long build): run it as an `agentdeck job`,"
    " which wakes you when it ends, or register a time wait (a wake-on with"
    ' `{{"at": "11:30", "waiting_for": "the render"}}` or `after_minutes`);\n'
    "- your pull request is done: mark it ready (`gh pr ready`); the deck reviews it by itself;\n"
    "- a person settled the review's findings without a code change: ask for a fresh review"
    " (`POST $AGENTDECK_URL/api/sessions/<key>/review-request`), which also waits on its"
    " verdict;\n"
    "- its review passed: run this Work's verification job on its head, as a test run the deck"
    " can see, unless your first message says CI or the review alone proves that repository"
    " (for CI, register a wake-on for its checks; for the review, nothing is left to run);"
    " the deck merges once it is green;\n"
    "- the deck told you your pull request merged: check it live as that message says, and"
    " record the result (`PUT $AGENTDECK_URL/api/sessions/<key>/live-check`);\n"
    "- the work is done and nothing is left: close the issue;\n"
    "- a person asked you to leave it open until they write: add the `parked` label to the"
    " issue (`gh label create parked` first if the repository has none); the Work then waits"
    " for their message.\n"
    "Do whichever applies now. If the work is not done, carry on with it."
)

#: The request when the one ending that applies is known (#2999): the release holds the Work
#: only for its tests, so its review passed and nothing proves the head green.
REVIEW_PASSED_NUDGE_TEXT = (
    "AgentDeck: your last turn on {ref} ended without a next step, but its review passed and"
    " no verification job has proven its head green. Run this Work's verification job on that"
    " head now, as a test run the deck can see; the deck merges once it is green."
)

# A review that stopped with no verdict after the deck's own reviewer also ran (#3356). Sent
# once per review chat; nothing else starts a reviewer on it by itself.
REVIEW_STUCK_TEXT = (
    "AgentDeck: the review of PR #{pr} on {ref} stopped with no verdict, and the deck's own"
    " reviewer did not finish it either (review chat {chat}). {reasons}The Work page shows it"
    " as Needs retry, and nothing starts another reviewer by itself. If the reason is"
    " something you can fix (the pull request, its description, the issue), fix it, push and"
    " mark the pull request ready again. Otherwise ask for one fresh review of the same commit"
    " yourself, without asking the owner (`POST $AGENTDECK_URL/api/sessions/<key>/review-request`"
    ' with `"no_verdict": true` and a note naming what went wrong); ask the owner in your'
    " question box only if that request is refused."
)


def review_stuck_id(work_id: str, chat: int) -> str:
    return f"review-stuck:{work_id}:{chat}"


def review_stuck_reasons(reasons: Iterable[str]) -> str:
    """The reviewers' own reasons as one sentence for ``REVIEW_STUCK_TEXT``, or ``""``."""
    given = [why for why in reasons if why]
    if not given:
        return "No reviewer said why. "
    return "The reviewers said: " + "; ".join(f"\"{why}\"" for why in given) + ". "


class EndingLedger:
    """When the deck asked each Work for its next step, in one small JSON file."""

    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path).expanduser()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._data: dict | None = None
        # The overview pass and the Work page read it from worker threads.
        self._lock = threading.RLock()

    def _load(self) -> dict:
        if self._data is None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raw = {}
            except (OSError, ValueError):
                log.warning("work-endings: ignoring unreadable %s", self.path)
                raw = {}
            works = raw.get("works") if isinstance(raw, dict) else None
            reviews = raw.get("reviews") if isinstance(raw, dict) else None
            self._data = {
                "works": works if isinstance(works, dict) else {},
                "reviews": reviews if isinstance(reviews, dict) else {},
            }
        return self._data

    def _save(self) -> None:
        data = self._load()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".work-endings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(temp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise

    def get(self, work_id: str) -> tuple[datetime | None, datetime | None]:
        with self._lock:
            return self._get(work_id)

    def _get(self, work_id: str) -> tuple[datetime | None, datetime | None]:
        """``(asked at, for the turn that ended at)``, or ``(None, None)``."""
        entry = self._load()["works"].get(work_id)
        if not isinstance(entry, dict):
            return None, None
        return _parse(entry.get("nudged_at")), _parse(entry.get("for"))

    def record(self, work_id: str, ended_at: datetime) -> datetime:
        with self._lock:
            return self._record(work_id, ended_at)

    def _record(self, work_id: str, ended_at: datetime) -> datetime:
        asked, answered = self._get(work_id)
        if asked is not None and answered == ended_at:
            return asked  # already asked about this turn end: the first time stands
        now = self._clock()
        self._load()["works"][work_id] = {"nudged_at": now.isoformat(), "for": ended_at.isoformat()}
        self._save()
        return now

    def forget(self, work_ids: Iterable[str]) -> None:
        with self._lock:
            self._forget(list(work_ids))

    def _forget(self, work_ids: list[str]) -> None:
        works = self._load()["works"]
        gone = [work_id for work_id in work_ids if work_id in works]
        for work_id in gone:
            del works[work_id]
        if gone:
            self._save()


    def review_started(self, chat: int) -> bool:
        """Did the deck already start a reviewer on this quiet review chat (#2960)?"""
        with self._lock:
            return str(chat) in self._load()["reviews"]

    def record_review(self, chat: int) -> None:
        with self._lock:
            reviews = self._load()["reviews"]
            reviews[str(chat)] = self._clock().isoformat()
            # Review chats are numbered upwards; the newest few hundred are plenty.
            for old in sorted(reviews, key=int)[:-500]:
                del reviews[old]
            self._save()


    def forget_review(self, chat: int) -> None:
        with self._lock:
            if self._load()["reviews"].pop(str(chat), None) is not None:
                self._save()


def message_id(work_id: str, ended_at: datetime) -> str:
    return f"work-ending:{work_id}:{ended_at.isoformat()}"


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)
