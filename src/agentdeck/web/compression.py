"""Compression policy for dynamic pages and static assets."""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import Receive, Scope, Send


class RangeSafeGZipMiddleware(GZipMiddleware):
    """Keep byte-range responses in the representation named by Content-Range.

    Starlette's gzip responder otherwise compresses a selected byte range after
    ``StaticFiles`` has calculated its ``Content-Range`` header, making the
    header describe different bytes than the response body.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and Headers(scope=scope).get("range"):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)
