"""A wayfinder map drawn as a diagram on its Work page (#3627, the approved #3621 spec).

A map is an issue labelled ``wayfinder:map`` whose GitHub sub-issues are its tickets
(``skills/wayfinder/WAYFINDER.md``). The diagram has one box per ticket, laid as trees that
follow GitHub's native "blocked by" dependencies (drawn as arrows), one layer per blocking
depth (#3694), then the tickets with no arrow, then a dashed box per bullet under the map
body's **Not yet specified**, then the destination. Each box is coloured by its ticket's state
and opens that ticket's Work.

Everything here is pure: the caller reads the sub-issues, their dependencies and their
front-page rows, and this lays them out as an SVG the server draws. No JavaScript and no
GitHub call. The SVG scales to the page's width, so the layout is drawn for a phone.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

MAP_LABEL = "wayfinder:map"

# The shared GitHub cache's rows for a map's body and each ticket's "blocked by" list:
# ``IssueResolver`` reads them and keeps them a day, and ``webhook_status_cache`` marks them out
# of date when GitHub says they changed, so the diagram follows webhooks and never a timer
# (#3739: the day only covers a lost delivery).
ISSUE_BODY_KEY = "issue-body:v1:"
BLOCKED_BY_KEY = "blocked-by:v1:"
SUB_ISSUES_KEY = "sub-issues:v2:"  # ``IssueResolver.sub_issues``' rows (#3622), with labels


def cache_key(prefix: str, repository: str, number: int) -> str:
    return f"{prefix}{repository.lower()}#{number}"


# Moves whenever a webhook marked a map's cached body, sub-issue list or "blocked by" list
# out of date in this process (#3627), or a re-read behind an open page changed one (#3739).
# Part of every Work page's build key: those reads are the page's own, so nothing else would
# tell an open map page to build again.
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
COLUMNS = 3  # loose tickets and fog side by side in a row, at least; the trees may be wider
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
    # The question it waits on the person for (``question``), None for none (#3696): its box
    # says what it asks, since a phone never shows the tooltip, and opens the chat's form.
    asks: Question | None = None


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
    # The tickets' waiting questions, oldest first, which the caller puts in the front-page
    # row's order when it has one: the map page's "Waiting for you" (#3696).
    questions: tuple[Question, ...] = ()

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


def is_ticket(labels: Sequence[str]) -> bool:
    """A wayfinder ticket: a ``wayfinder:<type>`` label other than the map's own."""
    return bool(ticket_kind(labels))


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


def _rows(
    items: Sequence, y: float, width: float, text, columns: int = COLUMNS
) -> tuple[list[tuple], float]:
    """Place ``items`` left to right, ``columns`` a row, each row centred in ``width``.

    ``text(item)`` is the title to wrap. Each frame is ``(item, x, y, height, lines)``; a row is
    as tall as its longest title, so every box in it lines up."""
    frames = []
    for start in range(0, len(items), columns):
        row = items[start : start + columns]
        left = (width - _span(len(row))) / 2
        wrapped = [_wrap(text(item), BOX_W, TITLE_LINES) for item in row]
        height = _height(max((len(lines) for lines in wrapped), default=1) or 1)
        for column, (item, lines) in enumerate(zip(row, wrapped, strict=True)):
            frames.append((item, left + column * (BOX_W + GAP_X), y, height, lines))
        y += height + GAP_Y
    return frames, y


_NUMBER = re.compile(r"#(\d+)\s*$")


def _order(tickets: Sequence[Ticket]) -> dict[str, tuple]:
    """Each ticket's place in reading order: by issue number, so a map reads in the order it
    was charted (#3694); a label with no number after every numbered one, in input order."""
    order = {}
    for index, ticket in enumerate(tickets):
        match = _NUMBER.search(ticket.label)
        order[ticket.key] = (0, int(match.group(1)), index) if match else (1, 0, index)
    return order


