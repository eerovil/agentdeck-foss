"""A wayfinder map drawn as a diagram on its Work page (#3627, the approved #3621 spec).

A map is an issue labelled ``wayfinder:map`` whose GitHub sub-issues are its tickets
(``skills/wayfinder/WAYFINDER.md``). The diagram has one box per ticket, laid top to bottom in
layers by blocking depth (GitHub's native "blocked by" dependencies, drawn as arrows), then a
dashed box per bullet under the map body's **Not yet specified**, then the destination. Each
box is coloured by its ticket's state and opens that ticket's Work.

Everything here is pure: the caller reads the sub-issues, their dependencies and their
front-page rows, and this lays them out as an SVG the server draws. No JavaScript and no
GitHub call. The SVG scales to the page's width, so the layout is drawn for a phone.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

MAP_LABEL = "wayfinder:map"

# The shared GitHub cache's rows for a map's body and each ticket's "blocked by" list:
# ``IssueResolver`` reads them, and ``webhook_status_cache`` drops them when GitHub says they
# changed, so the diagram follows webhooks and never a timer.
ISSUE_BODY_KEY = "issue-body:v1:"
BLOCKED_BY_KEY = "blocked-by:v1:"
SUB_ISSUES_KEY = "sub-issues:v2:"  # ``IssueResolver.sub_issues``' rows (#3622), with labels


def cache_key(prefix: str, repository: str, number: int) -> str:
    return f"{prefix}{repository.lower()}#{number}"


# Moves whenever a webhook dropped a map's cached body, sub-issue list or "blocked by" list
# in this process (#3627). Part of every Work page's build key: those reads are the page's
# own, so nothing else would tell an open map page to build again.
_generation = 0


def bump_generation() -> None:
    global _generation
    _generation += 1


def generation() -> int:
    return _generation


TYPE_PREFIX = "wayfinder:"

# The states a box is coloured by (D2, D10).
DONE = "done"
NEEDS_YOU = "needs-you"
RUNNING = "running"
WAITING = "waiting"
FOG = "fog"
DESTINATION = "destination"

STATE_WORDS = {
    DONE: "Done",
    NEEDS_YOU: "Needs you",
    RUNNING: "Running",
    WAITING: "Waiting",
    FOG: "Not yet specified",
}

# The drawing, in SVG user units (a CSS pixel at zoom 1). The page shows it in a frame a
# person pans, zooms and drags boxes in (``wayfinder_map.js``), so it may be wider than a phone.
BOX_W = 168
PAD = 8
COLUMNS = 3  # boxes side by side at most in one row of a layer
MIN_COLUMNS = 2  # the drawing is at least this many boxes wide, for the destination's sake
GAP_X = 14
GAP_Y = 30  # room between rows for the arrows
TITLE_LINES = 3
LINE_H = 15
HEAD_H = 32  # the box's top padding and its small first line, to the first title line
DEST_LINES = 3
CHAR_W = 6.4  # an average character at the box's 12px title size
MAX_DEPTH = 64

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+(.*\S)\s*$")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


@dataclass(frozen=True)
class Ticket:
    """One sub-issue of the map, as the caller read it."""

    key: str  # its Work id
    label: str  # "#12", or "repo#12" in another repository
    title: str
    href: str
    state: str  # DONE, NEEDS_YOU, RUNNING or WAITING
    kind: str = ""  # its ``wayfinder:<type>``: research, task, prototype, grilling
    status: str = ""  # its Work's front-page line, for the box's tooltip
    blocked_by: tuple[str, ...] = ()  # the keys of the tickets it waits on


@dataclass(frozen=True)
class Box:
    key: str
    state: str
    x: float
    y: float
    w: float
    h: float
    head: str  # the small first line: "#12 · research"
    lines: tuple[str, ...]  # the title, wrapped
    href: str = ""
    tip: str = ""
    # Where the server's layout puts it, when a person moved it elsewhere (#3627): Reset's.
    home: tuple[float, float] | None = None

    @property
    def cx(self) -> float:
        return self.x + self.w / 2


@dataclass(frozen=True)
class Arrow:
    path: str
    state: str  # the blocker's: a done blocker's arrow is faint
    source: str = ""  # the blocker's box key; the script redraws the arrow when either moves
    target: str = ""


@dataclass(frozen=True)
class Diagram:
    boxes: tuple[Box, ...]
    arrows: tuple[Arrow, ...]
    height: float
    counts: tuple[tuple[str, int], ...]  # (state, how many), in legend order
    width: float = 0.0
    # The body or a ticket's dependencies could not be read from GitHub this time: said.
    partial: bool = False

    @property
    def legend(self) -> tuple[tuple[str, str, int], ...]:
        return tuple((state, STATE_WORDS[state], n) for state, n in self.counts if n)


def is_map(labels: Sequence[str]) -> bool:
    return any(str(label).strip().casefold() == MAP_LABEL for label in labels)


def ticket_kind(labels: Sequence[str]) -> str:
    """A ticket's type from its ``wayfinder:<type>`` label, or "" for none."""
    for label in labels:
        name = str(label).strip()
        if name.casefold().startswith(TYPE_PREFIX) and name.casefold() != MAP_LABEL:
            return name[len(TYPE_PREFIX) :].strip()
    return ""


