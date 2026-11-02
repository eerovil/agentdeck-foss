"""One pull request head's review history, and the one answer read from it (#3957).

Each writer that sets a review record today (the router's checklist and asks, the chat
board's openings and escalations, the release's verdicts) also appends one dated event to
the deck's ``review_events`` table, keyed by the pull request (``owner/repo#n``) and its full
head commit, both lower case. ``head_review`` reads one head's events, sorted by the event's
own time and then by arrival, and lets the newest one win, so nothing ever needs clearing
and the order webhooks arrive in does not matter.

The Work's review status (``web/work_view.derived_for``), the release's pass check (#3956) and
the Work router's own bookkeeping (#3955) read it: which heads were asked about, the checklist
that waits, the ask-again caps and the base-merge carry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

#: The review checklist was sent to the Work's agent (``data.covered``: none was sent, as a
#: checklist for an older head of the same pull request still waited).
CHECKLIST = "checklist"
#: The agent said it went through the checklist.
CHECKLIST_DONE = "checklist_done"
#: The deck asked for a review of the head (``data``: ``again``, ``note``, ``digest``).
ASKED = "asked"
#: A review chat was opened about the head (``data.chat``: ``owner/repo#n``).
CHAT = "chat"
#: Verdicts. ``accepted`` is the owner settling the findings with no code change: a pass.
PASS = "pass"
CHANGES = "changes"
NO_CODE = "no_code"
FINDINGS = "findings"
ESCALATED = "escalated"
ACCEPTED = "accepted"
#: The backstop started the ready head and nothing started.
UNSTARTED = "unstarted"

KINDS = frozenset(
    {
        CHECKLIST,
        CHECKLIST_DONE,
        ASKED,
        CHAT,
        PASS,
        CHANGES,
        NO_CODE,
        FINDINGS,
        ESCALATED,
        ACCEPTED,
        UNSTARTED,
    }
)
#: The events that answer a review, newest first wins.
VERDICTS = frozenset({PASS, CHANGES, NO_CODE, FINDINGS, ESCALATED, ACCEPTED})

#: ``note(pull, head, kind, key, at=None, data=None)``: append one event; a key already
#: stored adds nothing.
Note = Callable[..., object]


def no_note(*_args: object, **_kwargs: object) -> None:
    """The default writer: a service built without the deck's database keeps no history."""


@dataclass(frozen=True)
class ReviewEvent:
    pull: str
    head: str
    at: float
    kind: str
    key: str
    data: dict = field(default_factory=dict)
    seq: int = 0


@dataclass(frozen=True)
class HeadReview:
    """What one head's history says, newest event winning.

    Times are epoch seconds. ``verdict`` is ``pass`` (``accepted`` too, with ``accepted``
    set), ``changes`` (findings too), ``no_code`` or ``escalated``; "" when none came.
    ``review_since`` is set only for an ask or a chat newer than the verdict.
    ``findings_told``: the findings reached the Work's agent (a chat owned it, and the
    message was stored). ``passed``: the head's pass stands (``standing_verdict``), the one
    answer the release merges on; a pass a review was asked for again after does not.
    """

    checklist_since: float | None = None
    review_since: float | None = None
    review_chat: str = ""
    verdict: str = ""
    verdict_at: float | None = None
    verdict_chat: str = ""
    verdict_by: str = ""
    verdict_url: str = ""
    verdict_note: str = ""
    accepted: bool = False
    findings_at: float | None = None
    findings_told: bool = False
    unstarted: bool = False
    passed: bool = False


def chat_heads(
    events: Iterable[ReviewEvent], chat_repository: str
) -> dict[int, dict[tuple[str, int], str]]:
    """Which commit each review chat in ``chat_repository`` was opened about, per pull request
    it covers, keyed by casefolded repository and number: its ``chat`` events, the newest one
    per chat and pull request (#2411, #3956)."""
    prefix = (chat_repository or "").casefold() + "#"
    found: dict[int, dict[tuple[str, int], str]] = {}
    for event in sorted(events, key=lambda event: (event.at, event.seq)):
        chat = str((event.data or {}).get("chat") or "") if event.kind == CHAT else ""
        repository, _, number = event.pull.rpartition("#")
        if chat.startswith(prefix) and chat[len(prefix) :].isdigit() and number.isdigit():
            found.setdefault(int(chat[len(prefix) :]), {})[(repository, int(number))] = event.head
    return found


def at_head(events: Iterable[ReviewEvent], head: str) -> list[ReviewEvent]:
    """The events written at ``head``, a full commit id or an abbreviation of at least 7."""
    head = (head or "").lower()
    if len(head) < 7:
        return []
    return [event for event in events if event.head.startswith(head)]


def full_head(events: Iterable[ReviewEvent], sha: str) -> str:
    """The full head among ``events`` that ``sha`` (at least 7 characters) abbreviates; "" when
    none does, or when two different heads do."""
    found = {event.head for event in at_head(events, sha)}
    return found.pop() if len(found) == 1 else ""


