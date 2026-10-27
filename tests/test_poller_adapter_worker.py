"""Slice 4 of #180: worker delivery and the receipts that make it at-most-once.

Every delivery goes to the persistent runtime's ``ensure_delivery`` (#2144), which chooses the
account (#2146). ``curl`` is a script each test controls, answering in that contract's shape.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentdeck.poller_adapter.worker import Delivery, DeliveryOutcome, WorkerDispatcher
from agentdeck.poller_engine.github import receipt_path

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
_DIRECTIVE = {
    "action": "new",
    "repo": "store",
    "number": 5,
    "ref": "store#5",
    "generation": "gen1",
    "run_id": "gen1",
    "work_id": "github_issue:org/store#5",
    "dispatch_id": "d1",
    "messages": [{"payload": "fix it", "author": "eero"}],
}


def _answer(tmp_path, body: dict | str, status: int = 200) -> str:
    """A script that answers like the runtime: the body, then curl's ``--write-out`` status."""
    text = body if isinstance(body, str) else json.dumps(body)
    answer = tmp_path / f"answer-{len(list(tmp_path.glob('answer-*')))}"
    answer.write_text(f"{text}\n{status}")
    return f"cat {answer}\n"


def _accepts(tmp_path, account: str = "claude_code:main", **fields) -> str:
    return _answer(
        tmp_path,
        {
            "contract": "ensure-delivery-v1",
            "accepted": True,
            "action": "spawned",
            "schedule": {"decision": "selected", "account": account},
            **fields,
        },
    )


def _recording(tmp_path, seen: Path, account: str = "claude_code:main") -> str:
    """Accept, and write every argument down so a test can read the request that was sent."""
    return f'printf "%s\\n" "$@" > {seen}\n' + _accepts(tmp_path, account)


def _sent(seen: Path) -> dict:
    return json.loads(next(line for line in seen.read_text().splitlines() if "contract" in line))


def dispatcher(tmp_path, script: str) -> WorkerDispatcher:
    """A dispatcher whose `curl` is a script we control, so outcomes can be exercised exactly."""
    fake = tmp_path / "curl"
    fake.write_text("#!/bin/sh\n" + script)
    fake.chmod(0o755)
    return WorkerDispatcher(
        receipts_dir=tmp_path / "dispatch-acks",
        curl=str(fake),
        runtime_socket=tmp_path / "rt.sock",
        candidates=("claude_code:main",),
    )


def _receipt(tmp_path, dispatch_id="d1") -> Path:
    return receipt_path(tmp_path / "dispatch-acks", "store", 5, dispatch_id)


def _deferral(tmp_path, dispatch_id="d1") -> Path:
    return receipt_path(tmp_path / "dispatch-deferrals", "store", 5, dispatch_id)


def test_an_accepted_delivery_writes_its_receipt(tmp_path):
    subject = dispatcher(tmp_path, _accepts(tmp_path))
    assert subject.deliver(_DIRECTIVE, now=_NOW).outcome is DeliveryOutcome.DELIVERED
    # The receipt is what tells the next wake this exact dispatch landed.
    assert _receipt(tmp_path).exists()
    assert _NOW.isoformat() in _receipt(tmp_path).read_text()


def test_the_receipt_is_keyed_to_the_dispatch_it_confirms(tmp_path):
    subject = dispatcher(tmp_path, _accepts(tmp_path))
    subject.deliver(_DIRECTIVE, now=_NOW)
    assert _receipt(tmp_path, "d1").exists()
    # A different dispatch has a different receipt: an acknowledgement can only confirm its own.
    assert not _receipt(tmp_path, "d2").exists()


def test_a_positively_refused_delivery_writes_no_receipt(tmp_path):
    subject = dispatcher(tmp_path, _answer(tmp_path, {"detail": "unprocessable"}, 422))
    assert subject.deliver(_DIRECTIVE, now=_NOW).outcome is DeliveryOutcome.REFUSED
    assert not _receipt(tmp_path).exists()


def test_an_uncertain_delivery_writes_no_receipt_and_is_not_retried_here(tmp_path):
    subject = dispatcher(tmp_path, 'echo "operation timed out" >&2\nexit 28\n')
    outcome = subject.deliver(_DIRECTIVE, now=_NOW).outcome
    # Retrying here is the one way to get two workers on one card, so this module never does.
    assert outcome is DeliveryOutcome.UNCERTAIN
    assert not _receipt(tmp_path).exists()


