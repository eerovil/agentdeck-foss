"""PWA plumbing: the service worker and web manifest.

Both are physically stored under ``static/`` but must be served from the site
root. A service worker only controls clients at or below the path it is served
from, so ``sw.js`` has to live at ``/sw.js`` to control the whole app (serving
it from ``/static/`` would scope it to ``/static/`` and control nothing). The
manifest is served here too so it gets the correct ``application/manifest+json``
content type, which ``StaticFiles`` does not emit for ``.webmanifest``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"

# The SW file changes rarely; tell the browser to always revalidate so a new
# worker is picked up promptly rather than sitting behind an HTTP cache.
_NO_CACHE = {"Cache-Control": "no-cache"}

def cache_stamp() -> str:
    """Hash every web asset used by versioned URLs or the service worker.

    Keeping one complete input set prevents a ``?v=`` URL from staying unchanged
    when a less frequently changed script is deployed.
    """
    h = hashlib.sha1()
    for asset in sorted(_STATIC_DIR.iterdir()):
        if not asset.is_file():
            continue
        h.update(asset.name.encode())
        try:
            h.update(asset.read_bytes())
        except OSError:
            h.update(b"?")
    return h.hexdigest()[:12]


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    body = (_STATIC_DIR / "sw.js").read_text().replace("__CACHE_STAMP__", cache_stamp())
    return Response(
        body,
        media_type="text/javascript",
        headers={**_NO_CACHE, "Service-Worker-Allowed": "/"},
    )


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> FileResponse:
    return FileResponse(
        _STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )
