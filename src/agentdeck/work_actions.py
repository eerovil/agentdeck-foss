"""What the operator may do to a Work from its row or its page (#2237).

``work_attention`` names the decision a Work waits on and lists the action ids it allows. This
is the other half: what each write action does, spelled out as constants, and the one rule
that decides whether it may run. The Work action route (``POST /works/{id}/action``) is the
only caller that writes; nothing here touches GitHub.

**Only what the Work offers right now.** The route recomputes the Work's operator attention
when the press arrives and refuses any action that is not in that list. A row that was
rendered a minute ago, a replayed form or a hand-made request can therefore do nothing the
Work does not offer at that moment, and a Work that needs nobody offers nothing.

**What each action does:**

- ``merge`` is the deck's own release of a Work it runs (``work_release``, #2895).
- ``start`` adds the repository's ownership label to an issue no agent works on (#3558), so
  its Poller or the deck starts a Work on it as on any labelled issue.
- ``close`` closes the source issue; ``chatgpt_review`` and ``agent_review`` are
  ``chat_review.open_review``, the review chat the chat board already opens.
- ``approve_merge`` is Merge pressed before the Work is ready (#3320): the deck merges once the
  review passes the same diffs and the tests are green; ``withdraw_merge`` takes it back. Like
  Pause, both are offered from the release's own state (``approval_refusal``).
- ``pause`` and ``resume`` pause the Work on this deck (#2559), or take off the paused label a
  person put on its issue (#2548). Unlike the rest, they are offered from the Work's own state
  (``pause_refusal``), on any unfinished Work, not from its category.

``close`` and the reviews, which carry no one's authority, go to GitHub as the repository
owner's non-human authority. The Poller's board commands (``/merge``, ``/claude``) were retired
with its cards (#2895, #3025).

Every write is reserved by its client action id before it is made and never repeated on an
uncertain answer: see the ``work_actions`` table in ``db.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .work import WorkRef
from .work_attention import (
    AGENT_REVIEW,
    ANSWER,
    CHATGPT_REVIEW,
    CLOSE,
    FINISH,
    HIDE,
    INSPECT,
    LINKS,
    MERGE,
    OPEN,
    PAUSE,
    RESUME,
    REVIEW,
    START,
    OperatorAttention,
)
from .work_hidden import UNHIDE

# The actions the deck makes itself, in no particular order.
# Merge when ready and its Withdraw (#3320).
APPROVE_MERGE = "approve_merge"
WITHDRAW_MERGE = "withdraw_merge"
APPROVAL_ACTIONS = frozenset({APPROVE_MERGE, WITHDRAW_MERGE})
WRITES = frozenset(
    {
        AGENT_REVIEW, APPROVE_MERGE, CHATGPT_REVIEW, CLOSE, MERGE, PAUSE, RESUME, START,
        WITHDRAW_MERGE,
    }
)
# A side chat started from the Work's page (#2388). Its press is reserved in the same store,
# so a replay of its action id answers with that press, but it has its own route, not
# ``/action``, and is never one of the Work's offered actions.
NEW_CHAT = "new_chat"
# A new Work filed from the deck (#2415): reserved in the same store under the press's id, so a
# replay answers with what that press did. Its ``work_id`` is ``new-work:<owner/repo>``, since
# the Work it starts has no issue number until the press makes one.
NEW_WORK = "new_work"

LABELS = {
    MERGE: "Merge",
    APPROVE_MERGE: "Merge when ready",
    WITHDRAW_MERGE: "Withdraw",
    # The Poller's board commands, retired with its cards (#3025): only old presses name them.
    "resolve": "Resolve",
    "retry": "Retry",
    "rebase": "Rebase",
    "fix": "Fix findings",
    "verify": "Verify",
    "continue": "Continue work",
    AGENT_REVIEW: "Agent review",
    CHATGPT_REVIEW: "ChatGPT review",
    CLOSE: "Close issue",
    PAUSE: "Pause",
    RESUME: "Resume",
    START: "Start agent",
    HIDE: "Hide",
    UNHIDE: "Bring back",
    FINISH: "Finish",
    NEW_CHAT: "New chat",
    NEW_WORK: "New work",
    **LINKS,
}

# The presses that cannot be taken back ask first.
CONFIRMS = {
    CLOSE: "Close this Work's issue on GitHub?",
    MERGE: "Merge this Work? The deck checks its review and tests, then merges it.",
    APPROVE_MERGE: (
        "Approve merging this Work at its current diff? The deck merges once its review passes"
        " and its tests are green. Any change to the diff clears this."
    ),
}

# A second press of the same action on the same Work inside this window is refused unless the
# first is known to have failed: a second one is a double tap or a "did that work?".
RECENT_ACTION_S = 300.0


class Refused(Exception):
    """An action that must not run, in words for the person who pressed it."""

    def __init__(self, text: str, status: int = 409) -> None:
        super().__init__(text)
        self.text = text
        self.status = status


@dataclass(frozen=True)
class Step:
    """The one external write an action makes.

    ``kind`` is ``close`` (the source issue, with ``body`` as the close reason),
    ``remove_label`` (``body`` is the paused label a person put on the issue, #2548),
    ``add_label`` (``body`` is the ownership label that starts an agent, #3558),
    ``deck_pause`` or ``deck_resume`` (this deck's own record, #2559),
    ``chatgpt_review``, ``agent_review``, ``deck_merge`` (the deck's own release, #2895) or
    ``deck_withdraw`` (a Merge press taken back, #3320).
    """

    kind: str
    repository: str
    number: int
    body: str = ""
    # A ``chatgpt_review``'s open pull requests as ``owner/repo#n``, so the chat lists each
    # one even where GitHub links none of them to the issue (#2637).
    pulls: tuple[str, ...] = ()

    @property
    def issue(self) -> str:
        return f"{self.repository}#{self.number}"


def plan(
    action: str,
    card: Any,
    attention: OperatorAttention | None,
    *,
    deck_release: bool = False,
    approval: str = "",
) -> Step:
    """The write ``action`` makes for this Work now, or ``Refused`` saying why it may not.

    ``deck_release`` is the deck's own release taking this Work (#2895): only then is Merge a
    ``deck_merge`` step. ``approval`` is the release's ``approval`` for it (#3320).
    """
    if action not in WRITES:
        raise Refused("That is not a Work action.", 422)
    label = LABELS[action]
    if action in {PAUSE, RESUME}:
        return _plan_pause(action, card)
    if action in {APPROVE_MERGE, WITHDRAW_MERGE}:
        why = approval_refusal(action, card, attention, approval)
        if why:
            raise Refused(f"{label} is not offered for this Work now: {why} Nothing was sent.")
        ref = WorkRef.from_work_id(card.work_id)
        if ref is None:
            raise Refused("This Work has no GitHub issue to act on.", 422)
        if action == APPROVE_MERGE:
            # ``fresh``: refused by the release while a press already stands (#3320 review).
            return Step("deck_merge", ref.repository, ref.number, "fresh")
        return Step("deck_withdraw", ref.repository, ref.number)
    if attention is None or action not in attention.actions:
        raise Refused(f"{label} is not offered for this Work now; nothing was sent.")
    ref = WorkRef.from_work_id(card.work_id)
    if ref is None:
        raise Refused("This Work has no GitHub issue to act on.", 422)
    if action == MERGE:
        if deck_release:
            return Step("deck_merge", ref.repository, ref.number)
        raise Refused("The deck does not release this Work; nothing was sent.")
    if action == START:
        # The repository's ownership label: its Poller or the deck starts a Work on it (#3558).
        return Step("add_label", ref.repository, ref.number, card.start_label)
    if action == CLOSE:
        return Step("close", ref.repository, ref.number, "completed")
    if action in {CHATGPT_REVIEW, AGENT_REVIEW}:
        # The review chat covers the whole Work (#2637): the issue and every open pull request,
        # in this repository or another, so none of them is a reason to refuse or to review a
        # pull request on its own any more (#2234, #2395). Agent review is the same chat with a
        # Claude reviewer started on it at once, whose verdict the deck's release accepts (#2736).
        pulls = tuple(
            f"{output.repository}#{output.number}" for output in card.outputs if not output.settled
        )
        return Step(action, ref.repository, ref.number, pulls=pulls)
    raise Refused(f"{label} is not a write the deck makes.", 422)  # pragma: no cover


def approval_refusal(
    action: str, card: Any, attention: OperatorAttention | None, approval: str
) -> str:
    """Why Merge when ready or Withdraw is not offered now, or "" when it is (#3320).

    Only on a Work the deck releases, on a project that does not merge on its own, while a pull
    request is open and the Work is not already Ready for merge (its Merge shows then).
    Withdraw only while a press stands, which is cleared once its pull requests merge.
    """
    from .work_release import MERGE_APPROVED, MERGE_OFFERED

    if approval not in {MERGE_OFFERED, MERGE_APPROVED}:
        return "the deck does not hold this Work's merges for a person."
    if not any(output.is_open for output in getattr(card, "outputs", ())):
        return "it has no open pull request."
    if action == WITHDRAW_MERGE:
        return "" if approval == MERGE_APPROVED else "no Merge approval stands."
    if approval == MERGE_APPROVED:
        return "Merge is already approved."
    if attention is not None and MERGE in attention.actions:
        return "it is ready, so Merge is offered instead."
    return ""


def merge_approval_control(
    card: Any, attention: OperatorAttention | None, approval: str
) -> ActionButton | None:
    """The Work page's Merge when ready, or Withdraw while a press stands (#3320)."""
    for action in (APPROVE_MERGE, WITHDRAW_MERGE):
        if not approval_refusal(action, card, attention, approval):
            return ActionButton(action, LABELS[action], confirm=CONFIRMS.get(action, ""))
    return None


def pause_refusal(card: Any) -> str:
    """Why this Work cannot be paused now, or "" when it can (#2548, #2559).

    Any unfinished Work can, whatever it waits on. It is paused on this deck, which then sends
    nothing to its chats.
    """
    if getattr(card, "paused", False):
        return "It is already paused."
    if getattr(card, "start_label", ""):
        return "No agent works on it yet."
    if getattr(card, "finished_at", None) is not None or getattr(card, "awaiting_finish", False):
        return "It has finished, so nothing new starts on it."
    return ""


def pausable(card: Any) -> bool:
    """May a person pause this Work now (``pause_refusal``)?"""
    return not pause_refusal(card)


def _plan_pause(action: str, card: Any) -> Step:
    """Pause pauses the Work on this deck (#2559); Resume undoes whichever pause holds it: this
    deck's, or the paused label a person put on its issue (#2548)."""
    ref = WorkRef.from_work_id(card.work_id)
    if ref is None:
        raise Refused("This Work has no GitHub issue to act on.", 422)
    if action == PAUSE:
        why = pause_refusal(card)
        if why:
            raise Refused(f"Pause is not offered for this Work now: {why} Nothing was sent.")
        return Step("deck_pause", ref.repository, ref.number)
    if getattr(card, "label_paused", False):
        # Resume takes the label off; the route ends the deck pause too, so one Resume clears
        # both.
        return Step("remove_label", ref.repository, ref.number, card.pause_label)
    if getattr(card, "deck_paused", False):
        return Step("deck_resume", ref.repository, ref.number)
    raise Refused("Resume is not offered for this Work now; nothing was sent.")


@dataclass(frozen=True)
class WorkActionPress:
    """One press, and what became of it.

    ``outcome`` stays ``None`` while the write is in flight and when it ended without proof
    either way: that press may have landed, so it counts as sent and is never repeated.
    """

    action_id: str
    work_id: str
    action: str
    reserved_at: str
    settled_at: str | None = None
    outcome: str | None = None
    result: str = ""

    @property
    def sent(self) -> bool:
        return self.outcome != "failed"

    @property
    def reserved(self) -> datetime | None:
        try:
            when = datetime.fromisoformat(self.reserved_at)
        except ValueError:
            return None
        return when if when.tzinfo is not None else when.replace(tzinfo=UTC)

    def age_s(self, now: datetime | None = None) -> float:
        """Seconds since the press, or infinity for a timestamp that will not parse."""
        when = self.reserved
        if when is None:
            return float("inf")
        return ((now or datetime.now(UTC)) - when).total_seconds()

    @property
    def label(self) -> str:
        return LABELS.get(self.action, self.action)

    @property
    def state(self) -> str:
        """``sent``, ``failed``, or ``uncertain`` for a press with no proof either way."""
        if self.outcome == "done":
            return "sent"
        return "failed" if self.outcome == "failed" else "uncertain"


def recent_press(
    presses: tuple[WorkActionPress, ...],
    action: str,
    *,
    now: datetime | None = None,
    window_s: float = RECENT_ACTION_S,
) -> WorkActionPress | None:
    """The newest press of ``action`` inside the window that did not fail, if any."""
    candidates = [
        press
        for press in presses
        if press.action == action and press.sent and press.age_s(now) < window_s
    ]
    return max(candidates, key=lambda press: press.reserved_at) if candidates else None


@dataclass(frozen=True)
class ActionButton:
    """One control for one action id: a form posting it, or a link when ``href`` is set.

    ``route`` is the Work route the form posts to: ``action`` for the writes above,
    ``visibility`` for Hide and Bring back, which change only this deck (#2238).
    """

    action: str
    label: str
    href: str = ""
    external: bool = False
    confirm: str = ""
    route: str = "action"
    #: What the press confirms, posted with it so the route can tell a stale confirm from a
    #: current one: Finish's open pull requests (#3187).
    confirmed: str = ""


# Hide and Bring back: this deck's own display preference, never a write (``work_hidden``).
HIDE_BUTTON = ActionButton(HIDE, LABELS[HIDE], route="visibility")
BRING_BACK = ActionButton(UNHIDE, LABELS[UNHIDE], route="visibility")
# A finished Work's Archive is the same deck-only hide, offered off Recently finished (#2254).
ARCHIVE_BUTTON = ActionButton(HIDE, "Archive", route="visibility")
# A person's sign-off on a Work whose issue closed (#2510): this deck's record only, like Hide.
FINISH_BUTTON = ActionButton(FINISH, LABELS[FINISH], route="visibility")


def finish_button(card: Any) -> ActionButton:
    """Finish, asking once first when a registered pull request is still open (#3187).

    Finishing leaves that pull request alone, so the question names it.
    """
    opened = [o for o in getattr(card, "outputs", ()) if o.is_open]
    if not opened:
        return FINISH_BUTTON
    return replace(
        FINISH_BUTTON,
        confirm=finish_question(opened),
        confirmed=" ".join(output.label for output in opened),
    )


def finish_question(opened: Sequence[Any]) -> str:
    """``store#3593 is still open. Finish anyway?`` for a Work's open pull requests."""
    names = ", ".join(output.label.rpartition("/")[2] for output in opened)
    verb = "is" if len(opened) == 1 else "are"
    return f"{names} {verb} still open. Finish anyway?"


# Pause and Resume (#2548): a label on the issue, so they post to the action route.
PAUSE_BUTTON = ActionButton(PAUSE, LABELS[PAUSE])
START_BUTTON = ActionButton(START, LABELS[START])
RESUME_BUTTON = ActionButton(RESUME, LABELS[RESUME])
# Star and Unstar (#3116): this deck's record only, so they post to their own route.
STAR = "star"
UNSTAR = "unstar"
STAR_BUTTON = ActionButton(STAR, "☆ Star", route="star")
UNSTAR_BUTTON = ActionButton(UNSTAR, "★ Starred", route="star")


def star_control(card: Any) -> ActionButton:
    """The Work page's Star, or Unstar on a starred Work (#3116)."""
    return UNSTAR_BUTTON if getattr(card, "starred", False) else STAR_BUTTON


def finish_control(card: Any, attention: OperatorAttention | None) -> ActionButton | None:
    """The Work page's own Finish while its issue is closed and its actions offer none (#3187).

    A pull request still open reads first, so Finish is not among its actions; it stays
    reachable here, and asks before it finishes.
    """
    if not getattr(card, "awaiting_finish", False):
        return None
    if attention is not None and FINISH in attention.actions:
        return None
    return finish_button(card)


def pause_control(card: Any) -> ActionButton | None:
    """The Work page's Pause or Resume, whichever the Work offers now (#2548)."""
    if getattr(card, "paused", False):
        return RESUME_BUTTON
    return PAUSE_BUTTON if pausable(card) else None


def buttons(
    card: Any,
    attention: OperatorAttention | None,
    where_href: str = "",
    *,
    page: bool = False,
    hidden: bool = False,
) -> tuple[ActionButton, ...]:
    """The controls for the Work's current actions, strongest first.

    The front page's row and the Work page show the same list; ``page`` drops Inspect, which
    only opens the page it is already on, and a Work already ``hidden`` does not offer Hide
    again: where it is shown, it offers ``BRING_BACK`` instead (#2238).
    """
    if attention is None:
        return ()
    found: list[ActionButton] = []
    for action in attention.actions:
        if action in WRITES:
            found.append(ActionButton(action, LABELS[action], confirm=CONFIRMS.get(action, "")))
        elif action == ANSWER:
            href = "#work-message" if page else f"{card.href}#work-message"
            # Only a question is answered; a deck-run Work's stopped, died, closed or merged
            # turn is started again by a message (#2944). Read from the derived kind, since a
            # stopped turn is in Needs answer too (#2956).
            derived = getattr(card, "derived", None)
            asks = derived is None or getattr(derived, "kind", "") == "question"
            label = LABELS[action] if asks else "Message"
            found.append(ActionButton(action, label, href))
        elif action == OPEN:
            found.append(ActionButton(action, LABELS[action], where_href or card.href))
        elif action == INSPECT and not page:
            found.append(ActionButton(action, LABELS[action], card.href))
        elif action == REVIEW and attention.review_url:
            found.append(ActionButton(action, LABELS[action], attention.review_url, True))
        elif action == HIDE and not hidden:
            found.append(HIDE_BUTTON)
        elif action == FINISH:
            found.append(finish_button(card))
    return tuple(found)
