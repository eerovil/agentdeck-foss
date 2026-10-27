"""Incremental transcript parsing: projects/<slug>/<uuid>.jsonl → events.

Each line is one JSON object. We tolerate partial trailing lines (the CLI
appends line-buffered, but a read can still catch a half-written line) and
skip malformed lines with a counter rather than ever raising.

seq = 1-based line index, so a live tail asks for events with seq > last_seq
and a byte offset lets us resume a read without rescanning the whole file.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from ...images import SUPPORTED_IMAGE_MEDIA_TYPES
from ...models import TokenTotals, TranscriptEvent
from ...questions import trailing_question as trailing_question
from ..transcript_reader import (
    AGENT_TEXT,
    PROMPT,
    RESET,
    LineParser,
    NewestMessages,
    TranscriptMeta,
    TranscriptRead,
    TranscriptReader,
    first_line,
    newest_messages_backwards,
    objects_backwards,
    parse_ts,
    read_line,
)
from ..transcript_reader import (
    token_totals as token_totals,
)
from ..transcript_reader import (
    transcript_cursor as transcript_cursor,
)

log = logging.getLogger(__name__)

# Local alias keeps existing private-name call sites unchanged.
_parse_ts = parse_ts

_MAX_TOOL_SUMMARY = 160
# Matches the Codex reader's expandable-input cap so both providers truncate the
# same way behind one shared `<details>` row.
_MAX_TOOL_DETAIL = 8000
# Fields worth reading first when a tool call is expanded; everything else keeps
# the order the CLI wrote it in.
_DETAIL_KEY_ORDER = {
    key: index
    for index, key in enumerate(
        ("command", "file_path", "path", "pattern", "query", "url", "prompt", "description")
    )
}
# Short caps for list-card previews (last_text) and bookkeeping snippets.
_MAX_TEXT = 4000
# The rendered chat transcript shows the *full* agent/user message, not a
# preview — cap it generously so long responses aren't truncated, while still
# bounding a pathological multi-MB paste from bloating a transcript payload.
_MAX_RENDERED_TEXT = 200_000
_MAX_IMAGE_DATA_CHARS = 14 * 1024 * 1024
_MAX_IMAGE_TOTAL_CHARS = 28 * 1024 * 1024
_MAX_IMAGES = 4
_TASK_ID_MARKER = b"<task-id>"
_TASK_ID_RE = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
_COMMAND_ENVELOPE_RE = re.compile(
    r"^\s*<command-message>\s*(?P<message>.*?)\s*</command-message>\s*"
    r"<command-name>\s*(?P<name>.*?)\s*</command-name>"
    r"(?:\s*<command-args>\s*(?P<args>.*?)\s*</command-args>)?\s*$",
    re.DOTALL,
)
# The CLI stamps the id of a backgrounded shell command on the structured
# ``toolUseResult`` of the tool_result line that launched it. Reading the
# structured field rather than its prose ("Command running in background with
# ID: …" / "…was moved to the background (ID: …)") keeps a chat that merely
# *quotes* such a line — grepping transcripts, pasting a log — from inventing a
# task that was never launched here.
_BACKGROUND_TASK_MARKER = b'"backgroundTaskId"'
# A *synchronous* Agent/Task subagent never sends a ``<task-notification>``: it
# hands its report straight back as the tool_result for the call that spawned it,
# and the CLI stamps that line's structured ``toolUseResult`` with the subagent's
# own id and how it ended. Reading the structured field, as with a backgrounded
# command above, is what keeps a chat that merely quotes such a line from
# inventing a collection (#1708).
_AGENT_RESULT_MARKER = b'"agentId"'
# A background command the chat stopped itself never notifies: the CLI's TaskStop
# result, stamped with the task's id, is the last word on it (#3123).
_STOPPED_TASK_MARKER = b"Successfully stopped task"
# Only a finished agent counts as collected. A backgrounded one reports back on
# the notification path #617 built, and its launch result must not be mistaken
# for its report — hence an explicit terminal status rather than "has an id".
_COLLECTED_AGENT_STATUSES = frozenset({"completed"})
# How far back ``task_ledger`` walks for launch and notification records. A chat
# only needs the ones written since its outstanding work started, and this covers
# more than an hour of a busy orchestrator's transcript; the prefilter keeps the
# walk to a byte scan for everything else.
COLLECTED_TASK_SCAN_LIMIT = 512 * 1024

_CONTINUING_STOP_REASONS = frozenset({"tool_use", "pause_turn"})
_TERMINAL_STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "stop_sequence", "refusal", "model_context_window_exceeded"}
)


def _turn_continues(stop_reason: object) -> bool | None:
    """Normalize known Claude lifecycle values; unknown future values stay ambiguous."""
    if stop_reason in _CONTINUING_STOP_REASONS:
        return True
    if stop_reason in _TERMINAL_STOP_REASONS:
        return False
    return None


class _Content(NamedTuple):
    """What one message.content value contributes to a normalized event."""

    text: str | None = None
    tool_name: str | None = None
    tool_summary: str | None = None
    tool_detail: str | None = None
    question: str | None = None
    answer: str | None = None
    image_media_types: tuple[str, ...] = ()


def _text_from_content(content: object) -> _Content:
    """Project a message.content value onto the normalized event fields.
    ``question`` is the AskUserQuestion prompt when this line asks one;
    ``answer`` is your reply, parsed from the AskUserQuestion tool_result (which
    the CLI writes as "Your questions have been answered: …") so the choice is
    visible in the chat instead of being hidden with the other tool output."""
    if isinstance(content, str):
        return _Content(text=content[:_MAX_RENDERED_TEXT] or None)
    if not isinstance(content, list):
        return _Content()
    texts: list[str] = []
    tool_name: str | None = None
    tool_summary: str | None = None
    tool_detail: str | None = None
    question: str | None = None
    answer: str | None = None
    image_media_types = tuple(media_type for media_type, _data in _image_sources(content))
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif btype == "tool_use":
            tool_name = block.get("name")
            tool_detail = _detail_tool_input(block.get("input"))
            if tool_name == "AskUserQuestion":
                question = _ask_question(block.get("input"))
                tool_summary = question or _summarize_tool_input(block.get("input"))
            else:
                tool_summary = _summarize_tool_input(block.get("input"))
        elif btype == "tool_result":
            result_text = _stringify_tool_result(block.get("content"))
            parsed = _askuserquestion_answer(result_text)
            if parsed is not None:
                answer = parsed  # surfaced as your reply; the raw text stays hidden
            else:
                texts.append(result_text)
        elif btype == "thinking" and isinstance(block.get("thinking"), str):
            texts.append(block["thinking"])
    text = "\n".join(t for t in texts if t).strip()
    return _Content(
        text=text[:_MAX_RENDERED_TEXT] or None,
        tool_name=tool_name,
        tool_summary=tool_summary,
        tool_detail=tool_detail,
        question=question,
        answer=answer,
        image_media_types=image_media_types,
    )


def _image_sources(content: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(content, list):
        return ()
    images = []
    total = 0
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        source = block.get("source")
        if not isinstance(source, dict) or source.get("type") != "base64":
            continue
        media_type = source.get("media_type")
        data = source.get("data")
        if (
            media_type not in SUPPORTED_IMAGE_MEDIA_TYPES
            or not isinstance(data, str)
            or not data
            or len(data) > _MAX_IMAGE_DATA_CHARS
            or len(images) >= _MAX_IMAGES
        ):
            continue
        total += len(data)
        if total > _MAX_IMAGE_TOTAL_CHARS:
            break
        images.append((media_type, data))
    return tuple(images)


def transcript_image(path: Path, seq: int, image_index: int) -> tuple[str, bytes] | None:
    """Decode one bounded image from an exact transcript line."""
    if seq < 1 or image_index < 0:
        return None
    data = read_line(path, seq)
    message = data.get("message") if isinstance(data, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    images = _image_sources(content)
    if image_index >= len(images):
        return None
    media_type, encoded = images[image_index]
    try:
        return media_type, base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None


# The AskUserQuestion tool_result the CLI writes once you answer (or dismiss)
# through the interactive widget. The answer payload after the prefix is a
# comma-joined list of ``"question"="answer"`` pairs, or free text for the
# responded/selected variants. "did not answer" carries no choice, so it is not
# surfaced. (Phrasings are stable strings emitted by the tool; if they change a
# future CLI just falls back to hiding the tool_result — no wrong output.)
_ASKQ_ANSWER_PREFIXES = (
    "Your questions have been answered: ",
    "Before going idle the user had selected: ",
    "The user responded: ",
)
_ASKQ_ANSWER_SUFFIX = " You can now continue with these answers in mind."
_ASKQ_CURRENT_PREFIX = "The user answered: "
_ASKQ_CURRENT_SUFFIX = (
    ". Read the answers carefully — they may request clarification, changes, or that you "
    "not proceed — and follow what they actually say."
)
_ASKQ_PAIR = re.compile(r'"([^"]*)"="([^"]*)"')


def _askuserquestion_answer(text: object) -> str | None:
    """Your choice, extracted from an AskUserQuestion tool_result, or None."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.startswith(_ASKQ_CURRENT_PREFIX):
        if not stripped.endswith(_ASKQ_CURRENT_SUFFIX):
            return None
        body = stripped[len(_ASKQ_CURRENT_PREFIX) : -len(_ASKQ_CURRENT_SUFFIX)]
        pairs = _ASKQ_PAIR.findall(body)
        # This newer envelope is common prose, so accept only the exact quoted
        # question/answer payload the CLI writes for AskUserQuestion.
        if not pairs or body != ", ".join(f'"{q}"="{a}"' for q, a in pairs):
            return None
        return "; ".join(f"{q}: {a}" for q, a in pairs)[:_MAX_TEXT] or None
    for prefix in _ASKQ_ANSWER_PREFIXES:
        if stripped.startswith(prefix):
            body = stripped[len(prefix) :].removesuffix(_ASKQ_ANSWER_SUFFIX).strip()
            body = body[:-1] if body.endswith(".") else body
            pairs = _ASKQ_PAIR.findall(body)
            if pairs:
                return "; ".join(f"{q}: {a}" for q, a in pairs)[:_MAX_TEXT] or None
            return body[:_MAX_TEXT] or None
    return None