def test_a_missing_curl_is_uncertain_rather_than_a_crash(tmp_path):
    subject = WorkerDispatcher(
        receipts_dir=tmp_path / "dispatch-acks",
        curl=str(tmp_path / "absent"),
        runtime_socket=tmp_path / "rt.sock",
    )
    assert subject.deliver(_DIRECTIVE, now=_NOW).outcome is DeliveryOutcome.UNCERTAIN


def test_a_host_with_no_runtime_socket_refuses_rather_than_using_the_web(tmp_path):
    """#2150: the web delivery path is gone, so there is nowhere else to send it."""
    called = tmp_path / "called"
    fake = tmp_path / "curl"
    fake.write_text(f"#!/bin/sh\ntouch {called}\n")
    fake.chmod(0o755)
    subject = WorkerDispatcher(receipts_dir=tmp_path / "dispatch-acks", curl=str(fake))

    delivery = subject.deliver(_DIRECTIVE, now=_NOW)

    assert delivery == Delivery(DeliveryOutcome.REFUSED, "runtime_unavailable")
    assert not called.exists()
    assert json.loads(_deferral(tmp_path).read_text())["reason"] == "runtime_unavailable"


def test_the_delivery_carries_the_engines_identity_and_the_batch(tmp_path):
    seen = tmp_path / "seen.json"
    # Record every argument, so the assertion does not depend on curl's flag order.
    subject = dispatcher(tmp_path, _recording(tmp_path, seen))
    subject.deliver(_DIRECTIVE, now=_NOW)
    body = _sent(seen)
    # AgentDeck's delivery id is derived from the engine's, so a retried logical delivery is
    # recognizable on both sides without either owning the other's identity.
    assert body["delivery_id"] == "poller:d1"
    assert (body["work_id"], body["run_id"]) == ("github_issue:org/store#5", "gen1")
    # One worker per card: the ref is the dedupe key, and the instructions are the message.
    assert body["worker_key"] == "store#5"
    assert body["role"] == "kanban"
    # The runtime chooses the account (#2146); the host names none, and sends no duplicate
    # transport beside the Run.
    assert body["requirements"]["candidates"] == ["claude_code:main"]
    assert not {"fixed_target", "dispatch_generation", "delegated"} & set(body)
    assert "fix it" in body["message"]
    assert "/deliveries/ensure" in seen.read_text()


def test_a_personal_start_carries_its_budget_override_to_the_deck(tmp_path):
    seen = tmp_path / "seen.json"
    subject = dispatcher(tmp_path, _recording(tmp_path, seen))
    directive = {**_DIRECTIVE, "allow_over_budget": True}

    subject.deliver(directive, now=_NOW)

    assert _sent(seen)["allow_over_budget"] is True


def test_a_continuation_is_refused_if_the_lineage_became_active_again(tmp_path):
    """#835: the turn was over when the engine revived it, but delivery happens later.
    Require an idle lineage so activity in that gap cannot turn the continuation into a steer
    or queued duplicate."""
    seen = tmp_path / "seen.json"
    subject = dispatcher(tmp_path, _recording(tmp_path, seen))

    subject.deliver({**_DIRECTIVE, "continuation": True}, now=_NOW)

    assert _sent(seen)["require_idle"] is True


def test_a_continuation_names_the_chat_it_is_taking_over(tmp_path):
    """#1267: the follow-up account is chosen on headroom, so it often differs.

    The brief already points the worker at the previous conversation. Sending
    the deck the same URL is what makes the session it starts the next *leg* of
    that chat instead of an unrelated chat beside it.
    """
    seen = tmp_path / "seen.json"
    subject = dispatcher(tmp_path, _recording(tmp_path, seen))

    subject.deliver(
        {
            **_DIRECTIVE,
            "continuation": True,
            "previous_session_url": "https://deck.example/chats/abc123",
        },
        now=_NOW,
    )

    assert _sent(seen)["continues_session_url"] == "https://deck.example/chats/abc123"


def test_a_dispatch_names_its_card_so_the_session_keeps_it(tmp_path):
    """#2127: the deck registers this against the session that takes the delivery,
    so "which chats worked this card" survives the chat grouping failing."""
    seen = tmp_path / "seen.json"
    subject = dispatcher(tmp_path, _recording(tmp_path, seen))

    subject.deliver(
        {
            **_DIRECTIVE,
            "ref": "eerovil/agentdeck#2127",
            "work_id": "github_issue:eerovil/agentdeck#2127",
        },
        now=_NOW,
    )

    assert _sent(seen)["issue_ref"] == "eerovil/agentdeck#2127"


