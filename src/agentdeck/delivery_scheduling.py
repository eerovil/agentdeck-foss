"""Which provider account a new piece of work starts on (#2146, #2757).

``choose_account`` was the Runtime's account selection for the Poller's ``ensure_delivery``.
That route and its per-delivery routing went with the poller's card dispatcher (#3741); the
selection itself stays, because starting a Work (``work_start``) and moving a chat to another
account (``usage_move``) still choose with it:

* a hard-quota circuit takes an account out of the running;
* new work starts where the most budget is left per hour (#2757);
* an account about to lose unspent subscription budget moves to the front (#1965);
* a new card skips an account whose five-hour window is nearly full (#3580);
* otherwise the operator's order;
* an account whose usage window will refuse a turn is passed over, and when every account
  would refuse, the one whose block clears first still gets the work (#1519, #1726).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from .planned_reset import plan_for
from .poller_engine import github as usage
from .providers import account_health

# How long a busy worker's activity clock pins it without a fresh tick (#2059). The same two
# hours the Poller once gave a stop before calling it stalled, which is where it was defined.
STALE_SECONDS = 2 * 3600

# The provider a bare account label means. The Poller has always read `main` as Claude's.
DEFAULT_PROVIDER = "claude_code"


def target_of(account: str) -> tuple[str, str]:
    """``provider:label`` → both halves; a bare label is Claude's, as it always was."""
    provider, sep, label = account.partition(":")
    return (provider, label) if sep else (DEFAULT_PROVIDER, account)


def _expiring_budget_first(
    order: list[str],
    usage_cache: Path | None,
    usage_targets: Mapping[str, float],
    notes: list[str],
    planned_resets: Mapping[str, datetime] | None = None,
) -> list[str]:
    """An account about to waste the most subscription budget leads (#1965); order ties."""
    shortfalls = {
        name: usage.account_expiring_headroom(
            usage_cache,
            name,
            target_pct=usage_targets.get(name, usage.USAGE_TARGET_PCT),
            planned_reset=plan_for(planned_resets, name),
        )
        for name in order
    }
    if not any(shortfalls.values()):
        return order
    ranked = sorted(enumerate(order), key=lambda pair: (-shortfalls[pair[1]], pair[0]))
    promoted = [name for _, name in ranked]
    if promoted != order:
        leader = promoted[0]
        notes.append(
            f"{leader} is {shortfalls[leader]:.0f} points short of its target with its window "
            f"about to reset — offering it the work ahead of {order[0]}"
        )
    return promoted


# Points per hour within which two accounts count as equally behind, so the operator's order
# decides rather than the jitter between two usage readings.
PACE_TIE = 0.1


def _budget_pace_first(
    order: list[str],
    usage_cache: Path | None,
    usage_targets: Mapping[str, float],
    notes: list[str],
    planned_resets: Mapping[str, datetime] | None = None,
) -> list[str]:
    """A new card starts on the account with the most budget left per hour (#2757).

    Only accounts of the leading account's provider are ranked, each keeping one of the slots
    those accounts held, so another provider's fallback never moves: Codex's meter reads 0%
    whatever it has spent. Any ranked account without a fresh reading leaves the order alone,
    because ignorance must not move work. Paces within ``PACE_TIE`` keep the operator's order.
    """
    provider = target_of(order[0])[0]
    slots = [index for index, name in enumerate(order) if target_of(name)[0] == provider]
    if len(slots) < 2:
        return order
    paces: dict[str, float] = {}
    for index in slots:
        name = order[index]
        pace = usage.account_budget_pace(
            usage_cache,
            name,
            target_pct=usage_targets.get(name, usage.USAGE_TARGET_PCT),
            planned_reset=plan_for(planned_resets, name),
        )
        if pace is None:
            return order
        paces[name] = pace
    ranked = sorted((order[index] for index in slots), key=lambda name: -paces[name])
    if paces[ranked[0]] - paces[order[0]] <= PACE_TIE:
        return order  # readings taken moments apart differ by a hair; that is not a reason
    promoted = list(order)
    for index, name in zip(slots, ranked, strict=True):
        promoted[index] = name
    if promoted[0] != order[0]:
        leader, before = promoted[0], order[0]
        notes.append(
            f"{leader} has {paces[leader]:.1f} points/hour of its budget left against "
            f"{before}'s {paces[before]:.1f} — starting the card there"
        )
    return promoted


# A five-hour window this full is about to stop the chat mid-turn (#3580).
FIVE_HOUR_NEAR_FULL_PCT = 90.0


