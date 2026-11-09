"""Read an old chat in parts, without waking it (#4169).

Waking a chat whose cache has expired pays for its whole context again, and most of that
context is tools: of 60 recent chats, the biggest held 2.66 MB, of which the words of the
person and the agent were 6 %. So a chat continued from an old one starts small, and its
agent reads only what it needs from here. Everything here reads transcripts the deck
already has; nothing writes to the old chat.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from shlex import quote
from typing import Any

from .chat_transcript import chat_event_id
from .models import Capability, TranscriptEvent

#: One page, about 5k tokens: small enough that a shell tool shows it whole.
PAGE_CHARS = 20_000
MIN_PAGE_CHARS = 1_000
MAX_PAGE_CHARS = 100_000
#: Where one long event is cut in a page; ``--event`` reads the rest.
EVENT_CHARS = 2_000
#: How much of the last message and the last reply a continuation note quotes.
QUOTE_CHARS = 1_500
#: How many characters come before a search match when its event is cut.
MATCH_LEAD = 200
OUTLINE_WORDS = 80

TOOLS_NONE, TOOLS_CALLS, TOOLS_ALL = "none", "calls", "all"
TOOL_LEVELS = (TOOLS_NONE, TOOLS_CALLS, TOOLS_ALL)
ROLES = ("user", "assistant", "system")
DEFAULT_ROLES = ("user", "assistant")

COMMAND = "~/agentdeck/.venv/bin/agentdeck chat-read"


@dataclass(frozen=True)
class Line:
    """One transcript event, with its place in the whole chat."""

    event_id: str
    turn: int
    event: TranscriptEvent
    session_key: str  # the part that holds it; its images are read through this


@dataclass(frozen=True)
class Query:
    """What a reader asked for. Every field but ``after`` selects; ``after`` pages."""

    first_turn: int | None = None
    last_turn: int | None = None
    last: int | None = None
    search: str = ""
    roles: tuple[str, ...] = DEFAULT_ROLES
    tools: str = TOOLS_NONE
    after: str = ""
    page_chars: int = PAGE_CHARS

    def flags(self) -> str:
        """These filters as command flags, for the line that names the next page."""
        parts = []
        if self.last is not None:
            parts.append(f"--last {self.last}")
        elif self.first_turn is not None or self.last_turn is not None:
            parts.append(f"--turns {self.first_turn or 1}-{self.last_turn or ''}")
        if self.search:
            parts.append(f"--search {quote(self.search)}")
        if self.roles != DEFAULT_ROLES:
            parts.append(f"--role {','.join(self.roles)}")
        if self.tools != TOOLS_NONE:
            parts.append(f"--tools {self.tools}")
        if self.page_chars != PAGE_CHARS:
            parts.append(f"--page-chars {self.page_chars}")
        return " ".join(parts)


def lines_of(legs: Iterable[tuple[int, str, Sequence[TranscriptEvent]]]) -> list[Line]:
    """Number a chat's events: a turn starts at each message from the user's side.

    A message can be only an image. A subagent's lines are its own conversation, so they
    are left out.
    """
    found: list[Line] = []
    turn = 0
    for leg_index, session_key, events in legs:
        for event in events:
            if event.subagent:
                continue
            if event.role == "user" and (event.text or event.image_media_types):
                turn += 1
            found.append(Line(chat_event_id(leg_index, event.seq), turn, event, session_key))
    return found


async def load_lines(
    session_key: str,
    *,
    leg_keys: Sequence[str],
    sessions: Mapping[str, Any],
    accounts_by_key: Mapping[str, Any],
    providers: Mapping[str, Any],
) -> list[Line] | None:
    """The chat's lines up to and including ``session_key``'s leg, or None if none reads.

    A later leg is left out, so a chat continued from this one never reads itself.
    """
    if session_key in leg_keys:
        wanted = list(enumerate(leg_keys[: leg_keys.index(session_key) + 1]))
    else:
        wanted = [(0, session_key)]
    legs: list[tuple[int, str, Sequence[TranscriptEvent]]] = []
    for leg_index, key in wanted:
        session = sessions.get(key)
        account = accounts_by_key.get(session.account_key) if session is not None else None
        if account is None or Capability.TRANSCRIPT not in session.capabilities:
            continue
        provider = providers[account.provider_id]
        legs.append((leg_index, key, await provider.read_transcript(account, session)))
    return lines_of(legs) if legs else None


def _images(line: Line) -> list[str]:
    """How to fetch each image of a message; a page is text, so it names them."""
    event = line.event
    return [
        f'[image {media_type}: curl -s "$AGENTDECK_URL/sessions/{line.session_key}'
        f'/transcript-images/{event.seq}/{index}" -o image-{event.seq}-{index}]'
        for index, media_type in enumerate(event.image_media_types)
    ]


def _body(line: Line, tools: str) -> str:
    event = line.event
    parts = []
    if event.question:
        parts.append(f"Asked: {event.question}")
    if event.answer:
        parts.append(f"Answered: {event.answer}")
    if event.tool_name and tools != TOOLS_NONE:
        parts.append(f"[{event.tool_name}] {event.tool_summary or ''}".rstrip())
        if tools == TOOLS_ALL and event.tool_detail:
            parts.append(event.tool_detail)
    is_tool_row = bool(event.tool_name) or event.role == "tool"
    if event.text and (not is_tool_row or tools == TOOLS_ALL or event.role == "assistant"):
        parts.append(event.text)
    parts.extend(_images(line))
    return "\n".join(parts)


def _matches(line: Line, query: Query, turns: tuple[int | None, int | None]) -> bool:
    first, last = turns
    if first is not None and line.turn < first:
        return False
    if last is not None and line.turn > last:
        return False
    event = line.event
    if event.role == "tool":
        if query.tools != TOOLS_ALL and not (event.question or event.answer):
            return False
    elif event.role not in query.roles:
        return False
    body = _body(line, query.tools)
    if not body:
        return False
    return not query.search or query.search.casefold() in body.casefold()


def _turn_range(lines: Sequence[Line], query: Query) -> tuple[int | None, int | None]:
    if query.last is None:
        return query.first_turn, query.last_turn
    newest = max((line.turn for line in lines), default=0)
    return max(newest - query.last + 1, 0), None


def _block(line: Line, session_key: str, query: Query) -> str:
    body = _body(line, query.tools)
    start = 0
    if query.search and len(body) > EVENT_CHARS:
        start = max(body.casefold().find(query.search.casefold()) - MATCH_LEAD, 0)
    shown = body[start : start + EVENT_CHARS]
    when = f" {line.event.ts:%Y-%m-%d %H:%M}" if line.event.ts else ""
    head = f"#{line.event_id} turn {line.turn} {line.event.role}{when}"
    lead = f"(from character {start}) " if start else ""
    tail = ""
    left = len(body) - start - len(shown)
    if left > 0 or start:
        tail = f"\n(cut; the whole event: {COMMAND} {session_key} --event {line.event_id})"
    return f"{head}\n{lead}{shown}{tail}\n"


def read(lines: Sequence[Line], session_key: str, query: Query) -> str:
    """One page of what the query selects, ending with how to get the next one."""
    turns = _turn_range(lines, query)
    selected = [line for line in lines if _matches(line, query, turns)]
    if query.after:
        ids = [line.event_id for line in selected]
        selected = selected[ids.index(query.after) + 1 :] if query.after in ids else []
    page: list[str] = []
    used = 0
    last_id = ""
    for index, line in enumerate(selected):
        block = _block(line, session_key, query)
        if page and used + len(block) > query.page_chars:
            flags = f"{query.flags()} " if query.flags() else ""
            page.append(
                f"{len(selected) - index} more events. Next page: "
                f"{COMMAND} {session_key} {flags}--after {last_id}\n"
            )
            return "\n".join(page)
        page.append(block)
        used += len(block)
        last_id = line.event_id
    page.append("End of what these filters select.\n" if page else "Nothing matches.\n")
    return "\n".join(page)


def outline(lines: Sequence[Line], session_key: str, query: Query) -> str:
    """One line per turn: how an agent knows what there is to ask for."""
    first, last = _turn_range(lines, query)
    by_turn: dict[int, list[Line]] = {}
    for line in lines:
        by_turn.setdefault(line.turn, []).append(line)
    rows: list[str] = []
    used = 0
    for turn in sorted(by_turn):
        if turn == 0 or (first is not None and turn < first) or (last is not None and turn > last):
            continue
        members = by_turn[turn]
        opening = next((item.event for item in members if item.event.role == "user"), None)
        words = " ".join(((opening.text if opening else "") or "").split())[:OUTLINE_WORDS]
        if opening and opening.image_media_types:
            words = f"{words} [{len(opening.image_media_types)} image(s)]".strip()
        size = sum(len(item.event.text or "") for item in members)
        when = f"{opening.ts:%Y-%m-%d %H:%M}" if opening and opening.ts else "?"
        row = f"{turn:>4}  {when}  {size:>9,} chars  {words}"
        if rows and used + len(row) + 1 > query.page_chars:
            rows.append(f"More: {COMMAND} {session_key} --outline --turns {turn}-")
            return "\n".join(rows) + "\n"
        rows.append(row)
        used += len(row) + 1
    return ("\n".join(rows) if rows else "Nothing matches.") + "\n"


def one_event(
    lines: Sequence[Line], session_key: str, event_id: str, offset: int, page_chars: int
) -> str | None:
    """One event in whole, tools included, a page at a time; None when it is not here."""
    line = next((item for item in lines if item.event_id == event_id), None)
    if line is None:
        return None
    body = _body(line, TOOLS_ALL)
    shown = body[offset : offset + page_chars]
    left = len(body) - offset - len(shown)
    more = (
        f"\n{left:,} more characters: {COMMAND} {session_key} --event {event_id} "
        f"--offset {offset + len(shown)}"
        if left > 0
        else ""
    )
    return f"#{line.event_id} turn {line.turn} {line.event.role}\n{shown}{more}\n"


def _quote(text: str | None) -> str:
    text = (text or "").strip()
    if len(text) <= QUOTE_CHARS:
        return text
    return f"{text[:QUOTE_CHARS]}… (cut)"


def last_exchange(lines: Sequence[Line]) -> tuple[str, str]:
    """The last message from the user's side and the last plain reply, each cut to size."""
    message = reply = ""
    for line in reversed(lines):
        event = line.event
        if not message and event.role == "user" and (event.text or event.image_media_types):
            message = "\n".join(filter(None, [_quote(event.text), *_images(line)]))
        if (
            not reply
            and event.role == "assistant"
            and event.text
            and not event.tool_name
            and not event.failed
        ):
            reply = _quote(event.text)
        if message and reply:
            break
    return message, reply


