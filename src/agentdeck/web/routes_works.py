"""The canonical Work routes (#2147).

``/works/{work_id}`` is a Work's page. The id is the canonical ``github_issue:<owner/repo>#<n>``
(#2140); its ``/`` and ``#`` travel escaped. A plain issue spelling (``owner/repo#n``, an issue
URL, ``owner/repo/n``) is accepted only as a lookup and redirected to the canonical id, so
there is exactly one Work URL. ``/works`` lists the Active and Recent Works, and
``/works?view=archived`` pages through the rest, searchable and filterable by repository
(#2201). Archiving is presentation only: an Archived Work keeps its page and all its history.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import re
import shutil
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from markupsafe import Markup

from .. import wayfinder_map, work_actions, work_counter
from ..board_watch import BOARD_TOPIC
from ..models import IssueRef
from ..state import REVIEWS_TOPIC
from ..work import WorkRef
from ..work_actions import STAR, UNSTAR, WorkActionPress, finish_question
from ..work_attention import FINISH, HIDE
from ..work_claims import own_assignee, run_gh_write, start_issue
from ..work_hidden import UNHIDE
from ..work_overview import (
    WorkCandidate,
    archived_count,
    archived_works,
    bucket_works,
    work_chats,
    work_href,
    work_inventory,
    work_title,
)
from ..work_resolution import MAX_TITLE, normalize_title
from ..work_start import release_work_worktrees, start_action_id
from ..work_status_log import flaps
from ..work_story import DONE, waiting_attention
from ..work_workspace import parse_work_id
from .deps import (
    get_config,
    get_control_db,
    get_state,
    get_templates,
    require_access,
    resolve_current_leg,
)
from .work_view import (
    SECTION_INPUTS,
    WORK_INPUTS_TOPIC,
    WORK_SECTIONS,
    WORKS_TOPIC,
    RowRenders,
    StepTimes,
    WorkOverview,
    WorkOverviewService,
    _cached_issues,
    _work_candidates,
    forget_unsent,
    gather_pass,
    load_work_detail,
    mark_sent,
    needs_you_rows,
    note_work_opened,
    population_rows,
    render_changed_sections,
    render_work_sections,
    row_stub,
    runtime_queue,
    start_chat_title,
    tree_revision,
    work_checkouts,
)

#: Worktree releases in flight after a Finish, held so none is collected before it ends.
_RELEASING: set[asyncio.Task] = set()


def _release_worktrees(request: Request, work_id: str) -> None:
    """Give the finished Work's worktrees back, off the press (#3274)."""
    task = asyncio.create_task(release_work_worktrees(request.app, work_id))
    _RELEASING.add(task)
    task.add_done_callback(_RELEASING.discard)


router = APIRouter(dependencies=[Depends(require_access)])
# A Work page slower than this logs where its time went (#2654).
SLOW_WORK_PAGE_MS = 1000.0
logger = logging.getLogger(__name__)

# Archived Works per page. The archive grows for years; a page never does.
ARCHIVE_PAGE_SIZE = 50


def _resolve(work_id: str) -> tuple[WorkRef | None, bool]:
    """(the Work, whether the spelling was already canonical)."""
    ref = parse_work_id(work_id)
    if ref is not None:
        return ref, work_id == ref.work_id
    text = work_id.strip().strip("/")
    parts = text.split("/")
    if len(parts) == 3 and parts[2].isdigit():
        text = f"{parts[0]}/{parts[1]}#{parts[2]}"
    issue = IssueRef.parse(text)
    return (WorkRef.from_issue(issue), False) if issue is not None else (None, False)


@dataclass(frozen=True)
class WorkIndexRow:
    work_id: str
    href: str
    title: str
    source_label: str
    repository: str
    work_state: str
    chat_count: int
    last_activity: datetime | None


    @property
    def revision(self) -> str:
        """Changes exactly when anything the row shows does, so ``keyed-works`` keeps the rest."""
        return hashlib.sha1(repr(self).encode(), usedforsecurity=False).hexdigest()[:12]


def _row_gate(app, snapshot, item: WorkCandidate, record: dict | None) -> tuple:
    """What one ``/works`` row reads, cheaply: its candidate, its chats' change numbers, its
    titles and issue record (#3228). A row whose gate is unchanged is reused."""
    state = snapshot.state
    revision = getattr(state, "session_revision", None)
    memberships = item.work.memberships if item.work is not None else ()
    keys = tuple(
        leg
        for membership in memberships
        for leg in (snapshot.legs.get(membership.chat_id, ()) or (membership.chat_id,))
    )
    return (
        item,
        keys,
        tuple(revision(key) for key in keys) if revision is not None else object(),
        tuple(len(snapshot.projection.works_for_chat(m.chat_id)) for m in memberships),
        snapshot.titles.get(item.ref.work_id, ""),
        snapshot.agent_titles.get(item.ref.work_id, ""),
        start_chat_title(snapshot, item.ref.work_id),
        snapshot.backlog.issue(item.ref.repository, item.ref.number) if snapshot.backlog else None,
        repr(record),
    )


def _index_rows(
    snapshot, candidates: tuple[WorkCandidate, ...], *, app=None, records=None
) -> list[WorkIndexRow]:
    """One plain row per Work: no Run, Runtime or git read, so any number stays cheap.

    ``records`` are the Works' cached issue records, by Work id: a closed issue is in no
    open-issue list, so without its record the row fell back to a chat id for a title (#3257).

    With ``app``, a Work whose ``_row_gate`` is as it was keeps its last row, so a chat
    change rebuilds the rows of its own Works only (#3228).
    """
    memo = getattr(app.state, "works_index_rows", None) if app is not None else None
    if app is not None and memo is None:
        memo = app.state.works_index_rows = {}
    records = records or {}
    rows = []
    for item in candidates:
        record = records.get(item.ref.work_id)
        gate = None
        if memo is not None:
            gate = _row_gate(app, snapshot, item, record)
            hit = memo.get(item.ref.work_id)
            if hit is not None and hit[0] == gate:
                rows.append(hit[1])
                continue
        members = work_chats(
            snapshot.state,
            item.work,
            snapshot.projection,
            chats=snapshot.chats,
            legs_by_chat=snapshot.legs,
            presentation=snapshot.presentation,
        )
        title, issue_state = work_title(
            item.ref,
            backlog=snapshot.backlog,
            chats=members,
            record=record,
            custom=snapshot.titles.get(item.ref.work_id, ""),
            agent=snapshot.agent_titles.get(item.ref.work_id, ""),
            chat=start_chat_title(snapshot, item.ref.work_id),
        )
        if item.work is not None:
            source = f"{item.work.issue.repository}#{item.work.issue.number}"
        else:  # only the durable inventory still names it
            source = item.ref.source_key
        row = WorkIndexRow(
            work_id=item.ref.work_id,
            href=work_href(item.ref.work_id),
            title=title or source,
            source_label=source,
            repository=source.rpartition("#")[0],
            work_state=issue_state,
            chat_count=len(members),
            last_activity=item.last_activity,
        )
        if gate is not None:
            memo[item.ref.work_id] = (gate, row)
        rows.append(row)
    if memo is not None and len(memo) > 4 * max(len(candidates), 250):
        memo.clear()  # bounded: Works that left the list are not kept for ever
    return rows


def _matches(row: WorkIndexRow, query: str) -> bool:
    """Every word of the search appears in the title, the source or the Work id."""
    text = f"{row.title} {row.source_label} {row.work_id}".casefold()
    return all(word in text for word in query.casefold().split())


# What the default ``/works`` list follows: the overview, every chat and the board.
WORKS_INDEX_TOPICS = ("works", "sessions", BOARD_TOPIC)


async def works_index_context(app, repo: str = "", *, fresh: bool = False) -> dict:
    """The default ``/works`` list: its Active and Recent rows, filtered to ``repo``.

    Shared by the page and its stream (#3228): the stream asks once per ``works`` change for
    every tab showing the same filter, and renders only the rows whose revision moved. A page
    load reads afresh (``fresh``), as it always did: a chat moving a Work the overview does
    not list changes the row without a ``works`` change. A stream connecting right after its
    page load takes that read only while nothing it follows has moved since; otherwise it
    reads again, and ``_index_rows`` rebuilds only the rows whose Works moved.
    """
    bus = app.state.app_state.bus
    cache = getattr(app.state, "works_index_cache", None)
    if cache is None:
        cache = app.state.works_index_cache = {}
    slot = repo.strip().casefold()
    # Its rows read chats and the board beyond what the overview lists, so a change to
    # either moves it too, not only a ``works`` publication (#3228).
    revision = bus.revision(*WORKS_INDEX_TOPICS)
    hit = cache.get(slot)
    if hit is not None and not fresh and hit[0] == revision:
        return hit[1]
    snapshot = gather_pass(app)
    # The front page's own candidates, so a recently finished Work whose chats the live
    # window let go stays under Recent here too (#3229).
    candidates = await _work_candidates(app, snapshot, await runtime_queue(app))
    buckets = bucket_works(candidates, now=datetime.now(UTC))
    records = _cached_issues(app, buckets.active + buckets.recent)
    active = _index_rows(snapshot, buckets.active, app=app, records=records)
    recent = _index_rows(snapshot, buckets.recent, app=app, records=records)
    # The project filter (#2467): one link per repository with an Active or Recent Work.
    # A chosen repository stays offered even when nothing of it is left, so it can be seen
    # and cleared.
    chosen = repo.strip()
    wanted = chosen.casefold()
    named = {row.repository.casefold(): row.repository for row in active + recent}
    if wanted:
        chosen = named.setdefault(wanted, chosen)
    context = {
        "archived_count": archived_count(buckets, snapshot.state.db),
        "active": [row for row in active if not wanted or row.repository.casefold() == wanted],
        "recent": [row for row in recent if not wanted or row.repository.casefold() == wanted],
        "repo": chosen,
        "repositories": sorted(named.values(), key=str.casefold),
    }
    # One entry per filter, replaced at the next change: a filter is a query parameter, so
    # the slots are bounded like ``SharedFragments``' per-client keys.
    cache[slot] = (revision, context)
    while len(cache) > 16:
        cache.pop(next(iter(cache)))
    return context


WORKS_INDEX_PARTS = ("active", "recent")


def index_row_html(renders: RowRenders | None, part: str, sent: dict | None):
    """``row_html`` for ``works_index.html``: as ``work_view.front_row_html``, for the plain
    ``/works`` rows."""

    shown: set[str] = set()

    def row_html(macro, row: WorkIndexRow) -> Markup:
        revision = row.revision
        if sent is not None and mark_sent(sent, shown, f"index-{part}", row.work_id, revision):
            return row_stub(row.work_id, revision, tag="li")
        if renders is None:
            return Markup(macro(row))
        return renders.get(("index", row), lambda: macro(row))

    return row_html


def render_works_index(
    templates, context: dict, renders: RowRenders, sent: dict
) -> list[tuple[str, str]]:
    """The ``/works`` list's stream events, ``(event, html)``: each bucket's rows, keyed, and
    its count."""
    template = templates.get_template("partials/works_index.html")
    events = []
    for part in WORKS_INDEX_PARTS:
        html = template.render(
            part=part, rows=context[part], row_html=index_row_html(renders, part, sent)
        )
        forget_unsent(sent, f"index-{part}", html)
        events.append((f"works-index-{part}", html))
        events.append((f"works-index-{part}-count", str(len(context[part]))))
    return events