def _five_hour_room_first(
    order: list[str],
    usage_cache: Path | None,
    usage_targets: Mapping[str, float],
    notes: list[str],
) -> list[str]:
    """A new card skips an account whose five-hour window is nearly full (#3580).

    The budget rankings ignore the five-hour window, so on their own they pile every new card
    onto one account until that window runs out and stops its chats mid-turn. Accounts of the
    leading account's provider at ``FIVE_HOUR_NEAR_FULL_PCT`` or more move behind those with
    room, each keeping their order. When none has room, or nothing reads, the order stands.
    An account its operator opted out with a target of 0 never counts as having room.
    """
    provider = target_of(order[0])[0]
    slots = [index for index, name in enumerate(order) if target_of(name)[0] == provider]
    if len(slots) < 2:
        return order
    now = datetime.now(UTC)
    full: dict[str, float] = {}
    for index in slots:
        name = order[index]
        for window, pct, resets in usage.account_window_readings(usage_cache, name, now=now):
            if window == "five_hour" and pct >= FIVE_HOUR_NEAR_FULL_PCT and resets > now:
                full[name] = pct
    roomy = [
        order[index]
        for index in slots
        if order[index] not in full
        and usage_targets.get(order[index], usage.USAGE_TARGET_PCT) > 0
    ]
    if not full or not roomy:
        return order
    promoted = list(order)
    ranked = roomy + [order[index] for index in slots if order[index] not in roomy]
    for index, name in zip(slots, ranked, strict=True):
        promoted[index] = name
    if promoted[0] != order[0]:
        before = order[0]
        why = (
            f"{before}'s five-hour window is {full[before]:.0f}% full"
            if before in full
            else f"{before} is opted out and {', '.join(full)} nearly full for five hours"
        )
        notes.append(f"{why} — starting the card on {promoted[0]} instead")
    return promoted


_NEVER_CLEARS = datetime.max.replace(tzinfo=UTC)


def choose_account(
    accounts: list[str],
    usage_cache: Path | None,
    usage_targets: Mapping[str, float],
    notes: list[str],
    *,
    cold: bool = False,
    planned_resets: Mapping[str, datetime] | None = None,
) -> str:
    """An eligible account, or ``""`` when every one has an open hard-quota circuit.

    ``cold`` is a card with no account of its own yet; its first account is chosen by the
    budget each account has left per hour (#2757) rather than by the operator's order alone.
    ``planned_resets`` is ``Db.planned_resets``: an account a person will reset soon has its
    budget end then, aimed at 100%, so it is offered the work first (#3379). A cold card
    then skips an account whose five-hour window is nearly full (#3580).
    """
    order = [name for name in accounts if name]
    if not order:
        return ""
    circuits = [(name, *account_health.refusal_detail(usage_cache, name)) for name in order]
    open_accounts = {name for name, reason, _ in circuits if reason is not None}
    if open_accounts:
        details = "; ".join(
            f"{name} ({reason})" for name, reason, _ in circuits if reason is not None
        )
        notes.append(f"skipping provider-ineligible accounts: {details}")
    order = [name for name in order if name not in open_accounts]
    if not order:
        notes.append("all configured accounts have open hard-quota circuits; withholding delivery")
        return ""
    if cold:
        order = _budget_pace_first(order, usage_cache, usage_targets, notes, planned_resets)
    order = _expiring_budget_first(order, usage_cache, usage_targets, notes, planned_resets)
    if cold:
        order = _five_hour_room_first(order, usage_cache, usage_targets, notes)
    refused: list[tuple[str, str, datetime | None]] = []
    for name in order:
        reason, clears_at = usage.account_refusal_detail(usage_cache, name)
        if not reason:
            if refused:
                skipped = "; ".join(f"{who} ({why})" for who, why, _ in refused)
                notes.append(f"skipping {skipped} — delivering to {name} instead")
            return name
        refused.append((name, reason, clears_at))
    chosen, _, chosen_clears = min(
        refused, key=lambda entry: entry[2] if entry[2] is not None else _NEVER_CLEARS
    )
    blocked = "; ".join(f"{who} ({why})" for who, why, _ in refused)
    why_chosen = (
        f"its block clears first, at {chosen_clears.isoformat()}"
        if chosen_clears is not None
        else "no refusal says when it clears, so the operator's order decides"
    )
    notes.append(
        f"every configured account will refuse a turn: {blocked} — delivering to "
        f"{chosen} anyway rather than withholding the work: {why_chosen}"
    )
    return chosen