def forest(tickets: Sequence[Ticket]) -> tuple[list[list[str]], dict[str, float], list[str]]:
    """The tickets as trees that follow the arrows (#3694), and the loose ones with none.

    Each blocked ticket hangs under one blocker a layer above it (its deepest, the lowest
    number on a tie), so a tree's subtrees stand side by side and never overlap. Returns the
    trees' roots grouped by connected arrows, each ticket's centre in box columns counted from
    the left of the trees, and the tickets with no arrow at all, all in reading order."""
    layer_of = depths(tickets)
    order = _order(tickets)
    keys = {ticket.key for ticket in tickets}
    parent: dict[str, str] = {}
    children: dict[str, list[str]] = {ticket.key: [] for ticket in tickets}
    linked: dict[str, set[str]] = {ticket.key: set() for ticket in tickets}
    for ticket in tickets:
        above = [
            key
            for key in ticket.blocked_by
            if key in keys and key != ticket.key and layer_of[key] < layer_of[ticket.key]
        ]
        for key in above:  # exactly the arrows ``layout`` draws
            linked[key].add(ticket.key)
            linked[ticket.key].add(key)
        if above:
            parent[ticket.key] = min(above, key=lambda key: (-layer_of[key], order[key]))
            children[parent[ticket.key]].append(ticket.key)
    loose = sorted((key for key in linked if not linked[key]), key=order.__getitem__)
    groups: list[list[str]] = []
    seen: set[str] = set()
    for key in sorted((key for key in linked if linked[key]), key=order.__getitem__):
        if key in seen:
            continue
        members, stack = [], [key]
        seen.add(key)
        while stack:
            item = stack.pop()
            members.append(item)
            for other in linked[item] - seen:
                seen.add(other)
                stack.append(other)
        groups.append(
            sorted((item for item in members if item not in parent), key=order.__getitem__)
        )
    centre: dict[str, float] = {}

    def place(key: str, left: float) -> float:
        """Lay ``key``'s subtree from column ``left``; returns the column after it."""
        kids = sorted(children[key], key=order.__getitem__)
        if not kids:
            centre[key] = left + 0.5
            return left + 1
        right = left
        for kid in kids:
            right = place(kid, right)
        centre[key] = (centre[kids[0]] + centre[kids[-1]]) / 2
        return right

    left = 0.0
    for roots in groups:
        for root in roots:
            left = place(root, left)
    return groups, centre, loose


