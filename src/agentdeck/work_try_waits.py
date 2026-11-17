"""Deployed work unblocks the issues it blocks (#4384).

A Work whose change is deployed and waits only for a person to try it (its live check says
``needs_person``, and the deck shows it as **Waiting for a try**, ``work_derived.TRY_WAIT``)
counts as done for the issues it blocks, and those start, while it stays in Waiting for a
try. Owner's choice in the Work's chat.

Two parts, two rows of ``work_try_waits``, one writer each:

- **The set** (:func:`deployed_set`, written by :meth:`TryWaits.keep`): the Work ids the
  overview pass shows as Waiting for a try, never one whose issue was reopened. Saved under
  the pass's own lock, so an older pass never writes over a newer one; a degraded pass only
  takes Works out. Every blocker check reads it (``work_signals.blocker_done``).
- **The notes** (:class:`TryWaits`'s task): for each Work in the newest complete set, once per
  live-check record (``<work_id>@<live_check_at>``), the router is asked to start the issues
  it blocks (``note_deployed``). Handed a set only after the row holding it is saved; a
  record is noted only when the router saw it in the row.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from .work import WorkRef

log = logging.getLogger(__name__)

#: ``work_derived.TRY_WAIT``, read off a card without importing the status rules.
TRY_WAIT = "try_wait"


def deployed_set(cards: Iterable[Any]) -> frozenset[str]:
    """The Works shown as Waiting for a try whose issue was never reopened (#4384).

    ``TRY_WAIT`` already needs ``needs_person``, an open issue, no open pull request, no live
    check asked after a newer merge and no pending question. A reopened issue is left out
    whatever its check says: the pass cannot tell a reopen before the check from one after.
    """
    return frozenset(
        card.work_id
        for card in cards
        if getattr(getattr(card, "derived", None), "kind", "") == TRY_WAIT
        and not getattr(card, "reopened", False)
    )


def note_key(work_id: str, check: Any) -> str:
    """One live-check record: a record made again (a new ``checked_at``) is sent again."""
    checked = getattr(check, "checked_at", None)
    return f"{work_id}@{checked.isoformat() if checked is not None else ''}"


class TryWaits:
    """The web's half: the saved set and the note task (#4384)."""

    def __init__(self, app: Any, lock: asyncio.Lock) -> None:
        self._app = app
        # The overview pass's lock: the set is kept only under it.
        self._lock = lock
        # The set as saved; ``None`` until first read from the row.
        self._saved: frozenset[str] | None = None
        # The newest complete set handed over, and a wake for the task.
        self._latest: frozenset[str] | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        # The noted keys, read once from their row by the task, its only writer.
        self._noted: set[str] | None = None
        # Whether a complete pass has saved its set and handed it over since this process
        # started: until then the overview owes a pass with nobody watching (#4384 review).
        self.swept = not hasattr(self._db(), "save_try_waits")

    def _db(self) -> Any:
        return getattr(self._app.state, "control_db", None)

    async def keep(self, cards: Iterable[Any], *, degraded: bool) -> None:
        """Save this pass's set, then hand it to the note task. Only under the pass's lock.

        A degraded pass only takes Works out (``previous ∩ now``), since a failed read could
        make a Work look ``TRY_WAIT``, and hands nothing over. A failed write keeps the old
        copy and hands nothing over: the next pass writes again.
        """
        if not self._lock.locked():
            raise RuntimeError("the deployed set is kept only under the overview's lock")
        db = self._db()
        if db is None or not hasattr(db, "save_try_waits"):
            self.swept = True
            return
        if self._saved is None:
            # ``None`` again when unreadable: never cached as an empty set, which a complete
            # empty pass would then not rewrite, leaving the old row's Works deployed.
            self._saved = await asyncio.to_thread(db.read_try_waits)
        previous = self._saved
        now = deployed_set(cards)
        if degraded:
            if previous is None:
                return  # nothing known to take out of; the next complete pass writes it
            now = now & previous
        if now != previous:
            if not await asyncio.to_thread(db.save_try_waits, now):
                log.warning("work try-waits: the deployed set was not saved; next pass")
                return
            self._saved = now
        if degraded:
            return
        self.swept = True
        self._latest = now
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="work-try-waits")

    async def _run(self) -> None:
        while self._wake.is_set():
            self._wake.clear()
            works = self._latest
            if works is None:
                continue
            try:
                await self._note(works)
            except Exception:  # noqa: BLE001 - the next pass hands the set over again
                log.exception("work try-waits: noting failed")

    async def _note(self, works: frozenset[str]) -> None:
        """Ask the router once per record in ``works``; drop keys of Works no longer in it."""
        db = self._db()
        router = getattr(self._app.state, "work_router", None)
        if db is None:
            return
        if self._noted is None:
            loaded = await asyncio.to_thread(db.load_try_waits_noted)
            self._noted = loaded if loaded is not None else set()
        noted = set(self._noted)
        kept = {key for key in noted if key.rpartition("@")[0] in works}
        enabled = router is not None and getattr(router, "enabled", False)
        for work_id in sorted(works):
            if self._wake.is_set():
                break  # a newer set came in: act on it, not on this one
            check = await asyncio.to_thread(db.work_live_check, work_id)
            key = note_key(work_id, check)
            # An older record's key goes: only the current record is remembered.
            kept = {item for item in kept if item == key or item.rpartition("@")[0] != work_id}
            ref = WorkRef.from_work_id(work_id)
            if key in kept or ref is None or not enabled:
                continue
            try:
                answer = await router.note_deployed(ref.repository, ref.number)
            except Exception as exc:  # noqa: BLE001 - un-noted: the next pass sends it again
                log.warning("work try-waits: %s not sent: %s", work_id, exc)
                continue
            if isinstance(answer, dict) and answer.get("in_set"):
                kept.add(key)
            else:
                log.info("work try-waits: %s not in the saved set yet; next pass", work_id)
        if kept != noted:
            if await asyncio.to_thread(db.save_try_waits_noted, kept):
                self._noted = kept
            else:
                log.warning("work try-waits: noted keys not saved; they may be sent again")
                self._noted = kept

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
