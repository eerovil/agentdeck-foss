"""The one-time backfill of delegated children's Work membership (#2207).

Before #2207 the Work projection inferred a delegated child's Work on every
pass by walking up to its parent: a child inherited its parent's Work when the
parent had exactly one. A child's membership is now a saved fact instead
(``session_work_bindings``), written when the child is created. Children
recorded before that rule have no saved fact, so this module runs the old
parent rule over them exactly once and says what to save:

* the old rule placed the child in one Work → a ``backfilled`` binding;
* it placed nothing, but the parents its legs name are in some Works → one
  ``ambiguous`` row per candidate, which joins no Work;
* it placed nothing, a parent resolved and none of them is in a Work → a
  ``none`` row, so the decision is saved too;
* no parent resolves → nothing to save: the delegation stays pending until its
  parent is scanned (``AppState.finalize_pending_delegations``).

Only chats the deck itself recorded as delegated are backfilled. A
provider-internal subagent (``Session.parent_session_key``) is never a Work
member of its own. The caller records that the backfill ran, so it never runs
again and the projection never walks the parent chain.

LEGACY(epic:work): this is the last reader of the parent-inheritance rule.
Delete the module once every deck has recorded the ``delegated_work_v1``
backfill.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .work import AMBIGUOUS, WorkEvidence, WorkRef

BACKFILL_NAME = "delegated_work_v1"
# When this rule first ran on the deck. Only delegations recorded before it are the
# backfill's; a later one is decided by the creation rule, Run first (#2207).
CUTOVER_NAME = "delegated_work_cutover"

# Saved delegation outcomes that are not memberships: the child took its parent's
# Run (the Run binding carries the membership), or its parent was in no Work.
DELEGATION_RUN = "run"
DELEGATION_NONE = "none"

# A parent reference that matched no chat, or more than one.
UNRESOLVED_PARENT = "unresolved:"


@dataclass(frozen=True)
class LegacyChat:
    """One leg of a chat as the old parent rule saw it.

    ``works`` is the Work set the leg carries through saved, explicit evidence
    (the new projection's answer for it). ``parent`` is the parent chat id this
    leg's own delegation resolved to; an unresolved reference matches no chat.
    A reference that resolved to no chat starts with ``UNRESOLVED_PARENT``.
    ``delegated`` marks a leg the deck recorded as delegated, and ``internal`` a
    provider-internal subagent leg, which is never backfilled.
    """

    chat_id: str
    session_key: str
    works: frozenset[WorkRef] = frozenset()
    parent: str | None = None
    delegated: bool = False
    internal: bool = False


@dataclass(frozen=True)
class BackfillRow:
    session_key: str
    work_id: str
    evidence: str
    parent_chat_id: str


@dataclass
class BackfillResult:
    rows: list[BackfillRow] = field(default_factory=list)
    considered: int = 0  # delegated chats with no saved outcome yet
    backfilled: int = 0  # placed in exactly one Work
    ambiguous: int = 0  # placed in none, with candidates saved
    no_work: int = 0  # a parent resolved, but none of them is in a Work
    pending: int = 0  # no parent resolves yet; nothing saved

    def summary(self) -> dict[str, int]:
        return {
            "considered": self.considered,
            "backfilled": self.backfilled,
            "ambiguous": self.ambiguous,
            "no_work": self.no_work,
            "pending": self.pending,
            "rows": len(self.rows),
        }


def plan_backfill(legs: Iterable[LegacyChat], *, decided: set[str]) -> BackfillResult:
    """What to save for every recorded delegated chat not in ``decided``.

    ``decided`` holds chat ids that already carry a saved outcome — a
    delegated, backfilled or ambiguous row, or a Run binding.
    """
    by_chat: dict[str, list[LegacyChat]] = {}
    for leg in legs:
        by_chat.setdefault(leg.chat_id, []).append(leg)
    direct = {
        chat: set().union(*(leg.works for leg in entries)) for chat, entries in by_chat.items()
    }
    parents = {chat: _single_parent(chat, entries) for chat, entries in by_chat.items()}
    on_cycle = _delegation_cycles(parents)
    resolved: dict[str, set[WorkRef]] = {}

    def memberships(chat_id: str) -> set[WorkRef]:
        # The pre-#2207 projection rule, iterative so a deep chain cannot recurse.
        chain: list[str] = []
        current: str | None = chat_id
        while current is not None and current not in resolved:
            chain.append(current)
            parent = parents.get(current)
            current = None if current in on_cycle or parent not in direct else parent
        for node in reversed(chain):
            own = set(direct[node])
            parent = parents.get(node)
            if node not in on_cycle and parent in direct and len(resolved[parent]) == 1:
                own |= resolved[parent]
            resolved[node] = own
        return resolved[chat_id]

    result = BackfillResult()
    for chat_id in sorted(by_chat):
        entries = by_chat[chat_id]
        keys = sorted(leg.session_key for leg in entries if leg.delegated and not leg.internal)
        if not keys or chat_id in decided:
            continue
        result.considered += 1
        parent = parents[chat_id]
        placed = (
            resolved_parent
            if parent is not None
            and chat_id not in on_cycle
            and parent in direct
            and len(resolved_parent := memberships(parent)) == 1
            else set()
        )
        if placed:
            (work,) = placed
            result.backfilled += 1
            result.rows.extend(
                BackfillRow(key, work.work_id, str(WorkEvidence.BACKFILLED), parent or "")
                for key in keys
            )
            continue
        # A parent that resolved to a chat with no scanned leg is still a parent: it
        # simply carries no Work.
        named = sorted(
            {
                leg.parent
                for leg in entries
                if leg.parent
                and not leg.parent.startswith(UNRESOLVED_PARENT)
                and leg.parent != chat_id
            }
        )
        candidates = set().union(*(memberships(p) for p in named if p in direct))
        if not named:
            result.pending += 1
            continue
        if not candidates:
            result.no_work += 1
            result.rows.extend(
                BackfillRow(key, "", DELEGATION_NONE, ",".join(named)) for key in keys
            )
            continue
        result.ambiguous += 1
        result.rows.extend(
            BackfillRow(key, work.work_id, AMBIGUOUS, ",".join(named))
            for key in keys
            for work in sorted(candidates)
        )
    return result


def _single_parent(chat_id: str, entries: Sequence[LegacyChat]) -> str | None:
    """The chat's parent when every leg that names one agrees; else none."""
    named = {leg.parent for leg in entries if leg.parent}
    named.discard(chat_id)
    return named.pop() if len(named) == 1 else None


def _delegation_cycles(parents: Mapping[str, str | None]) -> set[str]:
    """Chats that are their own ancestor; they inherit nothing."""
    on_cycle: set[str] = set()
    done: set[str] = set()
    for start in sorted(parents):
        path: list[str] = []
        index: dict[str, int] = {}
        current: str | None = start
        while current is not None and current in parents and current not in done:
            if current in index:
                on_cycle.update(path[index[current] :])
                break
            index[current] = len(path)
            path.append(current)
            current = parents[current]
        done.update(path)
    return on_cycle
