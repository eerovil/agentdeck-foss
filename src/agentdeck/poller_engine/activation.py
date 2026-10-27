"""Activation: one validated configuration apply (#2157, replacing the staged machine of #180).

Turning a project on is the moment a poller starts writing to other people's repositories, but
that makes it a *configuration apply*, not a release. It is one operation:

    inspect → resolve → authorize → provision → publish the binding → write the audit record

* **The apply request is the approval.** The operator asking to apply the current config is the
  one gate, and it is checked against Release authority (#110) at apply time. Nothing persists a
  preview for a later command to prove it saw: apply re-inspects and re-resolves the config as
  it is *now*, so an earlier dry run can never be stale.
* **Provisioning only adds what is missing** — labels and the instance's directories — and every
  step is idempotent, so a retry simply runs them again. A step that fails stops the apply
  *before* anything is published, which leaves the previously active binding selected.
* **Publishing is one atomic commit** (`config.publish`). There is no canary wake,
  no health confirmation and no rollback workflow: the next normal wake is the first real use,
  and `config.load` fails closed on drift, so an instance that is wrong says so in its own health.

The record built here is audit only, and `config.publish` commits it together with the Manifest
and binding under one lock, so it always describes the pair it sits beside. What a wake runs,
and what drift is judged against, is the binding file; nothing reads this record to decide
anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class ProvisionStep:
    """One add-only, idempotent setup step: it creates what is missing and nothing else."""

    name: str
    apply: Callable[[], bool]
    detail: str = ""


def provision(steps: list[ProvisionStep]) -> list[ProvisionStep]:
    """Run every step and return the ones that did not land.

    Every step runs even after one fails, so a single retry finishes as much as it can; a step
    that raises is a failure with its reason in ``detail``, not a crash.
    """
    failed: list[ProvisionStep] = []
    for step in steps:
        try:
            landed = bool(step.apply())
        except Exception as exc:  # a step's own failure is evidence, not a crash
            landed = False
            step.detail = f"{exc!r}"
        if not landed:
            failed.append(step)
    return failed


def audit_record(
    *,
    instance: str,
    contract: int,
    fingerprint: dict,
    activated_by: str,
    now: datetime,
    previous: dict | None = None,
) -> dict:
    """Who applied which configuration, and when. One level of ``previous``, never a history."""
    record = {
        "instance": instance,
        "contract": contract,
        "activated_at": now.astimezone(UTC).isoformat(),
        "activated_by": activated_by,
        "config_fingerprint": fingerprint.get("manifest", ""),
        "binding_fingerprint": fingerprint.get("binding", ""),
        "fingerprint": fingerprint.get("digest", ""),
    }
    if previous:
        record["previous"] = {key: value for key, value in audit_view(previous).items() if value}
    return record


def audit_view(record: dict | None) -> dict:
    """The audit fields of an activation record, empty where the record does not say."""
    record = record if isinstance(record, dict) else {}
    digest = record.get("fingerprint")
    return {
        "instance": record.get("instance", ""),
        "contract": record.get("contract"),
        "activated_at": record.get("activated_at") or "",
        "activated_by": record.get("activated_by") or "",
        "config_fingerprint": record.get("config_fingerprint") or "",
        "binding_fingerprint": record.get("binding_fingerprint") or "",
        "fingerprint": digest if isinstance(digest, str) else "",
    }
