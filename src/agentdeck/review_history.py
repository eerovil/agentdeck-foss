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

from collections.abc import Callable, Iterable, Sequence
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
#: Only the GitHub check reads these three (#4152), so they stay out of ``KINDS``: a ready
#: head the check should show as waiting, a person's Handled press on its red check
#: (``data.by``), which is no pass and which nothing that merges reads, and a Re-run press the
#: router turned down, which the check says since GitHub shows a press nothing by itself.
SEEN = "seen"
HANDLED = "handled"
RERUN_REFUSED = "rerun-refused"
#: A check run takes 65,535 characters of text; findings are kept up to here.
FINDINGS_LIMIT = 60_000

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


#: The GitHub check that shows a pull request's review (#4152). It mirrors the review history
#: and is no CI result, so every read of a commit's CI leaves it out: the public copy's too,
#: because a colleague's deck sees this check on the pull requests it merges.
CHECK_NAME = "AgentDeck review"
#: The check suite GitHub makes for that check run is named by the App that wrote it.
_DECK_APP_PREFIX = "agentdeck-"


def deck_check(name: object) -> bool:
    """Whether a check run called ``name`` is the deck's own review check."""
    return isinstance(name, str) and name.casefold() == CHECK_NAME.casefold()


def deck_check_delivery(event: str, payload: dict) -> bool:
    """Whether a ``check_run`` or ``check_suite`` delivery is about that check, not about CI."""
    run = payload.get(event)
    if not isinstance(run, dict):
        return False
    if event == "check_run":
        return deck_check(run.get("name"))
    app = run.get("app")
    slug = app.get("slug") if isinstance(app, dict) else ""
    return event == "check_suite" and str(slug or "").startswith(_DECK_APP_PREFIX)


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
class ShippedHead:
    """A pull request shipped at ``head`` with its review findings left to fix in the same
    Work, the one of issue ``work`` (``owner/repo#n``); kept after the merge (#4212).
    ``shipped`` is when its review counted as passed (0 while the press is unfinished: then
    it did not ship). ``fix`` is the pull request that fixes those findings, "" until the
    agent names one; the Work's issue stays open until it merges (#4212 review)."""

    pull: str
    head: str
    work: str = ""
    at: float = 0.0
    shipped: float = 0.0
    fix: str = ""


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


def clip_findings(text: str) -> str:
    """Findings as kept on their event, for the GitHub check (#4152), saying when cut."""
    text = (text or "").strip()
    if len(text) <= FINDINGS_LIMIT:
        return text
    return text[:FINDINGS_LIMIT].rstrip() + "\n\n(Cut here: the findings are longer than this.)"


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


def _unaccepted(events: Iterable[ReviewEvent]) -> list[ReviewEvent]:
    """``events`` in time order, less the findings and escalations of a round the owner
    already accepted: one dated after an ``accepted`` row with no ask between (#4069). The
    owner may accept on the escalation's own comment, minutes before the chat board writes
    the same escalation from its chat's closing line, and that copy must not undo the pass.
    An acceptance made while a newer review is out ends no round: that review's findings
    count."""
    kept: list[ReviewEvent] = []
    accepted: set[str] = set()  # per head: a pull request's whole history may be read
    waiting: set[str] = set()  # heads with an ask nothing has answered yet
    for event in sorted(events, key=lambda event: (event.at, event.seq)):
        if event.kind == ASKED:
            accepted.discard(event.head)
            waiting.add(event.head)
        elif event.kind == ACCEPTED:
            if event.head not in waiting:
                accepted.add(event.head)
        elif event.head in accepted and event.kind in (FINDINGS, ESCALATED):
            continue
        elif event.kind in VERDICTS:
            waiting.discard(event.head)
        kept.append(event)
    return kept


def newest_verdict(events: Iterable[ReviewEvent]) -> ReviewEvent | None:
    """The newest verdict among ``events`` (by its own time, then arrival), or ``None``; the
    findings or escalation of a round already accepted do not count (``_unaccepted``)."""
    verdicts = [event for event in _unaccepted(events) if event.kind in VERDICTS]
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


#: The verdicts a person may ship anyway, leaving their findings to fix in the same Work (#4212).
SHIPPABLE = frozenset({CHANGES, FINDINGS, ESCALATED})


def shippable(events: Iterable[ReviewEvent], head: str) -> ReviewEvent | None:
    """The findings or escalation that ``head``'s review came back with, when the owner may
    ship ``head`` now and leave them to fix in the same Work (#4212); else ``None``.

    Only the newest verdict at the head, and only while no newer review was asked for: that
    review may pass, and shipping must never skip a diff the reviewer has not finished."""
    verdict = standing_verdict(at_head(events, head))
    return verdict if verdict is not None and verdict.kind in SHIPPABLE else None


