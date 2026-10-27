"""Which user messages in a chat the deck itself sent, and their one-line label (#3200).

A Work's chat is mostly machine text: wake-ons, review verdicts, "merged, check it live",
the "ended without a next step" reminder and the start brief all arrive as user turns. The
transcript shows each as one small grey marker that opens to the full text, so the owner can
read what happened without wading through the deck's instructions to its agent.

A message is the deck's when its delivery record says so: every one of these is sent through
``scheduled_messages`` under an ID whose prefix names who sent it (``DECK_KINDS``), and a
person's own held send carries a ``hold_reason`` instead. The text prefix is only the
fallback, for a message whose record is gone or that predates one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

#: ``scheduled_messages`` ID prefixes the deck writes for its own messages to a chat.
DECK_KINDS = frozenset({"wake-on", "work-release", "work-ending", "work-event", "work-handover"})

#: What the deck's own messages begin with, when no delivery record is found.
_PREFIX_KINDS = (
    ("AgentDeck wake-on: ", "wake-on"),
    ("AgentDeck event: ", "work-event"),
    ("AgentDeck: your last turn on ", "work-ending"),
    ("AgentDeck: ", "work-release"),
)

#: The Work start brief (``work_start.FIRST_TEXT`` after ``work_side_chat.context_line``).
#: Spawned rather than scheduled, so it has no ``scheduled_messages`` row; a side chat's
#: context line ahead of a person's own text does not carry the brief and stays theirs.
_START_RE = re.compile(r"Context: Work for \S+#\d+[^\n]*\n\nThis chat is the Work for this issue\.")

_LABEL_MAX = 72

#: When the transcript event a delivery record wrote can carry a time, relative to the
#: record's handover. A provider writes the turn within seconds of accepting it, but a
#: message handed to a busy agent may be written only when its turn ends, so the window
#: runs on for longer than any turn. Before the handover it allows only clock rounding.
DELIVERY_BEFORE = timedelta(minutes=1)
DELIVERY_AFTER = timedelta(hours=6)

_REF = r"([\w.-]+/([\w.-]+))#(\d+)"


def kind_of_record(record_id: str) -> str | None:
    kind = record_id.split(":", 1)[0]
    return kind if kind in DECK_KINDS else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def notice_kind(
    text: str | None,
    records: Mapping[str, Sequence[tuple[str, datetime | None]]],
    ts: datetime | None = None,
) -> str | None:
    """The sender kind when this user turn is a message the deck sent, else ``None``.

    A record counts only for the turn it wrote: one handed over shortly before ``ts``. A turn
    whose text the deck sent this chat at some other time is a person repeating it,
    and stays theirs whatever its prefix. A turn with no time (a queued Claude turn)
    is matched by its text alone.
    """
    if not text:
        return None
    sent = [(kind_of_record(rid), at) for rid, at in records.get(text, ())]
    sent = [(kind, at) for kind, at in sent if kind is not None]
    if sent:
        for kind, at in sent:
            if ts is None or at is None:
                return kind
            gap = _aware(ts) - _aware(at)
            if -DELIVERY_BEFORE <= gap <= DELIVERY_AFTER:
                return kind
        return None
    if _START_RE.match(text):
        return "work-start"
    for prefix, prefix_kind in _PREFIX_KINDS:
        if text.startswith(prefix):
            return "work-handover" if " is now run by the deck" in text[:120] else prefix_kind
    return None


def candidate_texts(events: Iterable) -> list[str]:
    """The user texts worth looking up in the delivery records."""
    return [e.text for e in events if e.role == "user" and e.text and not e.answer]


def _short(repo_ref: re.Match) -> str:
    return f"{repo_ref.group(2)}#{repo_ref.group(3)}"


def _refs(text: str) -> str:
    return ", ".join(_short(m) for m in re.finditer(_REF, text))


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _LABEL_MAX else text[: _LABEL_MAX - 1].rstrip() + "…"


def _ci_word(state: str) -> str:
    return {"success": "passed", "failure": "failed", "error": "failed"}.get(state, state)


def label(text: str, kind: str) -> str:
    """One short line naming what happened, e.g. ``CI passed · drone/pr``."""
    if kind == "work-start":
        return "Work started"
    if kind == "work-ending":
        return "Turn ended without a next step"
    if kind == "work-handover":
        return "Work handed over to the deck"
    body = text.split(": ", 1)[1] if ": " in text[:40] else text
    first = body.split("\n", 1)[0]
    if kind == "wake-on":
        if m := re.match(rf"pull request {_REF} was merged", first):
            return f"Merged {_short(m)}"
        if m := re.match(rf"pull request {_REF} was closed without merging", first):
            return f"Closed {_short(m)} without merging"
        if m := re.match(
            r"(?:workflow|check run|check suite|commit status) (.+?) on \S+ finished: (\w+)", first
        ):
            name = m.group(1).removeprefix("continuous-integration/")
            return _clip(f"CI {_ci_word(m.group(2))} · {name}")
        if m := re.match(r"deployment to (\S+) on \S+ finished: (\w+)", first):
            env, state = m.group(1), m.group(2)
            return _clip(f"Deployed to {env}" if state == "success" else f"Deploy to {env} {state}")
        if m := re.match(r"(?:the web worker serving )?this deck now runs ([0-9a-f]+)", first):
            return f"Deck now runs {m.group(1)[:8]}"
        if m := re.match(r"it is time to check on (.+?), as you asked", first):
            return _clip(f"Check on {m.group(1)}")
        if m := re.match(r"nothing arrived for (.+?) within (\d+) minutes", first):
            return _clip(f"Nothing arrived in {m.group(2)} min · {m.group(1)}")
    if kind == "work-event":
        if m := re.match(rf"(.+?) (?:commented|posted) on (?:issue|pull request) {_REF}", first):
            return _clip(
                f"{m.group(1)[:1].upper()}{m.group(1)[1:]} commented on {m.group(3)}#{m.group(4)}"
            )
    if kind == "work-release":
        if m := re.match(rf"the review of {_REF}.*? passed", first):
            chat = re.search(rf"\(review chat {_REF}\)", first)
            return f"Review passed · {_short(chat) if chat else _short(m)}"
        if m := re.match(rf"the deck's reviewer asks for changes on {_REF}", first):
            return f"Review asks for changes · {_short(m)}"
        if re.match(rf"{_REF}(?:, {_REF})* merged\.", first):
            return _clip(f"Merged {_refs(first.split(' merged.', 1)[0])}")
        for pattern, words in (
            (r"was behind its base, so the deck merged", "Base merged in"),
            (r"is behind its base", "Behind its base"),
            (r"GitHub checks failed", "Checks failed"),
            (r"conflicts with its base", "Conflicts with its base"),
            (r"verification failed", "Verification failed"),
            (r"GitHub refused to merge", "Merge refused"),
        ):
            if re.search(pattern, first) and (m := re.search(_REF, first)):
                return f"{words} · {_short(m)}"
    sentence = re.split(r"(?<=[.:])\s", first, maxsplit=1)[0].rstrip(".:")
    return _clip(sentence[:1].upper() + sentence[1:])