def _ask_question(value: object) -> str | None:
    """The prompt(s) from an AskUserQuestion tool input — its question text lives
    in ``input.questions[].question`` (the multiple-choice tool), not in any text
    block, so it is invisible to the plain-text extractors. Multiple questions are
    joined so the whole ask surfaces."""
    if not isinstance(value, dict):
        return None
    questions = value.get("questions")
    if not isinstance(questions, list):
        return None
    asks = [
        q["question"].strip()
        for q in questions
        if isinstance(q, dict) and isinstance(q.get("question"), str) and q["question"].strip()
    ]
    return " ".join(asks)[:_MAX_TEXT] or None if asks else None


def _summarize_tool_input(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    # A run of shell calls in one worktree peeks as the same truncated
    # `cd /long/path/…`; the call's own description is prose about intent, so it
    # tells the rows apart. The whole command stays one expansion away.
    described = first_line(value.get("description"), _MAX_TOOL_SUMMARY)
    if described:
        return described
    for key in ("command", "file_path", "path", "query", "pattern", "prompt", "url"):
        v = value.get(key)
        if isinstance(v, str) and v:
            return f"{key}: {v}"[:_MAX_TOOL_SUMMARY]
    keys = ", ".join(str(k) for k in value)
    return (keys or None) and keys[:_MAX_TOOL_SUMMARY]


def _detail_tool_input(value: object) -> str | None:
    """Full tool input for the expandable row: one labelled section per field.

    The one-line summary truncates hard, so the long thing you actually want to
    read — the whole shell command, the patch, the subagent prompt — is only
    reachable here. Primary fields lead so the expansion opens on the point.
    """
    if isinstance(value, str):
        return value.strip()[:_MAX_TOOL_DETAIL] or None
    if not isinstance(value, dict) or not value:
        return None
    keys = sorted(value, key=lambda key: (_DETAIL_KEY_ORDER.get(key, len(_DETAIL_KEY_ORDER)),))
    sections = []
    for key in keys:
        item = value[key]
        if isinstance(item, str):
            body = item.strip()
        elif item is None:
            continue
        else:
            try:
                body = json.dumps(item, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                body = str(item)
        if not body:
            continue
        sections.append(f"{str(key).replace('_', ' ').capitalize()}\n{body}")
    return "\n\n".join(sections)[:_MAX_TOOL_DETAIL] or None


def _stringify_tool_result(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


# Slash-command echoes are written as `user` lines wrapped in these tags. A
# command with ``<command-message>`` starts an agent turn and is normalized below
# so a dashboard send can reconcile with its durable transcript record. Local
# commands such as ``/compact`` have only ``<command-name>`` and remain hidden
# bookkeeping, along with stdout/stderr and injected caveats.
_COMMAND_NOISE_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
)
_LOCAL_COMMAND_PREFIXES = tuple(
    prefix for prefix in _COMMAND_NOISE_PREFIXES if prefix != "<command-message>"
)
_TASK_NOTIFICATION_PREFIX = "<task-notification>"


def _user_content_text(obj: dict) -> str | None:
    """Plain text of a user line's content, or None."""
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            b["text"]
            for b in content
            if isinstance(b, dict)
            and b.get("type") == "text"
            and isinstance(b.get("text"), str)
            and b["text"]
        ]
        return "\n".join(texts).strip() or None
    return None


def _is_slash_command_text(text: object) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(_COMMAND_NOISE_PREFIXES)


def _agent_slash_command_text(obj: dict) -> str | None:
    """The command the user typed when Claude expanded an agent-running slash command."""
    text = _user_content_text(obj)
    if not text or (envelope := _COMMAND_ENVELOPE_RE.fullmatch(text)) is None:
        return None
    message = html.unescape(envelope.group("message")).strip()
    command = html.unescape(envelope.group("name")).strip()
    if (
        not message
        or any(character.isspace() or character in "/<>" for character in message)
        or command != f"/{message}"
    ):
        return None
    argument_text = html.unescape(envelope.group("args") or "").strip()
    return f"{command} {argument_text}".strip()[:_MAX_RENDERED_TEXT]


def _is_task_notification_text(text: object) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(_TASK_NOTIFICATION_PREFIX)


def _is_bookkeeping_text(text: object) -> bool:
    """Protocol text that is never a conversational message, wherever it appears.

    The CLI carries the same payload through several envelopes: the processed
    ``user`` line, the ``queue-operation`` that enqueued it, and the
    ``queued_command`` attachment that delivers it into an open turn. Judging the
    *text* (not one envelope) keeps a task notification out of the chat in all of
    them, instead of hiding it in one shape and rendering it as your message in
    the others."""
    return _is_slash_command_text(text) or _is_task_notification_text(text)


def _is_local_slash_command_line(obj: dict) -> bool:
    """A local CLI command that does not start an agent turn."""
    text = _user_content_text(obj)
    return isinstance(text, str) and text.lstrip().startswith(_LOCAL_COMMAND_PREFIXES)


def _is_task_notification_line(obj: dict) -> bool:
    return _is_task_notification_text(_user_content_text(obj))


_NOTIFICATION_FIELD_RE = re.compile(r"<(summary|result)>(.*?)</\1>", re.DOTALL)


def _task_notification_event(seq: int, obj: dict) -> TranscriptEvent:
    """A ``<task-notification>`` that opened a turn, as a one-line notice (#3377).

    Hiding it left no boundary between the reply to the person and the automatic reply
    after it, so the chat folded the person's answer away as an in-between line.
    """
    fields = dict(_NOTIFICATION_FIELD_RE.findall(_user_content_text(obj) or ""))
    summary = " ".join(html.unescape(fields.get("summary", "")).split())
    result = html.unescape(fields.get("result", "")).strip()
    return TranscriptEvent(
        seq=seq,
        role="system",
        text=result[:_MAX_RENDERED_TEXT] or summary or None,
        notice=(summary or "Background task finished")[:_MAX_TOOL_SUMMARY],
        ts=_parse_ts(obj.get("timestamp")),
        turn_continues=True,
    )


def _is_compact_boundary(obj: dict) -> bool:
    """A completed local command or a compaction summary: the *user* took the
    turn, so the agent's prior turn is closed (idle), not open/working. Used to
    stop a post-``/compact`` transcript reading as a live, in-progress turn."""
    return bool(obj.get("isCompactSummary")) or _is_local_slash_command_line(obj)


def _is_noise_user(obj: dict) -> bool:
    """User lines that are bookkeeping, not conversation: meta caveats, the
    compaction summary, local slash-command echoes, and task notifications."""
    return (
        bool(obj.get("isMeta") or obj.get("isCompactSummary"))
        or _is_local_slash_command_line(obj)
        or _is_task_notification_line(obj)
    )


def _usage_totals(usage: object) -> TokenTotals | None:
    """Normalize a Claude ``message.usage`` block into TokenTotals, or None."""
    if not isinstance(usage, dict):
        return None
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cread = int(usage.get("cache_read_input_tokens", 0) or 0)
    ccreate = int(usage.get("cache_creation_input_tokens", 0) or 0)
    if not (inp or out or cread or ccreate):
        return None
    return TokenTotals(
        input_tokens=inp, output_tokens=out, cache_read_tokens=cread, cache_creation_tokens=ccreate
    )


def _event_from_line(seq: int, data: dict) -> TranscriptEvent | None:
    ltype = data.get("type")
    if ltype == "attachment":
        attachment = data.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
            return None
        parsed = _text_from_content(attachment.get("prompt"))
        text, image_media_types = parsed.text, parsed.image_media_types
        if text is None and not image_media_types:
            return None
        if _is_bookkeeping_text(text):
            return None  # a task notification delivered into an open turn
        return TranscriptEvent(
            seq=seq,
            role="user",
            text=text,
            queued=True,
            ts=(_parse_ts(data.get("timestamp")) or _parse_ts(attachment.get("timestamp"))),
            image_media_types=image_media_types,
        )
    if ltype == "queue-operation":
        # A message typed while the agent was busy. Only the "enqueue" carries
        # the text; render it as a user turn (deduped in read_events against the
        # real user line it becomes if/when the agent later processes it).
        content = data.get("content")
        if _is_bookkeeping_text(content):
            return None  # the CLI queues its own task notifications like a message
        if data.get("operation") == "enqueue" and isinstance(content, str) and content.strip():
            return TranscriptEvent(
                seq=seq,
                role="user",
                text=content.strip()[:_MAX_RENDERED_TEXT],
                queued=True,
                ts=_parse_ts(data.get("timestamp")),
            )
        return None
    if ltype not in ("user", "assistant", "system"):
        return None  # skip mode/summary lines
    if ltype == "user" and (command := _agent_slash_command_text(data)) is not None:
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        return TranscriptEvent(
            seq=seq,
            role="user",
            text=command,
            ts=_parse_ts(data.get("timestamp")),
            image_media_types=_text_from_content(content).image_media_types,
        )
    if (
        ltype == "user"
        and not data.get("isMeta")
        and not data.get("isSidechain")
        and _is_task_notification_line(data)
    ):
        return _task_notification_event(seq, data)
    if ltype == "user" and _is_noise_user(data):
        return None  # meta caveat, command echo, or internal task notification
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    parsed = _text_from_content(content)
    text, tool_name, answer = parsed.text, parsed.tool_name, parsed.answer
    image_media_types = parsed.image_media_types
    model = message.get("model") if isinstance(message, dict) else None
    stop_reason = message.get("stop_reason") if isinstance(message, dict) else None
    usage = message.get("usage") if isinstance(message, dict) else None
    is_tool_result = tool_name is None and ltype == "user" and _looks_like_tool_result(content)
    # An AskUserQuestion answer arrives on a tool_result line but is your reply —
    # render it as a user turn, not a (hidden) tool result.
    role = "user" if answer is not None else ("tool" if is_tool_result else ltype)
    if text is None and tool_name is None and answer is None and not image_media_types:
        return None  # nothing renderable (e.g. an isMeta bookkeeping line)
    return TranscriptEvent(
        seq=seq,
        role=role,
        text=text,
        tool_name=tool_name,
        tool_summary=parsed.tool_summary,
        tool_detail=parsed.tool_detail,
        question=parsed.question,
        answer=answer,
        model=model if isinstance(model, str) else None,
        tokens=_usage_totals(usage),
        ts=_parse_ts(data.get("timestamp")),
        turn_continues=_turn_continues(stop_reason),
        # The CLI gives up on a failing request by writing its own apology as if
        # the model had replied ("API Error: 529 Overloaded…"). Only this flag
        # tells that apart from a real answer, so the deck can show the turn as
        # failed instead of as a healthy finished chat (#1463).
        failed=data.get("isApiErrorMessage") is True,
        image_media_types=image_media_types,
    )


def _looks_like_tool_result(content: object) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


class _ClaudeLineParser(LineParser):
    def event_from_line(self, seq: int, obj: dict) -> TranscriptEvent | None:
        return _event_from_line(seq, obj)

    def is_turn_boundary(self, obj: dict) -> bool:
        # A completed /compact (or other slash command) closes the turn: the
        # agent is idle afterwards, so the open-turn probe must reset here and a
        # resumed read must not carry a stale pre-compact open turn.
        return _is_compact_boundary(obj)


_READER = TranscriptReader(_ClaudeLineParser())


def read_events(
    path: Path, *, byte_offset: int = 0, seq: int = 0, limit: int | None = None
) -> TranscriptRead:
    """Read new complete lines from ``byte_offset`` onward.

    A trailing line without a newline is left unconsumed (offset stops before
    it) so the next read picks it up once fully written. ``limit`` caps the new
    bytes one call consumes, for a bulk walker that must not hold a whole
    transcript in memory.
    """
    return _READER.read_events(path, byte_offset=byte_offset, seq=seq, limit=limit)


def last_event(path: Path, *, tail: int = 65536) -> TranscriptEvent | None:
    """The most recent renderable event, read cheaply from the file tail. Used
    to tell whether the agent's turn is still open (last line is a tool call /
    tool result / user prompt → busy) or closed (last line is an assistant text
    reply → waiting for input)."""
    return _READER.last_event(path, tail=tail)


def recent_conversation(
    path: Path, *, limit: int = 4, tail: int = 1024 * 1024
) -> list[TranscriptEvent]:
    """Recent conversational messages from a bounded complete-line file tail."""
    return _READER.recent_conversation(path, limit=limit, tail=tail)


def last_context_tokens(path: Path, *, tail: int = 65536) -> int | None:
    """Current context-window occupancy in tokens: the input side of the most
    recent usage block (``input + cache_read + cache_creation``) — i.e. how large
    the prompt last sent to the model was, which is how full the context window
    is right now. Deliberately NOT ``token_totals``: that sums cache reads over
    every turn and so balloons far past the window. Read cheaply from the tail
    (the latest usage block lives near the end); None when no usage is present."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > tail:
                f.seek(size - tail)
                f.readline()  # discard the partial first line after a mid-file seek
            data = f.read()
    except OSError:
        return None
    latest: dict | None = None
    reset = False  # a compaction *after* the last usage block makes it stale
    for raw in data.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("isCompactSummary"):
            reset = True  # context was just compacted; the last usage predates it
        message = obj.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict):
            latest = usage
            reset = False  # a usage block after the compaction reflects the new size
    if latest is None or reset:
        # No usage yet, or the only usage predates a compaction — the real
        # (smaller) context isn't known until the session's next turn.
        return None
    return (
        int(latest.get("input_tokens", 0) or 0)
        + int(latest.get("cache_read_input_tokens", 0) or 0)
        + int(latest.get("cache_creation_input_tokens", 0) or 0)
    )


def _user_text(obj: dict) -> str | None:
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()[:200] or None
    if isinstance(content, list):
        parts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        joined = " ".join(parts).strip()
        return joined[:200] or None
    return None


def _assistant_text(obj: dict) -> str | None:
    """The visible text of an assistant line (text blocks only; tool_use and
    thinking blocks are ignored) — used as the agent's 'latest response'."""
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()[:_MAX_TEXT] or None
    if isinstance(content, list):
        parts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        joined = " ".join(parts).strip()
        return joined[:_MAX_TEXT] or None
    return None


def _may_carry_message(raw: bytes) -> bool:
    """Cheap gate before the JSON parse: every line that can decide the newest
    prompt or reply is a ``user`` line (real turn, compaction summary, or command
    echo), an ``assistant`` line, or a queue operation. Permissive on purpose — a
    false positive costs one parse, a false negative hides a message."""
    return b'"user"' in raw or b'"assistant"' in raw or b'"queue-operation"' in raw


def _newest_messages(path: Path) -> NewestMessages:
    """The newest prompt and newest agent reply, by reverse scan from EOF.

    ``transcript_meta``'s head+tail windows cannot see a message that a large
    agent turn has pushed into the unread middle, which is what made a card show
    an older prompt (#470) and — once a turn ended in image reads whose base64
    tool results filled the whole tail — an older *reply* than the chat holds
    (#558). Walking back to the newest line that decides each side keeps both
    independent of how much the agent wrote afterwards.
    """

    def classify(obj: dict) -> tuple[str, str | None] | None:
        if _is_compact_boundary(obj):
            # A completed /compact or local command resets the conversation; the
            # pre-compact turn must not resurface (see the forward scan below).
            return (RESET, None)
        ltype = obj.get("type")
        if ltype == "assistant":
            # Sidechain lines are kept: a spawned subagent's own transcript is
            # nothing but sidechain, and that file's reply is its card's reply.
            return (AGENT_TEXT, _assistant_text(obj))
        if ltype == "user" and (command := _agent_slash_command_text(obj)) is not None:
            return (PROMPT, command)
        if ltype == "user" and not _is_noise_user(obj) and not obj.get("isSidechain"):
            return (PROMPT, _user_text(obj))  # None for tool_result-only user lines
        if (
            ltype == "queue-operation"
            and obj.get("operation") == "enqueue"
            and isinstance(obj.get("content"), str)
            and obj["content"].strip()
            and not _is_bookkeeping_text(obj["content"])
        ):
            return (PROMPT, obj["content"].strip())
        return None

    return newest_messages_backwards(
        path,
        classify,
        prefilter=_may_carry_message,
        message_timestamp=lambda obj: _parse_ts(obj.get("timestamp")),
    )


# How ``jobs.job_message`` opens the message a chat gets when its queued job ends. A test keeps
# the two in step: this module must not import the job queue.
JOB_MESSAGE_PREFIX = "Your queued job "


def _only_tool_results(obj: dict) -> bool:
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def _is_background_text(text: object) -> bool:
    return _is_task_notification_text(text) or (
        isinstance(text, str) and text.lstrip().startswith(JOB_MESSAGE_PREFIX)
    )


# How far back the turn's opener is looked for: a working chat is re-read whenever it writes,
# so a long turn's tool output is not walked whole. Past it, nothing is known and the chat
# reads as an ordinary working one (#2760).
TURN_OPENER_SCAN_LIMIT = 1024 * 1024


def _may_open_turn(raw: bytes) -> bool:
    return _may_carry_message(raw) or b'"queued_command"' in raw


def turn_opened_by_notification(path: Path) -> bool:
    """Whether the newest turn was opened by a background notification, not a message (#2760).

    Walks back to the newest main-thread user line that carries something: a
    ``<task-notification>`` (a task the chat started has finished) or the deck's queued-job
    report says yes; anything a person, the poller or the deck sent says no, and so does a
    compaction or a local command. A message sent while the turn runs — queued, or delivered
    into it — says no as well: someone answered. Tool results in between decide nothing, and
    an opener further back than ``TURN_OPENER_SCAN_LIMIT`` is not looked for.
    """
    for obj in objects_backwards(path, prefilter=_may_open_turn, limit=TURN_OPENER_SCAN_LIMIT):
        if _is_compact_boundary(obj):
            return False
        kind = obj.get("type")
        if kind == "queue-operation" and obj.get("operation") == "enqueue":
            content = obj.get("content")
            if isinstance(content, str) and content.strip() and not _is_background_text(content):
                return False
            continue
        if kind == "attachment":
            attachment = obj.get("attachment")
            if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
                parsed = _text_from_content(attachment.get("prompt"))
                sent = parsed.text is not None or bool(parsed.image_media_types)
                if sent and not _is_background_text(parsed.text):
                    return False
            continue
        if kind != "user" or obj.get("isSidechain") or obj.get("isMeta"):
            continue
        if _only_tool_results(obj):
            continue
        return _is_background_text(_user_content_text(obj))
    return False


def _task_ids(payload: object) -> set[str]:
    """Every ``<task-id>`` a notification payload names, whatever shape it is in."""
    if isinstance(payload, str):
        return set(_TASK_ID_RE.findall(payload))
    if isinstance(payload, list):
        found: set[str] = set()
        for block in payload:
            found |= _task_ids(
                block.get("text") or block.get("content")
                if isinstance(block, dict)
                else block
            )
        return found
    return set()


class TaskLedger(NamedTuple):
    """What one chat's transcript says about the background work it started.

    ``collected`` is every task id the chat has taken delivery of; ``outstanding``
    is every background *shell* task it launched and has not. Both come out of one
    backward walk on purpose: a launch always precedes its own collection, so any
    launch the budget reaches carries its collection inside the same budget. Two
    independently-bounded reads could disagree and report a long-collected task as
    still running.
    """

    collected: frozenset[str] = frozenset()
    outstanding: frozenset[str] = frozenset()


def _may_carry_task_record(raw: bytes) -> bool:
    """Cheap gate before the JSON parse for the records the ledger reads."""
    return (
        _TASK_ID_MARKER in raw
        or _BACKGROUND_TASK_MARKER in raw
        or _AGENT_RESULT_MARKER in raw
        or _STOPPED_TASK_MARKER in raw
    )


def _launched_background_task(obj: dict) -> str | None:
    """The background shell task id this tool_result line launched, if any."""
    result = obj.get("toolUseResult")
    if not isinstance(result, dict):
        return None
    task_id = result.get("backgroundTaskId")
    return task_id.strip() or None if isinstance(task_id, str) else None


def _finished_agent(obj: dict) -> str | None:
    """The synchronous subagent this tool_result line takes delivery of, if any.

    The id is bare (``a1b2c3``) while the subagent's transcript is
    ``agent-a1b2c3.jsonl``; ``_is_collected`` already matches both spellings, so
    record what the CLI wrote rather than guessing at the prefix here.
    """
    result = obj.get("toolUseResult")
    if not isinstance(result, dict):
        return None
    if result.get("status") not in _COLLECTED_AGENT_STATUSES:
        return None
    agent_id = result.get("agentId")
    return agent_id.strip() or None if isinstance(agent_id, str) else None


def _stopped_task(obj: dict) -> str | None:
    """The background task this TaskStop tool_result stopped, if any."""
    result = obj.get("toolUseResult")
    if not isinstance(result, dict):
        return None
    message, task_id = result.get("message"), result.get("task_id")
    if not isinstance(message, str) or not message.startswith("Successfully stopped task"):
        return None
    return task_id.strip() or None if isinstance(task_id, str) else None


def task_ledger(path: Path, *, limit: int = COLLECTED_TASK_SCAN_LIMIT) -> TaskLedger:
    """This chat's background work: what it has collected, what is still out.

    A chat that spawned an async agent — or backgrounded a shell command, whether
    deliberately or by letting one run past its timeout — gets the result as a
    ``<task-notification>`` the CLI *queues*: an ``enqueue`` queue-operation first,
    then, when the chat actually reads it, either a real ``user`` turn carrying the
    notification or a ``remove`` queue-operation for one consumed while a turn was
    already running. Only that second half means the chat has the result; between
    the two the work is still outstanding, because the chat is about to wake up and
    deal with it.

    Deliberately a *collection* ledger rather than a completion one: it says which
    work this chat has finished with, which is the question ``ClaudeCodeProvider``
    asks to decide whether a subagent is still running and whether the chat itself
    is still waiting on a background command. A launch or notification older than
    the scan budget simply is not found, so a truncated read delays a handoff or
    misses an outstanding task — it never invents either.

    A *synchronous* subagent is collected on a different line entirely, and until
    #1708 this missed it: it never notifies, so its finished tool_result was the
    one collection record the ledger did not read, and every Explore/Plan agent a
    chat ran stayed "working" until its own ten-minute stall window expired —
    keeping a dead child row, an activity badge and a THINKING header on a chat
    whose answer was already on screen.
    """
    collected: set[str] = set()
    launched: set[str] = set()
    for obj in objects_backwards(path, prefilter=_may_carry_task_record, limit=limit):
        ltype = obj.get("type")
        if ltype == "user":
            message = obj.get("message")
            collected |= _task_ids(
                message.get("content") if isinstance(message, dict) else None
            )
            task_id = _launched_background_task(obj)
            if task_id is not None:
                launched.add(task_id)
            agent_id = _finished_agent(obj)
            if agent_id is not None:
                collected.add(agent_id)
            stopped = _stopped_task(obj)
            if stopped is not None:
                collected.add(stopped)
        elif ltype == "queue-operation" and obj.get("operation") == "remove":
            collected |= _task_ids(obj.get("content"))
    return TaskLedger(frozenset(collected), frozenset(launched - collected))


def collected_task_ids(
    path: Path, *, limit: int = COLLECTED_TASK_SCAN_LIMIT
) -> frozenset[str]:
    """Background task ids this chat has already taken delivery of."""
    return task_ledger(path, limit=limit).collected


def _may_carry_read_message(raw: bytes) -> bool:
    return b'"user"' in raw or b'"queued_command"' in raw


def read_message_texts(path: Path, *, since: float) -> Counter[str]:
    """How many times the chat has actually read each message text since ``since``.

    A message written into a turn that is still running is only *queued* by the
    CLI: the ``enqueue`` queue-operation it writes carries no text in stream-json
    mode, and the message is read only when it comes out of that queue — as a
    main-thread ``user`` line, or as a ``queued_command`` attachment delivered into
    the open turn. A message with neither was never read, and dies with the
    process (#2898). Texts are stripped, which is how they are compared.

    Counted rather than collected, because the same text sent twice and read once
    is one message still unread. The walk covers the whole file back to
    ``since``: a turn can print far more after reading a message than any fixed
    window, and a read missed there would be quoted to the chat a second time.
    """
    texts: Counter[str] = Counter()
    try:
        limit = path.stat().st_size
    except OSError:
        return texts
    for obj in objects_backwards(path, prefilter=_may_carry_read_message, limit=limit):
        stamp = _parse_ts(obj.get("timestamp"))
        if stamp is not None and stamp.timestamp() < since:
            break
        text: str | None = None
        if obj.get("type") == "user" and not obj.get("isSidechain"):
            text = _user_content_text(obj)
        elif obj.get("type") == "attachment":
            attachment = obj.get("attachment")
            if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
                text = _text_from_content(attachment.get("prompt")).text
        if text and text.strip():
            texts[text.strip()] += 1
    return texts


def transcript_meta(path: Path, *, head: int = 65536, tail: int = 32768) -> TranscriptMeta:
    """Cheap head/tail session metadata as a shared ``TranscriptMeta``. Claude
    Code writes an ``ai-title`` line (a concise session summary), ``last-prompt``
    lines, and a ``cwd`` field on every entry; we read the head (ai-title/first
    prompt/cwd live there) and the tail (latest last-prompt + latest assistant
    reply), latest occurrence winning. ``last_role`` ("user"/"agent") is who sent
    the most recent *text* message, so the card can order the two lines
    correctly. ``cwd`` is shown for idle sessions too — the live-process registry
    is gone once a session is idle."""
    ai_title: str | None = None
    last_prompt: str | None = None
    first_user: str | None = None
    cwd: str | None = None
    last_text: str | None = None
    last_role: str | None = None
    last_message_at: datetime | None = None
    model: str | None = None
    compacted = False  # a /compact with no real prompt after it → drop stale last-prompt
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head_bytes = f.read(head)
            tail_bytes = b""
            if size > head:
                f.seek(max(head, size - tail))
                tail_bytes = f.read()
    except OSError:
        return TranscriptMeta()

    def scan(blob: bytes, skip_first_partial: bool) -> None:
        nonlocal ai_title, last_prompt, first_user, cwd, last_text, last_role
        nonlocal last_message_at, model, compacted
        lines = blob.split(b"\n")
        if skip_first_partial and lines:
            lines = lines[1:]  # a mid-file seek can land inside a line
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if _is_compact_boundary(obj):
                # A completed /compact resets the conversation: drop the stale
                # pre-compact prompt/reply so the card doesn't resurface an old
                # turn (the cheap head+tail read can't reach the real latest
                # message past a huge summary). A real turn after the compaction
                # repopulates these below.
                compacted = True
                last_prompt = None
                last_text = None
                last_role = None
                last_message_at = None
                continue
            if cwd is None and isinstance(obj.get("cwd"), str) and obj["cwd"]:
                cwd = obj["cwd"]
            t = obj.get("type")
            if t == "ai-title" and isinstance(obj.get("aiTitle"), str):
                ai_title = obj["aiTitle"].strip() or ai_title
            elif t == "last-prompt" and not compacted and isinstance(obj.get("lastPrompt"), str):
                # bookkeeping fallback — written a few lines *after* the user
                # turn, so the real user line below is what keeps us in sync.
                # Skipped after a compaction: its record is the stale pre-compact
                # prompt, which would undo the reset above.
                last_prompt = obj["lastPrompt"].strip() or last_prompt
            elif t == "assistant":
                message = obj.get("message")
                if isinstance(message, dict) and isinstance(message.get("model"), str):
                    model = message["model"] or model
                at = _assistant_text(obj)
                if at:
                    last_text = at
                    last_role = "agent"
                    last_message_at = _parse_ts(obj.get("timestamp"))
            elif t == "user":
                if not obj.get("isSidechain"):
                    command = _agent_slash_command_text(obj)
                    ut = command if command is not None else (
                        None if _is_noise_user(obj) else _user_text(obj)
                    )
                    if ut:
                        if first_user is None:
                            first_user = ut
                        last_prompt = ut  # authoritative newest prompt (matches detail)
                        last_role = "user"
                        last_message_at = _parse_ts(obj.get("timestamp"))
                        compacted = False  # a genuine post-compact prompt is current
            elif (
                t == "queue-operation"
                and obj.get("operation") == "enqueue"
                and isinstance(obj.get("content"), str)
                and obj["content"].strip()
                and not _is_bookkeeping_text(obj["content"])
            ):
                last_prompt = obj["content"].strip()  # typed-while-busy message
                last_role = "user"
                last_message_at = _parse_ts(obj.get("timestamp"))
                compacted = False

    scan(head_bytes, skip_first_partial=False)
    if tail_bytes:
        scan(tail_bytes, skip_first_partial=True)
    # The windows above can miss the newest prompt or reply entirely when one
    # agent turn is larger than the tail; the reverse scan is what decides them,
    # and their order with it. A slot it could not settle keeps the window value.
    last_prompt, last_text, last_role, last_message_at = _newest_messages(
        path
    ).merged_with(
        prompt=last_prompt,
        text=last_text,
        role=last_role,
        message_at=last_message_at,
    )
    return TranscriptMeta(
        title=ai_title,
        last_prompt=last_prompt,
        first_prompt=first_user,
        cwd=cwd,
        last_text=last_text,
        last_role=last_role,
        last_message_at=last_message_at,
        model=model,
    )


def last_model(events: list[TranscriptEvent]) -> str | None:
    for ev in reversed(events):
        if ev.model:
            return ev.model
    return None


def load_todos(config_dir: Path, session_id: str) -> list[dict]:
    """Read tasks/<sessionId>/*.json todo items; best-effort, never fatal."""
    tdir = config_dir / "tasks" / session_id
    if not tdir.is_dir():
        return []
    todos: list[dict] = []
    for path in sorted(tdir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and (item.get("subject") or item.get("description")):
                todos.append(
                    {
                        "subject": item.get("subject") or item.get("description"),
                        "status": item.get("status", "pending"),
                    }
                )
    return todos


def _prompt_line(obj: dict) -> bool:
    """A line where the person (or the deck) starts a new turn."""
    if obj.get("type") != "user" or obj.get("isMeta") or obj.get("isSidechain"):
        return False
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        return not any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        )
    return isinstance(content, str)


def first_prompt_seq(path: Path) -> int:
    """The line of the chat's first prompt, which no rollback can go before (#3474); 0
    when there is none yet. Reads only up to it."""
    try:
        with path.open("rb") as handle:
            for seq, raw in enumerate(handle, 1):
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict) and _prompt_line(obj):
                    return seq
    except OSError:
        return 0
    return 0