def split_head(events: Iterable[ReviewEvent], live: str) -> str:
    """The earlier head that Ship now may still ship while ``live`` waits for its review
    (#4212 review): the agent pushed a fix on top of findings, and the owner may ship the
    reviewed commit in a pull request of its own. "" when ``live`` has a verdict standing
    (ship or merge it instead) or the newest head answered before it was not answered with
    findings or an escalation."""
    events = list(events)
    live = (live or "").lower()
    if len(live) < 7 or standing_verdict(at_head(events, live)) is not None:
        return ""
    newest: ReviewEvent | None = None
    for head in {event.head for event in events if not event.head.startswith(live)}:
        verdict = standing_verdict(at_head(events, head))
        if verdict is not None and (
            newest is None or (verdict.at, verdict.seq) > (newest.at, newest.seq)
        ):
            newest = verdict
    if newest is None or newest.kind not in SHIPPABLE or len(newest.head) != 40:
        return ""
    return newest.head


_NOT_A_FINDING = ("/claude", "review findings", "#", "<!--", "**review")


def first_finding(text: str, limit: int = 160) -> str:
    """The first line of findings that says something: not the command, heading or marker
    lines a reviewer's comment opens with; clipped to ``limit`` characters (#4212 D10)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith(_NOT_A_FINDING):
            continue
        return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return ""


def findings_text(events: Iterable[ReviewEvent], head: str) -> tuple[str, str]:
    """``(text, url)`` of the newest findings kept at ``head``: the words the history kept, and
    the comment they were posted in; either may be empty (#4212)."""
    rows = [e for e in at_head(events, head) if e.kind in SHIPPABLE]
    text = url = ""
    for event in sorted(rows, key=lambda event: (event.at, event.seq)):
        data = event.data or {}
        text = str(data.get("text") or "").strip() or text
        url = str(data.get("url") or "").strip() or url
    return text, url


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


#: The note of the pass a Ship now press records (#4212).
SHIP_NOTE = "Shipped now with its findings left to fix in this Work"


def ship_pass_head(events: Iterable[ReviewEvent], head: str) -> str:
    """The head a Ship now press passed, when ``head``'s standing verdict is that pass or one
    base updates carried from it, through any number of them; else "". Such a pass counts only
    once its press finished (#4212 review): ``WorkRelease._verdict_at``, ``WorkRouter.ship_now``
    and the Work page read it by this one rule. ``carried_from`` names only the head before,
    so the chain is walked back to the press's own row, each head once."""
    events = list(events)
    verdict = standing_verdict(at_head(events, head))
    if not _ship_pass(verdict):
        return ""
    seen = {verdict.head}
    while older := str((verdict.data or {}).get("carried_from") or "").lower():
        if older in seen:
            break
        seen.add(older)
        before = standing_verdict(at_head(events, older))
        if not _ship_pass(before):
            return older  # the rows before were dropped: the head carried from is the best kept
        verdict = before
    return verdict.head.lower()


def _ship_pass(event: ReviewEvent | None) -> bool:
    return passed(event) and str((event.data or {}).get("note") or "") == SHIP_NOTE


def background_chat(events: Sequence[ReviewEvent], chat: ReviewEvent) -> bool:
    """Whether ``chat`` (a ``chat`` row among one pull request's ``events``) only listed this
    pull request as already passed while it reviewed a sibling (#3341): its head's newest
    verdict before it is a pass, with no ask between, and no ask at that head follows it
    before the next chat (a review asked for again writes its chat a moment before its ask,
    #3956 review). The rule ``head_review`` applies; the earlier-rounds list counts no such
    chat as a round of this pull request (#4208 review)."""
    def when(event: ReviewEvent) -> tuple[float, int]:
        return (event.at, event.seq)

    order = sorted(events, key=when)
    head = chat.head.lower()
    before = [e for e in order if when(e) < when(chat) and e.head.lower() == head]
    verdicts = [e for e in _unaccepted(before) if e.kind in VERDICTS]
    if not verdicts or verdicts[-1].kind not in (PASS, ACCEPTED):
        return False
    if any(e.kind == ASKED and when(e) > when(verdicts[-1]) for e in before):
        return False
    for event in order:
        if when(event) <= when(chat):
            continue
        if event.kind == CHAT:
            break
        if event.kind == ASKED and event.head.lower() == head:
            return False
    return True


def head_review(events: Iterable[ReviewEvent]) -> HeadReview:
    """The answer of one head's ``events``, whatever order they arrived in."""
    events = list(events)
    checklist = review = verdict_at = findings = None
    review_chat = verdict = chat = by = url = note = background = ""
    accepted = unstarted = told = False
    for event in _unaccepted(events):
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
