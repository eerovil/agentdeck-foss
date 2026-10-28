"""Works as a tree of GitHub parent issues and sub-issues (#3622).

GitHub's own sub-issues are the link, read from fields every issue read already carries: a
child's record holds ``parent`` (the payload's ``parent_issue_url``) and a parent's holds
``subIssues`` (its ``sub_issues_summary``). Nothing here calls GitHub.

The front page lists a parent issue's Work as one row with its sub-issues' Works folded in
(``fold``): the row stands in the most urgent section of itself and its children, shows the
child that needs the owner when one does, and says how far along the children are. Each
child stays a whole Work with its own page, chats and notifications; only the front page's
rows are grouped. A Work folds into its top-most ancestor the front page has a row for.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from . import wayfinder_map
from .models import IssueRef
from .work import WorkRef
from .work_story import FINISHED, IN_PROGRESS, NEEDS_YOU, PAUSED, QUIET, FrontRow

# GitHub nests sub-issues at most eight levels deep; a walk stops there, and at a cycle.
MAX_DEPTH = 8

_API_ISSUE = re.compile(r"^https://api\.github\.com/repos/([^/\s]+)/([^/\s]+)/issues/(\d+)$")


def parent_ref(record: Any) -> WorkRef | None:
    """The parent issue a cached issue record names, or None for none or any other shape."""
    url = record.get("parent") if isinstance(record, Mapping) else None
    match = _API_ISSUE.match(url) if isinstance(url, str) else None
    if match is None:
        return None
    issue = IssueRef.parse(f"{match.group(1)}/{match.group(2)}#{match.group(3)}")
    return WorkRef.from_issue(issue) if issue is not None else None


def sub_issue_counts(record: Any) -> tuple[int, int] | None:
    """``(completed, total)`` of an issue's direct sub-issues, or None when it has none."""
    value = record.get("subIssues") if isinstance(record, Mapping) else None
    if not isinstance(value, Mapping):
        return None
    total, completed = value.get("total"), value.get("completed")
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in (total, completed)):
        return None
    if total <= 0:
        return None
    return (max(0, min(completed, total)), total)


def parents_of(records: Mapping[str, Any]) -> dict[str, str]:
    """Each Work's direct parent Work id, from its issue record; an issue is never its own."""
    found = {}
    for work_id, record in records.items():
        parent = parent_ref(record)
        if parent is not None and parent.work_id != work_id:
            found[work_id] = parent.work_id
    return found


def _walk(work_id: str, parents: Mapping[str, str]) -> tuple[tuple[str, ...], bool]:
    """The Work's ancestors, nearest first, at most ``MAX_DEPTH``; and whether they loop."""
    chain: list[str] = []
    seen = {work_id}
    current = work_id
    while len(chain) < MAX_DEPTH:
        parent = parents.get(current)
        if parent is None:
            return tuple(chain), False
        if parent in seen:
            return tuple(chain), True
        chain.append(parent)
        seen.add(parent)
        current = parent
    return tuple(chain), False


def ancestors(work_id: str, parents: Mapping[str, str]) -> tuple[str, ...]:
    """The Work's parent, its parent's parent and so on: at most ``MAX_DEPTH``, no cycle."""
    return _walk(work_id, parents)[0]


def root_of(
    work_id: str,
    parents: Mapping[str, str],
    present: Collection[str],
    stop: Collection[str] = frozenset(),
) -> str:
    """The top-most ancestor among ``present``, or the Work itself when none is.

    The walk ends at an ancestor in ``stop`` (a parent the owner hid): its descendants fold
    into it and are hidden with it. Issues that name each other as parents have no top:
    every one of them takes the smallest id among them, so one of them is still the row the
    others fold into."""
    chain, cyclic = _walk(work_id, parents)
    if cyclic:
        return min(item for item in (work_id, *chain) if item == work_id or item in present)
    root = work_id
    for ancestor in chain:
        if ancestor in present:
            root = ancestor
            if ancestor in stop:
                break
    return root


