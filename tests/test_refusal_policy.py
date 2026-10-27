from __future__ import annotations

from agentdeck.poller_engine.refusal_policy import (
    EngineRetryDisposition,
    LedgerDisposition,
    redecided_refusal_reasons,
    refusal_disposition,
    same_delivery_retry_reasons,
)


def test_transient_same_delivery_refusals_are_redecided_by_the_ledger():
    reasons = redecided_refusal_reasons()
    assert reasons == {
        "active_turn",
        "at_capacity",
        "context_operation_pending",
        "host_shutting_down",
        "worker_changed",
        # An older Run of the same Work may still be writing its workspace (#2159).
        "prior_run_executing",
    }
    for reason in reasons:
        policy = refusal_disposition(reason)
        assert policy.engine is EngineRetryDisposition.SAME_DELIVERY
        assert policy.ledger is LedgerDisposition.REDECIDE


def test_retryable_same_delivery_resumes_instead_of_redeciding():
    policy = refusal_disposition("retryable_same_delivery")
    assert policy.engine is EngineRetryDisposition.SAME_DELIVERY
    assert policy.ledger is LedgerDisposition.RESUME
    assert "retryable_same_delivery" in same_delivery_retry_reasons()
    assert "retryable_same_delivery" not in redecided_refusal_reasons()


def test_delivery_id_conflict_is_terminal_for_the_dispatch():
    policy = refusal_disposition("delivery_id_conflict")
    assert policy.engine is EngineRetryDisposition.TERMINAL
    assert policy.ledger is LedgerDisposition.REPLAY


def test_unknown_refusal_uses_the_existing_next_attempt_and_replay_defaults():
    policy = refusal_disposition("some_future_refusal")
    assert policy.engine is EngineRetryDisposition.NEXT_ATTEMPT
    assert policy.ledger is LedgerDisposition.REPLAY


def test_context_delivery_compatibility_projection_comes_from_shared_policy():
    from agentdeck.context_delivery import RE_DECIDED_REFUSALS

    assert RE_DECIDED_REFUSALS == redecided_refusal_reasons()
