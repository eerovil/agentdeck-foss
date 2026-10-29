"""Change what every open deck page shows, by asking the deck chat (#3814).

In voice mode the owner asks the deck chat to change the view, and the chat calls
``POST /api/view/project-filter``. The command is published on ``VIEW_TOPIC``, which
every page stream carries beside the top bar's icons, so every open page in every
browser receives it as a ``view`` event and ``static/front_filters.js`` applies it as if
the project's tab had been tapped. Only showing one project, or All, is supported.

A command is sent only when it is published, never as a stream's prime: a page that
reconnects later must not have a tap made since then undone by an old command. A page
that was not connected at that moment misses it; the owner can ask again.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

VIEW_TOPIC = "view"
# A stream that renders a command later than this (a page stalled mid-burst) drops it.
FRESH_S = 60.0


@dataclass(frozen=True)
class ViewCommand:
    id: str
    # The one project to show, or "" for All.
    project: str
    # Every project the deck knows, so a page whose list is not mounted can still hide
    # the others.
    projects: tuple[str, ...]
    at: float


class UnknownProject(ValueError):
    pass


class ViewCommands:
    def __init__(self, bus) -> None:
        self._bus = bus
        self.latest: ViewCommand | None = None

    def show_project(self, project: str, projects: Iterable[str]) -> ViewCommand:
        """Show only ``project`` on every open page ("" or "all": every project)."""
        known = tuple(sorted({name for name in projects if name}, key=str.casefold))
        wanted = project.strip()
        if wanted.casefold() in ("", "all"):
            name = ""
        else:
            match = [p for p in known if p.casefold() == wanted.casefold()]
            if not match:
                raise UnknownProject(wanted)
            name = match[0]
        command = ViewCommand(uuid.uuid4().hex, name, known, time.monotonic())
        self.latest = command
        self._bus.publish(VIEW_TOPIC)
        return command

    def fresh(self) -> ViewCommand | None:
        command = self.latest
        if command is None or time.monotonic() - command.at > FRESH_S:
            return None
        return command