def progress(counts: tuple[int, int] | None, children: Sequence[FrontRow]) -> str:
    """The parent row's progress line: "3/7 done · 1 waits for you · 2 working".

    How many are done is GitHub's own count of the direct sub-issues, so a child the deck no
    longer lists still counts; the rest counts the children the row shows."""
    parts = []
    if counts is not None:
        parts.append(f"{counts[0]}/{counts[1]} done")
    waits = sum(1 for child in children if child.section == NEEDS_YOU)
    working = sum(1 for child in children if child.section == IN_PROGRESS)
    if waits:
        parts.append(f"{waits} {'waits' if waits == 1 else 'wait'} for you")
    if working:
        parts.append(f"{working} working")
    if not parts and children:
        parts.append(f"{len(children)} {'sub-issue' if len(children) == 1 else 'sub-issues'}")
    return " · ".join(parts)


def map_progress(
    own: FrontRow,
    children: Sequence[FrontRow],
    counts: tuple[int, int] | None,
    elsewhere: int = 0,
) -> wayfinder_map.Progress | None:
    """A wayfinder map's progress from its row and its tickets' rows (#3696), or None when
    ``own`` is not a map.

    Asking counts only real questions, the map's own included. Blocked counts the listed
    tickets a Poller card holds back, and every open ticket the front page lists no Work
    for: a map files a blocked ticket with no agent, so no Work starts until its last
    blocker closes (``WAYFINDER.md``). ``elsewhere`` are its open tickets the front page lists
    as rows of their own (one the owner hid, for the Hidden list): listed, so not blocked."""
    if not wayfinder_map.is_map(own.card.labels):
        return None
    asking = sorted(
        ((row, found) for row in (own, *children) if (found := wayfinder_map.question(row))),
        key=lambda pair: _needs_order(pair[0]),
    )
    questions = tuple(found for _, found in asking)
    open_rows = [
        row for row in children if row.section != FINISHED and not row.card.awaiting_finish
    ]
    held = [row for row in open_rows if row.card.blocked_by]
    unlisted = max(0, counts[1] - counts[0] - len(open_rows) - elsewhere) if counts else 0
    working = sum(
        1
        for row in open_rows
        if row.section == IN_PROGRESS and not row.waits and not row.card.blocked_by
    )
    return wayfinder_map.Progress(
        work_id=own.card.work_id,
        title=own.card.issue_title or own.card.title,
        href=own.card.href,
        counts=counts,
        working=working,
        blocked=len(held) + unlisted,
        questions=questions,
        tickets=tuple(row.card.work_id for row in children),
    )


def _stamp(row: FrontRow) -> float:
    when = row.card.last_activity
    return when.timestamp() if when is not None else 0.0


def _needs_order(row: FrontRow) -> tuple[int, int, float]:
    """Needs you's own order: the category first, then strongest rank, then newest."""
    from .work_attention import FRONT_ORDER

    category = row.attention.category if row.attention is not None else ""
    place = FRONT_ORDER.index(category) if category in FRONT_ORDER else len(FRONT_ORDER)
    return (place, row.rank, -_stamp(row))


# D5: where a parent row stands, by the most urgent of itself and its children after Needs you.
_PLACE = {"active": 0, "waiting": 1, "live": 2, FINISHED: 3}


def _place(row: FrontRow) -> str:
    """The front-page list a row would be in on its own (``front_sections``)."""
    if row.section == IN_PROGRESS:
        return "live" if row.live else "waiting" if row.waits else "active"
    return row.section


def _open_work(counts: tuple[int, int] | None, children: Sequence[FrontRow]) -> bool:
    """Whether any of a parent's sub-issues is still to do (owner's rule, #3622): GitHub's
    count when known, whatever the front page lists; else any listed child not finished."""
    if counts is not None:
        return counts[0] < counts[1]
    return any(child.section != FINISHED for child in children)


def _demoted(own: FrontRow, *, tree_only: bool, open_work: bool) -> FrontRow:
    """The parent's own row, without a Needs you it does not have as a parent.

    A parent the front page lists only for its children (``tree_only``) is never Needs you
    on its own. One no agent works on is not either while a sub-issue is still to do
    (``open_work``): they are the work (#3622). With all of them done it is an ordinary
    unstarted issue again."""
    from .work_attention import UNSTARTED

    if own.section != NEEDS_YOU:
        return own
    unstarted = own.attention is not None and own.attention.category == UNSTARTED
    if not (tree_only or (unstarted and open_work)):
        return own
    return replace(
        own, section=QUIET, attention=None, action="", rank=0, stopped_at=None, unseen=False
    )


