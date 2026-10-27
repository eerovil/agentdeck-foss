"""The web process's read-only view of the runtime's heavy-command queue (#1301).

The queue itself lives in the persistent runtime (``jobs.py``), because that is
the process that outlives a web deploy and can write a result back into the chat
that submitted it. The web process therefore cannot read it out of memory: it
asks over the same Unix socket ``agentdeck job`` uses, and keeps the answer in
``AppState`` so the topbar pill can be rendered on a page load and re-pushed over
SSE without a socket read per request.

Only jobs that have not finished are carried. A finished row is still in the
runtime's durable state for a day so ``agentdeck job status`` can answer about
it, but the pill answers a different question — what is the host busy with right
now — and a finished job is not part of that answer.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

import httpx

log = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"

# How many rows of each kind the panel lists before it says "and N more". A
# fifty-deep queue is a real state (`jobs.MAX_QUEUED` allows it) and reading it
# as a phone-height wall of identical `pytest -q` lines helps nobody.
PANEL_ROWS = 6

# A command line can be arbitrarily long; the panel is 260px wide.
_DISPLAY_MAX = 120


def format_elapsed(seconds: int) -> str:
    """Render an age the way ``static/job_queue.js`` keeps rendering it.

    The two have to agree, or the label visibly jumps one second after the
    fragment lands.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


@dataclass(frozen=True)
class JobRow:
    """One unfinished job, as the topbar shows it."""

    id: str
    state: str
    display: str
    since: float
    session_key: str | None = None
    # Where the runtime writes the job's output; its test timings sit beside it.
    log_path: str = ""
    # Filled in at render time by `JobQueueView.resolved`, so that the stored
    # view compares equal across polls while chat titles come and go.
    chat_title: str | None = None
    chat_path: str | None = None
    # Its place among the waiting jobs, as the runtime will start them (#3116); None from a
    # runtime from before that, whose order was the submission order.
    place: int | None = None

    @property
    def elapsed_s(self) -> int:
        """Seconds this job has been in its current state, at render time.

        Deliberately not a stored field: it must not make two otherwise
        identical polls compare unequal and push a fragment every tick.
        """
        return max(0, int(time.time() - self.since))

    @property
    def elapsed_label(self) -> str:
        return format_elapsed(self.elapsed_s)

    @property
    def running(self) -> bool:
        return self.state == RUNNING


@dataclass(frozen=True)
class SlotHolderRow:
    """A heavy slot held by something that is not one of the queue's jobs.

    This is the row that explains "0 running, 4 waiting" (#1391). The queue's
    own jobs are already listed above it, so a slot the queue holds produces no
    row at all — only a direct ``pytest`` behind the conftest backstop, or an
    API lease, ends up here.
    """

    kind: str
    display: str
    detail: str
    since: float

    @property
    def elapsed_s(self) -> int:
        return max(0, int(time.time() - self.since))

    @property
    def elapsed_label(self) -> str:
        return format_elapsed(self.elapsed_s)


@dataclass(frozen=True)
class JobQueueView:
    """What the runtime's queue is doing, as of the last successful read."""

    running: tuple[JobRow, ...] = ()
    queued: tuple[JobRow, ...] = ()
    holders: tuple[SlotHolderRow, ...] = ()
    # How many waiting local jobs this host holds back for lack of memory (#3614).
    memory_waiting: int = 0

    @property
    def waiting_label(self) -> str:
        """What the waiting jobs are waiting for: a slot, or memory for the ones held here."""
        held = min(self.memory_waiting, self.queued_count)
        if not held:
            return f"{self.queued_count} waiting for a slot"
        if held == self.queued_count:
            return f"{self.queued_count} waiting for this host to free memory"
        return f"{self.queued_count} waiting, {held} of them for this host to free memory"

    @property
    def active(self) -> bool:
        return bool(self.running or self.queued)

    @property
    def running_count(self) -> int:
        return len(self.running)

    @property
    def queued_count(self) -> int:
        return len(self.queued)

    @property
    def active_rows(self) -> tuple[JobRow, ...]:
        """Every unfinished job, without the panel's display limit."""
        return self.running + self.queued

    @property
    def panel_rows(self) -> tuple[JobRow, ...]:
        """The rows the panel lists: running first, then the queue's own order."""
        return self.running[:PANEL_ROWS] + self.queued[:PANEL_ROWS]

    @property
    def hidden_count(self) -> int:
        """How many active jobs the panel left out, for its "and N more" line."""
        return max(0, self.running_count - PANEL_ROWS) + max(
            0, self.queued_count - PANEL_ROWS
        )

    @property
    def summary(self) -> str:
        """One sentence for the pill's tooltip and its accessible name."""
        parts = [f"{self.running_count} running"]
        if self.queued_count:
            parts.append(self.waiting_label)
        if self.holders:
            # Without this the "0 running, 4 waiting" case reads as a stalled
            # queue, which is exactly the wrong conclusion.
            held = "1 slot" if len(self.holders) == 1 else f"{len(self.holders)} slots"
            parts.append(f"{held} held outside the queue")
        return f"Job queue: {', '.join(parts)}"

    def resolved(self, chat: Callable[[str], tuple[str | None, str | None]]) -> JobQueueView:
        """Return this view with each row's originating chat filled in.

        ``chat`` maps a session key to ``(title, path)``. Resolution happens at
        render rather than at poll time: a chat gains a generated title minutes
        after the job starts, and that must not count as the queue changing.
        """

        def one(row: JobRow) -> JobRow:
            if not row.session_key:
                return row
            title, path = chat(row.session_key)
            return replace(row, chat_title=title, chat_path=path)

        return JobQueueView(
            running=tuple(one(row) for row in self.running),
            queued=tuple(one(row) for row in self.queued),
            holders=self.holders,
            memory_waiting=self.memory_waiting,
        )


