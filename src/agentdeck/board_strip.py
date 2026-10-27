"""The front page's one-line read of the poller board (#1007, #1025).

Every card the engine is tracking except the ones that reached the project's own
done column, each with the column it sits in and its pull request when the deck
knows of one. Cards come from the engine's durable state and pull requests from
the shared metadata cache, so rendering the dashboard makes no GitHub call.

It used to answer a second question — which cards nothing was working — by
reading each chat's state. That group never worked: a session is `thinking`,
`idle`, `remote` or `failed`, and `idle` means *no live process*, so a finished
chat read as one still working the card. What the operator saw was the window
after a restart when the collector had not scanned yet (#1014). The column is
the answer the strip shows instead, and it still derives nothing from a chat's
state.

What it does carry is a way in: a row whose issue has a chat gets a link to the
newest one, so the chat working a card is one press from the card rather than
something to go and find in the list (#1694). That is identity only — which
chat, and where it lives — never a claim about what the chat is doing.

The honesty rule the rest of the issue read model follows applies here too: an
incomplete read is not an empty board. ``complete`` false means the strip is
showing part of the answer, and the bar says so rather than showing a zero.

A project with no GitHub Project board keeps its card status in a ``status:*``
label instead (#1481), so until an issue carries one the engine tracks no card
for it and the whole project used to be missing from the strip. There the open
issue *is* the card, so a boardless project's cardless open issues get rows of
their own (#1691). Only that project: elsewhere an issue nobody put on the board
is genuinely not a card, and the strip must not invent one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .issue_context import PendingItem, PendingView

# How many cardless issues one boardless project may put on the strip. Its
# backlog is the whole repository, which the front page's collapsed one-liner is
# not the place to read; the rest is counted and pointed at ``/pollers``.
BOARDLESS_ROW_LIMIT = 10


@dataclass(frozen=True)
class StripRow:
    """One card on the strip, already resolved to what its row shows."""

    repository: str
    number: int
    title: str
    url: str
    status: str  # the board column the engine last wrote
    in_review: bool = False
    pull_number: int = 0
    pull_url: str = ""
    # The newest chat working this card, when the deck knows of one: its leg key
    # and the page that opens it. No state — the row says which chat to open,
    # not what it is up to (#1694).
    session_key: str = ""
    session_path: str = ""
    session_title: str = ""

    @property
    def identity(self) -> str:
        return f"{self.repository}#{self.number}"

    @property
    def short_identity(self) -> str:
        """``#412`` — the instance heading already carries the repository."""
        return f"#{self.number}"

    @property
    def has_pull(self) -> bool:
        return bool(self.pull_number)

    @property
    def has_session(self) -> bool:
        return bool(self.session_path)


@dataclass(frozen=True)
class InstanceStrip:
    """One poller instance's rows."""

    instance: str
    cards: tuple[StripRow, ...] = ()
    # Boardless issues this instance has beyond ``BOARDLESS_ROW_LIMIT``. Said out
    # loud on the strip rather than dropped quietly: a cut list that looks whole
    # is the same lie as an unread one that looks empty.
    overflow: int = 0

    @property
    def review_count(self) -> int:
        return sum(1 for row in self.cards if row.in_review)

    @property
    def empty(self) -> bool:
        return not self.cards


@dataclass(frozen=True)
class BoardStrip:
    """Every instance with something to show, plus whether the read was whole."""

    instances: tuple[InstanceStrip, ...] = ()
    complete: bool = True
    unknown: tuple[str, ...] = ()

    @property
    def card_count(self) -> int:
        return sum(len(entry.cards) for entry in self.instances)

    @property
    def review_count(self) -> int:
        return sum(entry.review_count for entry in self.instances)

    @property
    def empty(self) -> bool:
        return not self.instances

    def as_json(self) -> dict:
        return {
            "complete": self.complete,
            "unknown": list(self.unknown),
            "cards": self.card_count,
            "review": self.review_count,
            "instances": [
                {
                    "instance": entry.instance,
                    "cards": [row.identity for row in entry.cards],
                    "overflow": entry.overflow,
                }
                for entry in self.instances
            ],
        }


def _open_pull(item: PendingItem) -> dict | None:
    for pull in item.pull_requests:
        if pull.get("status") == "open":
            return pull
    return None


def _chat_page(session_key: str) -> str:
    """Where a chat opens when the caller gave no resolver of its own.

    The leg-scoped page, which is the one route that always exists: a durable
    ``/chats/<id>`` needs the chat store, and only the caller holding the app
    state can say whether that id will still resolve after a restart.
    """
    return f"/sessions/{session_key}"


