"""Live prototyping's page and the agent's API for it (#3569).

The agent registers the prototype's link and versions and records each change; the
live prototype page shows the prototype in a frame and reloads it when a change is recorded.
Everything the page sends to the chat (a suggestion, "Go back", "Write the spec") goes
through the chat's own send route, so it queues like any other message. Use cases are
written by the agent and the page alike, one at a time (#3571), and the page's stream
also says what the agent is doing right now.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from .. import live_prototype
from ..models import detailed_activity_label
from ..state import ALL_CHATS_TOPIC, chat_topic
from .deps import (
    get_state,
    get_templates,
    require_access,
    resolve_chat_key,
    resolve_current_leg,
)
from .routes_sse import _seed_recent_events

router = APIRouter(dependencies=[Depends(require_access)])

HEARTBEAT_S = 15.0


class LinkRequest(BaseModel):
    url: str | None = None
    versions: list[dict] | None = None
    # A diagram's full-screen view (#3827), kept apart from the prototype's link.
    diagram: str | None = None


class PickRequest(BaseModel):
    version: str | None = None


class StepRequest(BaseModel):
    summary: str
    commit: str = ""


class UseCaseRequest(BaseModel):
    title: str
    steps: list[str] = []
    version: str | None = None
    url: str | None = None


def _chat_key(request: Request, session_key: str) -> str:
    return get_state(request).chat_identity_key(session_key)


def _payload(session_key: str, record: dict | None) -> dict:
    return {
        "session_key": session_key,
        "page_url": live_prototype.page_url(session_key),
        # The old name, for agents still following the guide before #3811.
        "meeting_url": live_prototype.page_url(session_key),
        "url": (record or {}).get("url"),
        "diagram": (record or {}).get("diagram"),
        "versions": (record or {}).get("versions", []),
        "steps": (record or {}).get("steps", []),
        "revision": (record or {}).get("revision", 0),
        "picked": (record or {}).get("picked"),
        "use_cases": (record or {}).get("use_cases", []),
        "spec_request": live_prototype.spec_request(record) if record else None,
    }


def _publish(request: Request, chat_key: str, record: dict) -> None:
    get_state(request).bus.publish(live_prototype.topic(chat_key), record.get("revision"))


@router.get("/api/live-prototype/guide", response_class=PlainTextResponse)
async def guide() -> str:
    return live_prototype.GUIDE


@router.get("/api/sessions/{session_key}/live-prototype")
async def get_live_prototype(request: Request, session_key: str) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    return _payload(session_key, live_prototype.read(_chat_key(request, session_key)))


@router.put("/api/sessions/{session_key}/live-prototype")
async def put_live_prototype(request: Request, session_key: str, body: LinkRequest) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    chat_key = _chat_key(request, session_key)
    if body.url is None and body.diagram is None:
        raise HTTPException(status_code=422, detail="give url, diagram or both")
    try:
        # Both checked before either is stored, so a bad one changes nothing.
        if body.diagram is not None:
            live_prototype.clean_url(body.diagram)
        if body.url is not None:
            record = live_prototype.set_link(chat_key, body.url, body.versions)
        if body.diagram is not None:
            repository = await asyncio.to_thread(
                live_prototype.diagram_repository, live_prototype.diagram_file(body.diagram)
            )
            record = live_prototype.set_diagram(chat_key, body.diagram, repository)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _publish(request, chat_key, record)
    if body.diagram is not None:
        # The Work page's Diagram button reads it (#3827).
        from .work_view import WORK_INPUTS_TOPIC

        get_state(request).bus.publish(WORK_INPUTS_TOPIC)
    return _payload(session_key, record)


@router.post("/api/sessions/{session_key}/live-prototype/steps")
async def post_live_prototype_step(request: Request, session_key: str, body: StepRequest) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    chat_key = _chat_key(request, session_key)
    try:
        record = live_prototype.add_step(chat_key, body.summary, body.commit)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _publish(request, chat_key, record)
    return _payload(session_key, record)


@router.put("/api/sessions/{session_key}/live-prototype/picked")
async def put_live_prototype_pick(request: Request, session_key: str, body: PickRequest) -> dict:
    """The page's version switch: the person's pick, kept for reloads and the spec."""
    session_key, _session = resolve_chat_key(request, session_key)
    chat_key = _chat_key(request, session_key)
    try:
        record = live_prototype.pick(chat_key, body.version)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _publish(request, chat_key, record)
    return _payload(session_key, record)


def _use_case_write(request: Request, session_key: str, write) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    chat_key = _chat_key(request, session_key)
    try:
        record = write(chat_key)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _publish(request, chat_key, record)
    return _payload(session_key, record)


@router.post("/api/sessions/{session_key}/live-prototype/use-cases")
async def post_live_prototype_use_case(
    request: Request, session_key: str, body: UseCaseRequest
) -> dict:
    return _use_case_write(
        request, session_key, lambda chat: live_prototype.save_use_case(chat, body.model_dump())
    )


@router.put("/api/sessions/{session_key}/live-prototype/use-cases/{use_case_id}")
async def put_live_prototype_use_case(
    request: Request, session_key: str, use_case_id: str, body: UseCaseRequest
) -> dict:
    return _use_case_write(
        request,
        session_key,
        lambda chat: live_prototype.save_use_case(chat, body.model_dump(), use_case_id),
    )


@router.delete("/api/sessions/{session_key}/live-prototype/use-cases/{use_case_id}")
async def delete_live_prototype_use_case(
    request: Request, session_key: str, use_case_id: str
) -> dict:
    return _use_case_write(
        request, session_key, lambda chat: live_prototype.delete_use_case(chat, use_case_id)
    )


async def _activity(request: Request, session_key: str) -> str | None:
    """What the agent is doing now, in the chat badge's words, or None when it is idle."""
    try:
        account, session, provider = resolve_current_leg(request, session_key)
    except HTTPException:
        return None
    shown = get_state(request).session_presentation().display(session)
    if not shown.thinking or shown.is_waiting:
        return None
    last = await provider.last_event(account, session)
    recent = await _seed_recent_events(provider, account, session, last)
    return detailed_activity_label(shown.activity or "Working", last, recent)


