"""The private end of the GitHub webhook path (#1601).

A public hop (``scripts/webhook_gateway.py``, or any reverse proxy you already trust)
forwards a delivery here over a private network; this route stores it and returns. It
does not wake a Poller Instance, does not touch board state, and does not look at the
payload for anything except which repository's stream the bytes belong in. Stage one is
a tape recorder, not a participant.

**Where verification lives.** GitHub signs the raw body with the shared webhook secret,
and this route checks that signature when ``AGENTDECK_WEBHOOK_SECRET`` is set in the web
process's environment. When it is not set, the route accepts what reaches it — which is
safe only because it is not reachable from the internet. That is the boundary this stage
deliberately does not expand: restoring the full GitHub App identity stack is what #1186
removed and what this card is not. Bind the deck to localhost or a private overlay, and
set the secret if the forwarding hop is anything you would not hand a raw body to.

AgentDeck's dashboard and control API are not exposed publicly to make any of this work.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..github_webhook_log import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    MAX_BODY_BYTES,
    SIGNATURE_HEADER,
    RepositoryUnknown,
    verify_signature,
)

log = logging.getLogger(__name__)

router = APIRouter()


def _log(request: Request):
    stream = getattr(request.app.state, "github_webhook_log", None)
    if stream is None:
        raise HTTPException(status_code=503, detail="webhook log is not configured")
    return stream


async def _bounded_body(request: Request) -> bytes:
    """Read at most ``MAX_BODY_BYTES``, refusing anything larger.

    The declared ``Content-Length`` is checked first so an oversized delivery costs
    nothing, and the stream is capped as it is read so a missing or dishonest
    ``Content-Length`` cannot turn into unbounded memory either.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="delivery too large")

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="delivery too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/api/github/webhook")
async def receive(request: Request) -> JSONResponse:
    stream = _log(request)
    body = await _bounded_body(request)

    delivery_id = (request.headers.get(DELIVERY_HEADER) or "").strip()
    event = (request.headers.get(EVENT_HEADER) or "").strip()

    secret = stream.secret()
    if secret and not verify_signature(secret, body, request.headers.get(SIGNATURE_HEADER)):
        log.warning("rejected webhook delivery %s: signature did not verify", delivery_id or "?")
        raise HTTPException(status_code=401, detail="signature did not verify")

    try:
        repository, path = stream.append(body=body, headers=request.headers)
    except RepositoryUnknown as exc:
        # Refused, not filed under a shared name. One repository per file is the entire
        # guarantee of this stage, and a bucket for deliveries we could not read would
        # quietly retire it.
        log.info("refused webhook delivery %s (%s): %s", delivery_id or "?", event or "?", exc)
        raise HTTPException(status_code=400, detail=f"no repository in delivery: {exc}") from exc
    except OSError as exc:
        log.warning("could not store webhook delivery %s: %s", delivery_id or "?", exc)
        raise HTTPException(status_code=500, detail="could not store delivery") from exc

    return JSONResponse(
        {
            "status": "stored",
            "delivery": delivery_id,
            "event": event,
            "repository": repository.slug,
            "bytes": len(body),
            "path": str(path),
        }
    )