def test_a_short_card_reference_names_no_card_at_all(tmp_path):
    """`store#5` names no owner, and two configured repositories can share a short
    name — so registering one would be a guess at which repository was meant."""
    seen = tmp_path / "seen.json"
    subject = dispatcher(tmp_path, _recording(tmp_path, seen))

    subject.deliver(_DIRECTIVE, now=_NOW)

    assert _sent(seen)["issue_ref"] is None


def test_an_ordinary_dispatch_continues_nothing(tmp_path):
    """A first dispatch has no predecessor, and must not claim one."""
    seen = tmp_path / "seen.json"
    subject = dispatcher(tmp_path, _recording(tmp_path, seen))

    subject.deliver(_DIRECTIVE, now=_NOW)

    assert _sent(seen)["continues_session_url"] is None


def test_command_artifact_policy_crosses_the_worker_delivery_boundary(tmp_path):
    seen = tmp_path / "seen.json"
    subject = dispatcher(tmp_path, _recording(tmp_path, seen))

    subject.deliver(
        {**_DIRECTIVE, "command_artifacts": {"mode": "observe", "card": "store#5"}},
        now=_NOW,
    )

    assert _sent(seen)["command_artifacts"] == {"mode": "observe", "card": "store#5"}


def test_a_steer_requires_the_live_turn_and_records_a_clean_refusal(tmp_path):
    seen = tmp_path / "seen.json"
    subject = dispatcher(
        tmp_path, f'printf "%s\\n" "$@" > {seen}\n' + _rejects(tmp_path, "worker_inactive")
    )

    delivery = subject.deliver({**_DIRECTIVE, "steer": True}, now=_NOW)

    assert delivery.outcome is DeliveryOutcome.REFUSED
    assert _sent(seen)["require_active"] is True
    assert not _receipt(tmp_path).exists()
    assert json.loads(_deferral(tmp_path).read_text())["reason"] == "worker_inactive"


def test_a_generation_with_no_recorded_account_is_refused_without_a_runtime(tmp_path):
    called = tmp_path / "called"
    subject = dispatcher(tmp_path, f"touch {called}\n" + _accepts(tmp_path))

    delivery = subject.refuse({**_DIRECTIVE, "steer": True}, "worker_inactive", now=_NOW)

    assert delivery == Delivery(DeliveryOutcome.REFUSED, "worker_inactive")
    assert not called.exists()
    assert json.loads(_deferral(tmp_path).read_text())["reason"] == "worker_inactive"


def _keyed_runtime(tmp_path) -> WorkerDispatcher:
    """A dispatcher whose `curl` behaves like the runtime's at-most-once ``ensure_delivery``.

    It replays a finalized receipt for a delivery id it has already accepted — the real
    runtime's `DeliveryReceipts.lookup`, which runs *before* the `require_active` check — and
    otherwise refuses an active-only send once the card's turn is over. The very first
    accepted send answers with a lost connection, which is the case this exercises. Which
    account holds the Run is the runtime's own frozen route (#2146), so the host names none.
    """
    script = tmp_path / "runtime-curl"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "state = pathlib.Path(sys.argv[0]).parent\n"
        "payload = json.loads(sys.argv[sys.argv.index('--data-binary') + 1])\n"
        "def answer(body):\n"
        "    body = {'contract': 'ensure-delivery-v1', **body}\n"
        "    sys.stdout.write(json.dumps(body) + '\\n200')\n"
        "    sys.exit(0)\n"
        "store_path = state / 'runtime-receipts.json'\n"
        "store = json.loads(store_path.read_text()) if store_path.exists() else {}\n"
        "sent = state / 'delivery-ids'\n"
        "with sent.open('a') as handle:\n"
        "    handle.write(str(payload.get('delivery_id')) + '\\n')\n"
        "if payload.get('delivery_id') in store:\n"
        "    answer({'accepted': True, 'action': 'steered',"
        " 'schedule': {'account': 'claude_code:main'}})\n"
        "if payload.get('require_active') and (state / 'turn-over').exists():\n"
        "    answer({'accepted': False, 'reason': 'worker_inactive'})\n"
        "store[payload.get('delivery_id')] = 'steered'\n"
        "store_path.write_text(json.dumps(store))\n"
        "sys.exit(28)\n"  # accepted and written; the answer never came back
    )
    script.chmod(0o755)
    return WorkerDispatcher(
        receipts_dir=tmp_path / "dispatch-acks",
        curl=str(script),
        runtime_socket=tmp_path / "rt.sock",
        candidates=("claude_code:main",),
    )


