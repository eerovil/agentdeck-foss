"""The public edition's stand-in for ChatGPT review chats (#3623).

The public copy has no reviewer, so a person's Merge press stands for the review pass
(``review_refs.reviewer_available``). The plain GitHub reads and reference rules are kept
in ``review_refs``; this keeps the ChatGPT-only names, and asking for a review is refused.
"""

from __future__ import annotations

from .review_refs import (  # noqa: F401 -- the same names the real module re-exports
    _TARGET_RE,
    _TIMEOUT_S,
    _TITLE_LIMIT,
    _TITLE_RE,
    DIGEST_UNREADABLE,
    TITLE,
    ReviewBusy,
    ReviewError,
    _read_json,
    first_line_title,
    normalise,
    pull_facts,
    review_target,
    target_digest,
    target_head,
)

PUBLIC_STUB = True


def pinned_heads(body: str, title: str = "") -> dict[tuple[str, int], str]:
    return {}


async def open_review(*args, **kwargs):
    raise ReviewError("this edition has no review chats; press Merge on the Work instead")
