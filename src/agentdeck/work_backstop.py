"""Start the labelled Works a lost webhook delivery never started (#2951).

On a ``works_router`` project a Work starts only when the deck hears the issue's "label
added" delivery (``WorkRouterService._maybe_start``). A delivery lost after the relay's last
try (#2950), or while it restarts, leaves the issue labelled and nobody working it. A minute
after start and then every 15 minutes this looks at each switched repository's cached open
issues and starts any that carry a start label and have no chat, through the same
``start_work`` the router uses, which starts a Work's agent at most once.

Only an issue GitHub shows as updated in the last day is started: a lost label delivery
always leaves a fresh update time, and the owner chose to leave the old labelled backlog, which
nobody has touched in days, alone rather than start it all at the first pass after a deploy.

Left alone besides: a closed issue, a pull request, an issue carrying its project's paused
label, a Work paused on this deck, a Work a Poller card still holds, and a Work whose
deck start was already spent. A spec-only issue is started like any other, and its agent is
told to write the spec (#3134). The issue list is the shared cache the refresher keeps warm, so
a pass costs no GitHub call; a candidate's own record is re-read once before it is started,
so a stale list never starts an issue a person has since closed or held back. Each start is
logged as the backstop's, so a lost delivery stays visible.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime

from .issue_context import IssueBacklog
from .work import WorkRef

log = logging.getLogger(__name__)

INTERVAL_S = 15 * 60.0
#: How soon after start the first pass runs: a restart must not push it back a whole interval.
FIRST_PASS_S = 60.0
#: How recently an issue must have been updated for the backstop to start it.
RECENT_S = 24 * 3600.0


class MissedStartBackstop:
    """Every ``interval_s``, start each switched repository's labelled Work that has no chat.

    ``start_labels()`` and ``hold_labels()`` map a lowercased ``owner/repo`` to its lowercased
    labels. ``backlog()`` is the cached issue lists; ``reread(repository, number)`` refreshes
    one issue's record and ``record(repository, number)`` returns it (``kanban._ISSUE_JQ``'s
    shape). ``owned``, ``paused`` and ``spent`` say a Work needs no start from here.
    """

    def __init__(
        self,
        *,
        start_labels: Callable[[], dict[str, Iterable[str]]],
        hold_labels: Callable[[], dict[str, Iterable[str]]],
        backlog: Callable[[], IssueBacklog | None],
        reread: Callable[[str, int], Awaitable[object]],
        record: Callable[[str, int], dict | None],
        owned: Callable[[str, int], bool],
        paused: Callable[[str], bool],
        spent: Callable[[str], bool],
        start_work: Callable[[str, int], Awaitable[object]],
        enabled: bool = True,
        interval_s: float = INTERVAL_S,
        first_pass_s: float = FIRST_PASS_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._start_labels = start_labels
        self._hold_labels = hold_labels
        self._backlog = backlog
        self._reread = reread
        self._record = record
        self._owned = owned
        self._paused = paused
        self._spent = spent
        self._start_work = start_work
        self._enabled = bool(enabled)
        self._interval_s = interval_s
        self._first_pass_s = first_pass_s
        self._clock = clock
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="work-missed-start-backstop")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        # The first pass a minute after start, then one every interval (#2971): web deploys a
        # few minutes apart once kept every pass from running for most of an hour.
        delay = self._first_pass_s
        while True:
            await asyncio.sleep(delay)
            delay = self._interval_s
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("work backstop: pass failed")

    async def sweep(self) -> list[str]:
        """One pass; the Work ids it started."""
        start_labels = {
            repository.lower(): {label.lower() for label in labels}
            for repository, labels in self._start_labels().items()
        }
        if not start_labels:
            return []
        hold_labels = {
            repository.lower(): {label.lower() for label in labels}
            for repository, labels in self._hold_labels().items()
        }
        backlog = self._backlog()
        if backlog is None:
            return []
        now = self._clock()
        started: list[str] = []
        for entry in backlog.repositories:
            repository = entry.repository.lower()
            wanted = start_labels.get(repository)
            if not wanted:
                continue
            holds = hold_labels.get(repository, set())
            for issue in entry.issues:
                if (
                    not issue.is_open
                    or not self._recent(issue.updated_at, now)
                    or not self._labels_start(issue.labels, wanted, holds)
                ):
                    continue
                work_id = WorkRef(repository, issue.number).work_id
                if (
                    self._owned(repository, issue.number)
                    or self._paused(work_id)
                    or self._spent(work_id)
                ):
                    continue
                if await self._start(repository, issue.number, wanted, holds):
                    started.append(work_id)
        return started

    @staticmethod
    def _recent(updated_at: str | None, now: float) -> bool:
        try:
            when = datetime.fromisoformat(str(updated_at or "").replace("Z", "+00:00"))
        except ValueError:
            return False
        if when.tzinfo is None:
            return False
        return now - when.timestamp() <= RECENT_S

    @staticmethod
    def _labels_start(labels: Iterable[str], wanted: set[str], holds: set[str]) -> bool:
        names = {str(label).lower() for label in labels}
        return bool(names & wanted) and not names & holds

    async def _start(self, repository: str, number: int, wanted: set[str], holds: set[str]) -> bool:
        work_id = WorkRef(repository, number).work_id
        try:
            await self._reread(repository, number)
        except Exception:  # noqa: BLE001 - an unread record is not proof; try next pass
            log.warning("work backstop: could not re-read %s", work_id, exc_info=True)
            return False
        record = self._record(repository, number) or {}
        labels = record.get("labels")
        if (
            str(record.get("state") or "").lower() != "open"
            or record.get("is_pr")
            or not isinstance(labels, list)
            or not self._labels_start(labels, wanted, holds)
            or self._owned(repository, number)
        ):
            return False
        try:
            outcome = await self._start_work(repository, number, labels=labels)
        except Exception:  # noqa: BLE001 - one failed start must not stop the pass
            log.exception("work backstop: start of %s failed", work_id)
            return False
        log.warning(
            "work backstop: %s was labelled with no chat and no webhook delivery started it;"
            " started it from the 15-minute backstop: %s",
            work_id,
            outcome,
        )
        return outcome == "started"