def _folded(
    own: FrontRow,
    children: Sequence[FrontRow],
    counts: tuple[int, int] | None,
    *,
    tree_only: bool,
    hidden: bool = False,
    elsewhere: int = 0,
) -> FrontRow:
    own = _demoted(own, tree_only=tree_only, open_work=_open_work(counts, children))
    waiting = [row for row in (own, *children) if row.section == NEEDS_YOU]
    if waiting:
        # ``min`` keeps the first of equals, so the parent's own question leads a tie.
        lead = min(waiting, key=_needs_order)
    elif own.section == PAUSED or hidden:
        # Paused only by its own pause, and hidden only by its own hide: only a question
        # goes past either (a working child does not bring a hidden parent back).
        lead = own
    else:
        # D5's order; the parent first among equals, then the newest child.
        lead = min(
            (own, *sorted(children, key=_stamp, reverse=True)),
            key=lambda row: _PLACE.get(_place(row), len(_PLACE)),
        )
    activity = [row.card.last_activity for row in (own, *children) if row.card.last_activity]
    card = replace(
        own.card,
        starred=any(row.card.starred for row in (own, *children)),
        last_activity=max(activity) if activity else None,
    )
    if lead is not own and lead.section == FINISHED:
        # Recently finished goes by when the Work finished: the child's, which the row shows.
        card = replace(card, finished_at=lead.card.finished_at)
    found = map_progress(own, children, counts, elsewhere)
    return replace(
        lead,
        card=card,
        children=tuple(children),
        own=own,
        lead=None if lead is own else lead,
        progress=found.line if found is not None else progress(counts, children),
        map=found,
    )


def fold(
    rows: Sequence[FrontRow],
    *,
    parents: Mapping[str, str],
    counts: Mapping[str, tuple[int, int]] | None = None,
    hidden: Collection[str] = frozenset(),
    tree_only: Collection[str] = frozenset(),
) -> list[FrontRow]:
    """The rows with every sub-issue's Work folded into its top-most listed ancestor's row.

    ``parents`` maps a Work id to its direct parent's (``parents_of``), ``counts`` a parent's
    Work id to its ``sub_issue_counts``. A child the owner hid out of Needs you or Recently
    finished is left out of its parent's row and kept as its own, for the Hidden list.
    ``tree_only`` are parents listed only because a child is: one left with no child to show
    is dropped. A row with no listed ancestor and no child is returned as it was.
    """
    counts = counts or {}
    hidden = frozenset(hidden)
    tree_only = frozenset(tree_only)
    present = {row.card.work_id for row in rows}
    # A Work the owner hid keeps its own row, for the Hidden list to bring back, and what
    # hangs under it folds into it and is hidden with it.
    put = {row.card.work_id for row in rows if _put_away(row, hidden)}
    roots = {
        row.card.work_id: (
            row.card.work_id
            if row.card.work_id in put
            else root_of(row.card.work_id, parents, present, stop=put)
        )
        for row in rows
    }
    children: dict[str, list[FrontRow]] = {}
    for row in rows:
        root = roots[row.card.work_id]
        if root != row.card.work_id:
            children.setdefault(root, []).append(row)
    # The open rows under a parent that a hide keeps out of its row (#3696): a map counts
    # them as listed, never as blocked tickets with no Work.
    apart: dict[str, int] = {}
    for row in rows:
        work_id = row.card.work_id
        under = root_of(work_id, parents, present)
        if under in (work_id, roots[work_id]) or row.section == FINISHED:
            continue
        if not row.card.awaiting_finish:
            apart[under] = apart.get(under, 0) + 1
    found = []
    for row in rows:
        work_id = row.card.work_id
        if roots[work_id] != work_id:
            continue
        kids = children.get(work_id, ())
        if not kids:
            if work_id in tree_only:
                continue
            count = counts.get(work_id)
            if not count:
                found.append(row)
                continue
            # GitHub's own count stands even when no sub-issue's Work is listed (D6): an old
            # never-started one, one in another repository, or an archived one. While any is
            # open, they are the work, so no agent on the parent is not a Needs you (D3).
            own = _demoted(row, tree_only=False, open_work=_open_work(count, ()))
            plan = map_progress(own, (), count, apart.get(work_id, 0))
            line = plan.line if plan is not None else progress(count, ())
            found.append(replace(own, progress=line, map=plan))
            continue
        found.append(
            _folded(
                row,
                kids,
                counts.get(work_id),
                tree_only=work_id in tree_only,
                hidden=work_id in put,
                elsewhere=apart.get(work_id, 0),
            )
        )
    return found


