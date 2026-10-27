"""Pure policy for poller-owned context maintenance.

This module decides only.  It performs no I/O and does not mutate compact-cycle
history; the coordinator in the enforcement PR will persist and execute decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .config import ContextManagementConfig


class ContextAction(StrEnum):
    NONE = "none"
    COMPACT = "compact"
    ROLLOVER = "rollover"


class MaintenanceResult(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContextCycle:
    action: ContextAction
    result: MaintenanceResult | None = None
    after_tokens: int | None = None
    run_id: str | None = None
    rearmed: bool = False


@dataclass(frozen=True)
class ContextPolicyInput:
    config: ContextManagementConfig
    provider_id: str
    context_tokens: int | None
    delivery_id: str | None
    # The Run this delivery belongs to (#2142). A different Run on a full window is new work, so
    # it rolls over directly instead of compacting first (#1471). Before #2194 the same value
    # travelled as ``dispatch_generation``.
    run_id: str | None = None
    previous_run_id: str | None = None
    previous_cycle: ContextCycle | None = None
    context_current: bool = True
    runtime_supported: bool = True
    lineage_supported: bool = True
    rollover_supported: bool = True
    compact_supported: bool = True
    require_active: bool = False
    fresh: bool = False
    image_count: int = 0
    active_delivery: bool = False
    pending_interaction: bool = False
    background_work: bool = False
    model_change_pending: bool = False


@dataclass(frozen=True)
class ContextDecision:
    proposed_action: ContextAction
    executed_action: ContextAction
    reason: str


def decide_context_action(value: ContextPolicyInput) -> ContextDecision:
    """Return the proposed and actually executable action for one new ID."""
    if value.model_change_pending:
        # An operator asking this chat to move model is not token maintenance, so it
        # does not wait for ``mode`` to be turned up.  With the mode off — the code
        # default, and what this host runs — there is no other moment at which a live
        # session would ever notice the change: every nudge into one is a stdin write
        # that drops the model argument silently (#1665).  What the mode comment
        # actually protects is delivering a maintenance operation to a runtime too old
        # to perform it, and that is `runtime_supported`, checked below either way.
        action, reason = _preference_proposal(value)
        return ContextDecision(action, action, reason)
    mode = value.config.mode
    # ``off`` decides exactly like ``observe`` and differs only in what the
    # caller does with the answer: neither executes anything, and the ledger row
    # keeps the mode it was decided under.  Off-mode used to short-circuit to
    # "disabled" here, which made every row unreadable — nothing could say what
    # ``enforce`` would have done at the same boundary (#1666).
    action, reason = _proposal(value)
    executed = action if mode == "enforce" else ContextAction.NONE
    return ContextDecision(action, executed, reason)


def _bypass(value: ContextPolicyInput, *, need_tokens: bool) -> str | None:
    """The reason this delivery is not a safe boundary, or ``None`` when it is."""
    bypasses = [
        (value.require_active or value.active_delivery, "active_delivery"),
        (value.pending_interaction, "pending_interaction"),
        (value.background_work, "background_work"),
        (value.fresh, "explicit_fresh"),
        (value.image_count > 0, "unsupported_payload"),
        (not value.runtime_supported, "unsupported_runtime"),
        (not value.lineage_supported, "unsupported_lineage"),
        (not value.context_current, "context_stale"),
    ]
    if need_tokens:
        bypasses.append((value.context_tokens is None, "context_unknown"))
    for blocked, reason in bypasses:
        if blocked:
            return reason
    return None


def _preference_proposal(value: ContextPolicyInput) -> tuple[ContextAction, str]:
    """Roll this chat over so its next leg starts on the model the operator chose.

    A token count is irrelevant here — the chat may be nearly empty — so this asks
    only whether the boundary is safe, which is the same question and the same
    answers the token path asks.
    """
    if not value.delivery_id:
        return ContextAction.NONE, "no_delivery_id"
    blocked = _bypass(value, need_tokens=False)
    if blocked is not None:
        return ContextAction.NONE, blocked
    if not value.rollover_supported:
        return ContextAction.NONE, "unsupported_runtime"
    return ContextAction.ROLLOVER, "model_preference"


def _proposal(value: ContextPolicyInput) -> tuple[ContextAction, str]:
    if not value.delivery_id:
        return ContextAction.NONE, "no_delivery_id"
    blocked = _bypass(value, need_tokens=True)
    if blocked is not None:
        return ContextAction.NONE, blocked

    tokens = value.context_tokens
    assert tokens is not None
    config = value.config
    cycle = value.previous_cycle
    previous_run = value.previous_run_id or (cycle.run_id if cycle is not None else None)
    if (
        value.run_id
        and previous_run is not None
        and previous_run != value.run_id
        and tokens >= config.new_generation_rollover_at_tokens
    ):
        if value.rollover_supported:
            return ContextAction.ROLLOVER, "new_generation"
        if value.compact_supported:
            return ContextAction.COMPACT, "new_generation"

    if tokens < config.compact_at_tokens:
        return ContextAction.NONE, "below_threshold"
    if not value.compact_supported:
        return (
            (ContextAction.ROLLOVER, "threshold")
            if value.rollover_supported
            else (ContextAction.NONE, "unsupported_runtime")
        )

    # An observation below the rearm threshold begins a new compact cycle.
    rearmed = cycle is None or cycle.rearmed or (
        cycle.after_tokens is not None and cycle.after_tokens < config.compact_rearm_below_tokens
    )
    if rearmed or cycle.action is not ContextAction.COMPACT:
        return ContextAction.COMPACT, "threshold"
    if cycle.result in {MaintenanceResult.FAILED, MaintenanceResult.UNKNOWN}:
        return ContextAction.ROLLOVER, "compact_failed"
    if tokens >= config.rollover_at_tokens:
        return ContextAction.ROLLOVER, "compact_insufficient"
    return ContextAction.NONE, "below_threshold"