def newest_verdict(events: Iterable[ReviewEvent]) -> ReviewEvent | None:
    """The newest verdict among ``events`` (by its own time, then arrival), or ``None``."""
    verdicts = [event for event in events if event.kind in VERDICTS]
    return max(verdicts, key=lambda event: (event.at, event.seq), default=None)


def standing_verdict(events: Iterable[ReviewEvent]) -> ReviewEvent | None:
    """The newest verdict among one head's ``events``, or ``None`` once a review was asked for
    after it: that review's own verdict is awaited, so its pass does not stand (#3956 review).
    The one rule the release and the Work card both read. A chat alone is not an ask: one that
    lists an already passed pull request beside others posts no verdict on it (#3341)."""
    events = list(events)
    verdict = newest_verdict(events)
    if verdict is not None and any(
        event.kind == ASKED and (event.at, event.seq) > (verdict.at, verdict.seq)
        for event in events
    ):
        return None
    return verdict


def answered(events: Iterable[ReviewEvent], head: str) -> bool:
    """Whether a verdict, findings or an escalation came back for ``head`` (or was carried to
    it from a head a base merge away): only then may the owner accept it as is (#3828)."""
    return newest_verdict(at_head(events, head)) is not None


def checklist_waits(events: Iterable[ReviewEvent]) -> bool:
    """Whether a pull request's review waits on its agent's checklist, at any head: the newest
    of its checklists, checklist-dones and asks is a checklist (#3880, #3933)."""
    marks = [e for e in events if e.kind in (CHECKLIST, CHECKLIST_DONE, ASKED)]
    newest = max(marks, key=lambda event: (event.at, event.seq), default=None)
    return newest is not None and newest.kind == CHECKLIST


def last_digest(events: Iterable[ReviewEvent]) -> tuple[str, str]:
    """``(head, digest)`` of the newest event naming the diff's review digest, or empty
    strings: the diff a later head is compared with (#3314)."""
    found = [e for e in events if str((e.data or {}).get("digest") or "")]
    newest = max(found, key=lambda event: (event.at, event.seq), default=None)
    return (newest.head, str(newest.data["digest"]).lower()) if newest else ("", "")


def same_change(events: Iterable[ReviewEvent], head: str, older: str) -> bool:
    """Whether ``head`` keeps ``older``'s verdict: the same head, or carried from it through
    base merges (``data.carried_from``); ``older`` may be a verdict's short sha (#3314)."""
    events = list(events)
    older, current = (older or "").lower(), (head or "").lower()
    if len(older) < 7:
        return False
    for _ in range(len(events) + 1):
        if current.startswith(older):
            return True
        current = next(
            (
                str(e.data["carried_from"]).lower()
                for e in at_head(events, current)
                if (e.data or {}).get("carried_from")
            ),
            "",
        )
        if not current:
            return False
    return False


def passed(event: ReviewEvent | None) -> bool:
    """Whether ``event`` (``newest_verdict``'s) is a pass, the owner's acceptance included."""
    return event is not None and event.kind in (PASS, ACCEPTED)


def head_review(events: Iterable[ReviewEvent]) -> HeadReview:
    """The answer of one head's ``events``, whatever order they arrived in."""
    events = list(events)
    checklist = review = verdict_at = findings = None
    review_chat = verdict = chat = by = url = note = background = ""
    accepted = unstarted = told = False
    for event in sorted(events, key=lambda event: (event.at, event.seq)):
        data = event.data or {}
        unstarted = event.kind == UNSTARTED or (unstarted and event.kind not in KINDS)
        if event.kind == CHECKLIST:
            checklist = event.at
        elif event.kind == CHECKLIST_DONE:
            checklist = None
        elif event.kind == CHAT and verdict == PASS and review is None:
            # A chat opened at a head that already passed, with no ask since: one reviewing a
            # sibling that lists this one as already passed, which posts no verdict on it
            # (#3341). Not a review of this head until an ask follows: a review asked for again
            # writes its chat a moment before its ask (#3956 review).
            background = str(data.get("chat") or "")
            continue
        elif event.kind in (ASKED, CHAT):
            checklist = None
            review = event.at
            if event.kind == CHAT:
                review_chat = str(data.get("chat") or "")
            elif background and not review_chat:
                review_chat = background
        elif event.kind in VERDICTS:
            checklist = review = None
            verdict_at = event.at
            accepted = event.kind == ACCEPTED
            verdict = {ACCEPTED: PASS, FINDINGS: CHANGES}.get(event.kind, event.kind)
            chat = str(data.get("chat") or review_chat)
            by, url, note = (str(data.get(name) or "") for name in ("by", "url", "note"))
            findings = None if verdict == PASS else event.at
            told = verdict != PASS and bool(data.get("told"))
            review_chat = background = ""
    return HeadReview(
        checklist_since=checklist,
        review_since=review,
        review_chat=review_chat if review is not None else "",
        verdict=verdict,
        verdict_at=verdict_at,
        verdict_chat=chat,
        verdict_by=by,
        verdict_url=url,
        verdict_note=note,
        accepted=accepted,
        findings_at=findings,
        findings_told=told,
        unstarted=unstarted,
        passed=passed(standing_verdict(events)),
    )