def _row(
    item: PendingItem,
    pull: dict | None,
    session_path: Callable[[str], str] = _chat_page,
) -> StripRow:
    issue = item.issue
    card = item.card
    # Already newest first: the join sorts an item's chats by start/activity
    # time, so the head is the latest chat that named this issue.
    session = item.sessions[0] if item.sessions else None
    return StripRow(
        repository=item.repository,
        number=item.number,
        title=(issue.title if issue else "") or "",
        url=(issue.url if issue else "")
        or f"https://github.com/{item.repository}/issues/{item.number}",
        status=(card.status if card else "") or "Status unknown",
        in_review=bool(card and card.in_review),
        pull_number=int(pull.get("number", 0)) if pull else 0,
        pull_url=str(pull.get("url", "")) if pull else "",
        session_key=session.key if session else "",
        session_path=session_path(session.key) if session else "",
        session_title=session.title if session else "",
    )


def build_strip(
    view: PendingView,
    scopes: Sequence[object],
    *,
    session_path: Callable[[str], str] = _chat_page,
) -> BoardStrip:
    """Group the joined pending view into per-instance card lists.

    ``scopes`` are the board's own ``BoardScope`` rows: they map a repository to
    the instance that declares it, which is the only place that mapping exists.
    A card whose repository no scope claims is dropped rather than guessed into
    an instance.

    ``session_path`` turns a chat's leg key into the page that opens it. The
    dashboard passes the app state's own resolver, which prefers the durable
    ``/chats/<id>`` address; the default here falls back to the leg page.
    """
    owner: dict[str, str] = {}
    order: list[str] = []
    boardless: set[str] = set()
    for scope in scopes:
        instance = getattr(scope, "instance", "")
        if not instance:
            continue
        if instance not in order:
            order.append(instance)
        if getattr(scope, "boardless", False):
            boardless.add(instance)
        for repository in getattr(scope, "issue_repositories", ()) or ():
            owner.setdefault(repository.lower(), instance)

    cards: dict[str, list[StripRow]] = {}
    uncarded: dict[str, list[StripRow]] = {}
    for item in view.items:
        card = item.card
        instance = owner.get(item.repository.lower())
        if not instance:
            continue
        if card is None:
            # No card is the whole point for a boardless project: its issues are
            # its board, and one that has not been given a `status:*` label yet
            # is still work the operator should see (#1691). It has to be an
            # issue the deck read as open — an item that exists only because a
            # chat or a pull request named it is not evidence of an open issue.
            if (
                instance not in boardless
                or item.issue is None
                or not item.issue.is_open
                or item.issue_closed
            ):
                continue
            uncarded.setdefault(instance, []).append(
                _row(item, _open_pull(item), session_path)
            )
            continue
        # Only the cards still on the board: a card in the project's own done
        # column is finished business, and leaving it here would grow the strip
        # forever (#1025). Neither is a card whose issue is already closed while
        # the column never moved — the board outlived the work, and the strip is
        # not the place to read about it (#1033).
        if card.done or item.issue_closed:
            continue
        cards.setdefault(instance, []).append(
            _row(item, _open_pull(item), session_path)
        )

    instances = []
    for instance in order:
        rows = list(cards.get(instance, []))
        spare = sorted(uncarded.get(instance, []), key=lambda row: -row.number)
        overflow = max(0, len(spare) - BOARDLESS_ROW_LIMIT)
        rows.extend(spare[:BOARDLESS_ROW_LIMIT])
        if rows:
            instances.append(
                InstanceStrip(
                    instance=instance,
                    # In review first — it is the column that wants the operator —
                    # then newest first within each column.
                    cards=tuple(
                        sorted(
                            rows,
                            key=lambda row: (
                                not row.in_review,
                                row.status.casefold(),
                                -row.number,
                            ),
                        )
                    ),
                    overflow=overflow,
                )
            )

    return BoardStrip(
        instances=tuple(instances),
        # Only what a row actually depends on: the cards, and the pull requests
        # that decide which cards are here. A stale issue list costs a title,
        # which the row already falls back from — counting it would put the
        # warning back on permanently, which is what #1016 removed (#1017).
        #
        # An issue list too old to say what is still open is a different thing,
        # and it does decide which cards are here: every row is on the strip
        # only because the deck could not prove its issue closed. That read is
        # rare — it means the refresher has been down for an hour or more — so
        # saying so costs nothing the way per-cycle staleness did (#1115).
        complete=(
            view.board_complete
            and view.pulls_complete
            and not view.expired_issue_reads
        ),
        unknown=_unread(view),
    )


def _unread(view: PendingView) -> tuple[str, ...]:
    """What the strip could not read, by name, for the "partial" tooltip."""
    names: list[str] = []
    if not view.board_complete:
        names.extend(view.unknown)
    for repository in view.expired_issue_reads:
        # Say what is wrong with it, not just its name: the operator reading
        # this tooltip is deciding whether to trust the rows below it.
        label = f"{repository} (issue list too old to say what is still open)"
        if label not in names:
            names.append(label)
    return tuple(names)
