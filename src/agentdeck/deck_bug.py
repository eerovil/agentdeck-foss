"""A worker agent reports a deck bug (#4579).

A Work's agent that thinks the deck asked for something wrong does what it asks and reports it
here, rather than quietly overriding the deck. The report becomes one request file for the
Poller instance that manages AgentDeck's own issues, the path the deck's own bug reports take
(``work_bugs``, ``poller_engine.bug_reports``): its next wake folds every report of one topic
into one issue, or a comment on it. The agent never files an issue itself: a worker on another
project speaks as that project's GitHub account, and duplicates must stay one issue.

A worker's words may carry anything, so its issue is filed without the ownership label (a
person reads it before any Work starts) and the words are quoted as untrusted evidence.

At most ``PER_CHAT`` new reports per chat in ``WINDOW``, kept in this process; one per chat,
Work and topic by the request id, which the engine also remembers. One lock covers the check,
the write and the count, so a burst from one chat cannot pass the cap.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .poller_engine import bug_reports
from .poller_engine.state import StateSchemaError, _unwrap
from .public_origin import public_url
from .work_bugs import HOME, instance
from .work_overview import work_href

PER_CHAT = 3
WINDOW = 24 * 3600.0
MIN_TEXT = 20
MAX_TEXT = 300
TOPIC_RE = re.compile(r"[a-z0-9_]{3,40}")
HEAD_RE = re.compile(r"[0-9a-fA-F]{7,40}")


#: Told with every malformed report and the topic list (#4579): the guide line names only the
#: path, so the answer teaches the body.
USAGE = (
    'Send JSON {"topic": "a_known_or_new_topic", "expected": "what the deck should have done",'
    ' "observed": "what it did"}, plus "pull_request" (one this chat registered) and "head"'
    " (a commit id) when they apply. GET this path first and reuse a listed topic."
)


class Refused(Exception):
    """A report the deck does not take: ``status`` is the HTTP answer; a malformed one (422)
    says ``USAGE`` too."""

    def __init__(self, status: int, detail: str) -> None:
        detail = f"{detail}. {USAGE}" if status == 422 else detail
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Limiter:
    """Accepted reports per chat, by time; one lock over check, write and count."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    accepted: dict[str, list[float]] = field(default_factory=dict)


def cause_for(topic: str) -> str:
    return f"{bug_reports.AGENT_REPORT}:{topic}"


def _text(name: str, value: str) -> str:
    text = bug_reports.clean_text(value, MAX_TEXT + 1)
    if len(text) < MIN_TEXT:
        raise Refused(422, f"`{name}` must say it in at least {MIN_TEXT} characters")
    if len(text) > MAX_TEXT:
        raise Refused(422, f"`{name}` must fit in {MAX_TEXT} characters")
    return text


def _state(state_path: str) -> dict | None:
    """The instance's state payload, read only, out of its ``schema_version`` envelope;
    None when it cannot be read."""
    try:
        found = json.loads(Path(state_path).expanduser().read_text())
        _version, payload = _unwrap(found) if isinstance(found, dict) else (0, None)
    except (OSError, ValueError, StateSchemaError):
        return None
    return payload if isinstance(payload, dict) else None


def report(
    limiter: Limiter,
    board: Any,
    *,
    session_key: str,
    work_id: str,
    topic: str,
    expected: str,
    observed: str,
    pull_request: str = "",
    head: str = "",
    revision: str = "",
    clock: Callable[[], float] = time.time,
) -> dict:
    """Leave one report for the Poller and say what became of it.

    ``state`` is ``filed`` for a new request file, ``already_reported`` when this chat
    reported this topic for this Work before (its file is waiting, or the engine took it).
    Only ``filed`` counts against the chat's limit; a refusal raises ``Refused``.
    """
    if not TOPIC_RE.fullmatch(topic or ""):
        raise Refused(422, "`topic` must be 3-40 of a-z, 0-9 and _, such as `pre_review_wrong`")
    expected, observed = _text("expected", expected), _text("observed", observed)
    if head and not HEAD_RE.fullmatch(head):
        raise Refused(422, "`head` must be a commit id, 7-40 hex digits")
    target = instance(board)
    if target is None:
        raise Refused(409, f"no Poller on this host manages {HOME}, so nothing files the report")
    state_path, repo = target
    cause = cause_for(topic)
    ident = bug_reports.request_id(work_id, cause, session_key)
    path = bug_reports.directory(state_path) / f"{ident}.json"
    with limiter.lock:
        now = clock()
        for key in list(limiter.accepted):
            kept = [at for at in limiter.accepted[key] if now - at < WINDOW]
            if kept:
                limiter.accepted[key] = kept
            else:
                del limiter.accepted[key]
        times = limiter.accepted.get(session_key, [])
        answer = {"cause": cause, "id": ident, "repository": HOME}
        if path.exists() or ident in ((_state(state_path) or {}).get(bug_reports.SEEN) or ()):
            return {"state": "already_reported", **answer}
        if len(times) >= PER_CHAT:
            minutes = math.ceil((min(times) + WINDOW - now) / 60)
            raise Refused(
                429,
                f"this chat already reported {PER_CHAT} deck bugs in 24 hours;"
                f" the next one is taken in {minutes} minutes",
            )
        try:
            bug_reports.write(
                state_path,
                repo=repo,
                work_id=work_id,
                cause=cause,
                run_id=session_key,
                reason="",
                work_url=public_url(work_href(work_id)),
                session_key=session_key,
                revision=revision,
                expected=expected,
                observed=observed,
                pull_request=pull_request,
                head=head,
            )
        except OSError as exc:
            raise Refused(503, f"the report was not written ({exc}); try again later") from exc
        limiter.accepted[session_key] = [*times, now]
    return {"state": "filed", **answer}


def topics(board: Any) -> dict:
    """Every agent-reported topic this deck knows of, so an agent reuses one (#4579).

    From the engine's per-cause records and the request files still waiting. ``issue`` is the
    last issue filed for it, whose open or closed state is not known here; ``waiting`` says a
    report of it is not filed yet. ``partial`` when one of the two could not be read.
    """
    target = instance(board)
    if target is None:
        return {"topics": [], "partial": True, "usage": USAGE}
    state_path, _repo = target
    found: dict[str, dict] = {}
    partial = False
    state = _state(state_path)
    if state is None:
        partial = True
    records = (state or {}).get(bug_reports.RECORDS)
    for record in (records or {}).values() if isinstance(records, dict) else ():
        if not isinstance(record, dict):
            continue
        cause = str(record.get("cause") or "")
        if not bug_reports.is_agent_report(cause):
            continue
        number = record.get("number")
        found[cause] = {
            "topic": cause.partition(":")[2],
            "issue": number if isinstance(number, int) else None,
            "waiting": bool(record.get("pending") or record.get("opening_at")),
        }
    try:
        waiting = sorted(bug_reports.directory(state_path).glob("*.json"))
    except OSError:
        waiting, partial = [], True
    for path in waiting:
        try:
            cause = str(json.loads(path.read_text()).get("cause") or "")
        except (OSError, ValueError, AttributeError):
            continue
        if bug_reports.is_agent_report(cause):
            found.setdefault(
                cause, {"topic": cause.partition(":")[2], "issue": None, "waiting": True}
            )["waiting"] = True
    return {
        "topics": sorted(found.values(), key=lambda item: item["topic"]),
        "partial": partial,
        "note": "`issue` is the last issue filed for the topic; it may be closed since.",
        "usage": USAGE,
    }