@router.get("/works", response_class=HTMLResponse)
async def works_index(
    request: Request, view: str = "", q: str = "", repo: str = "", page: int = 1
) -> HTMLResponse:
    if view == "paused":
        # Paused Works are a group on the front page now (#2566); an old link lands there.
        return RedirectResponse("/#front-needs", status_code=307)
    if view == "hidden":
        # The front page's own overview: the rows Hide took out of its Needs you (#2238).
        service = getattr(request.app.state, "work_overview", None)
        overview = await service.current() if service is not None else None
        response = get_templates(request).TemplateResponse(
            request, "works.html", {"view": view, "work_overview": overview or WorkOverview()}
        )
        response.headers["Cache-Control"] = "no-cache"
        return response
    if view != "archived":
        # The default list, which its stream keeps current row by row (#3228).
        context = {"view": view, **await works_index_context(request.app, repo, fresh=True)}
        response = get_templates(request).TemplateResponse(request, "works.html", context)
        response.headers["Cache-Control"] = "no-cache"
        return response
    snapshot = gather_pass(request.app)
    # The front page's own candidates, so a recently finished Work whose chats the live
    # window let go stays under Recent here too (#3229).
    candidates = await _work_candidates(request.app, snapshot, await runtime_queue(request.app))
    buckets = bucket_works(candidates, now=datetime.now(UTC))
    context: dict = {"view": view}
    # Only this view reads the whole inventory: it is what the operator asked to see.
    archived = archived_works(buckets, work_inventory(snapshot.state.db))
    context["archived_count"] = len(archived)
    rows = _index_rows(snapshot, archived, records=_cached_issues(request.app, archived))
    repositories = sorted({row.repository for row in rows}, key=str.casefold)
    wanted = repo.strip().casefold()
    matched = [
        row
        for row in rows
        if (not wanted or row.repository.casefold() == wanted)
        and (not q.strip() or _matches(row, q))
    ]
    pages = max(1, -(-len(matched) // ARCHIVE_PAGE_SIZE))
    page = min(max(1, page), pages)
    first = (page - 1) * ARCHIVE_PAGE_SIZE

    def link(number: int) -> str:
        params = {"view": "archived", "q": q.strip(), "repo": repo.strip(), "page": number}
        return "/works?" + urlencode({k: v for k, v in params.items() if v})

    context.update(
        rows=matched[first : first + ARCHIVE_PAGE_SIZE],
        matched=len(matched),
        first=first,
        page=page,
        pages=pages,
        prev_href=link(page - 1) if page > 1 else "",
        next_href=link(page + 1) if page < pages else "",
        q=q.strip(),
        repo=repo.strip(),
        repositories=repositories,
    )
    response = get_templates(request).TemplateResponse(request, "works.html", context)
    response.headers["Cache-Control"] = "no-cache"
    return response


@router.get("/api/works/population")
async def works_population(request: Request) -> JSONResponse:
    """Every Work in the front page's overview, one row each (#2250): read-only, for
    ``scripts/staging.sh works save``. The overview the page already built, and no new read.

    ``ready`` is whether every account has been scanned once since the deck started: before
    that, Works known only through their chats are still missing, so a save waits for it.
    ``revision`` is the commit this web process started from, so a save made right after a
    deploy can refuse a population the replaced worker served (#2628).
    """
    service = getattr(request.app.state, "work_overview", None)
    overview = await service.current() if service is not None else None
    collector = getattr(request.app.state, "collector", None)
    return JSONResponse(
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "ready": collector.initial_scans_finished() if collector is not None else True,
            "works": population_rows(overview or WorkOverview()),
            "revision": getattr(request.app.state, "revision", None),
        },
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/api/works/needs-you")
async def works_needs_you(request: Request, repo: str | None = None) -> JSONResponse:
    """What needs the owner right now, short enough to speak from (#3773): the front page's
    Needs you section, ``?repo=owner/repo`` for one repository. From the overview already
    built, like ``/api/works/population``; ``ready`` is that read's flag too.
    """
    service = getattr(request.app.state, "work_overview", None)
    overview = await service.current() if service is not None else None
    collector = getattr(request.app.state, "collector", None)
    rows = needs_you_rows(overview or WorkOverview(), repo)
    return JSONResponse(
        {
            "count": len(rows),
            "works": rows,
            "ready": collector.initial_scans_finished() if collector is not None else True,
        },
        headers={"Cache-Control": "no-cache"},
    )


# The status log's reads (#2751): a page of rows, and how far back the flap report looks.
STATUS_LOG_LIMIT = 500
STATUS_LOG_MAX = 5000
FLAP_WINDOW_S = 300
FLAP_LOOKBACK = timedelta(hours=24)
FLAP_ROWS = 50000


def _status_log_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="since is not an ISO time") from None
    # Stored stamps are UTC text, compared as text: another zone must be moved to UTC first.
    return when.astimezone(UTC) if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _status_log_work(value: str | None) -> str | None:
    if not value:
        return None
    ref, _ = _resolve(value)
    if ref is None:
        raise HTTPException(status_code=400, detail="not a Work id")
    return ref.work_id


@router.get("/api/works/status-log")
async def works_status_log(
    request: Request,
    work: str | None = None,
    since: str | None = None,
    limit: int = STATUS_LOG_LIMIT,
) -> JSONResponse:
    """Every logged change of a Work's front-page status (#2751), oldest first: the newest
    ``limit`` rows, of one Work (``work``: a Work id or ``owner/repo#n``) and after ``since``
    when given. Read-only, from this deck's control DB."""
    control = get_control_db(request)
    work_id = _status_log_work(work)
    rows = await asyncio.to_thread(
        control.work_status_log,
        work_id=work_id,
        since=_status_log_since(since),
        limit=max(1, min(limit, STATUS_LOG_MAX)),
    )
    return JSONResponse(
        {"work_id": work_id, "rows": [row.as_dict() for row in rows]},
        headers={"Cache-Control": "no-cache"},
    )


def _merge_queue_service(request: Request, owner: str, repo: str):
    release = getattr(request.app.state, "work_release", None)
    repository = f"{owner}/{repo}".lower()
    switched = getattr(getattr(release, "ports", None), "switched", None)
    if release is None or switched is None or not switched(repository):
        raise HTTPException(status_code=404, detail=f"the deck releases nothing in {repository}")
    return release, repository


@router.get("/api/merge-queue/{owner}/{repo}")
async def merge_queue(request: Request, owner: str, repo: str) -> JSONResponse:
    """A repository's merge queue (#3022), first in line first: each pull request, its Work,
    when it joined and how many times the deck brought it up to its base while first."""
    release, repository = _merge_queue_service(request, owner, repo)
    return JSONResponse(
        {"repository": repository, "queue": release.queue(repository)},
        headers={"Cache-Control": "no-cache"},
    )


@router.put("/api/merge-queue/{owner}/{repo}")
async def reorder_merge_queue(request: Request, owner: str, repo: str) -> JSONResponse:
    """Put a repository's merge queue in a person's order (#3022): ``{"order": ["#103",
    ...]}`` names pull requests first-first; the rest keep their order after them. A pull
    request moved to the front is brought up to its base at once."""
    release, repository = _merge_queue_service(request, owner, repo)
    try:
        body = await request.json()
    except ValueError:
        body = None
    order = body.get("order") if isinstance(body, dict) else None
    if not isinstance(order, list) or not all(isinstance(name, str) for name in order):
        raise HTTPException(status_code=400, detail='send {"order": ["#n", ...]}')
    try:
        release.reorder(repository, order)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await release.wake_first()
    return JSONResponse({"repository": repository, "queue": release.queue(repository)})


@router.get("/api/works/status-log/flaps")
async def works_status_flaps(
    request: Request,
    work: str | None = None,
    since: str | None = None,
    within: int = FLAP_WINDOW_S,
) -> JSONResponse:
    """Each short trip a Work made within ``within`` seconds (#2751, ``flaps``): a visit
    into Needs you, an absence from it, or a bounce somewhere and back. Looks at the last 24
    hours unless ``since`` says otherwise."""
    control = get_control_db(request)
    start = _status_log_since(since) or datetime.now(UTC) - FLAP_LOOKBACK
    rows = await asyncio.to_thread(
        control.work_status_log, work_id=_status_log_work(work), since=start, limit=FLAP_ROWS
    )
    found = flaps(rows, within=timedelta(seconds=max(0, within)))
    return JSONResponse(
        {"since": start.isoformat(), "within": within, "count": len(found), "flaps": found},
        headers={"Cache-Control": "no-cache"},
    )


#: How long ``/works/<id>/chat`` waits for a starting Work's chat before the Work page (#3590).
WORK_CHAT_WAIT_S = 15.0
WORK_CHAT_POLL_S = 0.5


@router.get("/works/{work_id:path}/chat")
async def work_chat(request: Request, work_id: str) -> RedirectResponse:
    """The Work's deck-started chat, as soon as it exists; else the Work page (#3590).

    The New work receipt links here because the chat does not exist when it is drawn. The
    chat is found by its session token: the one its start recorded under the Work's start
    id (an early start's), else the start id itself. A start under way is waited for a
    little, reading only this process's own state; one refused, or none, is the Work page.
    """
    ref, _canonical = _resolve(work_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="not a Work id")
    state = get_state(request)
    db = get_control_db(request)
    start_id = start_action_id(ref.work_id)

    def chat_path() -> str:
        # The early start's token, then the start id: a usual start after a refused early
        # one runs under the start id.
        claimed = db.start_spare_claim(start_id)
        for token in (claimed[0] if claimed else "", start_id):
            key = state.session_key_for_token(token) if token else None
            if key and key in state.sessions:
                return state.chat_path_for_session(key)
        return ""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + WORK_CHAT_WAIT_S
    while True:
        found = chat_path()
        if found:
            break
        press = db.work_action(start_id)
        binding = ref.work_id in _EARLY_BINDING
        if (press is None and not binding) or loop.time() >= deadline:
            break
        if press is not None and press.state == "failed":
            break
        await asyncio.sleep(WORK_CHAT_POLL_S)
    response = RedirectResponse(found or work_href(ref.work_id), status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/works/{work_id:path}", response_class=HTMLResponse)
async def work_page(request: Request, work_id: str) -> HTMLResponse:
    ref, canonical = _resolve(work_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="not a Work id")
    if not canonical:
        return RedirectResponse(work_href(ref.work_id), status_code=307)
    times = StepTimes(getattr(request.state, "received_at", None))
    # Read before the build, so a change landing during it moves the key past it (#3228).
    before = work_page_key(request.app, ref, ())
    revisions = _revision_reader(request.app.state.app_state)
    detail = await load_work_detail(request.app, ref, times=times)
    if detail is None:
        raise HTTPException(status_code=404, detail="nothing on this deck names this Work")
    note_work_opened(request.app, ref.work_id)
    templates = get_templates(request)
    sections = render_work_sections(templates, detail)
    # The page's build is the stream's first one (#3228): stored for every tab, so a stream
    # connecting while nothing moved builds nothing, and told each section's digest, so it
    # sends only the sections that changed.
    line = detail.line
    work_page_builds(request.app).store(
        ref,
        before,
        revisions,
        detail,
        {name: (SECTION_INPUTS[name](detail, line), html) for name, html in sections},
    )
    stream_url = (
        "/events/works/"
        + quote(ref.work_id, safe=":")
        + "?"
        + urlencode({"sig": _section_signature(sections)})
    )
    response = templates.TemplateResponse(
        request,
        "work.html",
        {
            "detail": detail,
            "sections": sections,
            "stream_url": stream_url,
            "message_url": work_href(ref.work_id) + "/message",
            # "Also post to issue" (#2416): the Work page always posts to the Work's own issue.
            "issue_post_target": ref,
            "rename_url": work_href(ref.work_id) + "/title",
            "new_chat": _new_chat_form(request, ref, detail),
            "title_max": MAX_TITLE,
            # Only a Work with a checkout on this host offers its diff (#2379).
            "diff_url": _diff_url(ref) if work_checkouts(detail.workspace) else "",
            # Opened just now: the top bar's list leaves it out before the rebuild has (#3289).
            "seen_now": (ref.work_id,),
        },
    )
    times.lap("render")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Server-Timing"] = times.server_timing()
    if times.total_ms > SLOW_WORK_PAGE_MS:
        logger.warning(
            "slow work page %s total=%.0fms %s", ref.work_id, times.total_ms, times.summary()
        )
    return response


# A Work page's live sections (#3228): every section but the message bar, whose text box
# is outside it anyway. At most one build of the Work per this many seconds per page.
WORK_PAGE_MIN_INTERVAL_S = 3.0
LIVE_SECTIONS = frozenset(name for name in WORK_SECTIONS if name != "message")


def _digest(html: str) -> str:
    return hashlib.sha1(html.encode(), usedforsecurity=False).hexdigest()[:12]


def _section_signature(sections: list[tuple[str, str]]) -> str:
    return ".".join(f"{name}-{_digest(html)}" for name, html in sections if name in LIVE_SECTIONS)


def _parse_section_signature(value: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in value.split(".")[: len(LIVE_SECTIONS)]:
        name, _, digest = item.partition("-")
        if name in LIVE_SECTIONS and len(digest) == 12:
            found[name] = digest
    return found


def _own_records(app, ref: WorkRef) -> tuple:
    """What only this Work's page shows from the control database: its resolution, live check,
    last action press and hide. Each a one-row read, so a key read stays cheap."""
    control = getattr(app.state, "control_db", None)
    if control is None:
        return ()
    presses = control.load_work_actions(ref.work_id, limit=1)
    return (
        control.work_resolution(ref.work_id),
        control.work_live_check(ref.work_id),
        presses[0] if presses else None,
        control.hidden_works().get(ref.work_id),
        # Where a person moved a wayfinder map's boxes (#3627), so every open page redraws,
        # and a map's GitHub reads a webhook dropped, so its arrows and fog follow.
        tuple(sorted(control.wayfinder_layout(ref.work_id).items())),
        wayfinder_map.generation(),
    )


def work_page_key(app, ref: WorkRef, chat_keys: tuple[str, ...]) -> tuple:
    """A cheap key that moves whenever this Work's page may need building again (#3228).

    The Work's card on the overview (``WorkCard.revision``: its board card, review, release
    record, Runs and jobs, which the overview rebuilds only for the Works whose inputs moved),
    its own chats' change numbers, and the records only its page shows (``_own_records``). A
    change to another Work moves none of them. A Work the overview does not list has no card,
    so for it every ``work_inputs`` and ``reviews`` change counts instead. A parent issue's
    Work also moves with any sub-issue's Work folded into its front-page row (#3622).
    """
    service = getattr(app.state, "work_overview", None)
    overview = getattr(service, "overview", None)
    card = None
    if overview is not None:
        card = next((card for card in overview.cards if card.work_id == ref.work_id), None)
    state = app.state.app_state
    revision = getattr(state, "session_revision", None)
    # Three parts, read by place (``WorkPageBuilds.store``): the tree's revision goes with the
    # card's, in the first.
    return (
        (
            card.revision
            if card is not None
            else ("unlisted", state.bus.revision(WORK_INPUTS_TOPIC, REVIEWS_TOPIC)),
            tree_revision(overview, ref.work_id),
        ),
        tuple(revision(key) for key in chat_keys) if revision is not None else (),
        _own_records(app, ref),
    )


def _revision_reader(state) -> Any:
    """``state.session_revision`` as it is now, for chats named only later."""
    revision = getattr(state, "session_revision", None)
    revisions = getattr(state, "_session_revisions", None)
    if revision is None:
        return lambda key: None
    if revisions is None:
        return revision
    frozen, epoch = dict(revisions), getattr(state, "_sessions_epoch", 0)
    return lambda key: (epoch, frozen.get(key, 0))


def _chat_keys(detail) -> tuple[str, ...]:
    return tuple(
        sorted({leg for chat in detail.chats for leg in (chat.legs or (chat.session_key,))})
    )


@dataclass(frozen=True)
class WorkPageBuild:
    """One build of a Work page's live sections: the key it was built at, the chats that key
    follows, and each section's digest and markup."""

    key: tuple
    chat_keys: tuple[str, ...]
    sections: dict[str, tuple[str, str]]
    # ``render_changed_sections``' record: each section's inputs and markup, for the next build.
    inputs: dict[str, tuple[tuple, str]] = field(default_factory=dict, compare=False)


class WorkPageBuilds:
    """Each Work page built once per change for every tab and the page load (#3228).

    ``current`` hands back the last build while its key is still the Work's key now; otherwise
    one caller builds and the others wait for that build. Bounded: a Work nobody has opened in
    a while is the first to go.
    """

    MAXSIZE = 64

    def __init__(self) -> None:
        self._builds: OrderedDict[str, WorkPageBuild] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    def store(self, ref: WorkRef, before: tuple, revisions, detail, rendered) -> WorkPageBuild:
        """Keep a build, keyed by what was read *before* it (``before``, ``revisions``), so a
        change landing during the build moves the key past it. ``rendered`` is
        ``render_changed_sections``' ``name -> (inputs, html)``: the next build re-renders
        only the sections whose inputs moved."""
        chat_keys = _chat_keys(detail)
        build = WorkPageBuild(
            key=(before[0], tuple(map(revisions, chat_keys)), before[2]),
            chat_keys=chat_keys,
            sections={
                name: (_digest(html), html)
                for name, (_inputs, html) in rendered.items()
                if name in LIVE_SECTIONS
            },
            inputs=dict(rendered),
        )
        self._builds[ref.work_id] = build
        self._builds.move_to_end(ref.work_id)
        while len(self._builds) > self.MAXSIZE:
            old, _ = self._builds.popitem(last=False)
            self._locks.pop(old, None)
        return build

    async def current(self, app, ref: WorkRef, templates) -> WorkPageBuild | None:
        lock = self._locks.setdefault(ref.work_id, asyncio.Lock())
        async with lock:
            build = self._builds.get(ref.work_id)
            if build is not None and build.key == work_page_key(app, ref, build.chat_keys):
                self._builds.move_to_end(ref.work_id)
                return build
            before = work_page_key(app, ref, ())
            revisions = _revision_reader(app.state.app_state)
            detail = await load_work_detail(app, ref)
            if detail is None:
                return None
            work_counter.count(work_counter.WORK_PAGE_BUILD)
            rendered = render_changed_sections(
                templates,
                detail,
                names=sorted(LIVE_SECTIONS, key=WORK_SECTIONS.index),
                previous=build.inputs if build is not None else {},
            )
            return self.store(ref, before, revisions, detail, rendered)


def work_page_builds(app) -> WorkPageBuilds:
    builds = getattr(app.state, "work_page_builds", None)
    if builds is None:
        builds = app.state.work_page_builds = WorkPageBuilds()
    return builds


async def _work_stream(request: Request, ref: WorkRef, sent: dict[str, str]) -> AsyncIterator[str]:
    """A Work page's sections, each sent only when it changed (#3228).

    Wakes on the topics a card reads (``WorkOverviewService.TOPICS``) and ``works``, at most
    every ``WORK_PAGE_MIN_INTERVAL_S``, and reads ``work_page_key``. Only when that moved does
    it ask ``WorkPageBuilds`` for the Work, which builds it once for every tab. A connect asks
    at once: the page load stored its own build, so a page nothing moved under costs no build,
    and a change that landed in between is sent. No timer: a quiet Work sends only the ping.
    """
    from .routes_sse import HEARTBEAT_S, format_sse

    state = get_state(request)
    templates = get_templates(request)
    loop = asyncio.get_running_loop()
    builds = work_page_builds(request.app)
    build: WorkPageBuild | None = None
    asked_at = 0.0
    # When the stream last sent its reader anything: a busy deck wakes it every few seconds,
    # so the ping goes by this, not only by a quiet ``HEARTBEAT_S``.
    sent_at = loop.time()
    topics = (*WorkOverviewService.TOPICS, WORKS_TOPIC)
    with state.bus.subscribe(*topics) as sub:
        while True:
            if await request.is_disconnected():
                break
            if build is None or work_page_key(request.app, ref, build.chat_keys) != build.key:
                build = await builds.current(request.app, ref, templates)
                asked_at = loop.time()
                for name, (digest, html) in (build.sections if build is not None else {}).items():
                    if sent.get(name) != digest:
                        sent[name] = digest
                        sent_at = loop.time()
                        yield format_sse(f"work-{name}", html)
            if loop.time() - sent_at >= HEARTBEAT_S:
                sent_at = loop.time()
                yield ": ping\n\n"
            try:
                await asyncio.wait_for(sub.get(), timeout=HEARTBEAT_S)
            except TimeoutError:
                continue
            await asyncio.sleep(max(0.0, asked_at + WORK_PAGE_MIN_INTERVAL_S - loop.time()))
            while sub.get_nowait() is not None:
                pass
            sub.changed_topics()


@router.get("/events/works/{work_id:path}")
async def work_events(request: Request, work_id: str, sig: str = "") -> StreamingResponse:
    ref, _ = _resolve(work_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="not a Work id")
    return StreamingResponse(
        _work_stream(request, ref, _parse_section_signature(sig)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _new_chat_form(request: Request, ref: WorkRef, detail) -> dict | None:
    """What the New chat control shows (#2388), or None when no account can start a chat.

    Read once with the page, never on the stream: the control holds a selection and text.
    """
    from ..work_side_chat import prefilled_account, work_checkout

    accounts = new_chat_accounts(request)
    if not accounts:
        return None
    keys = [account.key for account in accounts]
    return {
        "url": work_href(ref.work_id) + "/new-chat",
        "folder": work_checkout(detail.workspace, ref),
        "repository": ref.repository,
        "accounts": accounts,
        "account_key": prefilled_account(detail.chats, get_state(request).sessions, keys),
    }


def _diff_url(ref: WorkRef) -> str:
    return "/partials" + work_href(ref.work_id) + "/diff"


@router.get("/partials/works/{work_id:path}/diff", response_class=HTMLResponse)
async def work_diff(request: Request, work_id: str) -> HTMLResponse:
    """The Work's pending change, read when its Diff fold opens (#2379).

    The same fragment a chat's diff pane shows, over every checkout the Work's workspace
    knows about rather than one chat's: a person reading a Work should not have to open
    the right chat first. Like the chat's, it is never on a stream, because it runs git.
    """
    ref, canonical = _resolve(work_id)
    if ref is None or not canonical:
        raise HTTPException(status_code=404, detail="not a canonical Work id")
    detail = await load_work_detail(request.app, ref)
    if detail is None:
        raise HTTPException(status_code=404, detail="nothing on this deck names this Work")
    reader = request.app.state.worktree_diff
    wait = request.query_params.get("refresh") == "wait"
    read = []
    checkouts = work_checkouts(detail.workspace)
    # A worktree a chat made for its branch without registering it is found through
    # git, by the pull request's head branch (#2993).
    pulls = [
        pull
        for repo in (detail.workspace.repositories if detail.workspace else ())
        for pull in repo.pull_requests
    ]
    # A pull request in a repository the Work has no checkout of, as when the issue is on
    # one repository and the code in another, is looked for from a local clone (#3129).
    covered = {
        repo.repository.lower()
        for repo in (detail.workspace.repositories if detail.workspace else ())
        if any(item.observation.exists for item in (*repo.worktrees, *repo.cwds))
    }
    from ..work_start import pull_request_clones

    clones = await pull_request_clones(request.app, pulls, covered)
    found = await reader.pull_request_checkouts(
        [path for path, _pull in checkouts], pulls, clones=clones
    )
    for path, pull in (*checkouts, *found):
        diff = await reader.read(
            path,
            pull_request_base=pull.base_branch if pull else None,
            pull_request_repo=pull.repository if pull else None,
            await_refresh=wait,
        )
        read.append((diff, pull))
    shown = [item for item in read if not item[0].is_empty] or read[:1]
    return get_templates(request).TemplateResponse(
        request,
        "partials/session_diff.html",
        {
            "diffs": [diff for diff, _pull in shown],
            "pulls": [pull for _diff, pull in shown],
            "checkouts": len(read),
            "owner": "this Work",
            "refresh_url": _diff_url(ref) + "?refresh=wait",
            "refreshing": any(diff.refreshing for diff, _pull in read),
        },
    )


def _message_receipt(
    request: Request,
    *,
    text: str,
    target=None,
    state: str,
    status_code: int = 200,
    issue_post=None,
) -> HTMLResponse:
    response = get_templates(request).TemplateResponse(
        request,
        "partials/work_message_receipt.html",
        {"text": text, "target": target, "state": state, "issue_post": issue_post},
    )
    response.status_code = status_code
    return response


@router.post("/works/{work_id:path}/message", response_class=HTMLResponse)
async def work_message(request: Request, work_id: str) -> HTMLResponse:
    """Message this Work (#2218): the deck, not the operator, picks the chat it goes to.

    The target is chosen when the message is sent (``work_story.message_target``), then the
    send is the chat's own: the same route, the same refusals, the same queue behind a
    running turn. A refusal comes back in words and keeps what was typed.
    """
    from . import routes_actions

    routes_actions._require_same_origin(request)
    ref, _ = _resolve(work_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="not a Work id")
    detail = await load_work_detail(request.app, ref, workspace=False)
    if detail is None:
        raise HTTPException(status_code=404, detail="nothing on this deck names this Work")
    target = detail.message_target
    if target is None:
        return _message_receipt(
            request,
            text="No chat in this Work can take a message right now.",
            state="refused",
            status_code=409,
        )
    form = await request.form()
    if str(form.get("post_to_issue") or "").strip():
        # A ticked box here posts to this Work's issue, whichever chat takes the message; the
        # chat route checks that chat is still one of the Work's (#2416).
        request.state.issue_post_work = ref
    try:
        sent = await routes_actions.inject_message(request, target.session_key)
    except HTTPException as exc:
        return _message_receipt(
            request,
            text=f"Not sent: {exc.detail}",
            target=target,
            state="refused",
            status_code=exc.status_code if 400 <= exc.status_code < 500 else 409,
        )
    action = sent.headers.get("X-AgentDeck-Action-State", "")
    if sent.status_code != 202 or not action:
        # An answer that is not a send is never reported as one. A chat whose folder is
        # gone now resumes in its repository (#2524), so nothing answers this way today.
        return _message_receipt(
            request,
            text="Not sent.",
            target=target,
            state="refused",
            status_code=409,
        )
    text = "Queued behind the current turn" if action == "queued" else "Sent"
    return _message_receipt(
        request,
        text=text,
        target=target,
        state=action,
        status_code=202,
        issue_post=getattr(request.state, "issue_post_outcome", None),
    )


# A press's client action id: what the page's action timing generates (``action_timing.js``).
_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_GH_TIMEOUT_S = 30.0


def _pressed_receipt(request: Request, press: WorkActionPress) -> HTMLResponse:
    """The answer to a press whose action id was already used: what that press did."""
    if press.state == "sent":
        text = f"Already sent: {press.result or press.label}"
        return _message_receipt(request, text=text, state="sent")
    if press.state == "failed":
        text = f"{press.label} was not sent: {press.result}".rstrip(": ")
        return _message_receipt(request, text=text, state="refused", status_code=409)
    text = f"{press.label} may already have been sent, so it was not sent again."
    return _message_receipt(request, text=text, state="uncertain", status_code=202)


def _not_again(request: Request, label: str, how: str, age: float) -> HTMLResponse:
    """The refusal of a press that repeats one still standing."""
    when = "just now" if age < 60 else f"{round(age / 60)} min ago"
    return _message_receipt(
        request,
        text=f"{label} {how} {when}; it was not sent again.",
        state="refused",
        status_code=409,
    )


_DECK_REQUEST_TARGET_RE = re.compile(
    r"^(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(?P<n>[1-9][0-9]*)$"
)


async def _deck_released_verdict(
    request: Request, repository: str, number: int, command: str
) -> str | None:
    """The deck reviewer's ``/reviewed`` on a Work the deck releases itself (#2895).

    ``None`` when this is not that: another command, or a Work the deck does not release, so
    the caller answers as before. The verdict is checked and recorded at once, so the answer
    is its delivery, as the Poller's request file is.
    """
    from ..poller_engine.intake import CommandKind, parse_verdicts

    release = getattr(request.app.state, "work_release", None)
    work_id = WorkRef(repository.lower(), number).work_id
    parsed = parse_verdicts(command)
    if release is None or not parsed or parsed[0].kind != CommandKind.REVIEWED:
        return None
    if not release.handles(work_id):
        return None
    # Its GitHub reads, and the merge it may make, took 7 to 30 s on 2026-10-12/13, and a caller
    # that hangs up cancels this handler: clean verdicts were lost mid-read that way, and their
    # reviewers labelled the chats blocked (#2963). Protected, the pass is recorded even when
    # the caller has gone; a resend records the same pass again, which changes nothing.
    from .. import shutdown

    async def record() -> list[str]:
        # One verdict line per pull request may come in one request (#3161), all protected.
        answers = []
        for verdict in parsed:
            answers.append(
                await release.on_verdict(
                    work_id, verdict.verdict, verdict.head, verdict.digest, verdict.targets
                )
            )
        return answers

    answers = await shutdown.protect(f"deck-verdict:{work_id}", record())
    logger.info("deck-request: verdict for %s#%s: %s", repository, number, "; ".join(answers))
    return (
        f"The reviewer's verdict for {repository}#{number}: {'; '.join(answers)}. This answer"
        " is the delivery: it is not posted as a GitHub comment."
    )


async def _deck_run_request(
    request: Request, repository: str, number: int, command: str, action_id: str
) -> JSONResponse | None:
    """The deck reviewer's findings or plain comment on a Work the deck runs itself (#3004).

    ``None`` when the deck does not run this Work, so the caller answers 404. Findings (``/claude
    ...``) are posted on the card, quoted, and handed to the Work's chat once; a plain comment is
    posted on the card. Any other command is refused, never answered with "no Poller tracks", which
    would send the reviewer to post it on GitHub where the deck's own account is not heard.
    """
    from ..poller_engine.intake import CommandKind, parse_control

    release = getattr(request.app.state, "work_release", None)
    ref = WorkRef(repository, number)
    work_id = WorkRef(repository.lower(), number).work_id
    if release is None or not release.handles(work_id):
        return None
    issue = f"{repository}#{number}"
    parsed = parse_control(command, bot_marker="")
    findings = parsed is not None and parsed.kind == CommandKind.CLAUDE
    if command.startswith("/") and not findings:
        raise HTTPException(
            status_code=400,
            detail=(
                f"the deck runs {issue} itself and takes only the reviewer's /reviewed verdict,"
                " its /claude findings, or a plain comment"
            ),
        )
    stale = (
        await release.stale_findings(work_id, parsed.instruction, repository) if findings else ""
    )
    if stale:
        # About a commit the branch has left (#3192): the newer head has its own review, so
        # these neither reach the agent nor the card.
        logger.info("deck-request: findings for %s dropped: %s", issue, stale)
        detail = (
            f"These findings were not posted or handed on: {stale}. The newer commit has a"
            " review of its own; stop this one."
        )
        return JSONResponse({"outcome": "done", "detail": detail}, status_code=202)
    outcome, receipt = await _post_reviewer_comment(request, ref, command, action_id)
    if not findings:
        if outcome == "failed":
            raise HTTPException(status_code=400, detail=receipt)
        status = 202 if outcome == "done" else 503
        return JSONResponse({"outcome": outcome, "detail": receipt}, status_code=status)
    posted = re.search(r"https://\S+", receipt) if outcome == "done" else None
    url = posted.group(0) if posted else f"https://github.com/{repository}/issues/{number}"
    told = await release.on_findings(work_id, action_id, parsed.instruction, url)
    logger.info("deck-request: findings for %s: told=%s, %s", issue, told, receipt)
    if told:
        detail = (
            f"The reviewer's findings went to the agent of {issue}. {receipt}. This answer is"
            " the delivery: do not wait for the findings to show on GitHub"
        )
        return JSONResponse({"outcome": "done", "detail": detail}, status_code=202)
    if release.ports.chat(work_id):
        # A chat owns it and the store would not keep the message (#3183 review): nothing
        # recorded it as told, so sending the same request again tries again.
        detail = (
            f"The findings could not be handed to the agent of {issue}: the deck could not"
            f" store the message. Send the same request again. {receipt}"
        )
        return JSONResponse({"outcome": "retry", "detail": detail}, status_code=503)
    if outcome == "failed":
        raise HTTPException(
            status_code=400, detail=f"no chat owns {issue}, and the findings: {receipt}"
        )
    detail = f"No chat owns {issue}, so the findings are only on the card. {receipt}"
    return JSONResponse(
        {"outcome": outcome, "detail": detail}, status_code=202 if outcome == "done" else 503
    )


@router.post("/api/deck-request", dependencies=[Depends(require_access)])
async def deck_request_api(request: Request) -> JSONResponse:
    """The fallback reviewer's verdict, findings or plain comment, for ``agentdeck deck-request``
    (#2736).

    A ``/reviewed`` verdict is checked and recorded by the deck's release at once. On a Work the
    deck runs, ``/claude`` findings are posted on the card and handed to the Work's agent, and a
    plain comment is posted on the card (#3004, ``_deck_run_request``). ``id`` is the caller's
    idempotency key. Anything else is a 404, so the reviewer posts it on GitHub; the Poller's
    request files that once took it were retired with its cards (#3025).
    """
    try:
        body = await request.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    target = _DECK_REQUEST_TARGET_RE.match(str(body.get("issue") or "").strip())
    command = str(body.get("command") or "").strip()
    action_id = str(body.get("id") or "")
    if target is None:
        raise HTTPException(status_code=400, detail="issue must be owner/repo#123")
    if not command:
        raise HTTPException(status_code=400, detail="the request carried no command")
    if not _ACTION_ID_RE.match(action_id):
        raise HTTPException(status_code=400, detail="the request carried no usable id")
    repository, number = target.group("repo"), int(target.group("n"))
    released = await _deck_released_verdict(request, repository, number, command)
    if released is not None:
        return JSONResponse({"outcome": "done", "detail": released})
    taken = await _deck_run_request(request, repository, number, command, action_id)
    if taken is not None:
        return taken
    raise HTTPException(
        status_code=404,
        detail=f"the deck does not take this for {repository}#{number}: post it on GitHub",
    )


async def _post_reviewer_comment(
    request: Request, target: WorkRef, text: str, action_id: str
) -> tuple[str, str]:
    """Post a reviewer's plain comment on its card once; ``(outcome, receipt)`` (#2814).

    The ledger is read first, so a resent request reports what the first one did and never
    posts again; a post that may have landed stays unsettled and is never repeated.
    """
    from .. import issue_post, shutdown
    from ..db import REVIEWER_COMMENT_ACTION

    issue = f"{target.repository}#{target.number}"
    db = get_control_db(request)

    def recorded(pressed) -> tuple[str, str]:
        if pressed is None or pressed.action != REVIEWER_COMMENT_ACTION:
            return "failed", "the request's id already stands for something else"
        if pressed.outcome == issue_post.DONE:
            return "done", f"The reviewer's comment is on {issue}: {pressed.result}"
        if pressed.outcome == issue_post.FAILED:
            return "failed", f"The reviewer's comment was not posted on {issue}: {pressed.result}"
        return "uncertain", f"The reviewer's comment may be on {issue}; it was not posted again"

    pressed = db.work_action(action_id)
    if pressed is not None:
        return recorded(pressed)
    if shutdown.is_shutting_down():
        return "uncertain", "the deck is restarting; nothing was posted, so send it again"
    reserved = db.reserve_work_action(action_id, target.work_id, REVIEWER_COMMENT_ACTION)
    if reserved is None:
        return "failed", "the deck could not record the comment, so it was not posted"
    if reserved is False:
        return recorded(db.work_action(action_id))
    outcome = await shutdown.protect(
        f"reviewer-comment:{action_id}",
        issue_post.post(
            db,
            action_id=action_id,
            target=target,
            comment=issue_post.reviewer_body(text, action_id=action_id),
        ),
    )
    if outcome.code == issue_post.DONE:
        return "done", f"Posted the reviewer's comment on {issue}: {outcome.url}"
    if outcome.code == issue_post.FAILED:
        return "failed", f"The reviewer's comment was not posted on {issue}: {outcome.detail}"
    return "uncertain", f"The reviewer's comment may be on {issue}; do not send it again"


async def _agent_review(request: Request, step: work_actions.Step, label: str) -> tuple[str, str]:
    """Open the Work's review chat and start a Claude or Codex reviewer on it at once (#2736).

    The chat is the one ChatGPT review opens, so the Poller's review bookkeeping is the same;
    only the reviewer differs. Its verdict reaches the Poller through the deck and is checked
    there by head and diff like any other. A chat someone already holds is left to them.
    """
    from .. import chat_board, chat_review
    from .routes_chat_board import note_review_head, releases_of

    service = getattr(request.app.state, "review_fallback", None)
    if service is None:
        return "failed", "this deck has no agent reviewer"
    try:
        outcome = await chat_review.open_review(
            request.app.state.config.chat_board,
            step.issue,
            start=False,
            pulls=step.pulls,
            releases=releases_of(request.app),
        )
    except chat_review.ReviewError as exc:
        return "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 - an unknown failure may have written
        logger.exception("work action Agent review for %s failed", step.issue)
        return "uncertain", f"{label} did not finish ({exc}); it may have started"
    note_review_head(request.app, outcome)
    if outcome.note and outcome.note != "left for the deck to start":
        # ``open_review`` says why it started nothing: the chat is claimed or waiting on a
        # person, and a reviewer started on it would skip it by its labels.
        return "done", f"Agent review: chat #{outcome.issue_number} — {outcome.note}"
    row = chat_board.ChatRow(
        number=outcome.issue_number,
        title=chat_review.TITLE.format(repository=outcome.repository, issue=outcome.issue),
        url=outcome.issue_url,
    )
    started, note = await service.start_on(row)
    if not started:
        return "failed", f"Agent review: chat #{outcome.issue_number} is open, but {note}"
    who = note or "agent"
    return "done", f"Agent review: a {who} reviewer started on chat #{outcome.issue_number}"


async def _wake_release(request: Request, work_id: str) -> None:
    """A deck pause or resume moves the deck's own release at once (#2895): a resume looks
    again for a merge the pause held. Its failure never fails the press."""
    release = getattr(request.app.state, "work_release", None)
    if release is None or not release.handles(work_id):
        return
    try:
        await release.evaluate(work_id)
    except Exception:  # noqa: BLE001 - the pause is recorded; the release catches up later
        logger.warning("work release of %s after a pause press failed", work_id, exc_info=True)


async def _write(
    request: Request, step: work_actions.Step, label: str, action_id: str
) -> tuple[str, str]:
    """Make the step's one write; ``(outcome, receipt)`` with outcome ``done``, ``failed``
    or ``uncertain`` (no proof either way, so the reservation stays unsettled)."""
    if step.kind == "chatgpt_review":
        from .. import chat_review
        from .routes_chat_board import chat_board_of, note_review_head, releases_of

        try:
            outcome = await chat_review.open_review(
                request.app.state.config.chat_board,
                step.issue,
                start=True,
                pulls=step.pulls,
                board=chat_board_of(request.app),
                releases=releases_of(request.app),
            )
        except chat_review.ReviewError as exc:
            # ``open_review`` finds the open chat before it writes one, so a refusal here is
            # safe to press again.
            return "failed", str(exc)
        except Exception as exc:  # noqa: BLE001 - an unknown failure may have written
            logger.exception("work action ChatGPT review for %s failed", step.issue)
            return "uncertain", f"{label} did not finish ({exc}); it may have started"
        note_review_head(request.app, outcome)
        return "done", f"ChatGPT review: {outcome.summary}"
    if step.kind == "agent_review":
        return await _agent_review(request, step, label)
    if step.kind == "deck_merge":
        # A switched Work no Poller card holds merges in the deck (#2895): the press is the
        # person's ask, and the release's checks still decide.
        release = getattr(request.app.state, "work_release", None)
        work_id = WorkRef(step.repository.lower(), step.number).work_id
        if release is None or not release.handles(work_id):
            return "failed", "The deck does not release this Work; nothing was sent."
        from ..work_release import PressRefused

        try:
            answer = await release.request_merge(work_id, fresh=step.body == "fresh")
        except PressRefused as refused:
            return "failed", str(refused)
        except Exception as exc:  # noqa: BLE001 - a merge may have reached GitHub
            logger.exception("work action Merge for %s failed", step.issue)
            return "uncertain", f"{label} did not finish ({exc}); it may have merged"
        return "done", f"{label} asked for {step.issue}: {answer}"
    if step.kind == "deck_withdraw":
        # A Merge press taken back (#3320): the release's own record, under the Work's lock.
        release = getattr(request.app.state, "work_release", None)
        work_id = WorkRef(step.repository.lower(), step.number).work_id
        if release is None or not release.handles(work_id):
            return "failed", "The deck does not release this Work; nothing was sent."
        try:
            answer = await release.withdraw_merge(work_id)
        except Exception as exc:  # noqa: BLE001 - the press may already be gone
            logger.exception("work action Withdraw for %s failed", step.issue)
            return "uncertain", f"{label} did not finish ({exc}); it may have been withdrawn"
        return "done", f"{label} for {step.issue}: {answer}"
    if step.kind in {"deck_pause", "deck_resume"}:
        # A Work no labelled Poller card tracks is paused on this deck alone (#2559).
        db = get_control_db(request)
        work_id = WorkRef(step.repository, step.number).work_id
        if step.kind == "deck_pause":
            if not db.pause_work(work_id):
                return "failed", "the deck could not record the pause"
            await _wake_release(request, work_id)
            return "done", f"Paused {step.issue}: the deck sends nothing to its chats"
        if not db.resume_work(work_id):
            return "failed", "the deck could not record the resume"
        await _wake_release(request, work_id)
        return "done", f"Resumed {step.issue}: its chats take messages again"
    gh = shutil.which("gh")
    if gh is None:
        return "failed", "gh is not available on this host"
    # Only the close and the labels reach here. None carries anyone's authority, so each goes
    # out as the repository owner's non-human authority, like every other deck write.
    if step.kind == "add_label":
        # The same write a wayfinder ticket gets when its last blocker closes (#3764).
        return await start_issue(
            gh,
            step.repository,
            step.number,
            step.body,
            getattr(request.app.state, "deck_claims", None),
            label=label,
            timeout=_GH_TIMEOUT_S,
        )
    if step.kind == "remove_label":
        argv = [
            gh, "issue", "edit", str(step.number), "--repo", step.repository,
            "--remove-label", step.body,
        ]
        done = f"Resumed {step.issue}: its paused label is off"
    else:
        argv = [
            gh,
            "issue",
            "close",
            str(step.number),
            "--repo",
            step.repository,
            "--reason",
            step.body,
        ]
        done = f"Closed {step.issue}"
    outcome, text = await _run_gh(argv, step.repository, label)
    return (outcome, done) if outcome == "done" else (outcome, text)


async def _run_gh(argv: list[str], repository: str, label: str) -> tuple[str, str]:
    """One ``gh`` write of a Work action: ``("done", "")``, or its outcome and why."""
    return await run_gh_write(argv, repository, label, timeout=_GH_TIMEOUT_S)


@router.post("/works/{work_id:path}/action", response_class=HTMLResponse)
async def work_action(request: Request, work_id: str) -> HTMLResponse:
    """Perform one of the Work's current actions (#2237).

    The one write route for every Work action. The request names an action id and nothing
    else; the Work's operator attention is recomputed now and an action it does not offer is
    refused, so this can only do what the row or page offers at this moment. Each write is a
    fixed body (``work_actions``), reserved under the press's client action id before it is
    made: a replay of the same id answers with what that press did, a second press of the same
    action inside the window is refused unless the first is known to have failed, and a write
    that ended without proof either way is never made again by the deck.
    """
    from . import routes_actions

    routes_actions._require_same_origin(request)
    ref, _ = _resolve(work_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="not a Work id")
    form = await request.form()
    action = str(form.get("action") or "").strip()
    if action not in work_actions.WRITES:
        return _message_receipt(
            request, text="That is not a Work action.", state="refused", status_code=422
        )
    label = work_actions.LABELS[action]
    # The press's own id is its identity: one the server made up would give a replay a new
    # one and walk past the reservation, so a press without a usable id writes nothing.
    action_id = str(form.get("client_action_id") or "")
    if not _ACTION_ID_RE.fullmatch(action_id):
        return _message_receipt(
            request,
            text=f"{label} was not sent: the press carried no usable action ID.",
            state="refused",
            status_code=422,
        )
    db = get_control_db(request)
    earlier = db.work_action(action_id)
    if earlier is not None:
        if earlier.work_id != ref.work_id or earlier.action != action:
            return _message_receipt(
                request,
                text="That action ID was already used for something else; nothing was sent.",
                state="refused",
                status_code=409,
            )
        return _pressed_receipt(request, earlier)
    detail = await load_work_detail(request.app, ref, workspace=False)
    if detail is None:
        raise HTTPException(status_code=404, detail="nothing on this deck names this Work")
    try:
        release = getattr(request.app.state, "work_release", None)
        step = work_actions.plan(
            action,
            detail.card,
            detail.attention,
            deck_release=bool(release is not None and release.handles(detail.card.work_id)),
            approval=detail.approval,
        )
    except work_actions.Refused as refused:
        return _message_receipt(
            request, text=refused.text, state="refused", status_code=refused.status
        )
    # The window check and the reservation are adjacent synchronous calls with no await
    # between them, so two presses landing together cannot both pass the check.
    # Merge when ready and Withdraw are refused above while they would repeat themselves (a
    # press stands, or none does), and the release refuses a second Merge when ready under the
    # Work's lock, so two at once record one. A person may press either again at once after a
    # change cleared a press or a Withdraw took it back (#3320).
    recent = (
        None
        if action in work_actions.APPROVAL_ACTIONS
        else work_actions.recent_press(db.load_work_actions(ref.work_id), action)
    )
    if recent is not None:
        how = "was sent" if recent.state == "sent" else "may have been sent"
        return _not_again(request, label, how, recent.age_s())
    reserved = db.reserve_work_action(action_id, ref.work_id, action)
    if reserved is None:
        return _message_receipt(
            request,
            text="The deck could not record this action, so it was not sent.",
            state="refused",
            status_code=409,
        )
    if not reserved:
        pressed = db.work_action(action_id)
        if pressed is not None:
            return _pressed_receipt(request, pressed)
        return _message_receipt(
            request, text=f"{label} was already pressed.", state="refused", status_code=409
        )
    outcome, receipt = await _write(request, step, label, action_id)
    if outcome == "uncertain":
        logger.warning("work action %s on %s is uncertain: %s", action, ref.work_id, receipt)
        return _message_receipt(
            request,
            text=f"{receipt}, so it will not be sent again.",
            state="uncertain",
            status_code=202,
        )
    db.settle_work_action(action_id, outcome, receipt)
    if outcome == "failed":
        return _message_receipt(
            request, text=f"{label} was not sent: {receipt}", state="refused", status_code=409
        )
    if action == work_actions.CLOSE:
        # Closing it here is the person's sign-off too, so it needs no Finish after (#2510).
        if db.finish_work(ref.work_id, datetime.now(UTC)):
            _release_worktrees(request, ref.work_id)
    if action in {work_actions.PAUSE, work_actions.RESUME, work_actions.START}:
        if action == work_actions.RESUME:
            # One Resume ends every pause the Work had, the label's and this deck's (#2559).
            db.resume_work(ref.work_id)
        service = getattr(request.app.state, "work_overview", None)
        if service is not None:
            try:
                # The row moves to or from Paused on this press, not the next pass.
                await service.refresh()
            except Exception:  # noqa: BLE001 -- the pause is made; the next pass shows it
                logger.exception("work overview refresh after %s of %s failed", action, ref.work_id)
    logger.info("work action %s on %s: %s", action, ref.work_id, receipt)
    return _message_receipt(request, text=receipt, state="sent")


def _new_work_receipt(
    request: Request,
    *,
    text: str,
    state: str,
    url: str = "",
    status_code: int = 200,
    chat_url: str = "",
) -> HTMLResponse:
    response = get_templates(request).TemplateResponse(
        request,
        "partials/new_work_receipt.html",
        {"text": text, "state": state, "url": url, "chat_url": chat_url},
    )
    response.status_code = status_code
    if state == "sent":
        # What `new_work.js` clears the form on: only a filing known to have landed.
        response.headers["X-AgentDeck-New-Work"] = "filed"
    return response


def _new_work_targets(request: Request) -> tuple:
    """Every repository a new Work may be filed in, from the Poller boards this deck reads."""
    from .. import new_work

    read_board = getattr(getattr(request.app.state, "issues", None), "read_board", None)
    board = read_board() if read_board is not None else None
    return new_work.targets(board) if board is not None else ()


def new_work_form(
    request: Request, *, session_key: str = "", issues=(), repository: str | None = None
) -> dict | None:
    """What the New work form offers (#2415), or None when no Poller repository is known.

    A chat's own repository is its default when a Poller manages it (#3171), then the
    first of its registered issues that can take one.
    """
    from .. import guess, new_work

    options = _new_work_targets(request)
    if not options:
        return None
    current = new_work.target_for(options, repository)
    return {
        "targets": options,
        "default": current.repository if current else new_work.default_repository(options, issues),
        "session_key": session_key,
        "max_files": get_config(request).inject.max_images,
        # The front page's form guesses its project from what is typed (#3367).
        "guess_project": not session_key and guess.guessing_enabled(request.app),
    }


def _new_work_chat_url(request: Request, session_key: str) -> str:
    """The public link to the chat a new Work was filed from, or nothing."""
    from ..public_origin import public_url

    state = get_state(request)
    if not session_key or session_key not in state.sessions:
        return ""
    return public_url(state.chat_path_for_session(session_key))


def _new_work_chat_link(request: Request, issue_url: str, labels) -> str:
    """``/works/<id>/chat`` for a filed issue whose Work the deck starts itself, else ""."""
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)", issue_url or "")
    router_service = getattr(request.app.state, "work_router", None)
    if match is None or router_service is None:
        return ""
    if not router_service.starts_on(match.group(1), labels):
        return ""
    return work_href(WorkRef(match.group(1), int(match.group(2))).work_id) + "/chat"


#: Early starts finishing after their receipt was sent, held so none is collected (#3590).
_EARLY_STARTS: set[asyncio.Task] = set()
#: The Works whose early start is still being bound: their chat link waits rather than
#: sending the person to a Work page that cannot show the chat yet.
_EARLY_BINDING: set[str] = set()


async def _settle_early_start(
    app: Any,
    target: Any,
    early_task: asyncio.Task,
    outcome: Any,
    labels: list[str],
    reserved: bool,
    filed_at: float,
) -> None:
    """After GitHub answered: bind the early start to its Work, or stop it (#3590).

    ``reserved``: the route already reserved the Work's start for this press, so neither a
    webhook nor the backstop starts a second agent while this waits for the spawn.
    """
    from .. import work_start

    router_service = getattr(app.state, "work_router", None)
    number = outcome.url.rsplit("/", 1)[-1] if outcome.state == "done" else ""
    binding = WorkRef(target.repository, int(number)).work_id if number.isdigit() else ""
    try:
        early = await early_task

        def start_usual() -> None:
            if router_service is not None and number.isdigit():
                router_service.note_filed(target.repository, int(number), labels)

        if early is None:
            if reserved:
                # Nothing started early: free the start it held, for the usual one.
                start_id = start_action_id(binding)
                app.state.control_db.settle_work_action(start_id, "failed", "no early start")
            start_usual()
        elif number.isdigit():
            await work_start.bind_before_issue(
                app,
                early,
                WorkRef(target.repository, int(number)),
                url=outcome.url,
                labels=labels,
                on_refused=start_usual,
                reserved=reserved,
                filed_at=filed_at,
            )
        else:
            await work_start.stop_before_issue(
                app, early, outcome.text, maybe=outcome.state == "uncertain"
            )
    except Exception:  # noqa: BLE001 - the filing stands; the backstop starts a missed Work
        logger.exception("new work: the early start in %s could not be settled", target.repository)
    finally:
        _EARLY_BINDING.discard(binding)


async def _early_chat_link(early_task: asyncio.Task | None) -> str:
    """The stable link to an early start's chat, by its token, or "" when there is none."""
    from .routes_actions import starting_chat_path

    if early_task is None:
        return ""
    try:
        early = await asyncio.wait_for(asyncio.shield(early_task), EARLY_LINK_WAIT_S)
    except Exception:  # noqa: BLE001 - slow or failed: the issues link is still there
        return ""
    return starting_chat_path(early.token) if early is not None else ""


#: How long an uncertain filing's receipt waits for its early start to name a token.
EARLY_LINK_WAIT_S = 10.0


def _settle_later(
    app: Any,
    target: Any,
    early_task: asyncio.Task | None,
    outcome: Any,
    labels: list[str],
    guard: contextlib.AbstractContextManager,
) -> None:
    """Hold the router no longer than the create (D19), then settle an early start off the
    request, so its receipt is not held up.

    The new Work's start is reserved for this press first, still under the hold: from then
    on the per-Work reservation, not the repository-wide hold, keeps a second agent away.
    """
    reserved = False
    filed_at = time.monotonic()
    number = outcome.url.rsplit("/", 1)[-1] if outcome.state == "done" else ""
    try:
        if early_task is not None and number.isdigit():
            from .. import work_actions as actions

            work_id = WorkRef(target.repository, int(number)).work_id
            _EARLY_BINDING.add(work_id)
            reserved = bool(
                app.state.control_db.reserve_work_action(
                    start_action_id(work_id), work_id, actions.NEW_CHAT
                )
            )
    finally:
        guard.__exit__(None, None, None)
    if early_task is None:
        return
    settling = asyncio.create_task(
        _settle_early_start(app, target, early_task, outcome, labels, reserved, filed_at)
    )
    _EARLY_STARTS.add(settling)
    settling.add_done_callback(_EARLY_STARTS.discard)


@router.post("/works/new/warm", status_code=204)
async def warm_new_work(request: Request) -> None:
    """A New work form is open on this repository: keep a Work spare booted for it (#3590)."""
    from .. import new_work
    from . import routes_actions

    routes_actions._require_same_origin(request)
    form = await request.form()
    target = new_work.target_for(_new_work_targets(request), form.get("repository"))
    spares = getattr(request.app.state, "start_spares", None)
    router_service = getattr(request.app.state, "work_router", None)
    if (
        target is None
        or spares is None
        or router_service is None
        or not router_service.starts_on(target.repository, [target.ownership_label])
    ):
        return
    spares.want_work(target.repository)


@router.post("/works/new/guess-project")
async def guess_new_work_project(request: Request) -> JSONResponse:
    """Haiku's pick of a New work's project from what was typed (#3367).

    The answer is always one of the form's own repositories, or nothing: the page moves
    the picker only to a repository it already offers, and only until it is picked by hand.
    """
    from .. import guess
    from . import routes_actions

    routes_actions._require_same_origin(request)
    form = await request.form()
    text = str(form.get("text") or "").strip()
    options = _new_work_targets(request)
    if len(text) < guess.PROJECT_GUESS_MIN_CHARS or len(options) < 2:
        return JSONResponse({"repository": ""})
    if not guess.guessing_enabled(request.app):
        return JSONResponse({"repository": ""})
    reply = await guess.ask(
        request.app,
        guess.project_prompt(text, [(item.repository, item.project) for item in options]),
    )
    chosen = guess.chosen_repository(reply, (item.repository for item in options))
    return JSONResponse({"repository": chosen or ""})


@router.post("/works/new", response_class=HTMLResponse)
async def file_new_work(request: Request) -> HTMLResponse:
    """File a new Work's issue at once, from the front page or a chat (#2415).

    The press is reserved under its client action id before anything reaches GitHub, as a
    Work action is: a replay answers with what that press did, and a filing that ended
    without proof either way is never made again by the deck. Attachments are uploaded to
    the project's evidence repository first, so a failed upload files nothing.
    """
    from .. import new_work
    from ..review_refs import first_line_title
    from . import routes_actions
    from .uploads import MAX_MULTIPART_OVERHEAD_BYTES

    routes_actions._require_same_origin(request)
    config = get_config(request).inject

    def refuse(text: str, status_code: int = 422) -> HTMLResponse:
        return _new_work_receipt(
            request, text=f"Not filed: {text}", state="refused", status_code=status_code
        )

    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        return refuse("the form was not sent as a form.", 415)
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > (
        config.max_image_total_bytes + MAX_MULTIPART_OVERHEAD_BYTES
    ):
        return refuse("the attachments are too large.", 413)
    try:
        form = await request.form(
            max_files=config.max_images, max_fields=16, max_part_size=config.max_image_bytes
        )
    except Exception as exc:  # noqa: BLE001 - starlette raises its own for each limit
        return refuse(f"the form could not be read ({exc}).")
    action_id = str(form.get("client_action_id") or "")
    if not _ACTION_ID_RE.fullmatch(action_id):
        return refuse("the press carried no usable action ID.")
    target = new_work.target_for(_new_work_targets(request), form.get("repository"))
    if target is None:
        return refuse("choose a repository a Poller on this host manages.")
    raw = form.get("message")
    text = raw.strip() if isinstance(raw, str) else ""
    title = first_line_title(text)
    if not title:
        return refuse("write what the Work is about.")
    limit = new_work.max_text_chars(config.max_message_chars)
    if len(text) > limit:
        return refuse(f"the text is longer than {limit} characters.")
    try:
        attachments = await new_work.read_attachments(
            form,
            max_files=config.max_images,
            max_bytes=config.max_image_bytes,
            max_total=config.max_image_total_bytes,
        )
    except new_work.Refused as exc:
        return refuse(f"{exc}.")
    if attachments and not target.evidence_repository:
        return refuse(f"{target.project} has no evidence repository to hold files.")
    work_id = f"new-work:{target.repository}"
    spec_only = form.get("spec_only") == "1"
    labels = new_work.labels_for(target, spec_only)
    db = get_control_db(request)
    earlier = db.work_action(action_id)
    if earlier is not None:
        if earlier.work_id != work_id or earlier.action != work_actions.NEW_WORK:
            return refuse("that action ID was already used for something else.", 409)
        return _new_work_replay(request, earlier, labels)
    reserved = db.reserve_work_action(action_id, work_id, work_actions.NEW_WORK)
    if reserved is None:
        return refuse("the deck could not record this press.", 409)
    if not reserved:
        pressed = db.work_action(action_id)
        if pressed is not None:
            return _new_work_replay(request, pressed, labels)
        return refuse("that press was already made.", 409)
    router_service = getattr(request.app.state, "work_router", None)
    early_task: asyncio.Task | None = None
    early = bool(router_service is not None and router_service.starts_on(target.repository, labels))
    # The agent starts while GitHub creates the issue (#3590); the router waits for it.
    guard = router_service.filing(target.repository) if early else contextlib.nullcontext()
    guard.__enter__()

    def start_early(body: str) -> None:
        nonlocal early_task
        if not early:
            return
        from .. import work_start

        early_task = asyncio.create_task(
            work_start.start_before_issue(
                request.app, target.repository, request=body, press=action_id
            )
        )

    try:
        outcome = await new_work.file(
            target,
            title=title,
            text=text,
            spec_only=spec_only,
            folder=action_id,
            attachments=attachments,
            chat_url=_new_work_chat_url(request, request.query_params.get("from", "")),
            on_body=start_early,
            assignee=own_assignee(
                target.repository, getattr(request.app.state, "deck_claims", None)
            ),
        )
    except BaseException:
        # Cut short (a deploy, a crash in the filing): an early agent only waits.
        cut = new_work.Outcome("uncertain", "the filing was cut short")
        _settle_later(request.app, target, early_task, cut, labels, guard)
        raise
    _settle_later(request.app, target, early_task, outcome, labels, guard)
    issues_url = f"https://github.com/{target.repository}/issues"
    if outcome.state == "uncertain":
        # Left unsettled, as a Work action's uncertain write is: the table holds only
        # `done` and `failed`, and an unsettled press answers every replay as maybe-sent.
        logger.warning("new work in %s is uncertain: %s", target.repository, outcome.text)
        return _new_work_receipt(
            request,
            text=(
                f"May have been filed ({outcome.text}) — check the issues before filing again"
                + ("; its agent was told to wait" if early_task is not None else "")
            ),
            state="uncertain",
            url=issues_url,
            status_code=202,
            # The waiting agent's own chat, to give it the number or stop it (D20).
            chat_url=await _early_chat_link(early_task),
        )
    db.settle_work_action(action_id, outcome.state, outcome.url or outcome.text)
    if outcome.state == "failed":
        if early_task is None:
            return refuse(f"{outcome.text.rstrip('.')}.", 409)
        # Stopping runs in the background: say so, and link the chat to see it stop.
        return _new_work_receipt(
            request,
            text=f"Not filed: {outcome.text.rstrip('.')} — its agent is being stopped",
            state="refused",
            status_code=409,
            chat_url=await _early_chat_link(early_task),
        )
    logger.info("new work filed: %s", outcome.url)
    # Started now rather than when GitHub's delayed webhook arrives (#3371): early, or the
    # usual way once the issue exists when the early start could not be made.
    starting = early_task is not None
    return _new_work_receipt(
        request,
        text=(
            f"{outcome.text} — its Work is starting"
            if starting
            else f"{outcome.text} — the Work card appears after the Poller's next wake"
        ),
        state="sent",
        url=outcome.url,
        chat_url=_new_work_chat_link(request, outcome.url, labels) if starting else "",
    )


def _new_work_replay(request: Request, press: WorkActionPress, labels) -> HTMLResponse:
    """The answer to a New work press whose action id was already used."""
    if press.state == "sent":
        return _new_work_receipt(
            request,
            text="Already filed",
            state="sent",
            url=press.result,
            chat_url=_new_work_chat_link(request, press.result, labels),
        )
    if press.state == "failed":
        return _new_work_receipt(
            request,
            text=f"Not filed: {press.result}",
            state="refused",
            status_code=409,
        )
    return _new_work_receipt(
        request,
        text="New work may already have been filed, so it was not filed again",
        state="uncertain",
        status_code=202,
    )


def new_chat_accounts(request: Request) -> list:
    """The accounts a side chat may start on: exactly the ones the new-chat form offers."""
    from ..providers import PROVIDERS
    from .deps import get_accounts

    config = getattr(request.app.state.config, "inject", None)
    if config is None or not config.enabled:
        return []
    return [
        account
        for account in get_accounts(request)
        if PROVIDERS[account.provider_id].offers_new_session(account)
    ]


def _local_checkout_candidates(request: Request) -> list[str]:
    """Directories on this host that might be a checkout of some repository: the new-chat
    form's configured and remembered folders, then every folder a chat has run in."""
    from .deps import get_db

    state = get_state(request)
    found = [
        request.app.state.config.inject.new_chat_default_cwd or "",
        get_db(request).load_manual_new_chat_cwd() or "",
    ]
    found.extend(sorted({str(item.cwd) for item in state.all_sessions() if item.cwd}))
    return found


async def _new_chat_replay(request: Request, press: WorkActionPress) -> HTMLResponse:
    """A New chat press whose action id was already used: what that press did, never a
    second chat."""
    from .routes_actions import _render_start_accepted

    if press.state == "failed":
        return _message_receipt(
            request,
            text=f"That press did not start a chat: {press.result}. Press Start again to try."
            if press.result
            else "That press did not start a chat. Press Start again to try.",
            state="refused",
            status_code=409,
        )
    return _started(
        await _render_start_accepted(request, press.action_id, result_id=NEW_CHAT_RESULT_ID)
    )


def _started(response: HTMLResponse) -> HTMLResponse:
    """Mark an answer that names this press's chat, so the page closes the box."""
    response.headers["X-AgentDeck-New-Chat"] = "started"
    return response


# The slot a side chat's start is watched in; ``routes_actions._START_RESULT_IDS`` knows it.
NEW_CHAT_RESULT_ID = "work-new-chat-result"


@router.post("/works/{work_id:path}/new-chat", response_class=HTMLResponse)
async def work_new_chat(request: Request, work_id: str) -> HTMLResponse:
    """Start a side chat for this Work (#2388): a member of the Work that is never a Run.

    It runs in the Work's own checkout, else in a local checkout of its repository, and is
    refused when there is neither. The press's action id is reserved first and is also the
    new chat's session token, so a replay of the same id answers with the chat that press
    started and never spawns another. The Work, and the pull requests it shows now, are
    saved under that id before the spawn; the collector links the chat once it exists
    (``AppState.finalize_work_side_chats``).
    """
    from ..providers import PROVIDERS
    from ..work_side_chat import (
        first_message,
        local_repository_checkout,
        work_checkout,
        work_pull_requests,
    )
    from . import routes_actions
    from .account_settings import saved_new_session_defaults
    from .deps import get_injector

    routes_actions._require_same_origin(request)
    ref, _ = _resolve(work_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="not a Work id")
    form = await request.form()
    text = str(form.get("message") or "").strip()
    account_key = str(form.get("account_key") or "")
    action_id = str(form.get("client_action_id") or "")
    if not _ACTION_ID_RE.fullmatch(action_id):
        return _message_receipt(
            request,
            text="No chat was started: the press carried no usable action ID.",
            state="refused",
            status_code=422,
        )
    db = get_control_db(request)
    earlier = db.work_action(action_id)
    if earlier is not None:
        if earlier.work_id != ref.work_id or earlier.action != work_actions.NEW_CHAT:
            return _message_receipt(
                request,
                text="That action ID was already used for something else; no chat was started.",
                state="refused",
                status_code=409,
            )
        return await _new_chat_replay(request, earlier)
    if not text:
        return _message_receipt(
            request, text="Write what the new chat should do.", state="refused", status_code=422
        )
    account = next(
        (item for item in new_chat_accounts(request) if item.key == account_key), None
    )
    if account is None:
        return _message_receipt(
            request,
            text="That account cannot start a chat here; no chat was started.",
            state="refused",
            status_code=422,
        )
    provider = PROVIDERS[account.provider_id]
    if not provider.can_start_session(account):
        # Listed but not reachable this second, as while the runtime restarts (#2975):
        # refused before the press is recorded, so the same press works once it is back.
        return _message_receipt(
            request,
            text="That account's runtime is not reachable right now (it may be restarting); "
            "try again in a minute. No chat was started.",
            state="refused",
            status_code=409,
        )
    detail = await load_work_detail(request.app, ref)
    if detail is None:
        raise HTTPException(status_code=404, detail="nothing on this deck names this Work")
    if detail.card.paused:
        # A paused Work starts nothing new, a chat of its own included (#2559).
        return _message_receipt(
            request,
            text="This Work is paused, so no chat was started; resume it first.",
            state="refused",
            status_code=409,
        )
    folder = work_checkout(detail.workspace, ref) or await asyncio.to_thread(
        local_repository_checkout, ref, _local_checkout_candidates(request)
    )
    if not folder:
        return _message_receipt(
            request,
            text=f"No checkout of {ref.repository} on this host, so no chat was started.",
            state="refused",
            status_code=409,
        )
    pulls = work_pull_requests(detail.workspace)
    issue = IssueRef.parse(detail.card.source_label) or IssueRef(ref.repository, ref.number)
    message = first_message(issue, pulls, text)
    refusal = provider.user_message_refusal(message)
    if refusal is not None:
        return _message_receipt(
            request, text=f"Not started: {refusal}", state="refused", status_code=422
        )
    defaults = saved_new_session_defaults(request, account, provider)
    # No "not again so soon" window, unlike the Work's actions: a new id is a deliberate new
    # chat, and the same id is always the same one.
    reserved = db.reserve_work_action(action_id, ref.work_id, work_actions.NEW_CHAT)
    if reserved is None:
        return _message_receipt(
            request,
            text="The deck could not record this press, so no chat was started.",
            state="refused",
            status_code=409,
        )
    if not reserved:
        pressed = db.work_action(action_id)
        if pressed is not None:
            return await _new_chat_replay(request, pressed)
        return _message_receipt(
            request, text="That press was already made.", state="refused", status_code=409
        )
    if not db.record_work_side_chat(action_id, ref.work_id, [pull.url for pull in pulls]):
        db.settle_work_action(action_id, "failed", "the deck could not save its Work link")
        return _message_receipt(
            request,
            text="The deck could not save this chat's Work link, so no chat was started.",
            state="refused",
            status_code=409,
        )
    try:
        result = await get_injector(request).start_new(
            account,
            provider,
            Path(folder),
            message,
            model=defaults.model,
            effort=defaults.effort,
            client_action_id=action_id,
            session_token=action_id,
            allow_over_budget=True,
            origin="work",
        )
    except Exception as exc:  # noqa: BLE001 - the spawn may have begun; never repeat it
        logger.exception("side chat for %s did not report its start", ref.work_id)
        return _message_receipt(
            request,
            text=f"The start did not answer ({exc}); it may have begun, so it was not repeated.",
            state="uncertain",
            status_code=202,
        )
    if not result.accepted:
        reason = result.reason or "the start was refused"
        db.settle_work_action(action_id, "failed", reason)
        db.delete_work_side_chat(action_id)
        return _message_receipt(
            request, text=f"Not started: {reason}", state="refused", status_code=409
        )
    db.settle_work_action(action_id, "done", f"Started a side chat in {folder}")
    logger.info("side chat for %s started in %s on %s", ref.work_id, folder, account.key)
    return _started(
        await routes_actions._render_start_accepted(
            request, action_id, result_id=NEW_CHAT_RESULT_ID
        )
    )


@router.post("/works/seen", response_class=HTMLResponse)
async def works_seen(request: Request) -> HTMLResponse:
    """Mark Works read from the top bar's list without opening them (#3246).

    The same record opening a Work's page writes, so the front page's new mark goes too.
    The reply is the list again without them; the rebuild this starts pushes the rest.
    """
    from . import routes_actions

    routes_actions._require_same_origin(request)
    form = await request.form()
    seen = []
    for value in form.getlist("work_id"):
        ref, _ = _resolve(str(value))
        if ref is not None and ref.work_id not in seen:
            seen.append(ref.work_id)
    for work_id in seen:
        note_work_opened(request.app, work_id)
    return get_templates(request).TemplateResponse(
        request, "partials/needs_you_menu.html", {"seen_now": seen}
    )


@router.post("/works/{work_id:path}/visibility", response_class=HTMLResponse)
async def work_visibility(request: Request, work_id: str) -> HTMLResponse:
    """Hide a Work from Needs you, or bring it back (#2238).

    This deck's own display preference: nothing on GitHub, the board or the Work changes.
    Hide is offered while the Work is in Needs you and records the marks it is compared
    against (``work_hidden``); hiding again re-marks it and bringing back an unhidden Work
    does nothing, so a replay is harmless. The overview is rebuilt straight away, so the
    row leaves or rejoins Needs you on this press rather than the next pass.
    """
    from . import routes_actions

    routes_actions._require_same_origin(request)
    ref, _ = _resolve(work_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="not a Work id")
    form = await request.form()
    action = str(form.get("action") or "").strip()
    db = get_control_db(request)
    if action == HIDE:
        detail = await load_work_detail(request.app, ref, workspace=False)
        if detail is None:
            raise HTTPException(status_code=404, detail="nothing on this deck names this Work")
        attention = detail.attention
        # A Work waiting for Finish is done on GitHub but still in Needs you (#2510).
        finished = detail.status.tone == DONE and not detail.card.awaiting_finish
        if not finished and (attention is None or HIDE not in attention.actions):
            return _message_receipt(
                request,
                text="Only a Work that needs you, or one that finished, can be hidden.",
                state="refused",
                status_code=409,
            )
        done = db.hide_work(ref.work_id, detail.card.comments)
        text = (
            "Archived from Recently finished."
            if finished
            else "Hidden from Needs you until its issue gets a comment or its card moves."
        )
    elif action == UNHIDE:
        done = db.unhide_work(ref.work_id)
        text = "Back in Needs you, if it still needs you."
    elif action == FINISH:
        # A person's sign-off on a Work whose issue closed (#2510): this deck's record only.
        detail = await load_work_detail(request.app, ref, workspace=False)
        if detail is None:
            raise HTTPException(status_code=404, detail="nothing on this deck names this Work")
        if not detail.card.awaiting_finish:
            return _message_receipt(
                request,
                text="Only a Work whose issue closed can be finished; nothing changed.",
                state="refused",
                status_code=409,
            )
        # Finishing leaves an open pull request alone, so it asks once, naming it (#3187).
        confirmed = set(str(form.get("confirmed") or "").casefold().split())
        unasked = [
            output
            for output in detail.card.outputs
            if output.is_open and output.label.casefold() not in confirmed
        ]
        if unasked:
            return _message_receipt(
                request,
                text=finish_question(unasked) + " Nothing changed yet.",
                state="refused",
                status_code=409,
            )
        done = db.finish_work(ref.work_id, datetime.now(UTC))
        if done:
            # A finished Work belongs in Recently finished, not on the Hidden list.
            db.unhide_work(ref.work_id)
            _release_worktrees(request, ref.work_id)
        text = "Finished. It moves to Recently finished."
    else:
        return _message_receipt(
            request, text="That is not a visibility action.", state="refused", status_code=422
        )
    if not done:
        return _message_receipt(
            request,
            text="The deck could not record that; nothing changed.",
            state="refused",
            status_code=409,
        )
    service = getattr(request.app.state, "work_overview", None)
    if service is not None:
        try:
            await service.refresh()
        except Exception:  # noqa: BLE001 -- the change is recorded; the next pass shows it
            logger.exception("work overview refresh after %s of %s failed", action, ref.work_id)
    return _message_receipt(request, text=text, state="sent")


async def _star(request: Request, work_id: str, starred: bool) -> str | None:
    """Star or unstar a Work (#3116); its canonical id, or None when not recorded.

    A star only reorders: the merge queue moves a newly starred Work's queued pulls up behind
    the other starred ones at once, and every other line reads the star when it next picks.
    Unstarring moves nothing back.
    """
    ref, _ = _resolve(work_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="not a Work id")
    if not get_control_db(request).star_work(ref.work_id, starred):
        return None
    release = getattr(request.app.state, "work_release", None)
    if starred and release is not None:
        try:
            release.star(ref.work_id)
        except Exception:  # noqa: BLE001 -- the star is recorded; the next join honours it
            logger.warning("merge queue move for starred %s failed", ref.work_id, exc_info=True)
    service = getattr(request.app.state, "work_overview", None)
    if service is not None:
        try:
            await service.refresh()
        except Exception:  # noqa: BLE001 -- the star is recorded; the next pass shows it
            logger.exception("work overview refresh after starring %s failed", ref.work_id)
    return ref.work_id


@router.post("/works/{work_id:path}/star", response_class=HTMLResponse)
async def work_star(request: Request, work_id: str) -> HTMLResponse:
    """The Work page's Star and Unstar (#3116): this deck's record only."""
    from . import routes_actions

    routes_actions._require_same_origin(request)
    form = await request.form()
    action = str(form.get("action") or "").strip()
    if action not in (STAR, UNSTAR):
        return _message_receipt(
            request, text="That is not a star action.", state="refused", status_code=422
        )
    if await _star(request, work_id, action == STAR) is None:
        return _message_receipt(
            request,
            text="The deck could not record that; nothing changed.",
            state="refused",
            status_code=409,
        )
    text = (
        "Starred: it goes ahead in the merge queue, the job queue and review starts."
        if action == STAR
        else "Unstarred: it waits its normal turn from now on."
    )
    return _message_receipt(request, text=text, state="sent")


@router.put("/api/works/{work_id:path}/star")
async def put_work_star(request: Request, work_id: str) -> JSONResponse:
    """Star a Work for an agent asked to (#3116); repeating it changes nothing."""
    return await _star_json(request, work_id, True)


@router.delete("/api/works/{work_id:path}/star")
async def delete_work_star(request: Request, work_id: str) -> JSONResponse:
    """Unstar a Work (#3116); unstarring an unstarred Work changes nothing."""
    return await _star_json(request, work_id, False)


async def _star_json(request: Request, work_id: str, starred: bool) -> JSONResponse:
    found = await _star(request, work_id, starred)
    if found is None:
        raise HTTPException(status_code=409, detail="the deck could not record the star")
    return JSONResponse({"work_id": found, "starred": starred})


# A wayfinder map's moved boxes (#3627): each key at most this long, each place this near.
MAP_LAYOUT_KEY = 300
MAP_LAYOUT_FAR = 100_000.0


@router.put("/api/works/{work_id:path}/map-layout")
async def put_map_layout(request: Request, work_id: str) -> JSONResponse:
    """Move one of a wayfinder map's boxes, or put them all back (#3627).

    ``{"box": "<box key>", "at": [x, y]}`` moves one box, ``"at": null`` puts it back in the
    server's layout, and ``{"reset": true}`` puts every box back. One box per request, so
    two devices moving different boxes never erase each other's move. One layout per map:
    every device draws the same. ``wayfinder_map.js`` sends it when a drag ends."""
    ref, _ = _resolve(work_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="not a Work id")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="the body is not JSON") from None
    control = getattr(request.app.state, "control_db", None)
    if isinstance(body, dict) and body.get("reset") is True:
        done = control is not None and control.reset_wayfinder_layout(ref.work_id)
    else:
        key = body.get("box") if isinstance(body, dict) else None
        at = body.get("at") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key or len(key) > MAP_LAYOUT_KEY:
            raise HTTPException(status_code=400, detail="box must name one box")
        if at is not None and not (
            isinstance(at, list)
            and len(at) == 2
            and all(
                isinstance(v, int | float)
                and not isinstance(v, bool)
                and math.isfinite(v)
                and abs(v) <= MAP_LAYOUT_FAR
                for v in at
            )
        ):
            raise HTTPException(status_code=400, detail="at must be [x, y] or null")
        place = (round(float(at[0]), 1), round(float(at[1]), 1)) if at is not None else None
        done = control is not None and control.move_wayfinder_box(ref.work_id, key, place)
    if not done:
        raise HTTPException(status_code=409, detail="the deck could not save the layout")
    get_state(request).bus.publish(WORKS_TOPIC)
    moved = control.wayfinder_layout(ref.work_id)
    return JSONResponse({"work_id": ref.work_id, "boxes": len(moved)})


@router.get("/api/works/{work_id:path}/question")
async def get_work_question(request: Request, work_id: str) -> JSONResponse:
    """The Work's open questions, each with how to answer it (#3481).

    A question box (``answer_with: question_box``) is answered only by ``POST``ing to
    its ``answer_url``; a chat message reaches the chat but leaves the box open, so the
    Work keeps showing Needs answer. A question asked in a reply has no box, and a
    message to the chat is its answer (``answer_with: message``).
    """
    ref, _ = _resolve(work_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="not a Work id")
    service = getattr(request.app.state, "work_overview", None)
    overview = await service.current() if service is not None else None
    card = next(
        (card for card in (overview.cards if overview else ()) if card.work_id == ref.work_id),
        None,
    )
    questions = []
    boxes: set[str] = set()

    def listed(key, title, question, interaction) -> None:
        if interaction is not None:
            boxes.add(key)
        questions.append(
            {
                "session_key": key,
                "chat_title": title,
                "question": question,
                "answer_with": "question_box" if interaction is not None else "message",
                "interaction": interaction.answer_json() if interaction is not None else None,
                "answer_url": (
                    f"/api/sessions/{key}/interaction"
                    if interaction is not None
                    else f"/sessions/{key}/inject"
                ),
            }
        )

    for item in waiting_attention(card) if card is not None else ():
        if not item.asks:
            continue
        interaction = None
        if item.interaction:
            try:
                account, session, provider = resolve_current_leg(request, item.session_key)
            except HTTPException:
                continue
            interaction = provider.pending_interaction(account, session)
            if interaction is None:
                continue
        key = item.session_key if interaction is None else session.key
        listed(key, item.chat_title, item.question, interaction)
    # A deck-run Work reads Needs answer from its owning chat's box itself (``derived_for``),
    # whether or not Deckhand has a card for that chat, so the box is read the same way (#3503).
    owner = _owner_box(request, ref) if card is not None else None
    if owner is not None and owner[0].key not in boxes:
        session, interaction = owner
        asked = next((q.prompt for q in interaction.questions if q.prompt.strip()), "")
        question = (asked or interaction.message or interaction.title or "").strip()
        listed(session.key, session.display_title or session.key, question, interaction)
    return JSONResponse(
        {"work_id": ref.work_id, "listed": card is not None, "questions": questions},
        headers={"Cache-Control": "no-cache"},
    )


def _owner_box(request: Request, ref: WorkRef) -> tuple[Any, Any] | None:
    """The Work's owning chat's working leg and its open interaction, else ``None``."""
    from ..work_router import owner_of_issue

    state = get_state(request)
    owner = owner_of_issue(state, ref.repository, ref.number)
    chat = state.chat_by_id(owner[0]) if owner is not None else None
    leg = state.current_session_for_chat(chat) if chat is not None else None
    leg = leg or (state.sessions.get(owner[0]) if owner is not None else None)
    if leg is None:
        return None
    try:
        account, session, provider = resolve_current_leg(request, leg.key)
    except HTTPException:
        return None
    interaction = provider.pending_interaction(account, session)
    return (session, interaction) if interaction is not None else None


@router.post("/works/{work_id:path}/title", response_class=HTMLResponse)
async def work_rename(request: Request, work_id: str) -> HTMLResponse:
    """Give a Work a name of its own on this deck, or clear it (#2328).

    The name is stored with the Work (``work_metadata``) and shown wherever the Work is, the
    front page included. The GitHub issue is never renamed. An empty name, or ``action=clear``,
    restores the derived title. Setting the same name again changes nothing, so a replay is
    harmless.
    """
    from . import routes_actions

    routes_actions._require_same_origin(request)
    ref, _ = _resolve(work_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="not a Work id")
    form = await request.form()
    action = str(form.get("action") or "save").strip()
    title = "" if action == "clear" else normalize_title(form.get("title"))
    detail = await load_work_detail(request.app, ref, workspace=False)
    if detail is None:
        raise HTTPException(status_code=404, detail="nothing on this deck names this Work")
    if not get_control_db(request).set_work_title(ref.work_id, title):
        return _message_receipt(
            request,
            text="The deck could not record that; the name did not change.",
            state="refused",
            status_code=409,
        )
    service = getattr(request.app.state, "work_overview", None)
    if service is not None:
        try:
            await service.refresh()
        except Exception:  # noqa: BLE001 -- the name is recorded; the next pass shows it
            logger.exception("work overview refresh after renaming %s failed", ref.work_id)
    text = f"Renamed to “{title}”." if title else "Back to the automatic title."
    receipt = _message_receipt(request, text=text, state="sent")
    # The new name rides back with the receipt, so it shows at once rather than at the
    # stream's next push: the status section, which holds the heading, is swapped out of band,
    # still bound to the page's stream (#3228).
    renamed = await load_work_detail(request.app, ref)
    if renamed is None:
        return receipt
    status = dict(render_work_sections(get_templates(request), renamed)).get("status", "")
    oob = (
        '<div id="work-sec-status" class="work-sec" sse-swap="work-status" hx-swap="innerHTML"'
        f' data-hold-in-use hx-swap-oob="true">{status}</div>'
    )
    return HTMLResponse(receipt.body.decode() + oob, status_code=receipt.status_code)
