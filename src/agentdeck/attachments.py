"""Shared attachment cleanup and provider-facing message notes."""

from __future__ import annotations

import json
import re
from pathlib import Path

ATTACHMENT_MARKER = "\n\n<agentdeck-attachments>"
ATTACHMENT_END = "</agentdeck-attachments>"
ATTACHMENT_HEADER = (
    "Temporary files attached by the user. Read them from these local paths "
    "during this turn; save a copy elsewhere if they should persist."
)
_OPAQUE_UPLOAD_NAME = re.compile(r"^[1-9][0-9]*-[0-9a-f]{24}\.upload$")


def cleanup_uploaded_files(paths: list[Path] | tuple[Path, ...]) -> None:
    """Remove uploaded files and their private directories."""
    parents = {path.parent for path in paths}
    for path in paths:
        path.unlink(missing_ok=True)
    for parent in parents:
        try:
            parent.rmdir()
        except OSError:
            pass


def display_message(message: str | None) -> str:
    """Remove only a validated, generated attachment envelope at the end."""
    text = message or ""
    marker_at = text.rfind(ATTACHMENT_MARKER)
    if marker_at < 0:
        return text
    envelope = text[marker_at + len(ATTACHMENT_MARKER) :]
    end = f"\n{ATTACHMENT_END}"
    if not envelope.startswith("\n") or not envelope.endswith(end):
        return text
    lines = envelope[1 : -len(end)].splitlines()
    if len(lines) < 2 or lines[0] != ATTACHMENT_HEADER:
        return text
    for line in lines[1:]:
        if not line.startswith("- "):
            return text
        try:
            item = json.loads(line[2:])
        except (TypeError, ValueError):
            return text
        if not isinstance(item, dict) or set(item) != {"name", "path"} or not all(
            isinstance(item[key], str) for key in ("name", "path")
        ):
            return text
        path = Path(item["path"])
        if not path.is_absolute() or _OPAQUE_UPLOAD_NAME.fullmatch(path.name) is None:
            return text
    return text[:marker_at]
