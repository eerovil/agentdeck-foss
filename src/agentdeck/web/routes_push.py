"""Web Push subscription endpoints (issue #7).

The client fetches the VAPID application-server key, subscribes via
``PushManager``, and POSTs the resulting subscription here. The send path and
Deckhand trigger live elsewhere; these routes only manage the subscription set.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .deps import require_access
from .routes_actions import _require_same_origin

router = APIRouter(dependencies=[Depends(require_access)])


def _push(request: Request):
    return request.app.state.push


def _phone_changed(request: Request) -> None:
    """The Work list's clock follows whether a phone listens (#3348): tell it at once."""
    service = getattr(request.app.state, "work_overview", None)
    if service is not None:
        service.phone_changed()


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="invalid JSON body")
    return body


@router.get("/push/public-key")
async def public_key(request: Request) -> JSONResponse:
    """The VAPID application-server key the client subscribes with (null when
    push is disabled or no keypair is available)."""
    push = _push(request)
    return JSONResponse({"enabled": push.enabled, "key": push.public_key})


@router.post("/push/subscribe")
async def subscribe(request: Request) -> Response:
    _require_same_origin(request)
    push = _push(request)
    if not push.enabled:
        raise HTTPException(status_code=503, detail="push notifications are disabled")
    if not push.subscribe(await _json_body(request)):
        raise HTTPException(status_code=422, detail="invalid subscription")
    _phone_changed(request)
    return Response(status_code=201)


@router.post("/push/unsubscribe")
async def unsubscribe(request: Request) -> Response:
    _require_same_origin(request)
    endpoint = (await _json_body(request)).get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        raise HTTPException(status_code=422, detail="endpoint required")
    _push(request).unsubscribe(endpoint)
    _phone_changed(request)
    return Response(status_code=204)
