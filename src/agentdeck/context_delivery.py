"""Typed durable record for one context-managed keyed delivery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .poller_engine.refusal_policy import redecided_refusal_reasons

# The refusals whose ledger row is decided again rather than replayed (``refusal_policy``).
RE_DECIDED_REFUSALS = redecided_refusal_reasons()


@dataclass(frozen=True)
class ContextDelivery:
    action_id: str
    attempt: int
    provider_id: str
    account_key: str
    worker_key: str
    delivery_id: str
    request_sha256: str
    chat_id: str
    source_session_key: str
    source_session_id: str
    mode: str
    proposed_action: str
    executed_action: str
    reason: str
    status: str = "reserved"
    target_session_key: str | None = None
    runtime_capability_version: str | None = None
    runtime_fingerprint: str | None = None
    delivery_stage: str = "prepared_before_write"
    provider_turn_id: str | None = None
    pre_write_cursor: str | None = None
    post_write_cursor: str | None = None
    evidence_checked: str | None = None
    uncertainty_reason: str | None = None
    resolution_choice: str | None = None
    resolution_rationale: str | None = None
    resolved_at: str | None = None
    source_last_event_seq: int | None = None
    expected_session_id: str | None = None
    expected_last_result_at: float | None = None
    before_tokens: int | None = None
    after_tokens: int | None = None
    maintenance_completed_at: str | None = None
    after_observed_at: str | None = None
    after_observation_version: int | None = None
    rearmed: bool = False
    handoff_text: str | None = None
    handoff_sha256: str | None = None
    handoff_chars: int = 0
    effective_cwd: str | None = None
    effective_model: str | None = None
    effective_effort: str | None = None
    effective_permission_mode: str | None = None
    effective_role: str | None = None
    effective_delegated: bool = False
    preferred_model: str | None = None
    session_token: str | None = None
    final_payload_sha256: str | None = None
    maintenance_result: str | None = None
    provider_write_state: str = "not_started"
    successor_created: bool = False
    retryable_same_delivery: bool = False
    final_accepted: bool | None = None
    final_action: str | None = None
    final_reason: str | None = None
    final_session_id: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    # The Work and Run the delivery belongs to (#2142). Deliberately outside the request hash
    # (``context_request_sha256``), so a retry reserved before these existed still matches.
    work_id: str | None = None
    run_id: str | None = None


def context_request_sha256(
    *,
    provider_id: str,
    account_key: str,
    worker_key: str,
    message: str,
    cwd: str | None,
    fresh: bool,
    image_count: int,
    model: str | None,
    effort: str | None,
    permission_mode: str | None,
    allow_over_budget: bool,
    require_idle: bool,
    require_active: bool,
    expected_last_result_at: float | None,
    role: str | None,
    delegated: bool,
    caller_session_token: str | None,
    continues_session_url: str | None,
    previous_worker_summary: str | None,
    context_policy: str,
    command_artifacts: dict | None = None,
) -> str:
    """Hash the caller's original semantic request, excluding temporary paths."""
    summary_hash = (
        hashlib.sha256(previous_worker_summary.encode()).hexdigest()
        if previous_worker_summary is not None
        else None
    )
    payload = {
        "provider_id": provider_id,
        "account_key": account_key,
        "worker_key": worker_key,
        "message": message,
        "cwd": cwd,
        "fresh": fresh,
        "image_count": image_count,
        "model": model,
        "effort": effort,
        "permission_mode": permission_mode,
        "allow_over_budget": allow_over_budget,
        "require_idle": require_idle,
        "require_active": require_active,
        "expected_last_result_at": expected_last_result_at,
        "role": role,
        "delegated": delegated,
        "caller_session_token": caller_session_token,
        "continues_session_url": continues_session_url,
        # Retired in #2194 but kept, always null, so a row written before then still hashes
        # the same and its delivery id replays instead of refusing as a conflict.
        "dispatch_generation": None,
        "phase_delivery_id": None,
        "previous_worker_summary_sha256": summary_hash,
        "context_policy": context_policy,
        "command_artifacts": command_artifacts,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