def test_a_lost_steer_is_deduplicated_after_its_turn_ended(tmp_path):
    """#1259: an accepted steer whose answer was lost must never become a second send.

    The runtime deduplicates by delivery id, and it holds the route that says which account
    took the steer (#2146), so nothing the host once kept about the turn is needed. If the
    retry had to name the account itself, it could only guess — and the still-pending
    instruction would be sent again later under a fresh delivery id, repeating an instruction
    the worker may already have acted on.
    """
    subject = _keyed_runtime(tmp_path)
    steer = {**_DIRECTIVE, "steer": True}

    lost = subject.deliver(steer, now=_NOW)
    assert lost.outcome is DeliveryOutcome.UNCERTAIN
    assert not _receipt(tmp_path).exists(), "an uncertain send acknowledges nothing"

    (tmp_path / "turn-over").touch()

    replay = subject.deliver(steer, now=_NOW)

    # The runtime's own receipt answers before `require_active` does, so the outcome is
    # established: this instruction landed, and it is acknowledged rather than re-queued.
    assert replay.outcome is DeliveryOutcome.DELIVERED
    assert _receipt(tmp_path).exists()
    assert not _deferral(tmp_path).exists()
    assert (tmp_path / "delivery-ids").read_text().split() == ["poller:d1", "poller:d1"], (
        "the same stable delivery id, never a fresh one"
    )


def test_a_steer_that_never_landed_is_released_only_on_the_runtime_saying_so(tmp_path):
    """The other half of #1259: the control goes back to the queue on proof, not on silence."""
    subject = _keyed_runtime(tmp_path)
    (tmp_path / "turn-over").touch()

    delivery = subject.deliver({**_DIRECTIVE, "steer": True}, now=_NOW)

    assert delivery == Delivery(DeliveryOutcome.REFUSED, "worker_inactive")
    assert not _receipt(tmp_path).exists()
    assert json.loads(_deferral(tmp_path).read_text())["reason"] == "worker_inactive"


def test_an_ordinary_dispatch_can_still_steer_its_existing_worker(tmp_path):
    seen = tmp_path / "seen.json"
    subject = dispatcher(tmp_path, _recording(tmp_path, seen))

    subject.deliver(_DIRECTIVE, now=_NOW)

    assert _sent(seen)["require_idle"] is False
    assert _sent(seen)["fresh"] is False


def test_a_merge_review_replaces_the_implementation_session_with_a_fresh_one(tmp_path):
    seen = tmp_path / "seen.json"
    subject = dispatcher(tmp_path, _recording(tmp_path, seen))

    subject.deliver({**_DIRECTIVE, "action": "merge-review"}, now=_NOW)

    body = _sent(seen)
    assert body["worker_key"] == "store#5"
    assert body["fresh"] is True
    assert body["require_idle"] is True


def test_a_merge_review_defers_if_the_card_worker_became_active(tmp_path):
    subject = dispatcher(tmp_path, _rejects(tmp_path, "active_turn"))
    directive = {**_DIRECTIVE, "action": "merge-review"}

    delivery = subject.deliver(directive, now=_NOW)

    assert delivery == Delivery(DeliveryOutcome.REFUSED, "active_turn")
    assert json.loads(_deferral(tmp_path).read_text())["reason"] == "active_turn"
    assert not _receipt(tmp_path).exists()


def test_a_delivery_leaves_no_turn_watch_behind(tmp_path):
    """#2200: how the Run ends, and whether its account could run it, is the Runtime's to say.
    The engine asks the Runtime; nothing on this host is written for it to read."""
    subject = dispatcher(tmp_path, _accepts(tmp_path, "claude_code:alt"))

    assert subject.deliver(_DIRECTIVE, now=_NOW).outcome is DeliveryOutcome.DELIVERED

    assert not (tmp_path / "turn-watch").exists()
    assert not (tmp_path / "turn-ends").exists()


def _rejects(tmp_path, reason: str) -> str:
    """The runtime's own answer to a refused delivery: HTTP 200, `accepted: false` in the body."""
    return _answer(
        tmp_path,
        {
            "contract": "ensure-delivery-v1",
            "accepted": False,
            "action": "rejected",
            "reason": reason,
        },
    )


@pytest.mark.parametrize(
    "reason",
    [
        "at_capacity",
        "over_budget",
        "session_over_budget",
        "cwd_required",
        "delivery_id_conflict",
        "codex_create_failed",
        "codex_start_failed",
        "active_turn",
        "worker_inactive",
        "worker_not_steerable",
        "context_operation_pending",
    ],
)
def test_a_rejection_answered_with_http_200_is_a_refusal_not_a_delivery(tmp_path, reason):
    subject = dispatcher(tmp_path, _rejects(tmp_path, reason))
    # These are decided before anything is written, so no worker exists to record.
    assert subject.deliver(_DIRECTIVE, now=_NOW).outcome is DeliveryOutcome.REFUSED
    assert not _receipt(tmp_path).exists()