def rewind_ref(path: Path, seq: int) -> str | None:
    """The uuid a rollback to before the prompt on line ``seq`` resumes at (#3474).

    That is the nearest message line the prompt hangs off, so the resumed CLI holds
    the chat exactly as it was before the prompt was sent. None when line ``seq`` is
    not a prompt, or nothing comes before it to resume at (the chat's first message).
    """
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return None
    if not 1 <= seq <= len(lines):
        return None
    try:
        chosen = json.loads(lines[seq - 1])
    except ValueError:
        return None
    if not isinstance(chosen, dict) or not _prompt_line(chosen):
        return None
    earlier: dict[str, dict] = {}
    for raw in lines[: seq - 1]:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("uuid"), str):
            earlier[obj["uuid"]] = obj
    parent = chosen.get("parentUuid")
    seen: set[str] = set()
    while isinstance(parent, str) and parent and parent not in seen:
        seen.add(parent)
        obj = earlier.get(parent)
        if obj is None:
            return None
        if obj.get("type") in ("user", "assistant"):
            return parent
        parent = obj.get("parentUuid")
    return None


def fork_ref(path: Path, seq: int, *, turn_finished: bool = True) -> str | None:
    """The uuid ``--resume-session-at`` cuts at, when line ``seq`` ends a finished turn.

    A side chat copies the history up to and including that line (#2485). It must be
    an assistant line with text, and nothing the model wrote may follow it before the
    next prompt: cutting in the middle of a turn would hand the copy half an answer.
    Whether the newest turn is still running is the caller's to say, because only
    the live session knows: ``turn_finished=False`` refuses a cut with no prompt
    after it.
    """
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return None
    if not 1 <= seq <= len(lines):
        return None
    try:
        chosen = json.loads(lines[seq - 1])
    except ValueError:
        return None
    if not isinstance(chosen, dict) or chosen.get("type") != "assistant":
        return None
    if chosen.get("isSidechain") or chosen.get("isApiErrorMessage"):
        return None
    uuid = chosen.get("uuid")
    message = chosen.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    has_text = isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        for block in content
    )
    if not isinstance(uuid, str) or not uuid or not has_text:
        return None
    for raw in lines[seq:]:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("isSidechain"):
            continue
        if obj.get("type") == "assistant":
            return None
        if _prompt_line(obj):
            return uuid
    return uuid if turn_finished else None
