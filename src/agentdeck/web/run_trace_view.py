"""The Work's Run trace section: presentation seam (#2141).

``load_run_trace`` gathers one Work's facts, moving the execution-outcome read to a
thread; ``render_run_trace`` renders the section. The Work page (#2147) gathers the facts
for itself, sharing one job read with the rest of the page, and renders with the latter.
Nothing here writes: execution outcomes are read from their table read-only, not through
the Runtime route that records a fresh observation on every read.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..run_trace import RunTrace, build_run_trace, gather_run_trace, read_outcomes
from ..work import WorkRef
from .work_workspace_view import _job_rows

RUN_TRACE_TEMPLATE = "partials/run_trace.html"


async def load_run_trace(app: Any, work: WorkRef) -> RunTrace | None:
    """This Work's Run trace, or None when no card, route, outcome, session or job names it."""
    issues = getattr(app.state, "issues", None)
    board = issues.read_board() if issues is not None else None
    jobs = await _job_rows(app, work)
    outcomes = await _outcomes(app, work)
    # On the event loop, like the Workspace loader: it reads the in-memory session state.
    facts = gather_run_trace(app.state.app_state, work, board=board, jobs=jobs, outcomes=outcomes)
    if not facts.known:
        return None
    return build_run_trace(facts)


async def _outcomes(app: Any, work: WorkRef):
    config = getattr(app.state, "config", None)
    db_path = getattr(getattr(config, "history", None), "db_path", None)
    if db_path is None:
        return []
    return await asyncio.to_thread(read_outcomes, db_path, work.work_id)


def render_run_trace(templates: Any, trace: RunTrace, *, part: str = "") -> str:
    """The compact Run section: the current Run first, earlier Runs collapsed.

    ``part`` is "current" or "history" for the Work page, which shows them apart (#2147).
    """
    return templates.get_template(RUN_TRACE_TEMPLATE).render(trace=trace, rt_part=part)
