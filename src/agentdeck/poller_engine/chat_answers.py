"""An answer typed into a parked card's worker chat reaches the issue and resumes the card (#2348).

A worker that needs a decision finishes ``needs_more_info`` and its card rests in *Needs More
Info*. The operator very often answers in the worker's own chat rather than on GitHub. The worker
reads the answer and carries on, but the poller never heard it: the card stayed parked, and the
answer was nowhere on the issue a later reader opens.

**The deck, not the worker, reports it.** When a person's message is accepted into a chat whose
current Run is a card resting in Needs More Info, the deck leaves one small JSON file per message
in this instance's ``chat-answers/`` directory (temp file, then rename — the web process never
waits on a wake's lock). The next wake, quiet or full, reads them:

* every answer is posted on the issue, quoted verbatim, with a link to the chat;
* the first answer also moves the card to *In progress* under the **same generation**. No new
  dispatch is made — the chat that took the message is the worker, and its card-control command
  still proves this lease — so its later ``finish`` is an ordinary terminal report. A worker
  that finishes before that wake gets the same post-and-resume from ``finish`` itself (#2499).

That comment carries a lease-bound ``status=resumed`` marker. The card's terminal marker is the
*newest* marker for the generation, and the one the worker's ``needs_more_info`` left would
otherwise read as "the worker finished, its status write was lost" and move the card straight
back. ``resumed`` names no board role, so the lease reads as unreported again, which is exactly
what a running worker is.

A request names the generation the deck saw, and the wake acts only while that is still the
card's lease: an answer written against a Run that has since been replaced says nothing about the
new one. Resuming counts against no cap — the chat is already running — and is skipped for a card
with its own next step already decided (a dispatch in flight, a queued instruction, a stop, a
release), whose answer is still posted.

**Never lost, never twice.** A waiting file becomes a pending delivery on its card, and the
file is removed only after that is checkpointed. Every answer comment carries its own id
marker. A delivery is marked *attempted* and checkpointed before its comment is written, and it
leaves the card only once the write is confirmed. An attempted delivery that was not confirmed
is retried only after a live read of the issue's recent comments shows its marker is not
there; a read that cannot be made waits for the next wake. A board write that did not land is
retried on later wakes: it is idempotent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# RESUMED_TOKEN lives beside TERMINAL_MARKER, which reads it too (#2689).
from .github import CHAT_ANSWER_MARKER, RESUMED_TOKEN, TERMINAL_MARKER
from .state import write_json_atomic

DIRECTORY = "chat-answers"
# How many answered request ids a card remembers. Only the requests still waiting on disk can be
# read twice, and a wake removes them, so this only has to outlast a crash between the two.
REMEMBERED_IDS = 50
# GitHub refuses a comment over 65,536 characters; leave room for the quoting and the marker.
MAX_TEXT = 20_000

_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")
_KEY_RE = re.compile(r"[A-Za-z0-9_.-]+#[1-9][0-9]*")
_GENERATION_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def usable_id(value: str | None) -> bool:
    """Whether ``value`` can name a request file (a press's client action id can)."""
    return bool(_ID_RE.fullmatch(value or ""))


class RequestError(ValueError):
    """A request that cannot be written."""


@dataclass(frozen=True)
class Answer:
    """One well-formed answer waiting on disk."""

    id: str
    key: str
    generation: str
    text: str
    session_url: str
    requested_at: str


def directory(state_path: str | Path) -> Path:
    """Where one instance's waiting chat answers live: beside its ``state.json``."""
    return Path(state_path).expanduser().parent / DIRECTORY


def request_path(state_path: str | Path, request_id: str) -> Path:
    return directory(state_path) / f"{request_id}.json"


def write(
    state_path: str | Path,
    *,
    request_id: str,
    key: str,
    generation: str,
    text: str,
    session_url: str = "",
    now: datetime | None = None,
) -> Path:
    """Leave one chat answer for the next wake, atomically, and return its path.

    ``key`` is the engine's own card key (``repo#number``) and ``generation`` the lease the deck
    saw the chat's Run holding. Writing the same ``request_id`` twice rewrites one file.
    """
    if not _ID_RE.fullmatch(request_id or ""):
        raise RequestError("the answer carried no usable id")
    if not _KEY_RE.fullmatch(key or ""):
        raise RequestError(f"{key!r} is not a Poller card key")
    if not _GENERATION_RE.fullmatch(generation or ""):
        raise RequestError("the answer names no Run")
    if not (text or "").strip():
        raise RequestError("the answer is empty")
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    path = request_path(state_path, request_id)
    write_json_atomic(
        path,
        {
            "id": request_id,
            "key": key,
            "generation": generation,
            "text": text,
            "session_url": session_url,
            "requested_at": moment.isoformat(),
        },
    )
    return path


def parse(raw: object) -> Answer | None:
    """The answer a request file holds, or ``None`` when it is malformed."""
    if not isinstance(raw, dict):
        return None
    request_id = str(raw.get("id") or "")
    key = str(raw.get("key") or "")
    generation = str(raw.get("generation") or "")
    text = raw.get("text")
    if (
        not _ID_RE.fullmatch(request_id)
        or not _KEY_RE.fullmatch(key)
        or not _GENERATION_RE.fullmatch(generation)
        or not isinstance(text, str)
        or not text.strip()
    ):
        return None
    session_url = str(raw.get("session_url") or "")
    if not session_url.startswith(("http://", "https://")):
        session_url = ""
    return Answer(
        id=request_id,
        key=key,
        generation=generation,
        text=text,
        session_url=session_url,
        requested_at=str(raw.get("requested_at") or ""),
    )


def read(folder: Path) -> tuple[list[Path], list[Answer], list[str]]:
    """Every waiting answer, oldest first.

    Returns the files to remove once the wake has checkpointed what it recorded about them, the
    answers, and one log line per file it could not use. A file that will not parse is removed
    too; one that cannot be read right now is left for the next wake.
    """
    done: list[Path] = []
    answers: list[Answer] = []
    log: list[str] = []
    try:
        paths = list(folder.glob("*.json"))
    except OSError:
        return done, answers, log
    loaded: list[tuple[datetime, str, Answer]] = []
    for path in paths:
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.append(f"chat answer {path.name} unreadable ({exc}); left for next wake")
            continue
        except ValueError as exc:
            log.append(f"chat answer {path.name} is not JSON ({exc}); discarded")
            done.append(path)
            continue
        done.append(path)
        answer = parse(raw)
        if answer is None:
            log.append(f"chat answer {path.name} malformed; discarded")
            continue
        asked = _moment(answer.requested_at) or datetime.min.replace(tzinfo=UTC)
        loaded.append((asked, path.name, answer))
    answers = [answer for _, _, answer in sorted(loaded, key=lambda item: item[:2])]
    return done, answers, log


def discard(paths: list[Path]) -> None:
    """Remove answers whose outcome is now durable on the card."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # read again next wake, and passed over there as an id already answered


def seen(rec: dict, request_id: str) -> bool:
    return request_id in (rec.get("chat_answers") or ())


def remember(rec: dict, request_id: str) -> None:
    ids = [value for value in rec.get("chat_answers") or () if isinstance(value, str)]
    ids.append(request_id)
    rec["chat_answers"] = ids[-REMEMBERED_IDS:]


def queue(rec: dict, answer: Answer) -> None:
    """Add ``answer`` to its card's pending deliveries, which a wake empties in order."""
    pending = [entry for entry in rec.get("chat_answers_pending") or () if isinstance(entry, dict)]
    pending.append(
        {
            "id": answer.id,
            "generation": answer.generation,
            "text": answer.text,
            "session_url": answer.session_url,
            "requested_at": answer.requested_at,
            "attempted": False,
            "resume": False,
        }
    )
    rec["chat_answers_pending"] = pending


def from_entry(key: str, entry: object) -> Answer | None:
    """The answer a pending delivery holds, or ``None`` when the entry is unusable."""
    if not isinstance(entry, dict):
        return None
    return parse({**entry, "key": key})


def drop(rec: dict, entry: object) -> None:
    """Remove one pending delivery; the list goes with its last entry."""
    pending = [value for value in rec.get("chat_answers_pending") or () if value is not entry]
    if pending:
        rec["chat_answers_pending"] = pending
    else:
        rec.pop("chat_answers_pending", None)


def answer_marker(request_id: str) -> str:
    """The marker every answer comment carries, which a retry looks for first."""
    return f"{CHAT_ANSWER_MARKER}{request_id} -->"


def resume_marker(generation: str) -> str:
    """The lease-bound marker that makes this generation read as unreported again."""
    return f"{TERMINAL_MARKER}{generation} status={RESUMED_TOKEN} -->"


def _moment(stamp: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def answered_while_parked(rec: dict, answer: Answer, *, parked: bool) -> bool:
    """Whether this answer was typed while its card rested in Needs More Info.

    The deck decides from the state file, which a wake writes, so an answer typed a moment
    before the wake that resumed the card can arrive after it. That one still counts; anything
    written after the resume is an ordinary message to a running worker.
    """
    if parked:
        return True
    resumed = rec.get("chat_resumed")
    if not isinstance(resumed, dict) or resumed.get("generation") != answer.generation:
        return False
    at, asked = _moment(resumed.get("at")), _moment(answer.requested_at)
    return at is not None and asked is not None and asked <= at


def quote_operator_text(text: str, *, cut_note: str = "the whole answer is in the chat") -> str:
    """The operator's words as a Markdown quote that is safe to post as the bot.

    Quoted so no line of it reads as a control command, and with every HTML comment opener
    neutralised so typed text can never forge a lease marker. The deck's own "also post to
    issue" (#2416) quotes through here too, so the two can never drift apart.
    """
    text = text.strip()
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rstrip() + f"\n\n[… cut here; {cut_note}]"
    lines = text.replace("<!--", "&lt;!--").splitlines()
    return "\n".join(f"> {line}" if line.strip() else ">" for line in lines)


def comment_body(answer: Answer, *, marker: str = "") -> str:
    """The issue comment one answer becomes: the operator's words, quoted, and a chat link."""
    quoted = quote_operator_text(answer.text)
    chat = f"[worker chat]({answer.session_url})" if answer.session_url else "worker chat"
    parts = [f"**Answered in the {chat}:**", "", quoted]
    if marker:
        parts += ["", "The card is back In progress; the worker carries on in that chat."]
    parts += ["", answer_marker(answer.id)]
    if marker:
        parts.append(marker)
    return "\n".join(parts)
