"""One continuous transcript across a chat's ordered provider legs (#1265).

A chat is an ordered, append-only chain of concrete provider sessions (#1263).
Each leg numbers its own transcript events from its own file or bridge, so two
legs routinely both contain event ``1``. Everything that identifies a message —
the DOM anchor a pin jumps to, the "load earlier" cursor, the image URL — has to
name the leg as well as the leg-local sequence number, or a chained chat quietly
scrolls to the wrong message.

The identity here is a *projection*, not a new store. It carries the leg's
position in the chain plus that leg's own native ``seq``, so every provider read
still happens with the exact arguments the provider already understands. Leg
positions are handed out when a leg is appended and never move, so appending a
new leg cannot renumber or invalidate an identity that already exists.

A single-leg chat — which is every chat that existed before chaining — keeps the
bare ``seq`` spelling, so old links, old pins and old anchors still land where
they always did.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .meta_chat import IDLE_RESET
from .models import Account, Session, TranscriptDetail

#: Separates a leg position from the leg's native sequence number. Chosen so a
#: leg-0 identity is indistinguishable from the bare seq it used to be.
LEG_SEPARATOR = "."

#: The "no rollover reasons known" default. A shared read-only mapping, so a
#: caller that has none — a test, or a deck with no database — costs nothing
#: and cannot be handed a default that another call could mutate.
MAPPING_PROXY_EMPTY: Mapping[str, str] = MappingProxyType({})

#: Plain words for each reason ``context_policy`` records against a rollover.
#: A reason with no entry — a newer one this deck has not learned yet — falls
#: back to saying only that the chat rolled over, which is still true.
ROLLOVER_WORDS: Mapping[str, str] = MappingProxyType({
    "threshold": "the context window had filled up",
    "new_generation": "new work arrived on a full context window",
    "compact_failed": "compacting the context failed",
    "compact_insufficient": "compacting the context did not free enough",
})


def chat_event_id(leg_index: int, seq: int) -> str:
    """Stable chat-level identity for one leg-local transcript event.

    Leg 0 renders as the bare sequence number on purpose: that is exactly what
    every anchor, pin and cursor minted before chained chats existed says.
    """
    if leg_index < 0:
        raise ValueError(f"leg index cannot be negative: {leg_index!r}")
    if leg_index == 0:
        return str(seq)
    return f"{leg_index}{LEG_SEPARATOR}{seq}"


def parse_chat_event_id(value: str | int | None) -> tuple[int, int] | None:
    """``"2.17"`` → ``(2, 17)``; ``"17"`` → ``(0, 17)``; nonsense → ``None``.

    Returning ``None`` rather than raising is deliberate: these values arrive
    from URLs and from ``id`` attributes people can hand-edit, and a bad cursor
    should degrade to "nothing more to load" rather than to a 500.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    leg_text, separator, seq_text = text.partition(LEG_SEPARATOR)
    if not separator:
        leg_text, seq_text = "0", text
    try:
        leg_index, seq = int(leg_text), int(seq_text)
    except ValueError:
        return None
    if leg_index < 0 or seq < 0:
        return None
    return leg_index, seq


@dataclass(frozen=True)
class LegSwitch:
    """The inline "this chat moved" marker rendered between two legs (#1250).

    It names both sides. A reader scrolling up through a chained chat meets this
    row without having seen the badge that says where they are, so "continued on
    alt" leaves them working out what it was continued *from*; and the seam is
    exactly where the billing changed, which is the one place naming the account
    that stopped being charged is worth a row of its own (#1267).
    """

    to_index: int
    from_account_label: str | None
    from_provider_id: str
    to_account_label: str | None
    to_provider_id: str
    #: Why context management moved the chat here, from the successor leg's
    #: frozen delivery row. ``None`` for every seam that was not a rollover.
    #: A rollover can leave and arrive on the *same* account, where the row
    #: would otherwise read as "continued on another account" — which is not
    #: what happened (#1667).
    reason: str | None = None

    @property
    def anchor(self) -> str:
        return f"chat-switch-{self.to_index}"

    @property
    def crosses_provider(self) -> bool:
        return self.from_provider_id != self.to_provider_id

    @property
    def is_rollover(self) -> bool:
        return self.reason is not None

    @property
    def is_fresh_start(self) -> bool:
        """The deck chat's own reset after an idle hour (#3403), not context management."""
        return self.reason == IDLE_RESET

    @property
    def rollover_label(self) -> str:
        return "fresh start" if self.is_fresh_start else "context rollover"

    @property
    def rollover_words(self) -> str:
        """What to say the rollover was, in words a reader has not had to learn."""
        if self.is_fresh_start:
            return (
                "fresh start — no message for an hour, so the deck chat continued in a "
                "fresh session with a clean context"
            )
        explained = ROLLOVER_WORDS.get(self.reason or "")
        return (
            f"context rollover — {explained}, so the chat continued in a fresh session"
            if explained
            else "context rollover — the chat continued in a fresh session"
        )


