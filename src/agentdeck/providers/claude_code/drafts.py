"""The reply Claude is writing right now, for voice mode's early read (#4543).

A deck-owned worker runs with ``--include-partial-messages``, so its stdout carries
``stream_event`` lines: the message's start, each text piece, the block's end and the
message's end reason, before the finished ``assistant`` line. :class:`DraftBuffer` turns
them into one draft per worker that the web reads over the runtime socket.

What the page may read is decided here, not in the browser:

- A draft is built only from a ``message_start`` and a text ``content_block_start`` this
  buffer saw itself. Pieces after a runtime restart (the holder replays from mid-message)
  have no start and make no draft; that message is read from its finished transcript line.
- Only the main thread counts: a helper agent's pieces (``parent_tool_use_id``) and
  thinking blocks are ignored.
- Each text block that opens with 🔊 is marked ``first`` (the turn's first one), ``later``
  or ``unknown`` (this process did not see the turn start). The page reads a first block as
  it streams, and the others only once the message ends with ``end_turn``.
- Only whole sentences are shown while the block is being written, so the projection moves
  a handful of times a reply, not once per piece.
- The draft outlives ``message_stop``, the finished line and the turn's ``result``: those
  come within a millisecond of the end reason, and the change feed reads 100 ms later. It
  is replaced by the next message's start, or dropped with the process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VOICE_MARK = "🔊"
FIRST = "first"
LATER = "later"
UNKNOWN = "unknown"
# A sentence ends at one of these, followed by more text (a space or a new line).
_STOPS = ".!?…"


@dataclass
class _Draft:
    message_id: str
    block: int | None = None
    text: str = ""
    # "" while the block's opening is not written yet, or for a block without 🔊.
    voice: str = ""
    decided: bool = False
    complete: bool = False
    stop_reason: str = ""


def whole_sentences(text: str) -> str:
    """``text`` up to its last finished sentence: a stop or a line break with more after."""
    for index in range(len(text) - 2, -1, -1):
        char = text[index]
        if char == "\n" or (char in _STOPS and text[index + 1].isspace()):
            return text[: index + 1]
    return ""


class DraftBuffer:
    """One worker's draft, fed every stdout line in order by the worker's read loop."""

    def __init__(self, *, turn_seen: bool = True) -> None:
        self._draft: _Draft | None = None
        # 🔊 blocks written since this turn started; None when this process did not see
        # the turn start (a runtime that reattached mid-turn).
        self._voice_blocks: int | None = 0 if turn_seen else None

    def take(self, event: dict[str, Any]) -> bool:
        """Apply one stdout line; True when the draft may have changed."""
        etype = event.get("type")
        if etype == "result":
            # The next turn starts counting again; the draft itself stays readable.
            self._voice_blocks = 0
            return False
        if event.get("parent_tool_use_id"):
            return False
        if etype == "assistant":
            return self._finished_line(event.get("message"))
        if etype != "stream_event":
            return False
        inner = event.get("event")
        if not isinstance(inner, dict):
            return False
        kind = inner.get("type")
        if kind == "message_start":
            message = inner.get("message")
            message_id = message.get("id") if isinstance(message, dict) else None
            if not isinstance(message_id, str) or not message_id:
                return False
            self._draft = _Draft(message_id)
            return True
        draft = self._draft
        if draft is None:
            return False
        if kind == "content_block_start":
            block = inner.get("content_block")
            if isinstance(block, dict) and block.get("type") == "text":
                # A new text block of the same message replaces the last one.
                self._draft = _Draft(draft.message_id, block=inner.get("index"))
                return True
            return False
        if kind == "content_block_delta":
            delta = inner.get("delta")
            if (
                draft.block is None
                or inner.get("index") != draft.block
                or draft.complete
                or not isinstance(delta, dict)
                or delta.get("type") != "text_delta"
                or not isinstance(delta.get("text"), str)
            ):
                return False
            draft.text += delta["text"]
            if not draft.decided and draft.text.strip():
                draft.decided = True
                if draft.text.lstrip().startswith(VOICE_MARK):
                    draft.voice = self._next_voice()
            return True
        if kind == "content_block_stop":
            if draft.block is not None and inner.get("index") == draft.block:
                draft.complete = True
                return True
            return False
        if kind == "message_delta":
            delta = inner.get("delta")
            reason = delta.get("stop_reason") if isinstance(delta, dict) else None
            if isinstance(reason, str) and reason:
                draft.stop_reason = reason
                return True
        return False

    def _next_voice(self) -> str:
        if self._voice_blocks is None:
            return UNKNOWN
        self._voice_blocks += 1
        return FIRST if self._voice_blocks == 1 else LATER

    def _finished_line(self, message: object) -> bool:
        draft = self._draft
        if (
            draft is None
            or draft.block is None
            or draft.complete
            or not isinstance(message, dict)
            or message.get("id") != draft.message_id
        ):
            return False
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "text" for part in content
        ):
            draft.complete = True
            return True
        return False

    def view(self) -> dict[str, Any] | None:
        """What the web reads: only a 🔊 block, and only its whole sentences until it ends;
        None until it has one."""
        draft = self._draft
        if draft is None or not draft.voice:
            return None
        text = draft.text if draft.complete else whole_sentences(draft.text)
        if not text:
            return None
        return {
            "message_id": draft.message_id,
            "block": draft.block,
            "text": text,
            "complete": draft.complete,
            "stop_reason": draft.stop_reason,
            "voice": draft.voice,
        }