def layout(
    tickets: Sequence[Ticket],
    *,
    fog_items: Sequence[str],
    goal: str,
    moved: Mapping[str, tuple[float, float]] | None = None,
) -> Diagram:
    """The map's diagram: the tickets as trees down their arrows, then the tickets with no
    arrow, then the fog, then the destination.

    The trees stand side by side in reading order, one layer per blocking depth, each blocked
    ticket under its blockers (#3694); they may be wider than a phone, since the page pans
    and zooms. The loose tickets and the fog fill rows as wide as the trees, at least
    ``COLUMNS``. ``moved`` is where a person dragged boxes to (``ControlDB.wayfinder_layout``),
    by box key; those boxes stand there instead, and the drawing grows to hold them."""
    layer_of = depths(tickets)
    by_key = {ticket.key: ticket for ticket in tickets}
    _groups, centre, loose = forest(tickets)
    tree_columns = round(max((value + 0.5 for value in centre.values()), default=0))
    columns = max(COLUMNS, tree_columns)
    widest = max(tree_columns, min(columns, max(len(loose), len(fog_items))))
    width = _span(max(MIN_COLUMNS, widest)) + 2 * PAD
    boxes: dict[str, Box] = {}

    def box(ticket: Ticket, x: float, top: float, height: float, lines: tuple[str, ...]) -> Box:
        return Box(
            key=ticket.key,
            state=ticket.state,
            x=x,
            y=top,
            w=BOX_W,
            h=height,
            head=ticket.label + (f" · {ticket.kind}" if ticket.kind else ""),
            lines=lines,
            href=ticket.asks.href if ticket.asks is not None else ticket.href,
            tip=f"{ticket.label} {ticket.title} — "
            + (
                ticket.asks.line
                if ticket.asks is not None
                else ticket.status or STATE_WORDS.get(ticket.state, "")
            ),
        )

    y = float(PAD)
    layers: dict[int, list[str]] = {}
    for key in centre:
        layers.setdefault(layer_of[key], []).append(key)
    offset = (width - _span(tree_columns)) / 2 if tree_columns else 0.0
    for depth in sorted(layers):
        wrapped = {key: _wrap(_box_text(by_key[key]), BOX_W, TITLE_LINES) for key in layers[depth]}
        height = _height(max(len(lines) for lines in wrapped.values()) or 1)
        for key, lines in wrapped.items():
            x = offset + (centre[key] - 0.5) * (BOX_W + GAP_X)
            boxes[key] = box(by_key[key], x, y, height, lines)
        y += height + GAP_Y
    frames, y = _rows([by_key[key] for key in loose], y, width, _box_text, columns)
    for ticket, x, top, height, lines in frames:
        boxes[ticket.key] = box(ticket, x, top, height, lines)
    fog_boxes = []
    frames, y = _rows(list(fog_items), y, width, lambda text: text, columns)
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
    for index, item in enumerate(placed):
        at = (moved or {}).get(item.key)
        if at is not None and (item.x, item.y) != at:
            placed[index] = replace(item, x=at[0], y=at[1], home=(item.x, item.y))
            if item.key in boxes:
                boxes[item.key] = placed[index]
    arrows = []
    for ticket in tickets:
        target = boxes[ticket.key]
        for key in ticket.blocked_by:
            source = boxes.get(key)
            if source is None or key == ticket.key or layer_of[key] >= layer_of[ticket.key]:
                continue
            arrows.append(Arrow(_curve(source, target), by_key[key].state, key, ticket.key))
    counts = {}
    for item in (*boxes.values(), *fog_boxes):
        counts[item.state] = counts.get(item.state, 0) + 1
    order = (NEEDS_YOU, RUNNING, WAITING, DONE, FOG)
    height = max(y - GAP_Y + PAD, 2 * PAD)
    asked = [ticket.asks for ticket in tickets if ticket.asks is not None]
    epoch = datetime.min
    return Diagram(
        questions=tuple(
            sorted(asked, key=lambda item: (item.asked_at is None, (item.asked_at or epoch)))
        ),
        boxes=tuple(placed),
        arrows=tuple(arrows),
        height=max([height, *(item.y + item.h + PAD for item in placed)]),
        counts=tuple((state, counts.get(state, 0)) for state in order),
        width=max([width, *(item.x + item.w + PAD for item in placed)]),
    )


def _box_text(ticket: Ticket) -> str:
    """A box's title: what it asks first, when it asks (#3696)."""
    if ticket.asks is None:
        return ticket.title
    name = ticket.asks.header or "a question"
    return f"Asks: {name} · {ticket.title}"


def _curve(source: Box, target: Box) -> str:
    """From the blocker's bottom edge to just above the blocked box's top, as a soft S.

    ``wayfinder_map.js`` draws the same curve again when a box is dragged."""
    x1, y1 = source.cx, source.y + source.h
    x2, y2 = target.cx, target.y - 3
    bend = max(12.0, (y2 - y1) / 2)
    return (
        f"M{x1:.1f},{y1:.1f} C{x1:.1f},{y1 + bend:.1f} {x2:.1f},{y2 - bend:.1f} {x2:.1f},{y2:.1f}"
    )


def ticket_state(
    *, closed: bool, section: str | None, waits: bool = False, held: bool = False
) -> str:
    """A ticket's colour from its issue and its Work's front-page row (``FrontRow``).

    Closed is done. A Work that needs the owner is Needs you; one an agent works on now is
    Running. Everything else open — not started, blocked, waiting on a review or CI, paused —
    is Waiting. ``held`` is a ticket no agent started that still has an open blocker: it waits
    for that ticket, not for the owner, though its row on its own says Not started (#3736)."""
    from .work_story import IN_PROGRESS
    from .work_story import NEEDS_YOU as NEEDS

    if closed:
        return DONE
    if held:
        return WAITING
    if section == NEEDS:
        return NEEDS_YOU
    if section == IN_PROGRESS and not waits:
        return RUNNING
    return WAITING


# --- a map's waiting questions, wherever the deck points at them (#3696) --------------------

# Where a chat page's question form is: a link to a waiting question opens the chat there.
FORM_ANCHOR = "#pending-interaction"