@dataclass(frozen=True)
class ChatTranscriptLeg:
    """One leg's loaded window, positioned inside the chat it belongs to."""

    index: int
    session: Session
    account: Account
    detail: TranscriptDetail
    is_current: bool
    #: Set only on a leg's newest window, so the handoff into the following leg
    #: renders directly below it exactly once however the window was paged in.
    switch: LegSwitch | None = None

    @property
    def session_key(self) -> str:
        return self.session.key

    @property
    def has_earlier_in_leg(self) -> bool:
        return self.detail.earliest_seq > 1

    def event_id(self, seq: int) -> str:
        return chat_event_id(self.index, seq)


@dataclass(frozen=True)
class ChatTranscript:
    """The merged window a chat page renders, oldest leg first."""

    legs: tuple[ChatTranscriptLeg, ...]
    #: The leg the live tail belongs to. Historical legs are read-only history.
    current: ChatTranscriptLeg
    #: "Load earlier" cursor, or None when the whole chat is already on screen.
    earlier_before: str | None

    @property
    def detail(self) -> TranscriptDetail:
        """The current leg's bundle: tokens, model and todos are its own."""
        return self.current.detail

    @property
    def after_seq(self) -> int:
        """Live tail cursor, native to the current leg — the only live tail."""
        return max((event.seq for event in self.current.detail.events), default=0)


@dataclass(frozen=True)
class _ResolvedLeg:
    index: int
    session: Session
    account: Account


def _resolved_legs(
    chat_keys: tuple[str, ...],
    sessions: dict[str, Session],
    accounts_by_key: dict[str, Account],
) -> list[_ResolvedLeg]:
    """Legs this deck can still read, each keeping its permanent position.

    A leg whose provider session or account has gone away is skipped rather than
    renumbered: the surviving legs keep the positions their event identities
    were minted with.
    """
    resolved: list[_ResolvedLeg] = []
    for index, key in enumerate(chat_keys):
        session = sessions.get(key)
        if session is None:
            continue
        account = accounts_by_key.get(session.account_key)
        if account is None:
            continue
        resolved.append(_ResolvedLeg(index, session, account))
    return resolved


def _reading_order(
    resolved: list[_ResolvedLeg], current_session_key: str
) -> list[_ResolvedLeg]:
    """Chain order, except that the current leg is moved to the end (#1501).

    A chat can go back to a leg it left, so the leg that is working is not
    always the one appended last. Reading order has to end at the working leg or
    the page presents it as history and the idle leg as the live tail. Each
    leg's ``index`` is untouched: positions are what pins, anchors and "load
    earlier" cursors were minted from, and moving one would send an existing
    link to the wrong message.
    """
    current = next(
        (leg for leg in resolved if leg.session.key == current_session_key), None
    )
    if current is None or resolved[-1] is current:
        return resolved
    return [leg for leg in resolved if leg is not current] + [current]


def _switch_after(
    resolved: list[_ResolvedLeg],
    position: int,
    account_labels: dict[str, str],
    rollover_reasons: Mapping[str, str] = MAPPING_PROXY_EMPTY,
) -> LegSwitch | None:
    """How this chat left the leg at ``position``, if it left it at all."""
    if position + 1 >= len(resolved):
        return None
    return _switch_between(
        resolved[position], resolved[position + 1], account_labels, rollover_reasons
    )


def _switch_between(
    leaving: _ResolvedLeg,
    arriving: _ResolvedLeg,
    account_labels: dict[str, str],
    rollover_reasons: Mapping[str, str] = MAPPING_PROXY_EMPTY,
) -> LegSwitch:
    return LegSwitch(
        to_index=arriving.index,
        from_account_label=account_labels.get(leaving.account.key),
        from_provider_id=leaving.account.provider_id,
        to_account_label=account_labels.get(arriving.account.key),
        to_provider_id=arriving.account.provider_id,
        # Keyed on the leg being *arrived at*: a rollover's delivery row names
        # the successor it created, and that is the boundary being drawn here.
        reason=rollover_reasons.get(arriving.session.key),
    )


def switch_into_leg(
    *,
    chat_keys: tuple[str, ...],
    leg_index: int,
    sessions: dict[str, Session],
    accounts_by_key: dict[str, Account],
    account_labels: dict[str, str],
    from_leg_index: int | None = None,
    rollover_reasons: Mapping[str, str] = MAPPING_PROXY_EMPTY,
) -> LegSwitch | None:
    """The seam that belongs directly *above* leg ``leg_index``'s first event.

    ``_switch_after`` answers "how did this chat leave here", which is what a
    window loaded oldest-first needs. A live tail arrives at the seam from the
    other side: it has already rendered the leg being left and now has to
    introduce the one it moved to (#1267). ``None`` for the chat's first leg,
    and for a leg whose predecessor this deck can no longer read.

    ``from_leg_index`` names the leg actually being left when that is not the
    one before ``leg_index`` in the chain — a chat can move *back* to a leg it
    left, and the seam has to name where the reader is coming from rather than
    the chain neighbour they never saw (#1501).
    """
    resolved = _resolved_legs(chat_keys, sessions, accounts_by_key)
    position = next(
        (offset for offset, leg in enumerate(resolved) if leg.index == leg_index),
        None,
    )
    if position is None:
        return None
    if from_leg_index is None:
        if not position:  # the chat's first leg
            return None
        leaving = position - 1
    else:
        leaving = next(
            (
                offset
                for offset, leg in enumerate(resolved)
                if leg.index == from_leg_index
            ),
            None,
        )
        if leaving is None or leaving == position:
            return None
    return _switch_between(
        resolved[leaving], resolved[position], account_labels, rollover_reasons
    )