def _section(body: str, name: str) -> list[str]:
    """The lines under a ``## <name>`` heading, up to the next heading of any level."""
    found: list[str] = []
    inside = False
    fence = False
    for line in (body or "").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue  # an example in a code block is neither a heading nor an item
        heading = _HEADING.match(line)
        if heading is not None:
            inside = heading.group(1).strip().casefold() == name
            continue
        if inside:
            found.append(line)
    return found


def _plain(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    return re.sub(r"[*_`]+", "", text).strip()


def fog(body: str) -> tuple[str, ...]:
    """The bullets under the map body's **Not yet specified**: one dashed box each."""
    bullets = []
    for line in _section(body, "not yet specified"):
        match = _BULLET.match(line)
        if match is not None:
            text = _plain(match.group(1))
            if text and not text.startswith("<"):
                bullets.append(text)
    return tuple(bullets)


def destination(body: str) -> str:
    """The map body's **Destination**, as one line, or "" when it has none."""
    text = " ".join(line.strip() for line in _section(body, "destination") if line.strip())
    text = _plain(text)
    return "" if text.startswith("<") else text


def fog_keys(items: Sequence[str]) -> tuple[str, ...]:
    """Each fog bullet's box key, from its own text, so a bullet keeps the place a person
    dragged it to when another is added or removed (#3627). Identical bullets are told apart
    by their order among themselves: ``fog:<hash>``, then ``fog:<hash>.2`` and so on."""
    seen: dict[str, int] = {}
    keys = []
    for text in items:
        name = hashlib.sha1(
            " ".join(text.split()).casefold().encode(), usedforsecurity=False
        ).hexdigest()[:12]
        seen[name] = seen.get(name, 0) + 1
        keys.append(f"fog:{name}" if seen[name] == 1 else f"fog:{name}.{seen[name]}")
    return tuple(keys)


def _wrap(text: str, width: float, lines: int) -> tuple[str, ...]:
    """``text`` in at most ``lines`` lines that fit ``width``, the last cut with "…"."""
    per = max(4, int((width - 16) / CHAR_W))
    words = text.split()
    out: list[str] = []
    current = ""
    for index, word in enumerate(words):
        candidate = f"{current} {word}".strip()
        if len(candidate) <= per:
            current = candidate
            continue
        if current:
            out.append(current)
        current = word
        if len(out) == lines - 1:
            rest = " ".join(words[index:])
            current = rest
            break
    if current:
        out.append(current)
    out = out[:lines]
    if out and len(out[-1]) > per:
        out[-1] = out[-1][: per - 1].rstrip() + "…"
    elif out and len(out) == lines and " ".join(out) != " ".join(words):
        out[-1] = out[-1][: per - 1].rstrip() + "…"
    return tuple(out)


def depths(tickets: Sequence[Ticket]) -> dict[str, int]:
    """Each ticket's layer: 0 with no blocker on the map, else one below its deepest blocker.

    A done blocker still counts, so a ticket keeps its place as the work moves on. A loop of
    blockers is cut where it closes, so every ticket gets a layer."""
    keys = {ticket.key for ticket in tickets}
    blockers = {
        ticket.key: [key for key in ticket.blocked_by if key in keys and key != ticket.key]
        for ticket in tickets
    }
    found: dict[str, int] = {}

    def depth(key: str, path: frozenset[str]) -> int:
        if key in found:
            return found[key]
        if len(path) > MAX_DEPTH:
            return 0
        deeper = [depth(item, path | {key}) for item in blockers[key] if item not in path]
        found[key] = 1 + max(deeper) if deeper else 0
        return found[key]

    for ticket in tickets:
        depth(ticket.key, frozenset())
    return found


def _height(lines: int) -> float:
    return HEAD_H + (lines - 1) * LINE_H + 12


def _span(columns: int) -> float:
    return columns * BOX_W + (columns - 1) * GAP_X


def _rows(items: Sequence, y: float, width: float, text) -> tuple[list[tuple], float]:
    """Place ``items`` left to right, ``COLUMNS`` a row, each row centred in ``width``.

    ``text(item)`` is the title to wrap. Each frame is ``(item, x, y, height, lines)``; a row is
    as tall as its longest title, so every box in it lines up."""
    frames = []
    for start in range(0, len(items), COLUMNS):
        row = items[start : start + COLUMNS]
        left = (width - _span(len(row))) / 2
        wrapped = [_wrap(text(item), BOX_W, TITLE_LINES) for item in row]
        height = _height(max((len(lines) for lines in wrapped), default=1) or 1)
        for column, (item, lines) in enumerate(zip(row, wrapped, strict=True)):
            frames.append((item, left + column * (BOX_W + GAP_X), y, height, lines))
        y += height + GAP_Y
    return frames, y


def layout(
    tickets: Sequence[Ticket],
    *,
    fog_items: Sequence[str],
    goal: str,
    moved: Mapping[str, tuple[float, float]] | None = None,
) -> Diagram:
    """The map's diagram: ticket layers by blocking depth, then the fog, then the destination.

    ``moved`` is where a person dragged boxes to (``ControlDB.wayfinder_layout``), by box key;
    those boxes stand there instead, and the drawing grows to hold them."""
    layer_of = depths(tickets)
    by_key = {ticket.key: ticket for ticket in tickets}
    layers: dict[int, list[Ticket]] = {}
    for ticket in tickets:
        layers.setdefault(layer_of[ticket.key], []).append(ticket)
    widest = max((len(row) for row in (*layers.values(), fog_items)), default=0)
    width = _span(max(MIN_COLUMNS, min(COLUMNS, widest))) + 2 * PAD
    boxes: dict[str, Box] = {}
    y = float(PAD)
    for depth in sorted(layers):
        # Under their blockers: order a layer by where the boxes it waits on stand.
        def place(ticket: Ticket) -> float:
            above = [boxes[key].cx for key in ticket.blocked_by if key in boxes]
            return sum(above) / len(above) if above else 0.0

        ordered = sorted(layers[depth], key=place)
        frames, y = _rows(ordered, y, width, lambda ticket: ticket.title)
        for ticket, x, top, height, lines in frames:
            head = ticket.label + (f" · {ticket.kind}" if ticket.kind else "")
            boxes[ticket.key] = Box(
                key=ticket.key,
                state=ticket.state,
                x=x,
                y=top,
                w=BOX_W,
                h=height,
                head=head,
                lines=lines,
                href=ticket.href,
                tip=f"{ticket.label} {ticket.title} — "
                + (ticket.status or STATE_WORDS.get(ticket.state, "")),
            )
    fog_boxes = []
    frames, y = _rows(list(fog_items), y, width, lambda text: text)
    keys = fog_keys(fog_items)
    for index, (text, x, top, height, lines) in enumerate(frames):
        fog_boxes.append(
            Box(keys[index], FOG, x, top, BOX_W, height, "Not yet specified", lines, tip=text)
        )
    goal_box = None
    if goal or tickets:
        lines = _wrap(goal or "Not written yet", width - 2 * PAD, DEST_LINES)
        height = _height(len(lines))
        goal_box = Box(
            "destination",
            DESTINATION,
            PAD,
            y,
            width - 2 * PAD,
            height,
            "Destination",
            lines,
            tip=goal,
        )
        y += height + GAP_Y
    placed = [*boxes.values(), *fog_boxes, *((goal_box,) if goal_box else ())]
    for index, box in enumerate(placed):
        at = (moved or {}).get(box.key)
        if at is not None and (box.x, box.y) != at:
            placed[index] = replace(box, x=at[0], y=at[1], home=(box.x, box.y))
            if box.key in boxes:
                boxes[box.key] = placed[index]
    arrows = []
    for ticket in tickets:
        target = boxes[ticket.key]
        for key in ticket.blocked_by:
            source = boxes.get(key)
            if source is None or key == ticket.key or layer_of[key] >= layer_of[ticket.key]:
                continue
            arrows.append(Arrow(_curve(source, target), by_key[key].state, key, ticket.key))
    counts = {}
    for box in (*boxes.values(), *fog_boxes):
        counts[box.state] = counts.get(box.state, 0) + 1
    order = (NEEDS_YOU, RUNNING, WAITING, DONE, FOG)
    height = max(y - GAP_Y + PAD, 2 * PAD)
    return Diagram(
        boxes=tuple(placed),
        arrows=tuple(arrows),
        height=max([height, *(box.y + box.h + PAD for box in placed)]),
        counts=tuple((state, counts.get(state, 0)) for state in order),
        width=max([width, *(box.x + box.w + PAD for box in placed)]),
    )


def _curve(source: Box, target: Box) -> str:
    """From the blocker's bottom edge to just above the blocked box's top, as a soft S.

    ``wayfinder_map.js`` draws the same curve again when a box is dragged."""
    x1, y1 = source.cx, source.y + source.h
    x2, y2 = target.cx, target.y - 3
    bend = max(12.0, (y2 - y1) / 2)
    return (
        f"M{x1:.1f},{y1:.1f} C{x1:.1f},{y1 + bend:.1f} {x2:.1f},{y2 - bend:.1f} {x2:.1f},{y2:.1f}"
    )


def ticket_state(*, closed: bool, section: str | None, waits: bool = False) -> str:
    """A ticket's colour from its issue and its Work's front-page row (``FrontRow``).

    Closed is done. A Work that needs the owner is Needs you; one an agent works on now is
    Running. Everything else open — not started, blocked, waiting on a review or CI, paused —
    is Waiting."""
    from .work_story import IN_PROGRESS
    from .work_story import NEEDS_YOU as NEEDS

    if closed:
        return DONE
    if section == NEEDS:
        return NEEDS_YOU
    if section == IN_PROGRESS and not waits:
        return RUNNING
    return WAITING


def blocked_keys(items: Sequence[Mapping], work_id) -> tuple[str, ...]:
    """The Work ids of a dependency read's issues (``{"repository", "number"}`` each)."""
    keys = []
    for item in items:
        repository, number = item.get("repository"), item.get("number")
        if isinstance(repository, str) and isinstance(number, int):
            keys.append(work_id(repository, number))
    return tuple(keys)
