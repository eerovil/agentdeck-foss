"""The image a chat note shows from a private repository (#3227).

A screenshot in a comment is a ``github.com/<owner>/<repo>/blob/<sha>/<path>?raw=1`` link,
which a browser can load only while logged in to GitHub. The deck reads that one file as
the repository's authority and serves it. Only a link some kept note's rendering holds, and
only an image type, so the route is not a way to read any file the deck could.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .. import chat_notes

router = APIRouter()


@router.get("/github-image/{owner}/{repo}/{sha}/{path:path}")
async def github_image(request: Request, owner: str, repo: str, sha: str, path: str) -> Response:
    media_type = chat_notes.image_type(path)
    store = getattr(request.app.state, "control_db", None)
    if (
        media_type is None
        or store is None
        or not store.chat_note_html_mentions(chat_notes.blob_link(owner, repo, sha, path))
    ):
        raise HTTPException(status_code=404)
    data = await chat_notes.read_image(request.app.state.github_image_cache, owner, repo, sha, path)
    if data is None:
        raise HTTPException(status_code=502, detail="GitHub did not give the image")
    # A file at a commit never changes.
    return Response(
        data,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
