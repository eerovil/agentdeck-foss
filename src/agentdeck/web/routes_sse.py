"""Server-Sent Events: the dashboard's live channel.

One connection carries named events — ``usage``, ``works`` and others — each
whose ``data`` is server-rendered HTML that HTMX swaps into place (``sse-swap``).
``works`` follows the Work overview (#2147). The chat list at ``/chats`` is
rendered once per page load and has no event of its own (#2603).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import mac_runner, work_counter
from ..board_watch import BOARD_TOPIC
from ..chat_board import TOPIC as CHAT_BOARD_TOPIC
from ..chat_notes import TOPIC as CHAT_NOTES_TOPIC
from ..chat_transcript import chat_event_id, parse_chat_event_id, switch_into_leg
from ..models import (
    ACTIVITY_LOOKBACK,
    STALL_S,
    SessionStatus,
    TranscriptEvent,
    transcript_event_is_progress,
)
from ..providers import PROVIDERS
from ..runtime_feed import RUNTIME_TOPIC
from ..state import ALL_CHATS_TOPIC, REVIEWS_TOPIC, chat_topic
from .chat_board_page import chat_board_context
from .deps import (
    get_accounts,
    get_chat_renders,
    get_injector,
    get_shared_fragments,
    get_state,
    get_templates,
    require_access,
    resolve_session,
)
from .render import (
    SESSION_FRAGMENTS,
    SESSION_FRAGMENTS_BY_NAME,
    SessionView,
    render_assistant_session,
    render_chat_board_freshness,
    render_chat_board_rows,
    render_chat_notes,
    render_deployment_status,
    render_job_queue,
    render_leg_switch,
    render_limit_bars,
    render_mac_runner_link,
    render_transcript_events,
    resolve_activity_label,
    session_head_review_chip,
    session_issue_chips,
    session_labels,
    session_pull_request_chips,
)
from .routes_works import WORKS_INDEX_TOPICS, render_works_index, works_index_context
from .work_view import (
    FRONT_PARTS,
    FRONT_ROW_PARTS,
    NEEDS_YOU_TOPIC,
    WORKS_HIDDEN_VIEW,
    WORKS_INDEX_VIEW,
    WORKS_VIEWS,
    SentRows,
    render_work_front,
    render_work_other,
    row_renders,
)

router = APIRouter(dependencies=[Depends(require_access)])
logger = logging.getLogger(__name__)
# The default ``/works`` stream (#3228): at most one list read per this many seconds.
INDEX_MIN_INTERVAL_S = 3.0
# ``/pollers`` (#3228): what moves its rows, and at most one redraw per this many seconds,
# since each reads every instance's state and systemd.
POLLERS_VIEW = "pollers"
POLLERS_TOPICS = (BOARD_TOPIC, RUNTIME_TOPIC)
POLLERS_MIN_INTERVAL_S = 10.0
# The accounts pages (#3228): their own usage figures ride the ``usage`` push.
ACCOUNTS_VIEW = "accounts"
ACCOUNT_VIEW = "account"

HEARTBEAT_S = 15.0
# Brief recency fallback for ambiguous detail-page progress events.
THINKING_OFF_S = 3.0
# A stream that has sent nothing for this long re-sends the usage bars: base.html's
# watchdog reopens a stream that goes 75 s without an event, and a comment is not one.
# The bars come from the shared cache, so this is a keep-alive, not a rebuild.
USAGE_REFRESH_S = 30.0
# How often an open chat page may advance the chat's read marker (#2599). Each
# advance is one durable write and one Deckhand pass, so a streaming turn must
# not cost one per chunk; the last movement still lands within this long.
READ_MARK_INTERVAL_S = 5.0
# A chat page asks the live window to keep its chat for an hour (``ON_DEMAND_S``); it
# renews well inside that.
LIVE_WINDOW_RENEW_S = 900.0
# What a chat page wakes for besides its own chat's topic (ADR 0007, #2865). It never
# wakes on a clock: a change to one of these, or a deadline the page computed itself.
CHAT_PAGE_TOPICS = (
    ALL_CHATS_TOPIC,
    BOARD_TOPIC,
    REVIEWS_TOPIC,
    "assistant",
    "usage",
    "host",
    "deployment",
    "jobs",
    "mac_runner",
    NEEDS_YOU_TOPIC,
    CHAT_NOTES_TOPIC,
)
# Topics that can move a chat page's own fragments; the rest only touch the top bar.
_CHAT_VIEW_TOPICS = frozenset({ALL_CHATS_TOPIC, BOARD_TOPIC, REVIEWS_TOPIC, "assistant", "jobs"})
# Fragments that print the page's own clock (the tool pill's elapsed seconds): built per
# tab, because a tab that connects later must not be handed an earlier tab's count.
_CLOCK_FRAGMENTS = frozenset({"tools"})


# Which publications can move a fragment every attached client renders alike.
# A stream never rebuilds one of these except after such a publication, so a
# shared render keyed on these counters can only ever be handed to a client that
# an uncached deck would have handed the identical bytes (#1357). ``usage``
# carries the account meters and ``host`` the CPU/memory sample beside them.
USAGE_FRAGMENT_TOPICS = ("usage", "host")


def _progress_age_s(event) -> float:
    """Wall-clock age of one progress event; untimestamped live events are new."""
    if event.ts is None:
        return 0.0
    timestamp = event.ts if event.ts.tzinfo is not None else event.ts.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - timestamp).total_seconds())


async def _seed_recent_events(provider, account, session, last_ev) -> list[TranscriptEvent]:
    """Tail the activity badge needs at connect, beyond the last event (#1457).

    Only a tool *result* in last position needs history — it carries no tool
    name of its own, so without the call behind it the badge drops from
    "Running: pytest -q" back to a bare "Using tools" the moment the stream
    takes over from the first paint. Everything else reads from the last event
    alone and pays for no extra transcript read.
    """
    if last_ev is None:
        return []
    if last_ev.tool_name or last_ev.role != "tool":
        return [last_ev]
    detail = await provider.load_transcript(account, session)
    return list(detail.events)[-ACTIVITY_LOOKBACK:] or [last_ev]


def _usage_sig(accounts, state) -> tuple:
    """Cheap change-detector for the usage snapshots (keyed by fetch time), so
    the session stream re-renders the topbar only when a poll lands."""
    out = []
    for a in accounts:
        snap = state.usage.get(a.key)
        out.append((a.key, snap.fetched_at if snap else None))
    host = state.host_stats
    out.append(("host", host.sampled_at if host else None))
    return tuple(out)


def mac_runner_event(shared, templates) -> str:
    """The top bar's Mac icon as an SSE event, or nothing on a deck with no Mac (#2864)."""
    if not mac_runner.configured():
        return ""
    return format_sse(
        "mac_runner",
        shared.get("mac_runner", ("mac_runner",), (), lambda: render_mac_runner_link(templates)),
    )


def needs_you_event(shared, templates) -> str:
    """The top bar's new Needs you list as an SSE event (#3239)."""
    return format_sse(
        NEEDS_YOU_TOPIC,
        shared.get(
            NEEDS_YOU_TOPIC,
            (NEEDS_YOU_TOPIC,),
            (),
            lambda: templates.get_template("partials/needs_you_menu.html").render(),
        ),
    )


def watching_needs_you(request: Request) -> None:
    """A stream showing the list subscribed: the overview it reads is kept current (#3239)."""
    service = getattr(request.app.state, "work_overview", None)
    if service is not None and hasattr(service, "connected"):
        service.connected()


def format_sse(event: str, html: str, *, event_id: int | str | None = None) -> str:
    # SSE data may not contain raw newlines; prefix every line with "data: ".
    body = "".join(f"data: {line}\n" for line in html.splitlines()) or "data: \n"
    identity = f"id: {event_id}\n" if event_id is not None else ""
    return f"event: {event}\n{identity}{body}\n"


async def _stream(request: Request) -> AsyncIterator[str]:
    state = get_state(request)
    templates = get_templates(request)
    accounts = get_accounts(request)
    auth_watch = request.app.state.auth_watch
    shared = get_shared_fragments(request)
    view = request.query_params.get("view", "")
    rows = row_renders(request.app.state)
    # The Work rows this stream's reader holds, by part and Work (#3228): what it sent, and
    # what the page named on connecting (``?sig=``), so a connect or a return from a hidden
    # tab sends only the rows that changed meanwhile. A row the page lost is not named, so a
    # reconnect brings it whole: that is how it heals.
    sent = SentRows(request.query_params.get("sig", ""))
    # The default ``/works`` list's rows, read before each ``works`` render: the read is
    # async, the render is not. Shared by every tab on the same filter.
    index_context: dict | None = None

    async def read_index() -> None:
        nonlocal index_context
        if view == WORKS_INDEX_VIEW:
            try:
                index_context = await works_index_context(
                    request.app,
                    request.query_params.get("repo", ""),
                )
            except Exception:  # noqa: BLE001 -- the page keeps its rows; the next change retries it
                logger.exception("works index read failed")

    async def render_pollers() -> str:
        """The instance rows: read and rendered once per board and runtime change for every
        tab (#3228), a tab arriving mid-read waiting on that one."""
        from .render import poller_context

        async def build() -> str:
            boards = await request.app.state.issues.resolve_instance_boards()
            work_counter.count(work_counter.POLLER_ROWS)
            return templates.get_template("partials/poller_instances.html").render(
                {**poller_context(request, boards=boards), "request": request, "rows_only": True}
            )

        revision = state.bus.revision(*POLLERS_TOPICS)
        cached = getattr(request.app.state, "pollers_rows", None)
        if cached is None or cached[0] != revision or (
            cached[1].done() and cached[1].exception() is not None
        ):
            cached = (revision, asyncio.ensure_future(build()))
            request.app.state.pollers_rows = cached
        return format_sse("pollers", await asyncio.shield(cached[1]))

    def account_usage() -> str:
        """The accounts pages' own usage figures (#3228), from the same rows the top bar's
        meters read, built once per usage change for every tab."""
        from .render import _usage_rows

        if view == ACCOUNTS_VIEW:
            html = shared.get(
                "account-cards",
                USAGE_FRAGMENT_TOPICS,
                (),
                lambda: "".join(
                    format_sse(
                        f"account-usage-{row['account'].key}",
                        templates.get_template("partials/account_card_usage.html").render(
                            row=row
                        ),
                    )
                    for row in _usage_rows(accounts, state, auth_watch)
                ),
            )
            return html
        key = request.query_params.get("key", "")
        account = next((item for item in accounts if item.key == key), None)
        if view != ACCOUNT_VIEW or account is None:
            return ""
        return format_sse(
            "account-capacity",
            shared.get(
                "account-capacity",
                USAGE_FRAGMENT_TOPICS,
                (key,),
                lambda: templates.get_template("partials/account_capacity.html").render(
                    row=_usage_rows([account], state, auth_watch)[0]
                ),
            ),
        )

    def render(topic: str) -> str:
        if topic == "usage" and view in (ACCOUNTS_VIEW, ACCOUNT_VIEW):
            return render("usage-bars") + account_usage()
        if topic in ("usage", "usage-bars"):
            return format_sse(
                "usage",
                shared.get(
                    "usage",
                    USAGE_FRAGMENT_TOPICS,
                    (None,),
                    lambda: render_limit_bars(templates, accounts, state, auth_watch),
                ),
            )
        if topic == "deployment":
            return format_sse(
                "deployment",
                shared.get(
                    "deployment",
                    ("deployment",),
                    (),
                    lambda: render_deployment_status(templates),
                ),
            )
        if topic == "jobs":
            return format_sse(
                "jobs",
                shared.get("jobs", ("jobs",), (), lambda: render_job_queue(templates)),
            )
        if topic == "mac_runner":
            return mac_runner_event(shared, templates)
        if topic == NEEDS_YOU_TOPIC:
            return needs_you_event(shared, templates)
        if topic == "works":
            if view not in WORKS_VIEWS:
                return ""
            # The Work cards and the activity no Work owns (#2147), from the refresher's
            # snapshot alone: nothing here builds or sends the chat list.
            service = getattr(request.app.state, "work_overview", None)
            overview = service.overview if service is not None else None
            if overview is None:
                return ""
            if view == WORKS_HIDDEN_VIEW:
                return format_sse(
                    "works-hidden",
                    render_work_front(templates, overview, "hidden", renders=rows, sent=sent),
                ) + format_sse("works-hidden-count", str(len(overview.front.hidden)))
            if view == WORKS_INDEX_VIEW:
                if index_context is None:
                    return ""
                return "".join(
                    format_sse(event, html)
                    for event, html in render_works_index(templates, index_context, rows, sent)
                )

            # One event per front-page part (#2219), each swapped on its own. A part of rows
            # is this stream's own: each row is rendered once for every tab (``rows``), and
            # one this stream already sent at its revision goes as a placeholder (#3228).
            def part_html(part: str) -> str:
                if part in FRONT_ROW_PARTS:
                    return render_work_front(templates, overview, part, renders=rows, sent=sent)
                return shared.get(
                    f"works-{part}",
                    ("works",),
                    (),
                    lambda: render_work_front(templates, overview, part),
                )

            parts = "".join(format_sse(f"works-{part}", part_html(part)) for part in FRONT_PARTS)
            return parts + format_sse(
                "work-other",
                shared.get(
                    "work-other", ("works",), (), lambda: render_work_other(templates, overview)
                ),
            )
        return ""

    loop = asyncio.get_event_loop()
    # No page streams a chat list any more (#2603): `/chats` is rendered once per load. Only
    # a page that shows Works subscribes to them, so a settings or pollers page open
    # elsewhere neither keeps the overview rebuilding nor receives its rows (#3228).
    topics = ["usage", "host", "deployment", "jobs", "mac_runner", NEEDS_YOU_TOPIC]
    if view in WORKS_VIEWS:
        topics.append("works")
    if view == WORKS_INDEX_VIEW:
        # Its rows read chats and the board the overview may not list (#3228).
        topics.extend(t for t in WORKS_INDEX_TOPICS if t not in topics)
    if view == POLLERS_VIEW:
        topics.extend(t for t in POLLERS_TOPICS if t not in topics)
    index_read_at = 0.0
    pollers_at = 0.0
    # A board or runtime change owes ``/pollers`` a redraw at most every
    # ``POLLERS_MIN_INTERVAL_S``: a deadline the loop wakes for, so nothing else on the
    # stream waits for it (#3228 review).
    pollers_owed = False
    with state.bus.subscribe(*topics) as sub:
        # The overview is only rebuilt while a dashboard watches it (#2865): one that went
        # stale while nobody did is rebuilt now and arrives as a ``works`` push.
        service = getattr(request.app.state, "work_overview", None)
        if view in WORKS_VIEWS and service is not None and hasattr(service, "connected"):
            service.connected()
        # Prime the client with current state on connect. Neither topbar pill
        # is primed: the page render already carries both, and a re-push would
        # collapse a panel someone just opened.
        yield render("usage")
        # A keyed swap: cards the page already shows at this revision stay as they are.
        if "works" in topics:
            await read_index()
            index_read_at = loop.time()
        if works := render("works"):
            yield works
        if view == POLLERS_VIEW:
            # A change between the page render and this subscription is sent; the keyed swap
            # keeps every row that did not move.
            yield await render_pollers()
            pollers_at = loop.time()
        # The Mac icon is a plain link with no panel to collapse. A count that moved
        # between the page render and this subscription is already counted as seen,
        # so it is primed once here or the page could keep a stale icon (#2864).
        if mac := render("mac_runner"):
            yield mac
        # The Needs you list, once, for the same window (#3239): its script keeps an open
        # list open across the swap, so a prime costs nothing a reader can see.
        yield render(NEEDS_YOU_TOPIC)
        last_usage_push = loop.time()
        while True:
            if await request.is_disconnected():
                break
            timeout = HEARTBEAT_S
            if pollers_owed:
                timeout = max(0.0, min(timeout, pollers_at + POLLERS_MIN_INTERVAL_S - loop.time()))
            try:
                topic, _ = await asyncio.wait_for(sub.get(), timeout=timeout)
                dirty = {topic}
                while (item := sub.get_nowait()) is not None:
                    dirty.add(item[0])
            except TimeoutError:
                dirty = set()
            # The queue wakes us; per-topic revisions also retain a rare jobs
            # or deployment update displaced by a burst on another topic.
            dirty.update(sub.changed_topics())
            # Refresh usage on a fixed cadence so the "updated" time keeps ticking
            # even while sessions churn (which would otherwise starve the timeout).
            if loop.time() - last_usage_push >= USAGE_REFRESH_S:
                dirty.add("usage")
            # A fresh host sample re-renders the same topbar fragment the account
            # meters do — and nothing else, which is why it has its own topic (#1310).
            if "host" in dirty:
                dirty.add("usage")
            if view == POLLERS_VIEW and dirty & set(POLLERS_TOPICS):
                pollers_owed = True
            pollers_due = pollers_owed and loop.time() >= pollers_at + POLLERS_MIN_INTERVAL_S
            if not dirty and not pollers_due:
                yield ": ping\n\n"
                continue
            # Keep the queue's own fragment first so its pill still appears/disappears
            # in the first event an already-open dashboard receives (#1301).
            if view == WORKS_INDEX_VIEW and dirty & set(WORKS_INDEX_TOPICS):
                # A working chat writes constantly: the list follows at most every few
                # seconds, and reads only the rows whose Works moved (``_index_rows``).
                wait = index_read_at + INDEX_MIN_INTERVAL_S - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                    while (item := sub.get_nowait()) is not None:
                        dirty.add(item[0])
                    dirty.update(sub.changed_topics())
                dirty.add("works")
                await read_index()
                index_read_at = loop.time()
            for t in ("usage", "jobs", "works", "deployment", "mac_runner", NEEDS_YOU_TOPIC):
                if t in dirty and (fragment := render(t)):
                    yield fragment
            if pollers_due:
                yield await render_pollers()
                pollers_at = loop.time()
                pollers_owed = False
            if "usage" in dirty:
                last_usage_push = loop.time()


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/events/topbar")
@router.get("/events/mac-runner")  # the path before #3239, for a tab open across the deploy
async def topbar_events(request: Request) -> StreamingResponse:
    """The top bar's live icons alone, for a page with no stream of its own (#2864, #3239).

    `/works`, the Work page and the local-file view render no page stream, so the top bar
    connects here itself for the Mac icon and the new Needs you list. Both are primed once
    after subscribing, for the same reason as every other stream: a change that landed
    after the page rendered is already counted as seen.
    """
    state = get_state(request)
    templates = get_templates(request)
    shared = get_shared_fragments(request)

    def icons(topics) -> str:
        out = ""
        if "mac_runner" in topics:
            out += mac_runner_event(shared, templates)
        if NEEDS_YOU_TOPIC in topics:
            out += needs_you_event(shared, templates)
        return out

    async def stream() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        both = ("mac_runner", NEEDS_YOU_TOPIC)
        with state.bus.subscribe(*both) as sub:
            watching_needs_you(request)
            yield icons(both) or ": ping\n\n"
            last_sent = loop.time()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    topic, _ = await asyncio.wait_for(sub.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    # A comment fires no `htmx:sseMessage`, and base.html's watchdog reopens
                    # a stream that has gone 75 s without one. A quiet top bar therefore
                    # re-sends its icons at the usage cadence rather than only pinging (#2864).
                    if loop.time() - last_sent >= USAGE_REFRESH_S and (sent := icons(both)):
                        last_sent = loop.time()
                        yield sent
                    else:
                        yield ": ping\n\n"
                    continue
                dirty = {topic}
                while (item := sub.get_nowait()) is not None:
                    dirty.add(item[0])
                if sent := icons(dirty):
                    last_sent = loop.time()
                    yield sent

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/events/chat-board")
async def chat_board_events(request: Request, signature: str = "") -> StreamingResponse:
    """Push the chat rows when a delivery actually changed one, and never otherwise.

    Two targets on one connection: the rows, and the freshness line. They move apart —
    the hourly GitHub read changes when the board was last checked (and whether that read
    failed) without touching a single row, and that quiet case is the one the line exists
    to answer (#1872). The nudge section is outside both on purpose, so a chat closing on
    GitHub cannot close a nudge row somebody has open on their phone.
    """

    async def stream() -> AsyncIterator[str]:
        state = get_state(request)
        templates = get_templates(request)
        shared = get_shared_fragments(request)
        last_signature = signature
        # Not carried in the page's query string like the rows signature: this text is
        # relative ("3m ago"), so a signature taken at render would differ from the same
        # state a second later and every connection would open on a pointless swap.
        last_freshness: str | None = None

        def fragments(context: dict) -> Iterator[str]:
            nonlocal last_signature, last_freshness
            current = str(context["signature"])
            if current != last_signature:
                last_signature = current
                yield format_sse("chat-board", render_chat_board_rows(templates, context))
            # The cooldown line is part of this fragment's identity (#1945). Left out, a 429
            # recorded while somebody has the board open would only appear on their next
            # deliberate press — which is the press this is there to stop them making.
            fresh = (
                f"{context['freshness']}\n{context['backfill_error']}\n{context['rate_limited']}"
            )
            # The first subscribed pass sends this fragment whatever it says, because
            # nothing here knows what the page was rendered with. A GitHub read landing
            # between that render and this subscription is exactly the window the rows
            # path repairs by carrying its signature, and freshness cannot carry one —
            # so it repairs the same window by re-sending one small paragraph on connect.
            if last_freshness is None or fresh != last_freshness:
                yield format_sse(
                    "chat-board-freshness", render_chat_board_freshness(templates, context)
                )
            last_freshness = fresh

        with state.bus.subscribe(CHAT_BOARD_TOPIC, "mac_runner", NEEDS_YOU_TOPIC) as sub:
            watching_needs_you(request)
            # A delivery can land between the page render and this subscription. Comparing
            # once inside the subscription repairs that without pushing an unchanged
            # fragment — the signature is of what the page shows, not of when it was read.
            for fragment in fragments(chat_board_context(request, with_nudges=False)):
                yield fragment
            # The Mac icon once per connection, for the same window as above (#2864).
            if mac := mac_runner_event(shared, templates):
                yield mac
            yield needs_you_event(shared, templates)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    topic, _ = await asyncio.wait_for(sub.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                # The top bar's Mac icon rides this page's only stream too (#2864).
                if topic == "mac_runner":
                    if mac := mac_runner_event(shared, templates):
                        yield mac
                    continue
                if topic == NEEDS_YOU_TOPIC:
                    yield needs_you_event(shared, templates)
                    continue
                for fragment in fragments(chat_board_context(request, with_nudges=False)):
                    yield fragment

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class _CatchUp(NamedTuple):
    """Everything a reader missed, rendered as one append."""

    html: str
    #: The target leg's own events, for the progress clock.
    events: list
    #: The target leg's byte offset and seq, ready for the live tail.
    offset: int
    seq: int


async def _catch_up_to_leg(
    request: Request,
    *,
    chat_keys: tuple[str, ...],
    from_leg: int,
    from_seq: int,
    to_leg: int,
) -> _CatchUp:
    """Render the gap between a reader's cursor and one leg's newest event.

    ``from_leg == to_leg`` is the ordinary reconnect: replay that leg after the
    seq the reader last saw. When the chat has moved on since, every leg in
    between is replayed whole with its seam above it, so a switch that happened
    while the page was open — or in the gap between a disconnect and the
    reconnect after it — arrives as one continuous append rather than as a hole
    the reader can only fix by reloading (#1267).
    """
    templates = get_templates(request)
    state = get_state(request)
    injector = get_injector(request)
    accounts = get_accounts(request)
    accounts_by_key = {account.key: account for account in accounts}
    account_labels = session_labels(accounts)
    rollover_reasons = (
        state.chat_rollover_reasons(chat_keys[0]) if chat_keys else {}
    )
    parts: list[str] = []
    result = _CatchUp(html="", events=[], offset=0, seq=0)
    # Forwards, every leg in between is replayed so no switch is skipped.
    # Backwards, the reader has already seen the legs in between: only the leg
    # the chat went back to is replayed, whole, under its seam (#1501).
    legs_to_replay = (
        range(from_leg, to_leg + 1) if to_leg > from_leg else (to_leg,)
    )
    for index in legs_to_replay:
        if index >= len(chat_keys):
            break
        key = chat_keys[index]
        session = state.sessions.get(key)
        account = (
            accounts_by_key.get(session.account_key) if session is not None else None
        )
        if session is None or account is None:
            # A leg this deck can no longer read is skipped, exactly as the
            # page's own loader skips it. Its position is not reused.
            continue
        provider = PROVIDERS[account.provider_id]
        offset, seq = await provider.transcript_cursor(account, session)
        # A cursor ahead of the file means the leg was rewritten underneath us;
        # replaying it whole is the established repair.
        read_after = from_seq if index == from_leg and seq >= from_seq else 0
        events = [
            event
            for event in await provider.read_transcript(
                account, session, after_seq=read_after
            )
            if event.seq <= seq
        ]
        if index != from_leg:
            parts.append(
                render_leg_switch(
                    templates,
                    switch_into_leg(
                        chat_keys=chat_keys,
                        leg_index=index,
                        sessions=state.sessions,
                        accounts_by_key=accounts_by_key,
                        account_labels=account_labels,
                        # Only a backwards move needs to say where it came
                        # from; forwards, the chain neighbour is the leg the
                        # reader has just watched end.
                        from_leg_index=from_leg if to_leg < from_leg else None,
                        rollover_reasons=rollover_reasons,
                    ),
                )
            )
        if events:
            parts.append(
                render_transcript_events(
                    templates,
                    events,
                    session_key=key,
                    leg_index=index,
                    # Only the leg being tailed has a live queue behind it.
                    observed_messages=(
                        injector.observe_transcript(key, events)
                        if index == to_leg
                        else None
                    ),
                    store=getattr(request.app.state, "control_db", None),
                    chat_id=state.chat_identity_key(key),
                )
            )
        if index == to_leg:
            result = _CatchUp(html="", events=events, offset=offset, seq=seq)
    return result._replace(html="".join(parts))


async def _migrate_leg(
    request: Request,
    *,
    chat_keys: tuple[str, ...],
    from_leg: int,
    from_seq: int,
    to_key: str,
    revision: tuple,
):
    """Move a live tail onto ``to_key``, or ``None`` when it cannot move there.

    Refusing is the safe answer: the stream keeps tailing the leg it has, which
    is exactly the behaviour that existed before a tail could migrate at all.

    The answer is the resolved leg, the catch-up, and the new leg's last event and
    the activity seeded from it. Every tab on the chat moves at the same switch, so
    the replay and the seed are read once for all of them (#2865); ``revision`` is
    the bus revision of the leg being left.
    """
    try:
        to_leg = chat_keys.index(to_key)
    except ValueError:
        return None
    if to_leg == from_leg:
        return None
    # A chain only ever grows, but the chat running on it can go back to a leg
    # it left: the poller hands a card to an account it used before and the
    # process there was never killed (#1501). Moving backwards is therefore a
    # real move, not a stale read, and `_catch_up_to_leg` replays that leg whole
    # under a seam naming the leg the reader is coming from.
    try:
        resolved = resolve_session(request, to_key)
    except HTTPException:
        return None
    account, session, provider = resolved

    async def read():
        caught_up = await _catch_up_to_leg(
            request,
            chat_keys=chat_keys,
            from_leg=from_leg,
            from_seq=from_seq,
            to_leg=to_leg,
        )
        last_ev = await provider.last_event(account, session)
        recent = await _seed_recent_events(provider, account, session, last_ev)
        return caught_up, last_ev, tuple(recent)

    caught_up, last_ev, recent = await get_chat_renders(request).catch_up(
        (tuple(chat_keys), from_leg, from_seq, to_leg, revision), read
    )
    return resolved, caught_up, last_ev, list(recent)


def _session_stream(
    request: Request, session_key: str, *, after_seq: int | str | None = None
) -> AsyncIterator[str]:
    """Per-session live tail; see `_tail_session`.

    Reads the pending interaction now, before the response starts, rather than
    on the generator's first step. The page asks for the interaction once its
    stream opens (base.html), so a read taken after the headers went out could
    land after that ask: a question arriving between the two was then neither
    fetched nor pushed, and sat unseen until a reload (#2611)."""
    try:
        leg_key = get_state(request).chat_reachable_leg_key(session_key) or session_key
        account, session, provider = resolve_session(request, leg_key)
    except HTTPException:
        # The tail raises this itself on its first step, where it always has.
        connect_interaction = None
    else:
        connect_interaction = (leg_key, provider.pending_interaction(account, session))
    return _tail_session(
        request,
        session_key,
        after_seq=after_seq,
        connect_interaction=connect_interaction,
    )


async def _tail_session(
    request: Request,
    session_key: str,
    *,
    after_seq: int | str | None = None,
    connect_interaction: tuple[str, object] | None = None,
) -> AsyncIterator[str]:
    """Per-session live tail: appends new transcript events, and pushes a status
    fragment whenever the session flips LIVE↔IDLE *or* its thinking state
    changes. "Thinking" is driven directly off the tail here (transcript written
    within THINKING_OFF_S), so on the page you're watching it reacts within one
    poll instead of waiting for the ~10s sweep. Polls from a byte cursor, so an
    idle session costs almost nothing.

    The stream follows the *chat*, not the leg the URL happened to name. A page
    opened before an automatic account or provider switch keeps its socket and
    migrates onto the new leg in place, so the visible transcript never falls
    behind the card and the controls that already moved (#1267)."""
    state = get_state(request)
    templates = get_templates(request)
    accounts = get_accounts(request)
    injector = get_injector(request)
    auth_watch = request.app.state.auth_watch
    shared = get_shared_fragments(request)
    # Every tab on this chat shares one tail read and one render per change (#2865).
    renders = get_chat_renders(request)
    loop = asyncio.get_event_loop()

    # The topbar meters, shared with every other attached client. They differ only by
    # the leg this page selected, so two pages on the same chat, and a chat page and a
    # board alike, build them once between them (#1357).
    def _limit_bars(active_account_key: str | None) -> str:
        return shared.get(
            "usage",
            USAGE_FRAGMENT_TOPICS,
            (active_account_key,),
            lambda: render_limit_bars(
                templates,
                accounts,
                state,
                auth_watch,
                active_account_key=active_account_key,
            ),
        )

    # Leg positions never move once a leg is appended, so an identity minted for
    # a message stays valid however many times the chat moves afterwards.
    def _chat_keys() -> tuple[str, ...]:
        return state.chat_session_keys(session_key)

    def _leg_index_by_key() -> dict[str, int]:
        return {key: index for index, key in enumerate(_chat_keys())}

    # The leg being tailed right now. Starts at the chat's current leg rather
    # than at `session_key`, which matches the detail page: it too renders the
    # current leg whichever alias was asked for.
    # The reachable form: a chat can be linked to a leg no scan has installed
    # yet, and a tail that binds to one raises inside a response that has
    # already started (#1501).
    leg_key = state.chat_reachable_leg_key(session_key) or session_key
    account, session, provider = resolve_session(request, leg_key)
    leg_index = _leg_index_by_key().get(leg_key, 0)

    def _held_messages() -> tuple:
        return request.app.state.scheduled_messages.visible(
            state.chat_identity_key(session_key)
        )

    def _render_held_messages(messages: tuple) -> str:
        return templates.get_template("partials/held_messages.html").render(
            session_key=leg_key,
            held_messages=messages,
        )

    def _held_signature(messages: tuple) -> tuple:
        return tuple(
            (
                item.id,
                item.state,
                item.due_at,
                item.reason,
                item.text,
            )
            for item in messages
        )

    cursor = parse_chat_event_id(after_seq)
    backfill_html, backfill = "", []
    if cursor is None:
        offset, seq = await provider.transcript_cursor(account, session)
    else:
        # A cursor from a leg the chat has since left is caught up across the
        # seam, so a reconnect that straddles a switch neither replays what is
        # already on screen nor silently drops the leg in between.
        caught_up = await _catch_up_to_leg(
            request,
            chat_keys=_chat_keys(),
            from_leg=min(cursor[0], leg_index),
            from_seq=cursor[1],
            to_leg=leg_index,
        )
        backfill_html, backfill = caught_up.html, caught_up.events
        offset, seq = caught_up.offset, caught_up.seq
    last_ev = await provider.last_event(account, session)
    recent_evs = await _seed_recent_events(provider, account, session, last_ev)
    # Anchor the progress clock to normalized Turn Progress, so presentation
    # bookkeeping cannot postpone a real stall.
    progress_at = session.last_progress or session.last_activity
    init_age = (
        (datetime.now(UTC) - progress_at).total_seconds()
        if progress_at
        else 1e9
    )
    last_activity_t = loop.time() - init_age
    last_fragment_signatures: dict[str, tuple[object, ...]] = {}
    mac_primed = False
    notes_html: str | None = None
    assistant = request.app.state.assistant
    # Registration can land after the page paint but before EventSource starts.
    # Repair that gap inside the stream — but never in front of it: this used to
    # be awaited, so a chat whose context was not cached held every primed
    # fragment behind `git status`/`gh pr view` (measured up to 7s on a loaded
    # host, #766). The repair now runs behind the stream and the related-links
    # fragment, signature-gated on context read fresh each tick,
    # publishes it on the first tick after it lands.
    assistant.session_context(session)
    last_registration = {ref.identity for ref in session.pull_requests}
    # The topbar and the chat's own Deckhand section ride this same stream (see
    # session.html), so the page still holds one socket. Each fragment is re-pushed only
    # when its underlying state changes. No chat list rides it any more (#2603).
    # The page wakes for its own chat's topic and the few shared ones in
    # CHAT_PAGE_TOPICS, never on a clock (ADR 0007, #2865). The chat's topic is
    # keyed on its durable identity; the leg's own key is added for a chat that
    # has none yet, and each leg the page moves onto joins it.
    def _chat_topics() -> set[str]:
        return {chat_topic(state.chat_identity_key(leg_key)), chat_topic(leg_key)}

    chat_topics = _chat_topics()

    def _tail_revision() -> tuple:
        return state.bus.revision(*sorted(chat_topics))

    async def _shared_tail():
        """The tail after this tab's cursor, read and rendered once for every tab at that
        cursor until the chat announces a change (#2865). The read binds this
        pass's leg, so a tab that moves on meanwhile never takes another leg's answer."""

        async def read(
            account=account,
            session=session,
            provider=provider,
            leg_key=leg_key,
            leg_index=leg_index,
            offset=offset,
            seq=seq,
        ):
            events, next_offset, next_seq = await provider.tail_transcript(
                account, session, offset, seq
            )
            html = (
                render_transcript_events(
                    templates,
                    events,
                    session_key=leg_key,
                    leg_index=leg_index,
                    # Idempotent per event: a second tab gets the same links.
                    observed_messages=injector.observe_transcript(leg_key, events),
                    store=getattr(request.app.state, "control_db", None),
                    chat_id=state.chat_identity_key(leg_key),
                )
                if events
                else ""
            )
            return events, next_offset, next_seq, html

        return await renders.tail(
            (leg_key, leg_index, offset, seq, _tail_revision()), read
        )

    with state.bus.subscribe(*sorted(chat_topics), *CHAT_PAGE_TOPICS) as sidebar_sub:
        watching_needs_you(request)
        presentation = state.session_presentation()
        yield format_sse("usage", _limit_bars(session.account_key))
        yield format_sse(
            "assistant-session",
            render_assistant_session(templates, request.app.state.assistant, leg_key),
        )
        if backfill_html:
            yield format_sse(
                "transcript",
                backfill_html,
                event_id=chat_event_id(leg_index, seq),
            )
        if backfill:
            progress_events = [
                event for event in backfill if transcript_event_is_progress(event)
            ]
            if progress_events:
                last_ev = progress_events[-1]
                recent_evs = (recent_evs + progress_events)[-ACTIVITY_LOOKBACK:]
                last_activity_t = loop.time() - _progress_age_s(last_ev)
        # Transcript catch-up keeps its established place in the prime order;
        # a new presentation fragment must never postpone durable user events.
        held_messages = _held_messages()
        held_signature = _held_signature(held_messages)
        yield format_sse(
            "held-messages",
            _render_held_messages(held_messages),
        )
        # The pending-interaction widget is server-rendered on page load and kept
        # live here — pushed ONLY when the interaction actually changes (a new
        # question, or the current one answered/cancelled). Never re-pushing an
        # unchanged interaction is what lets the user select radios/checkboxes and
        # type an answer without a refresh wiping it mid-interaction. Seed from the
        # already-rendered state so we don't clobber it on connect/reconnect —
        # read at connect, before the page's own catch-up ask (#2611).
        if connect_interaction is not None and connect_interaction[0] == leg_key:
            _pending = connect_interaction[1]
        else:
            _pending = provider.pending_interaction(account, session)
        interaction_fragment = SESSION_FRAGMENTS_BY_NAME["interaction"]
        last_fragment_signatures[interaction_fragment.name] = interaction_fragment.signature(
            SessionView(
                current=session,
                effective=presentation.display(session),
                interaction=_pending,
            )
        )
        last_usage_sig = _usage_sig(accounts, state)
        last_usage_push = loop.time()
        # Read once here, the catch-up for a board move a file watch missed, and
        # again only when ``board`` is published (#2865).
        issue_board = (
            request.app.state.issues.read_board() if session.issues else None
        )
        forced_sidebar: set[str] = set()
        # Watching a chat is reading it (#2599). While this page is open, any
        # movement of the chat's activity clocks — a transcript update shown
        # here, or a write the collector saw that rendered nothing — advances the
        # chat's read marker, so a turn the boss watched finish does not light up
        # as unread. Batched: the marker only has to land after the last
        # movement, not once per streamed chunk, and only once the chat is at
        # rest — a working chat has no attention to clear, and each advance
        # forces a whole-deck Deckhand pass.
        read_mark_due = False
        read_marked_at = loop.time()
        read_seen_activity = (session.last_activity, session.last_progress)
        # What woke the page, carried into the next pass, and when the chat's own
        # view next changes by itself.
        woken: list[tuple[str, object]] = []
        chat_due = True
        next_deadline = float("inf")
        window_requested_at: float | None = None
        current = session
        label: str | None = None
        live = False
        while True:
            if await request.is_disconnected():
                break
            sidebar_dirty = forced_sidebar
            forced_sidebar = set()
            while woken or (item := sidebar_sub.get_nowait()) is not None:
                if woken:
                    item = woken.pop()
                sidebar_dirty.add(item[0])
            sidebar_dirty.update(sidebar_sub.changed_topics())
            if (
                chat_topics & sidebar_dirty
                or _CHAT_VIEW_TOPICS & sidebar_dirty
                or loop.time() >= next_deadline
            ):
                chat_due = True
            if chat_due:
                chat_due = False
                # A chat on someone's screen stays live, even when its Work finishes
                # (#2841). The request lasts an hour; renewed well inside it.
                window = getattr(state, "live_window", None)
                if (
                    window is not None
                    and window.enabled
                    and (
                        window_requested_at is None
                        or loop.time() - window_requested_at >= LIVE_WINDOW_RENEW_S
                    )
                ):
                    window.request(state.chat_session_keys(session_key))
                    window_requested_at = loop.time()
                # Has the chat moved on since the last pass? Read first, so the leg
                # being left is drained to its final event before the seam is
                # rendered — otherwise its last words land under the wrong account.
                moved_to = state.chat_reachable_leg_key(session_key) or leg_key
                if moved_to != leg_key:
                    # The same shared read as an ordinary pass: every tab on the chat
                    # drains the leg it is leaving at once (#2865).
                    new_events, offset, seq, transcript_html = await _shared_tail()
                    if new_events:
                        yield format_sse(
                            "transcript",
                            transcript_html,
                            event_id=chat_event_id(leg_index, seq),
                        )
                    migration = await _migrate_leg(
                        request,
                        chat_keys=_chat_keys(),
                        from_leg=leg_index,
                        from_seq=seq,
                        to_key=moved_to,
                        revision=_tail_revision(),
                    )
                    if migration is not None:
                        (account, session, provider), caught_up, last_ev, recent_evs = (
                            migration
                        )
                        leg_key = moved_to
                        leg_index = _leg_index_by_key().get(leg_key, leg_index + 1)
                        offset, seq = caught_up.offset, caught_up.seq
                        chat_topics |= _chat_topics()
                        sidebar_sub.listen(*sorted(chat_topics))
                        if caught_up.html:
                            yield format_sse(
                                "transcript",
                                caught_up.html,
                                event_id=chat_event_id(leg_index, seq),
                            )
                        last_activity_t = loop.time()
                        # Every session fragment names the leg — the account on the
                        # composer estimate, the controls, the diff link. Drop the
                        # signatures so all of them re-render for the leg that is
                        # now being billed, and refresh the row and Deckhand section
                        # beside them rather than waiting for an unrelated change.
                        last_fragment_signatures.clear()
                        forced_sidebar = {"assistant"}
                        chat_due = True
                        continue
                new_events, offset, seq, transcript_html = await _shared_tail()
                if new_events:
                    yield format_sse(
                        "transcript",
                        transcript_html,
                        event_id=chat_event_id(leg_index, seq),
                    )
                    progress_events = [
                        event for event in new_events if transcript_event_is_progress(event)
                    ]
                    if progress_events:
                        last_ev = progress_events[-1]
                        recent_evs = (recent_evs + progress_events)[-ACTIVITY_LOOKBACK:]
                        last_activity_t = loop.time() - _progress_age_s(last_ev)
                    read_mark_due = True
                current = state.sessions.get(leg_key) or session
                if (current.last_activity, current.last_progress) != read_seen_activity:
                    read_seen_activity = (current.last_activity, current.last_progress)
                    read_mark_due = True
                if (
                    read_mark_due
                    and not current.thinking
                    and loop.time() - read_marked_at >= READ_MARK_INTERVAL_S
                ):
                    state.mark_session_opened(leg_key)
                    read_mark_due = False
                    read_marked_at = loop.time()
                # An agent registers its own PR mid-turn, so the context cached for
                # this chat can go stale while the page is open. Re-resolve on the
                # registration edge only: Deckhand's own sweep covers triaged
                # sessions, and a level check would re-run `gh` on every pass for as
                # long as a lookup could not complete. Behind the pass, not inside
                # it — awaiting `gh` here stalled the transcript tail for as long as
                # the lookup took (#766).
                registration = {ref.identity for ref in current.pull_requests}
                if registration != last_registration:
                    last_registration = registration
                    assistant.session_context(current)
                # The projection is shared by every page and rebuilt only when a chat
                # changed (#1296, #2865), so asking for it here is a cache read.
                presentation = state.session_presentation()
                effective = presentation.display(current)
                active_child_sessions = presentation.active_child_sessions(current)
                live = current.status == SessionStatus.LIVE
                age = loop.time() - last_activity_t
                streaming = live and age < THINKING_OFF_S
                # "Busy" (pulsing dot + activity marker) tracks the open turn, not just
                # recent writes — so a long tool run or slow first token stays busy.
                label = resolve_activity_label(
                    has_question=bool(current.question),
                    live=live,
                    streaming=streaming,
                    last_event=last_ev,
                    age_s=age,
                    has_working_subagent=presentation.has_working_subagent(current),
                    lifecycle_active=current.lifecycle_active,
                    recent_events=recent_evs,
                )
                busy = label is not None
                interaction = provider.pending_interaction(account, current)
                if BOARD_TOPIC in sidebar_dirty or (current.issues and issue_board is None):
                    refreshed_board = (
                        request.app.state.issues.read_board() if current.issues else None
                    )
                    # An unread instance proves nothing about a card disappearing.
                    # Keep the last whole snapshot until a complete read can replace it.
                    if refreshed_board is None or refreshed_board.complete:
                        issue_board = refreshed_board
                pull_requests = session_pull_request_chips(assistant.contexts.get(leg_key))
                view = SessionView(
                    current=current,
                    effective=effective,
                    busy=busy,
                    label=label,
                    age=age,
                    active_child_sessions=active_child_sessions,
                    interaction=interaction,
                    issues=session_issue_chips(current, issue_board),
                    pull_requests=pull_requests,
                    # A dict read once this commit has been asked about, so the chip
                    # goes live with the rest of the row rather than waiting for a
                    # reload (#1127); ``reviews`` is what wakes it.
                    head_review=session_head_review_chip(
                        current, assistant.contexts.get(leg_key), state
                    ),
                )
                # The revisions every input of the view moves with: a tab woken by the
                # same change gets the HTML the first one built (#2865).
                view_revision = state.bus.revision(
                    *sorted(chat_topics), *sorted(_CHAT_VIEW_TOPICS)
                )
                for fragment in SESSION_FRAGMENTS:
                    if fragment.changed(view, last_fragment_signatures):
                        if fragment.name in _CLOCK_FRAGMENTS:
                            html = fragment.render(templates, view)
                        else:
                            html = renders.fragment(
                                (
                                    leg_key,
                                    fragment.name,
                                    last_fragment_signatures[fragment.name],
                                    view_revision,
                                ),
                                lambda fragment=fragment, view=view: fragment.render(
                                    templates, view
                                ),
                            )
                        yield format_sse(fragment.name, html)
                held_messages = _held_messages()
                next_held_signature = _held_signature(held_messages)
                if next_held_signature != held_signature:
                    held_signature = next_held_signature
                    yield format_sse(
                        "held-messages",
                        _render_held_messages(held_messages),
                    )
            if {"usage", "host"} & sidebar_dirty:
                sig = _usage_sig(accounts, state)
                if sig != last_usage_sig:
                    last_usage_sig = sig
                    last_usage_push = loop.time()
                    yield format_sse("usage", _limit_bars(session.account_key))
            if loop.time() - last_usage_push >= USAGE_REFRESH_S:
                # The keep-alive: see USAGE_REFRESH_S.
                last_usage_push = loop.time()
                yield format_sse("usage", _limit_bars(session.account_key))
            if "deployment" in sidebar_dirty:
                yield format_sse(
                    "deployment",
                    shared.get(
                        "deployment",
                        ("deployment",),
                        (),
                        lambda: render_deployment_status(templates),
                    ),
                )
            if "jobs" in sidebar_dirty:
                yield format_sse(
                    "jobs",
                    shared.get(
                        "jobs", ("jobs",), (), lambda: render_job_queue(templates)
                    ),
                )
            if "mac_runner" in sidebar_dirty and (mac := mac_runner_event(shared, templates)):
                yield mac
            if NEEDS_YOU_TOPIC in sidebar_dirty:
                yield needs_you_event(shared, templates)
            # The chat's notes (#3227): primed once per connection, so a note that landed
            # between the page render and this subscription is not missed, then on change.
            # A chat with none sends nothing.
            if notes_html is None or CHAT_NOTES_TOPIC in sidebar_dirty:
                next_notes = render_chat_notes(
                    templates,
                    getattr(request.app.state, "control_db", None),
                    state.chat_identity_key(leg_key),
                )
                before, notes_html = notes_html or "", next_notes
                if next_notes != before:
                    yield format_sse("chat-notes", next_notes)
            if "assistant" in sidebar_dirty:
                yield format_sse(
                    "assistant-session",
                    render_assistant_session(
                        templates, request.app.state.assistant, leg_key
                    ),
                )
            # The Mac icon, once per connection and after the established primes
            # (#2864): a count that moved between the page render and this subscription
            # is already counted as seen, so only a prime can repair it.
            if not mac_primed:
                mac_primed = True
                if mac := mac_runner_event(shared, templates):
                    yield mac
                # The new Needs you list too (#3239), for the same window.
                yield needs_you_event(shared, templates)
            if chat_due:
                continue
            # Nothing is due: sleep until something this page shows changes, or until
            # the chat's view changes by itself — a deadline, not a tick (ADR 0007).
            next_deadline = _chat_view_deadline(
                loop.time(),
                live=live,
                busy=label is not None,
                last_activity_t=last_activity_t,
                read_mark_at=(
                    read_marked_at + READ_MARK_INTERVAL_S
                    if read_mark_due and not current.thinking
                    else None
                ),
                window_renew_at=(
                    window_requested_at + LIVE_WINDOW_RENEW_S
                    if window_requested_at is not None
                    else None
                ),
                wall_deadlines=(
                    state.presentation_deadline(),
                ),
            )
            # Capped at the heartbeat so a closed page is noticed; a wake that finds
            # nothing dirty and nothing due does no work.
            wait = min(
                next_deadline, last_usage_push + USAGE_REFRESH_S, loop.time() + HEARTBEAT_S
            ) - loop.time()
            try:
                woken.append(await asyncio.wait_for(sidebar_sub.get(), timeout=max(0.0, wait)))
            except TimeoutError:
                pass


def _chat_view_deadline(
    now: float,
    *,
    live: bool,
    busy: bool,
    last_activity_t: float,
    read_mark_at: float | None,
    window_renew_at: float | None,
    wall_deadlines,
) -> float:
    """The loop time a chat page's own view next changes with no event, or infinity.

    Each one is a state the page derives from the clock (ADR 0007): the recency
    fallback ending ``THINKING_OFF_S`` after the last write, an open turn reading as
    stalled ``STALL_S`` after it, a batched read marker falling due, the live-window
    request needing renewal, and the presentation's own activity deadline.
    """
    candidates: list[float] = []
    if live:
        candidates.append(last_activity_t + THINKING_OFF_S)
    if busy:
        candidates.append(last_activity_t + STALL_S)
    if read_mark_at is not None:
        candidates.append(read_mark_at)
    if window_renew_at is not None:
        candidates.append(window_renew_at)
    wall_now = datetime.now(UTC)
    for deadline in wall_deadlines:
        if deadline is not None:
            candidates.append(now + (deadline - wall_now).total_seconds())
    future = [moment for moment in candidates if moment > now]
    return min(future, default=float("inf"))


@router.get("/events/sessions/{session_key}")
async def session_events(
    request: Request, session_key: str, after_seq: str | None = None
) -> StreamingResponse:
    # Both spellings are chat-level identities now: leg 0 is still the bare seq
    # it always was, and a later leg is "<leg>.<seq>" — so a reconnect after the
    # chat moved account resumes where the reader actually is (#1267).
    last_event_id = request.headers.get("last-event-id")
    if parse_chat_event_id(last_event_id) is not None:
        after_seq = last_event_id
    return StreamingResponse(
        _session_stream(request, session_key, after_seq=after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