EMPTY = JobQueueView()


def _display(raw: dict[str, Any]) -> str:
    label = str(raw.get("label") or "").strip()
    if not label:
        command = raw.get("command")
        parts = command if isinstance(command, list) else []
        label = " ".join(str(part) for part in parts).strip()
    label = label or "job"
    return label if len(label) <= _DISPLAY_MAX else label[: _DISPLAY_MAX - 1] + "…"


def _row(raw: dict[str, Any]) -> JobRow | None:
    """One wire row, or None if it is finished or not shaped like a job.

    The runtime keeps running across web deploys, so its payload can be older or
    newer than this code. An unreadable row is skipped, never fatal.
    """
    state = str(raw.get("state") or "")
    if state not in (QUEUED, RUNNING):
        return None
    job_id = str(raw.get("id") or "")
    if not job_id:
        return None
    stamp = raw.get("started_at") if state == RUNNING else raw.get("submitted_at")
    if not isinstance(stamp, (int, float)):
        stamp = raw.get("submitted_at")
    if not isinstance(stamp, (int, float)):
        return None
    return JobRow(
        id=job_id,
        state=state,
        display=_display(raw),
        since=float(stamp),
        session_key=str(raw["session_key"]) if raw.get("session_key") else None,
        log_path=str(raw.get("log_path") or ""),
        place=place if isinstance(place := raw.get("place"), int) else None,
    )


def _holder(raw: dict[str, Any]) -> SlotHolderRow | None:
    """One heavy slot, or None if the queue itself holds it or it is free.

    Like ``_row``, this reads a payload written by a runtime that can be older
    or newer than this code, so an unrecognised slot is skipped rather than
    guessed at — an unexplained blank row would be worse than the status quo.
    """
    kind = str(raw.get("kind") or "")
    if kind not in ("lease", "process"):
        return None
    since = raw.get("since")
    if not isinstance(since, (int, float)):
        return None
    if kind == "lease":
        display = str(raw.get("label") or "unlabelled")
        detail = ""
    else:
        display = str(raw.get("command") or "").strip() or f"pid {raw.get('pid')}"
        detail = str(raw.get("cwd") or "")
    if len(display) > _DISPLAY_MAX:
        display = display[: _DISPLAY_MAX - 1] + "…"
    return SlotHolderRow(kind=kind, display=display, detail=detail, since=float(since))


def _count(value: Any) -> int:
    """A non-negative count from the wire; anything else (an older runtime) is none."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def build_view(
    rows: Iterable[Any], slots: Iterable[Any] = (), *, memory_waiting: int = 0
) -> JobQueueView:
    """Turn the runtime's ``GET /jobs`` payload into what the topbar renders."""
    parsed = [_row(raw) for raw in rows if isinstance(raw, dict)]
    jobs = [row for row in parsed if row is not None]
    held = [_holder(raw) for raw in slots if isinstance(raw, dict)]
    return JobQueueView(
        # Longest-running first: that is the one holding the slot everything
        # else is waiting for.
        running=tuple(sorted((j for j in jobs if j.running), key=lambda j: j.since)),
        # Queued in the order the queue will start them.
        queued=tuple(
            sorted(
                (j for j in jobs if not j.running),
                key=lambda j: (j.place if j.place is not None else len(jobs), j.since),
            )
        ),
        holders=tuple(row for row in held if row is not None),
        memory_waiting=memory_waiting,
    )


async def read_job_queue(client: httpx.AsyncClient) -> JobQueueView | None:
    """Read the runtime's queue, preserving the old view on an uncertain read.

    A 404 means the queue is deliberately disabled and is therefore empty.
    Connection, server, and payload failures are unknown: callers retain their
    last successful view so a brief socket interruption cannot fabricate a job
    completion and a Deckhand stop edge.
    """
    try:
        # Only running and queued rows (#2864). A runtime from before that ignores the
        # parameter and sends the whole retained queue, which `build_view` still filters.
        response = await client.get("/jobs", params={"active": "1"})
        if response.status_code == 404:
            return EMPTY
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("job queue read failed: %s", exc)
        return None
    rows = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    # A runtime from before #1391 sends no slot section at all; the panel then
    # renders exactly as it used to rather than claiming every slot is free.
    slots = payload.get("heavy_slots")
    return build_view(
        rows,
        slots if isinstance(slots, list) else (),
        memory_waiting=_count(payload.get("memory_waiting")),
    )
