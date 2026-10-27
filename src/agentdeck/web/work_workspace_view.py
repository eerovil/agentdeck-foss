"""The Work Workspace section's presentation seam (#2143).

``load_work_workspace`` gathers the facts on the event loop and moves the bounded git reads
to a thread; ``render_work_workspace`` renders the section, which the Work page (#2147) shows.
Neither changes anything: no worktree is created, repaired or reassigned here, and the
Work's workspace claims (#2149) are only read.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..work import WorkRef
from ..work_workspace import WorkWorkspace, gather_work_workspace
from ..work_workspace_claims import WorkspaceClaim, read_claims

WORK_WORKSPACE_TEMPLATE = "partials/work_workspace.html"


async def load_work_workspace(app: Any, work: WorkRef) -> WorkWorkspace | None:
    """This Work's workspace, or None when no chat, Run or poller card names it."""
    assistant = getattr(app.state, "assistant", None)
    issues = getattr(app.state, "issues", None)
    facts = gather_work_workspace(
        app.state.app_state,
        work,
        board=issues.read_board() if issues is not None else None,
        git_contexts=assistant.contexts if assistant is not None else None,
        jobs=await _job_rows(app, work),
        claims=await _claims(app, work),
    )
    if not facts.known:
        return None
    return await asyncio.to_thread(facts.build)


async def _claims(app: Any, work: WorkRef) -> list[WorkspaceClaim] | None:
    """This Work's workspace claims (#2149), read-only; None when the store was unreadable."""
    config = getattr(app.state, "config", None)
    history = getattr(config, "history", None)
    db_path = getattr(history, "db_path", None)
    if db_path is None:
        return []
    return await asyncio.to_thread(read_claims, db_path, work.work_id)


async def _job_rows(app: Any, work: WorkRef) -> list[dict]:
    """This Work's verification jobs: the durable evidence plus whatever is still queued (#2148).

    The queue forgets a finished row a day later, so finished jobs come from the runtime's
    evidence record and live ones from the queue; a job in both is the queue's row. An
    unreachable runtime shows no verification rather than failing the section -- absence here
    is never presented as verified, only as nothing to show.
    """
    client: httpx.AsyncClient | None = getattr(app.state, "runtime_http", None)
    if client is None:
        return []
    rows: dict[str, dict] = {}
    for path, params, key in (
        ("/job-evidence", {"work_id": work.work_id}, "evidence"),
        ("/jobs", None, "jobs"),
    ):
        try:
            response = await client.get(path, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            continue
        found = payload.get(key) if isinstance(payload, dict) else None
        for row in found if isinstance(found, list) else ():
            if isinstance(row, dict) and row.get("id"):
                rows[str(row["id"])] = row
    return list(rows.values())


def render_work_workspace(
    templates: Any, workspace: WorkWorkspace, *, show_verifications: bool = True
) -> str:
    """The compact Workspace section, one stacked entry per repository."""
    return templates.get_template(WORK_WORKSPACE_TEMPLATE).render(
        workspace=workspace, show_verifications=show_verifications
    )
