"""HTTP revalidation policy for web assets."""

from __future__ import annotations

from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope


class RevalidatingStaticFiles(StaticFiles):
    """Store assets locally but revalidate them across every deployment.

    The checkout is updated before the web process restarts. Permanent caching
    could therefore expose new bytes under an old process's versioned URL for a
    brief window. Explicit revalidation keeps ETag/Last-Modified caching useful
    without making that transient mismatch durable in a browser.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, no-cache"
        if path.startswith("vendor/"):
            # A diagram frame has no origin of its own, so it loads modules with CORS (#3822).
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response
