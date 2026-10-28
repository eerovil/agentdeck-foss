from __future__ import annotations

from agentdeck.poller_engine.refusal_policy import redecided_refusal_reasons


def test_transient_same_delivery_refusals_are_redecided_by_the_ledger():
    reasons = redecided_refusal_reasons()
    assert reasons == {
        "active_turn",
        "at_capacity",
        "context_operation_pending",
        "host_shutting_down",
        "worker_changed",
    }
    # Fenced as unstarted but still behind context maintenance: resumed, never redecided.
    assert "retryable_same_delivery" not in reasons
    # Permanent properties of the delivery id are replayed.
    assert "delivery_id_conflict" not in reasons


def test_context_delivery_compatibility_projection_comes_from_shared_policy():
    from agentdeck.context_delivery import RE_DECIDED_REFUSALS

    assert RE_DECIDED_REFUSALS == redecided_refusal_reasons()