@dataclass(frozen=True)
class Question:
    """One question a map's ticket (or the map itself) waits on the person for."""

    work_id: str
    title: str  # the ticket issue's own title
    header: str  # the form question's short name; "" for a question in the chat's text
    prompt: str
    href: str  # its chat, at the form
    asked_at: datetime | None = None

    @property
    def line(self) -> str:
        """``<header>: <question>``, or the question alone when it has no header.

        Only the sentence that asks: a ticket's form often spells out what the agent found
        first, which made a wall of text of the front-page row and the map page (#3736). The
        form the line opens shows it all."""
        from .work_derived import question_sentence

        asked = question_sentence(self.prompt)
        return f"{self.header}: {asked}" if self.header else asked


@dataclass(frozen=True)
class Progress:
    """How far a map is, from its front-page row: what every page around a ticket's question
    shows of the map (#3696). ``questions`` are in Needs you's own order, so the first is the
    one the front-page row leads with."""

    work_id: str
    title: str  # the map issue's own title
    href: str  # the map's Work page
    counts: tuple[int, int] | None  # GitHub's (done, total) of its tickets
    working: int = 0
    blocked: int = 0
    questions: tuple[Question, ...] = ()
    tickets: tuple[str, ...] = ()  # the Work ids of the tickets its row folds in

    @property
    def line(self) -> str:
        """``5/9 done · 2 ask you · 1 working · 1 blocked``, zero counts left out."""
        parts = [f"{self.counts[0]}/{self.counts[1]} done"] if self.counts else []
        if asking := len(self.questions):
            parts.append(f"{asking} {'asks' if asking == 1 else 'ask'} you")
        if self.working:
            parts.append(f"{self.working} working")
        if self.blocked:
            parts.append(f"{self.blocked} blocked")
        return " · ".join(parts)

    @property
    def short(self) -> str:
        """The chat page's strip: done and asking only."""
        parts = [f"{self.counts[0]}/{self.counts[1]} done"] if self.counts else []
        if asking := len(self.questions):
            parts.append(f"{asking} {'asks' if asking == 1 else 'ask'} you")
        return " · ".join(parts)

    def holds(self, work_id: str) -> bool:
        return work_id == self.work_id or work_id in self.tickets

    def question_of(self, work_id: str) -> Question | None:
        return next((item for item in self.questions if item.work_id == work_id), None)

    def next_after(self, work_id: str) -> Question | None:
        """The first waiting question other than ``work_id``'s: where Submit leads on to."""
        return next((item for item in self.questions if item.work_id != work_id), None)


def question(row) -> Question | None:
    """The question a Work's front-page row waits on the person for, or None.

    Only a chat that really asks counts (``WorkAttention.asks``): a closed ticket waiting for
    Finish or a stopped chat is not a question (#3656)."""
    from .work_derived import QUESTION as DERIVED_QUESTION
    from .work_story import NEEDS_YOU as NEEDS
    from .work_story import waiting_attention

    card = row.card
    for item in waiting_attention(card):
        if item.asks and item.question:
            return Question(
                work_id=card.work_id,
                title=card.issue_title or card.title,
                header=item.header,
                prompt=item.question,
                href=f"{item.href}{FORM_ANCHOR}",
                asked_at=row.stopped_at,
            )
    # A deck-run Work reads its question from its own chat, with or without a Deckhand
    # card (#3503): #902 waited on an answer while its map said nobody asked (#3736).
    derived = card.derived
    if row.section == NEEDS and derived is not None and derived.kind == DERIVED_QUESTION:
        if derived.summary:
            return Question(
                work_id=card.work_id,
                title=card.issue_title or card.title,
                header="",
                prompt=derived.summary,
                href=f"{derived.chat_href}{FORM_ANCHOR}" if derived.chat_href else card.href,
                asked_at=row.stopped_at,
            )
    return None


def blocked_keys(items: Sequence[Mapping], work_id) -> tuple[str, ...]:
    """The Work ids of a dependency read's issues (``{"repository", "number"}`` each)."""
    keys = []
    for item in items:
        repository, number = item.get("repository"), item.get("number")
        if isinstance(repository, str) and isinstance(number, int):
            keys.append(work_id(repository, number))
    return tuple(keys)
