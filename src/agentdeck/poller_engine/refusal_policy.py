"""One policy for poller retry and durable-ledger refusal handling (#1909).

This module lives in the engine package so both the engine and the deck can consume it
without reversing their dependency direction. A refusal reason has two independent consequences:
what the engine should do with the dispatch and what the context-delivery ledger should do
if the same delivery id is presented again. Keeping those consequences together here
prevents the two callers from maintaining nearly-identical reason sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EngineRetryDisposition(StrEnum):
    """What the poller should do after a refused delivery."""

    SAME_DELIVERY = "same_delivery"
    NEXT_ATTEMPT = "next_attempt"
    TERMINAL = "terminal"


class LedgerDisposition(StrEnum):
    """What the durable delivery ledger should do with an existing refused row."""

    REDECIDE = "redecide"
    REPLAY = "replay"
    RESUME = "resume"


@dataclass(frozen=True)
class RefusalDisposition:
    engine: EngineRetryDisposition
    ledger: LedgerDisposition


_REDECIDED_SAME_DELIVERY = frozenset(
    {
        "active_turn",
        "at_capacity",
        "context_operation_pending",
        "host_shutting_down",
        "worker_changed",
        # The Runtime has not yet proved an older Run of this Work stopped (#2159). Nothing
        # was written; the same delivery goes again once it has.
        "prior_run_executing",
    }
)

# The provider explicitly fenced the original message as unstarted, but context maintenance
# still stands behind the refusal. The engine retries the same id while the ledger resumes
# that row instead of throwing it away and deciding from scratch (#1493).
_RESUMED_SAME_DELIVERY = "retryable_same_delivery"

# At-most-once makes this a permanent property of the delivery id (#1886).
_TERMINAL_SAME_DELIVERY = "delivery_id_conflict"
# The delivery id is already frozen under another Work or Run (#2142): just as permanent.
_TERMINAL_CORRELATION = "delivery_correlation_conflict"


def refusal_disposition(reason: str | None) -> RefusalDisposition:
    """Return both retry and ledger policy for one runtime refusal reason."""
    normalized = str(reason or "")
    if normalized in {_TERMINAL_SAME_DELIVERY, _TERMINAL_CORRELATION}:
        return RefusalDisposition(
            engine=EngineRetryDisposition.TERMINAL,
            ledger=LedgerDisposition.REPLAY,
        )
    if normalized == _RESUMED_SAME_DELIVERY:
        return RefusalDisposition(
            engine=EngineRetryDisposition.SAME_DELIVERY,
            ledger=LedgerDisposition.RESUME,
        )
    if normalized in _REDECIDED_SAME_DELIVERY:
        return RefusalDisposition(
            engine=EngineRetryDisposition.SAME_DELIVERY,
            ledger=LedgerDisposition.REDECIDE,
        )
    return RefusalDisposition(
        engine=EngineRetryDisposition.NEXT_ATTEMPT,
        ledger=LedgerDisposition.REPLAY,
    )


def redecided_refusal_reasons() -> frozenset[str]:
    """Compatibility projection for ledger callers while they migrate to the policy API."""
    return _REDECIDED_SAME_DELIVERY


def same_delivery_retry_reasons() -> frozenset[str]:
    """Compatibility projection for engine callers while they migrate to the policy API."""
    return _REDECIDED_SAME_DELIVERY | {_RESUMED_SAME_DELIVERY}


TERMINAL_SAME_DELIVERY_REFUSAL = _TERMINAL_SAME_DELIVERY