def continuation_note(
    session_key: str,
    *,
    title: str = "",
    issues: Sequence[str] = (),
    lines: Sequence[Line] = (),
    mid_task: bool = False,
) -> str:
    """The first message of a chat continued from an old one: small, and how to read more."""
    message, reply = last_exchange(lines)
    parts = [
        "Continue the work from the earlier part of this AgentDeck chat. That part is asleep "
        "and waking it is expensive, so do not write to it. Read only what you need from it.",
    ]
    facts = []
    if title:
        facts.append(f"Title: {title}")
    if issues:
        facts.append(f"Issues: {', '.join(issues)}")
    if facts:
        parts.append("\n".join(facts))
    if message:
        parts.append(f"The last message there:\n{message}")
    if reply:
        parts.append(f"The last reply there:\n{reply}")
    command = f"{COMMAND} {session_key}"
    if mid_task:
        parts.append(
            "It stopped in the middle of a task. Before acting, read its last turns in full, "
            f"so nothing is repeated:\n{command} --last 2 --tools all"
        )
    parts.append(
        "To read more, start with the outline, one line per turn:\n"
        f"{command} --outline\n"
        "Then read what you need, a page at a time:\n"
        f"{command} --turns 3-7\n"
        f"{command} --search 'some words'\n"
        "Tool calls and results are left out unless you add --tools calls or --tools all."
    )
    parts.append(
        "Treat what you read as the prior conversation and continue from where it ended. A "
        "final departure message that links to this new chat and tells the old chat to stop "
        "applies only to that old chat, not to this session."
    )
    return "\n\n".join(parts)