@router.get("/live/{session_key}", response_class=HTMLResponse)
async def live_prototype_page(request: Request, session_key: str) -> HTMLResponse:
    session_key, session = resolve_chat_key(request, session_key)
    record = live_prototype.read(_chat_key(request, session_key))
    return get_templates(request).TemplateResponse(
        request,
        "live_prototype.html",
        {
            "session_key": session_key,
            "title": session.display_title if session is not None else session_key,
            "live": _payload(session_key, record),
        },
    )


@router.get("/events/live/{session_key}")
async def live_prototype_events(request: Request, session_key: str) -> StreamingResponse:
    """A ``data:`` line per prototype change; the page fetches the record then.

    An ``activity`` event carries what the agent is doing (#3571), sent when it
    changes: the chat's own topics say when to look, never a clock.
    """
    session_key, _session = resolve_chat_key(request, session_key)
    state = get_state(request)
    chat_key = _chat_key(request, session_key)
    topic = live_prototype.topic(chat_key)
    chat_topics = {chat_topic(chat_key), chat_topic(session_key), ALL_CHATS_TOPIC}

    async def stream() -> AsyncIterator[str]:
        with state.bus.subscribe(topic, *sorted(chat_topics)) as sub:
            # Primed once: a change between the page's render and this connection
            # (or across a reconnect) is still fetched.
            sub.changed_topics()
            yield "data: connected\n\n"
            shown = await _activity(request, session_key)
            yield f"event: activity\ndata: {json.dumps(shown)}\n\n"
            while not await request.is_disconnected():
                try:
                    await asyncio.wait_for(sub.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                while sub.get_nowait() is not None:
                    pass
                # Read from the counters, not the queue: a busy deck's chat updates
                # can push the prototype's own wake-up out of it.
                changed = sub.changed_topics()
                if topic in changed:
                    yield "data: changed\n\n"
                if changed - {topic}:
                    activity = await _activity(request, session_key)
                    if activity != shown:
                        shown = activity
                        yield f"event: activity\ndata: {json.dumps(shown)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
