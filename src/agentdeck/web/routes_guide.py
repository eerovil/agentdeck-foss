"""The rarely needed sections of the session guide, served on request (#4459).

Every chat's guide keeps one line per section naming when to read it; the full text is
here, so it costs tokens only in the chats that read it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from ..providers.instructions import GUIDE_ROUTE, guide_topics
from .deps import require_access

router = APIRouter(dependencies=[Depends(require_access)])


@router.get(GUIDE_ROUTE, response_class=PlainTextResponse)
async def guide_index() -> str:
    return "Topics: " + ", ".join(f"{GUIDE_ROUTE}/{name}" for name in guide_topics())


@router.get(GUIDE_ROUTE + "/{topic}", response_class=PlainTextResponse)
async def guide_topic(topic: str) -> str:
    text = guide_topics().get(topic)
    if text is None:
        raise HTTPException(
            status_code=404,
            detail=f"no guide topic {topic!r}; topics: {', '.join(guide_topics())}",
        )
    return text
