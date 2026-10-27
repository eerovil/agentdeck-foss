"""The public edition's stand-in for the ChatGPT review nudges (#3623).

Pressing "Run now" on a ChatGPT task is left out of the public copy, so no project has an
external reviewer here: every read answers "none", and a press is refused.
"""

from __future__ import annotations

from pathlib import Path

from .external_reviews import ExternalReviews

PUBLIC_STUB = True


class NudgeError(Exception):
    """Why a nudge could not be pressed."""


def external_review_state_file(root, instance: str) -> tuple[bool, Path | None]:
    return False, None


def read_external_reviews(root, instance: str) -> ExternalReviews | None:
    return None


def read_all(*args, **kwargs) -> list:
    return []


def press(*args, **kwargs):
    raise NudgeError("this edition has no ChatGPT tasks to press")