def test_at_capacity_records_a_durable_deferral_for_the_engine(tmp_path):
    subject = dispatcher(tmp_path, _rejects(tmp_path, "at_capacity"))

    delivery = subject.deliver(_DIRECTIVE, now=_NOW)

    assert delivery.outcome is DeliveryOutcome.REFUSED
    assert json.loads(_deferral(tmp_path).read_text())["reason"] == "at_capacity"


def test_retry_consumes_the_old_capacity_deferral_before_delivery(tmp_path):
    subject = dispatcher(tmp_path, _accepts(tmp_path))
    _deferral(tmp_path).parent.mkdir(parents=True)
    _deferral(tmp_path).write_text("stale capacity refusal")

    delivery = subject.deliver(_DIRECTIVE, now=_NOW)

    assert delivery.outcome is DeliveryOutcome.DELIVERED
    assert not _deferral(tmp_path).exists()
    assert _receipt(tmp_path).exists()


def test_retry_does_not_deliver_when_the_capacity_deferral_cannot_be_consumed(
    tmp_path, monkeypatch
):
    seen = tmp_path / "sent"
    subject = dispatcher(tmp_path, f"touch {seen}\n" + _accepts(tmp_path))
    _deferral(tmp_path).parent.mkdir(parents=True)
    _deferral(tmp_path).write_text("stale capacity refusal")
    monkeypatch.setattr("agentdeck.poller_adapter.worker.clear_receipt", lambda *_args: False)

    delivery = subject.deliver(_DIRECTIVE, now=_NOW)

    assert delivery == Delivery(DeliveryOutcome.REFUSED, "deferral_cleanup_failed")
    assert not seen.exists()
    assert _deferral(tmp_path).exists()
    assert not _receipt(tmp_path).exists()


def test_every_clean_refusal_leaves_proof_it_wrote_nothing(tmp_path):
    """#1491: the receipt is the engine's only durable evidence of a pre-write refusal.

    It is written for every clean refusal, not just the four the engine retries under the same
    delivery id. The engine still acts on only those four — this receipt does not make
    `over_budget` masquerade as capacity backpressure — but a card parked after three positive
    refusals can be re-armed safely, and one parked with no answer at all cannot, and this is
    what tells them apart.
    """
    subject = dispatcher(tmp_path, _rejects(tmp_path, "over_budget"))

    delivery = subject.deliver(_DIRECTIVE, now=_NOW)

    assert delivery == Delivery(DeliveryOutcome.REFUSED, "over_budget")
    assert json.loads(_deferral(tmp_path).read_text())["reason"] == "over_budget"
    assert not _receipt(tmp_path).exists()


def test_an_uncertain_send_leaves_no_proof(tmp_path):
    """The other half: nothing on disk means nothing is known, and nothing may be re-armed."""
    subject = dispatcher(tmp_path, _rejects(tmp_path, "worker write failed"))

    assert subject.deliver(_DIRECTIVE, now=_NOW).outcome is DeliveryOutcome.UNCERTAIN
    assert not _deferral(tmp_path).exists()
    assert not _receipt(tmp_path).exists()


def test_an_unrecognized_rejection_stays_uncertain_rather_than_becoming_a_refusal(tmp_path):
    # The write-failure path reports `accepted: false` *after* the message may already have
    # reached the child, so retrying it under a fresh generation could run the task twice.
    subject = dispatcher(tmp_path, _rejects(tmp_path, "worker write failed"))
    assert subject.deliver(_DIRECTIVE, now=_NOW).outcome is DeliveryOutcome.UNCERTAIN
    assert not _receipt(tmp_path).exists()


def test_retryable_context_result_retries_only_the_same_delivery(tmp_path):
    body = json.dumps(
        {
            "contract": "ensure-delivery-v1",
            "accepted": False,
            "action": "rejected",
            "reason": "context_maintenance_failed",
            "provider_write_state": "not_started",
            "retryable_same_delivery": True,
        }
    )
    subject = dispatcher(tmp_path, _answer(tmp_path, body))

    delivery = subject.deliver(_DIRECTIVE, now=_NOW)

    assert delivery == Delivery(DeliveryOutcome.SAFE_RETRY, "retryable_same_delivery")
    assert json.loads(_deferral(tmp_path).read_text())["reason"] == "retryable_same_delivery"
    assert not _receipt(tmp_path).exists()