async def load_chat_transcript(
    *,
    chat_keys: tuple[str, ...],
    current_session_key: str,
    sessions: dict[str, Session],
    accounts_by_key: dict[str, Account],
    providers,
    account_labels: dict[str, str],
    window: int,
    rollover_reasons: Mapping[str, str] = MAPPING_PROXY_EMPTY,
) -> ChatTranscript:
    """Load one continuous window ending at the current leg's newest event.

    The walk goes backwards through the chain only while the window is not full.
    A leg that handed back fewer events than the provider's page size has nothing
    more to give, so the walk continues into the leg before it — that is what
    puts the switch marker on screen for a chat whose newest leg is still short.
    A busy current leg fills the window on its own and costs exactly what it did
    before chaining existed.
    """
    resolved = _reading_order(
        _resolved_legs(chat_keys, sessions, accounts_by_key), current_session_key
    )
    position = len(resolved) - 1
    legs: list[ChatTranscriptLeg] = []
    remaining = window
    while position >= 0:
        resolved_leg = resolved[position]
        provider = providers[resolved_leg.account.provider_id]
        detail = await provider.load_transcript(resolved_leg.account, resolved_leg.session)
        legs.append(
            ChatTranscriptLeg(
                index=resolved_leg.index,
                session=resolved_leg.session,
                account=resolved_leg.account,
                detail=detail,
                is_current=resolved_leg.session.key == current_session_key,
                switch=_switch_after(
                    resolved, position, account_labels, rollover_reasons
                ),
            )
        )
        remaining -= len(detail.events)
        if remaining <= 0 or len(detail.events) >= window:
            break
        position -= 1
    legs.reverse()
    oldest = legs[0]
    earlier_before = (
        chat_event_id(oldest.index, oldest.detail.earliest_seq)
        # ``position`` is where the walk stopped in reading order, so "is there
        # another leg above this one" stays right for a chat whose working leg
        # is not the one appended last.
        if oldest.has_earlier_in_leg or position > 0
        else None
    )
    current = next(
        (leg for leg in legs if leg.is_current),
        legs[-1],
    )
    return ChatTranscript(
        legs=tuple(legs), current=current, earlier_before=earlier_before
    )


async def load_chat_transcript_before(
    *,
    chat_keys: tuple[str, ...],
    before: str,
    current_session_key: str,
    sessions: dict[str, Session],
    accounts_by_key: dict[str, Account],
    providers,
    account_labels: dict[str, str],
    rollover_reasons: Mapping[str, str] = MAPPING_PROXY_EMPTY,
) -> tuple[ChatTranscriptLeg | None, str | None]:
    """One "load earlier" step → (the earlier window, the next cursor).

    The cursor names a leg and a leg-local seq. When that leg has nothing left
    before it, the step falls through to the newest window of the leg before it,
    which is how the boundary is crossed with neither a gap nor a repeat.
    """
    parsed = parse_chat_event_id(before)
    if parsed is None:
        return None, None
    leg_index, seq = parsed
    # The same reading order the page was built in, so stepping back off a leg
    # lands on the leg rendered above it rather than on its chain neighbour.
    resolved = _reading_order(
        _resolved_legs(chat_keys, sessions, accounts_by_key), current_session_key
    )
    position = next(
        (offset for offset, leg in enumerate(resolved) if leg.index == leg_index),
        None,
    )
    if position is None:
        return None, None
    # Seq 0 means "no upper bound" — the spelling the button used before chat
    # identities existed, and still the way to ask for a leg's newest window.
    before_seq: int | None = seq or None
    while position >= 0:
        resolved_leg = resolved[position]
        provider = providers[resolved_leg.account.provider_id]
        detail = await provider.load_transcript(
            resolved_leg.account, resolved_leg.session, before_seq=before_seq
        )
        if detail.events:
            leg = ChatTranscriptLeg(
                index=resolved_leg.index,
                session=resolved_leg.session,
                account=resolved_leg.account,
                detail=detail,
                is_current=False,
                # A window fetched with no upper bound is this leg's newest one,
                # so the handoff out of it belongs directly below these events.
                switch=(
                    _switch_after(
                        resolved, position, account_labels, rollover_reasons
                    )
                    if before_seq is None
                    else None
                ),
            )
            cursor = (
                chat_event_id(resolved_leg.index, detail.earliest_seq)
                if leg.has_earlier_in_leg or position > 0
                else None
            )
            return leg, cursor
        # Nothing left in this leg: step back and take the previous leg's tail.
        position -= 1
        before_seq = None
    return None, None
