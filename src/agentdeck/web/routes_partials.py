"""HTMX fragment routes. Each returns exactly what the SSE stream pushes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..chat_transcript import load_chat_transcript_before
from ..providers import PROVIDERS
from .deps import (
    get_accounts,
    get_state,
    get_templates,
    require_access,
    resolve_session,
)
from .render import (
    render_chat_transcript_leg,
    render_limit_bars,
    render_load_earlier,
    session_labels,
)

router = APIRouter(prefix="/partials", dependencies=[Depends(require_access)])


def _limit_bars(request: Request) -> HTMLResponse:
    return HTMLResponse(
        render_limit_bars(
            get_templates(request),
            get_accounts(request),
            get_state(request),
            request.app.state.auth_watch,
        )
    )



@router.get("/limit-bars", response_class=HTMLResponse)
async def limit_bars(request: Request) -> HTMLResponse:
    return _limit_bars(request)


@router.get("/sessions/{session_key}/transcript", response_class=HTMLResponse)
async def transcript_earlier(
    request: Request, session_key: str, before: str = "0"
) -> HTMLResponse:
    """ "Load earlier": the window ending just before ``before``.

    ``before`` is a chat-level event identity, so a step that exhausts one leg
    continues into the leg before it and the scroll stays continuous across the
    boundary — no gap, and no message rendered twice (#1265).
    """
    _, session, _ = resolve_session(request, session_key)
    state = get_state(request)
    templates = get_templates(request)
    accounts = get_accounts(request)
    leg, cursor = await load_chat_transcript_before(
        chat_keys=state.chat_session_keys(session.key),
        before=before or "0",
        current_session_key=session.key,
        sessions=state.sessions,
        accounts_by_key={item.key: item for item in accounts},
        providers=PROVIDERS,
        account_labels=session_labels(accounts),
        rollover_reasons=state.chat_rollover_reasons(session.key),
    )
    if leg is None:
        return HTMLResponse("")
    # Button first (stays at the top of the transcript), then the earlier window.
    html = render_load_earlier(templates, session.key, cursor)
    html += render_chat_transcript_leg(
        templates,
        leg,
        store=getattr(request.app.state, "control_db", None),
        chat_id=state.chat_identity_key(session.key),
    )
    return HTMLResponse(html)


@router.get("/sessions/{session_key}/diff", response_class=HTMLResponse)
async def worktree_diff(request: Request, session_key: str) -> HTMLResponse:
    """The chat's pending change, read on demand when its diff pane opens.

    Deliberately not a live fragment: reading a diff runs git in someone's
    checkout, so it happens when a person asks for it and not on every push.

    The chat's launch directory is read, and so is the worktree of each of its
    registered pull requests' branches; an empty one beside a changed one is
    dropped rather than shown as a blank section (#1120).

    The base branch is refreshed in the background, so this answers from the refs
    already on disk and comes back in milliseconds. When a refresh is genuinely
    in flight the fragment asks for itself once more with ``refresh=wait``, and
    that request is the one that waits for the network (#1133).
    """
    _account, session, _provider = resolve_session(request, session_key)
    context = request.app.state.assistant.session_context(session)
    pull = None
    if context is not None:
        pull = context.reviewable_pull or next(iter(context.pull_requests), None)
    checkouts: list[object] = [session.cwd]
    reader = request.app.state.worktree_diff
    wait = request.query_params.get("refresh") == "wait"
    diffs = await reader.read_many(
        checkouts,
        pull_request_base=pull.base_branch if pull else None,
        pull_request_repo=pull.repository if pull else None,
        await_refresh=wait,
    )
    # A chat that made its own worktree for its branch is not recorded there, so the
    # worktree each registered pull request's branch is checked out in is read too,
    # against that pull request's base (#2993).
    pulls = context.pull_requests if context is not None else ()
    # A pull request in a repository none of those checkouts is, as when the issue is on
    # one repository and the code in another, is looked for from a local clone, as the
    # Work page does (#3129, #3295).
    from ..work_start import pull_request_clones

    clones = await pull_request_clones(
        request.app, pulls, (diff.repository for diff in diffs if diff.repository)
    )
    for path, branch_pull in await reader.pull_request_checkouts(checkouts, pulls, clones=clones):
        diffs += (
            await reader.read(
                path,
                pull_request_base=branch_pull.base_branch,
                pull_request_repo=branch_pull.repository,
                await_refresh=wait,
            ),
        )
    shown = [diff for diff in diffs if not diff.is_empty] or diffs[:1]
    return get_templates(request).TemplateResponse(
        request,
        "partials/session_diff.html",
        {
            "session": session,
            "diffs": shown,
            "checkouts": len(diffs),
            "pull_request": pull,
            # Asked across every checkout, not just the shown ones: a refresh can
            # turn an empty diff into a section or the other way round.
            "refreshing": any(diff.refreshing for diff in diffs),
        },
    )


@router.get("/pollers", response_class=HTMLResponse)
async def poller_instances(request: Request) -> HTMLResponse:
    """The instance list as a fragment, refreshed on a deliberate action — never self-polling."""
    from .render import poller_context

    templates = get_templates(request)
    boards = await request.app.state.issues.resolve_instance_boards(force=True)
    return templates.TemplateResponse(
        request,
        "partials/poller_instances.html",
        poller_context(request, boards=boards),
    )
