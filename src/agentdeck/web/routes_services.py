"""The web's internal routes for the services process (#4013, #4016).

The Work router and the merge release run in ``agentdeck-services.service``. What they need
from the web goes through these routes: starting a Work's agent or a deck reviewer, opening a
ChatGPT review chat, interrupting a chat, loading a let-go Work's chats, refreshing the Work
overview, and the held read of the web's live chats (``session_copy``).

Every call carries the key the services process wrote to ``caller.key`` at its start; a
missing or wrong key gets 403. That keeps a page or an agent's ``curl`` from reaching these
routes by mistake. It does not stop a program of the same user that reads the file on
purpose, and nothing else on the deck does either: the dashboard has no login.

Left out of ``/openapi.json``: no agent is meant to call them.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import session_copy, work_services
from ..services_process import KEY_HEADER, WEB_PREFIX, caller_key_path
from ..work import WorkRef
from . import services_hooks

log = logging.getLogger(__name__)


def _caller(request: Request) -> None:
    given = request.headers.get(KEY_HEADER, "")
    try:
        wanted = caller_key_path(request.app.state.config).read_text(encoding="utf-8").strip()
    except OSError:
        wanted = ""
    if not given or not wanted or not hmac.compare_digest(given, wanted):
        raise HTTPException(status_code=403, detail="not the services process")


router = APIRouter(prefix=WEB_PREFIX, include_in_schema=False, dependencies=[Depends(_caller)])


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="send a JSON object")
    return body


@router.post("/start_work")
async def start_work(request: Request) -> dict:
    """Start a Work's agent, once: its ``work_actions`` row answers a resend."""
    body = await _body(request)
    app = request.app
    repository, number = str(body.get("repository") or ""), int(body.get("number") or 0)
    work_id = WorkRef(repository.lower(), number).work_id

    def refused() -> None:
        # The runtime refused the start later: the router keeps a retry (``on_refused``).
        app.state.services_later.send("services", "start_refused", work_id)

    outcome = await services_hooks.start_work(
        app,
        repository,
        number,
        wait=bool(body.get("wait")),
        on_refused=refused if body.get("refused") else None,
        labels=[str(label) for label in body.get("labels") or ()],
    )
    return {"outcome": outcome}


@router.post("/review_chat")
async def review_chat(request: Request) -> dict:
    """Open (or reuse) a pull request's ChatGPT review chat; its outcome, or its error."""
    body = await _body(request)
    try:
        outcome = await services_hooks.review_chat(
            request.app,
            str(body.get("pull") or ""),
            str(body.get("work") or ""),
            note=str(body.get("note") or ""),
            again=bool(body.get("again")),
        )
    except tuple(work_services.REVIEW_ERRORS.values()) as exc:
        return {"error": {"type": type(exc).__name__, "message": str(exc)}}
    return work_services.plain_outcome(outcome)


@router.post("/start_reviewer")
async def start_reviewer(request: Request) -> dict:
    """Start one deck reviewer, once per token."""
    body = await _body(request)
    started, why = await services_hooks.start_reviewer(
        request.app,
        str(body.get("kind") or ""),
        str(body.get("cwd") or ""),
        str(body.get("text") or ""),
        str(body.get("token") or ""),
    )
    return {"started": started, "why": why}


@router.post("/interrupt")
async def interrupt(request: Request) -> dict:
    body = await _body(request)
    accepted = await services_hooks.interrupt(request.app, str(body.get("chat_id") or ""))
    log.info("services: interrupt of %s: %s", body.get("chat_id"), accepted)
    return {"accepted": accepted}


@router.post("/load_work")
async def load_work(request: Request) -> dict:
    body = await _body(request)
    legs = await services_hooks.load_work(
        request.app, str(body.get("repository") or ""), int(body.get("number") or 0)
    )
    return {"legs": list(legs)}


@router.post("/refresh_overview")
async def refresh_overview(request: Request) -> dict:
    await services_hooks.refresh_overview(request.app)
    return {"ok": True}


@router.get("/sessions")
async def sessions(
    request: Request, since: int | None = None, epoch: str | None = None, wait: float = 0.0
) -> dict:
    """The live chats' copy: the ones changed since ``since``, held until one does."""
    feed: session_copy.SessionCopyFeed | None = getattr(request.app.state, "session_copy", None)
    if feed is None:
        raise HTTPException(status_code=503, detail="the web keeps no session copy")
    return await feed.wait(since, epoch, max(0.0, min(wait, session_copy.KEEPALIVE_S)))
