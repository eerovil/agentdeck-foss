"""Which runtime refusals the context-delivery ledger decides again (#1909).

A refused delivery left a row in the web's context-delivery ledger. For the reasons below
nothing was written and the refusal is transient, so when the same delivery id is presented
again the ledger throws that row away and decides from scratch rather than replaying the
refusal. The engine's same-delivery retry set that once lived beside it went with the card
dispatcher (#3741).
"""

from __future__ import annotations

_REDECIDED_SAME_DELIVERY = frozenset(
    {
        "active_turn",
        "at_capacity",
        "context_operation_pending",
        "host_shutting_down",
        "worker_changed",
    }
)


def redecided_refusal_reasons() -> frozenset[str]:
    """The refusal reasons whose ledger row is decided again on a repeat."""
    return _REDECIDED_SAME_DELIVERY