def _put_away(row: FrontRow, hidden: Collection[str]) -> bool:
    """A child the owner hid, in a section a hide takes it out of (``front_sections``)."""
    return row.card.work_id in hidden and row.section in {NEEDS_YOU, FINISHED}


def hides_ended(
    rows: Sequence[FrontRow],
    *,
    parents: Mapping[str, str],
    hidden_at: Mapping[str, Any],
) -> frozenset[str]:
    """The hidden parents a child newly needs the owner in: their hides end (#3622).

    ``hidden_at`` maps each hidden Work id to when it was hidden. A child counts when its row
    is Needs you and it stopped there after a listed ancestor of it was hidden, at any
    level, so a question is never buried under a hidden parent or grandparent."""
    present = {row.card.work_id for row in rows}
    ended = set()
    for row in rows:
        if row.card.work_id in hidden_at or row.section != NEEDS_YOU or row.stopped_at is None:
            continue
        for ancestor in ancestors(row.card.work_id, parents):
            when = hidden_at.get(ancestor)
            if ancestor in present and when is not None and row.stopped_at > when:
                ended.add(ancestor)
    return frozenset(ended)


# --- the Work page's tree -------------------------------------------------------------------


@dataclass(frozen=True)
class TreeLine:
    """One issue on a Work page's tree: its parent, or one of its sub-issues (#3622)."""

    work_id: str
    href: str
    label: str  # "#n", or "repo#n" in another repository
    title: str
    status: str  # the Work's front-page line, else the issue's own state
    needs_you: bool = False
    closed: bool = False
    # A child that needs the owner: its own strongest control, acting on its own Work (D9).
    card: Any = None
    button: Any = None


@dataclass(frozen=True)
class WorkTree:
    """What a Work page shows of its issue's place in a tree (#3622)."""

    parent: TreeLine | None = None
    children: tuple[TreeLine, ...] = ()
    counts: tuple[int, int] | None = None
    # The sub-issues could not be read from GitHub this time: said, never an empty list.
    unreadable: bool = False
    # A wayfinder map's diagram of its sub-issues (#3627, ``wayfinder_map``), drawn in place of
    # the list; None for any other parent issue.
    diagram: Any = None

    @property
    def done(self) -> str:
        return f"{self.counts[0]}/{self.counts[1]} done" if self.counts else ""


def label_in(ref: WorkRef, home: WorkRef) -> str:
    """``#n`` in the same repository as ``home``, else ``repo#n``."""
    if ref.repository == home.repository:
        return f"#{ref.number}"
    return f"{ref.repository.rsplit('/', 1)[-1]}#{ref.number}"


def tree_line(
    ref: WorkRef,
    home: WorkRef,
    *,
    title: str,
    state: str,
    row: FrontRow | None,
    href: str,
) -> TreeLine:
    """One line of the tree: the Work's own front-page state when the front page has a row
    for it, else whether its issue is open or closed."""
    closed = state.lower() == "closed"
    button = None
    if row is not None:
        status = row.line or row.status.headline
        needs = row.section == NEEDS_YOU
        controls = row.buttons if needs else ()
        button = controls[0] if controls else None
    else:
        status = "Closed" if closed else "Open"
        needs = False
    return TreeLine(
        work_id=ref.work_id,
        href=href,
        label=label_in(ref, home),
        title=title or label_in(ref, home),
        status=status,
        needs_you=needs,
        closed=closed,
        card=row.card if button is not None else None,
        button=button,
    )
