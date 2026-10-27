"""One poller delivery, followed from its Work to the session that took it (#2142).

``work_id`` is the source-derived id of #2140 and ``run_id`` is the poller generation. Neither
is minted, inferred or looked up anywhere: the engine records the Run on its durable dispatch,
the host derives the Work with the canonical function, the deck and the runtime freeze both on
the delivery's receipts before any provider write, and the session that takes the delivery is
bound to them. None of it changes what is delivered, to whom, or whether.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from worker_harness import HARNESSES, settle

from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, InjectConfig
from agentdeck.db import Db
from agentdeck.models import SNAPSHOT_RUN_CORRELATION, IssueRef
from agentdeck.poller_adapter.worker import (
    DeliveryOutcome,
    WorkerDispatcher,
    delivery_correlation,
)
from agentdeck.poller_engine import dispatch
from agentdeck.poller_engine.github import receipt_path
from agentdeck.poller_engine.refusal_policy import (
    EngineRetryDisposition,
    refusal_disposition,
)
from agentdeck.providers.delivery import (
    DeliverResult,
    DeliveryCorrelation,
    DeliveryReceipts,
    delivery_participated,
)
from agentdeck.runtime import create_runtime_app
from agentdeck.state import AppState
from agentdeck.web.routes_api import WorkerDeliverRequest
from agentdeck.work import work_id_for

pytestmark = pytest.mark.anyio

WORK = "github_issue:eerovil/agentdeck#2142"
KEY = "eerovil/agentdeck#2142"
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

adapters = pytest.mark.parametrize(
    "build_harness", [h for _, h in HARNESSES], ids=[name for name, _ in HARNESSES]
)


# --- the engine: the Run is the generation ----------------------------------


def test_the_durable_dispatch_names_its_run_and_it_is_the_generation():
    rec: dict = {}
    attempt = dispatch.new_dispatch(rec, "new")

    directive = dispatch.build_directive(
        rec, attempt, repo="agentdeck", number=2142, ref="agentdeck#2142"
    )

    assert attempt["run_id"] == attempt["generation"]
    assert directive["run_id"] == directive["generation"] == attempt["generation"]


def test_a_retry_of_one_dispatch_stays_in_its_run():
    """The runtime freezes a delivery id's Run, so a retry must present the same one (#2824)."""
    rec: dict = {}
    attempt = dispatch.new_dispatch(rec, "new")
    dispatch.stamp_work(attempt, owner="eerovil", repo="agentdeck", number=1)
    first = dispatch.build_directive(rec, attempt, repo="agentdeck", number=1, ref="agentdeck#1")

    dispatch.next_attempt(rec, attempt)
    second = dispatch.build_directive(rec, attempt, repo="agentdeck", number=1, ref="agentdeck#1")

    assert second["run_id"] == first["run_id"] == second["generation"]
    assert first["dispatch_id"] == second["dispatch_id"]
    ref = "eerovil/agentdeck#1"
    assert delivery_correlation(first, ref)["work_id"] == delivery_correlation(second, ref)[
        "work_id"
    ]


def test_the_engine_names_a_card_by_its_lowercased_issue():
    assert dispatch.work_id(" Eerovil", "AgentDeck ", 2142) == "github_issue:eerovil/agentdeck#2142"


def test_the_durable_dispatch_names_its_work_and_every_run_keeps_it():
    rec: dict = {}
    attempt = dispatch.new_dispatch(rec, "new")
    dispatch.stamp_work(attempt, owner="Eerovil", repo="agentdeck", number=2142)
    first = dispatch.build_directive(rec, attempt, repo="agentdeck", number=2142, ref="x")

    dispatch.next_attempt(rec, attempt)
    second = dispatch.build_directive(rec, attempt, repo="agentdeck", number=2142, ref="x")
    attempt = dispatch.new_dispatch(rec, "work")
    dispatch.stamp_work(attempt, owner="Eerovil", repo="agentdeck", number=2142)
    third = dispatch.build_directive(rec, attempt, repo="agentdeck", number=2142, ref="x")

    assert first["work_id"] == second["work_id"] == third["work_id"] == WORK
    assert first["run_id"] != third["run_id"]


def test_a_project_with_no_owner_names_no_work():
    rec: dict = {}
    attempt = dispatch.new_dispatch(rec, "new")

    dispatch.stamp_work(attempt, owner="", repo="agentdeck", number=1)

    assert "work_id" not in dispatch.build_directive(rec, attempt, repo="a", number=1, ref="a#1")


def test_a_dispatch_with_no_recorded_run_is_never_given_one():
    """The Run is read from the durable dispatch, not derived from its generation (#2150)."""
    rec: dict = {}
    attempt = dispatch.new_dispatch(rec, "new")
    attempt.pop("run_id")

    directive = dispatch.build_directive(rec, attempt, repo="a", number=1, ref="a#1")

    assert directive["run_id"] == ""


def test_a_steer_stays_in_the_run_it_joins():
    rec: dict = {}
    first = dispatch.new_dispatch(rec, "new")

    steer = dispatch.new_steer(rec, ["c1"])

    assert steer["run_id"] == first["run_id"]


def test_a_correlation_conflict_is_as_permanent_as_a_delivery_id_conflict():
    disposition = refusal_disposition("delivery_correlation_conflict")

    assert disposition.engine is EngineRetryDisposition.TERMINAL


# --- the host adapter: the canonical Work, carried and receipted -------------


def _dispatcher(tmp_path, script: str) -> WorkerDispatcher:
    fake = tmp_path / "curl"
    fake.write_text("#!/bin/sh\n" + script)
    fake.chmod(0o755)
    return WorkerDispatcher(
        receipts_dir=tmp_path / "dispatch-acks",
        curl=str(fake),
        runtime_socket=tmp_path / "rt.sock",
        candidates=("claude_code:main",),
    )


def _accepting(tmp_path, seen=None) -> str:
    """A runtime that accepts, optionally writing down the request it was sent."""
    answer = tmp_path / "answer"
    answer.write_text(
        json.dumps({
            "contract": "ensure-delivery-v1",
            "accepted": True,
            "schedule": {"account": "claude_code:main"},
        })
        + "\n200"
    )
    record = f'printf "%s\\n" "$@" > {seen}\n' if seen is not None else ""
    return f"{record}cat {answer}\n"


_DIRECTIVE = {
    "action": "new",
    "repo": "agentdeck",
    "number": 2142,
    "ref": "Eerovil/AgentDeck#2142",
    "generation": "gen-1",
    "run_id": "gen-1",
    "work_id": WORK,
    "dispatch_id": "d1",
    "messages": [{"payload": "do it", "author": "eero"}],
}


def test_the_delivery_carries_the_canonical_work_and_the_generation_as_run(tmp_path):
    seen = tmp_path / "seen.json"
    subject = _dispatcher(tmp_path, _accepting(tmp_path, seen))

    assert subject.deliver(_DIRECTIVE, now=NOW).outcome is DeliveryOutcome.DELIVERED

    payload = _payload(seen.read_text())
    # Every spelling of the issue is one Work (#2140): the owner's case is folded.
    assert payload["work_id"] == WORK == work_id_for(IssueRef.parse(KEY))
    assert payload["run_id"] == "gen-1"
    # No duplicate transport of the same value under its lease name (#2150).
    assert "dispatch_generation" not in payload
    # Machine metadata only: the model never reads it.
    assert "github_issue:" not in payload["message"]
    assert "gen-1" not in payload["message"]


def _payload(curl_args: str) -> dict:
    lines = curl_args.splitlines()
    return json.loads(lines[lines.index("--data-binary") + 1])


def test_the_accepted_delivery_receipt_names_its_work_and_run(tmp_path):
    subject = _dispatcher(tmp_path, _accepting(tmp_path))

    subject.deliver(_DIRECTIVE, now=NOW)

    receipt = json.loads(
        receipt_path(tmp_path / "dispatch-acks", "agentdeck", 2142, "d1").read_text()
    )
    assert receipt["work_id"] == WORK
    assert receipt["run_id"] == "gen-1"


def test_a_directive_naming_no_work_is_refused_not_guessed_from_its_card(tmp_path):
    """The Work is the engine's to name (#2142); the host never derives one from the ref."""
    seen = tmp_path / "seen.json"
    subject = _dispatcher(tmp_path, _accepting(tmp_path, seen))
    directive = {k: v for k, v in _DIRECTIVE.items() if k != "work_id"}

    delivery = subject.deliver(directive, now=NOW)

    assert (delivery.outcome, delivery.reason) == (DeliveryOutcome.REFUSED, "correlation_required")
    assert not seen.exists()  # nothing was sent


def test_the_engines_work_is_carried_even_when_the_ref_is_short(tmp_path):
    """The engine's durable dispatch is the source; the short board ref cannot drop it."""
    seen = tmp_path / "seen.json"
    subject = _dispatcher(tmp_path, _accepting(tmp_path, seen))

    subject.deliver({**_DIRECTIVE, "ref": "agentdeck#2142", "work_id": WORK}, now=NOW)

    payload = _payload(seen.read_text())
    assert (payload["work_id"], payload["run_id"]) == (WORK, "gen-1")


@pytest.mark.parametrize(
    "named",
    [
        "github_issue:eerovil/agentdeck#9",  # another card's Work
        "github_issue:Eerovil/agentdeck#2142",  # not the canonical spelling
        "gitlab_issue:eerovil/agentdeck#2142",
    ],
)
def test_the_adapter_refuses_a_work_that_is_not_this_cards(tmp_path, named):
    seen = tmp_path / "seen.json"
    subject = _dispatcher(tmp_path, _accepting(tmp_path, seen))

    delivery = subject.deliver({**_DIRECTIVE, "work_id": named}, now=NOW)

    assert delivery.outcome is DeliveryOutcome.REFUSED
    assert delivery.reason == "correlation_mismatch"
    assert not seen.exists()  # nothing was sent


def test_the_adapter_reads_the_explicit_run_and_refuses_one_that_is_not_the_lease(tmp_path):
    seen = tmp_path / "seen.json"
    subject = _dispatcher(tmp_path, _accepting(tmp_path, seen))

    delivery = subject.deliver({**_DIRECTIVE, "work_id": WORK, "run_id": "gen-other"}, now=NOW)

    assert delivery.reason == "correlation_mismatch"
    assert not seen.exists()


def test_a_directive_without_generation_is_correlated_by_its_run_alone(tmp_path):
    """After #2150 drops `generation`, the durable pair is all there is."""
    seen = tmp_path / "seen.json"
    subject = _dispatcher(tmp_path, _accepting(tmp_path, seen))
    directive = {k: v for k, v in _DIRECTIVE.items() if k != "generation"}

    subject.deliver({**directive, "work_id": WORK, "run_id": "gen-7"}, now=NOW)

    payload = _payload(seen.read_text())
    assert (payload["work_id"], payload["run_id"]) == (WORK, "gen-7")


def test_a_directive_naming_no_run_is_refused_not_given_its_generation(tmp_path):
    seen = tmp_path / "seen.json"
    subject = _dispatcher(tmp_path, _accepting(tmp_path, seen))
    directive = {k: v for k, v in _DIRECTIVE.items() if k != "run_id"}

    delivery = subject.deliver(directive, now=NOW)

    assert (delivery.outcome, delivery.reason) == (DeliveryOutcome.REFUSED, "correlation_required")
    assert not seen.exists()


def test_the_directive_carries_the_run_the_durable_dispatch_recorded():
    rec: dict = {}
    attempt = dispatch.new_dispatch(rec, "new")
    attempt["run_id"] = "recorded"  # what state.json says, not a recomputation

    directive = dispatch.build_directive(rec, attempt, repo="a", number=1, ref="a#1")

    assert directive["run_id"] == "recorded"


# --- the receipt: frozen before the write, never in the fingerprint ---------


def _receipts() -> tuple[DeliveryReceipts, dict]:
    store: dict = {}
    return DeliveryReceipts(store), store


RUN_1 = DeliveryCorrelation(WORK, "gen-1")
RUN_2 = DeliveryCorrelation(WORK, "gen-2")


def test_a_prepared_receipt_carries_the_correlation_and_finalize_keeps_it():
    receipts, store = _receipts()

    receipts.prepare("d", "fp", session_id=None, correlation=RUN_1)
    assert DeliveryCorrelation.from_receipt(store["d"]) == RUN_1
    receipts.finalize("d", "fp", DeliverResult(True, "spawned", session_id="s1"))

    assert DeliveryCorrelation.from_receipt(store["d"]) == RUN_1
    replay = receipts.lookup("d", "fp", live_session_id="s1", correlation=RUN_1)
    assert (replay.work_id, replay.run_id) == (WORK, "gen-1")


def test_a_conflicting_run_under_one_delivery_id_is_refused():
    receipts, _ = _receipts()
    receipts.prepare("d", "fp", session_id=None, correlation=RUN_1)
    receipts.finalize("d", "fp", DeliverResult(True, "spawned", session_id="s1"))

    refused = receipts.lookup("d", "fp", live_session_id="s1", correlation=RUN_2)

    assert not refused.accepted
    assert refused.reason == "delivery_correlation_conflict"


def test_an_uncertain_replay_answers_with_the_frozen_correlation():
    receipts, _ = _receipts()
    receipts.prepare("d", "fp", session_id="s1", correlation=RUN_1)

    replay = receipts.lookup("d", "fp", live_session_id="s1", correlation=RUN_1)

    assert replay.action == "uncertain"
    assert (replay.work_id, replay.run_id) == (WORK, "gen-1")


def test_a_legacy_receipt_adopts_the_incoming_correlation_without_resending():
    receipts, store = _receipts()
    receipts.prepare("d", "fp", session_id=None)  # written before #2142
    receipts.finalize("d", "fp", DeliverResult(True, "spawned", session_id="s1"))

    assert receipts.adopt_correlation("d", "fp", RUN_1)  # the store changed: persist
    assert not receipts.adopt_correlation("d", "fp", RUN_2)  # adopted once, then frozen
    assert not receipts.adopt_correlation("d", "other-fp", RUN_1)  # never another request
    replay = receipts.lookup("d", "fp", live_session_id="s1", correlation=RUN_1)

    assert replay.action == "spawned"
    assert (replay.work_id, replay.run_id) == (WORK, "gen-1")
    assert DeliveryCorrelation.from_receipt(store["d"]) == RUN_1


def test_an_uncorrelated_retry_still_replays_the_frozen_correlation():
    receipts, _ = _receipts()
    receipts.prepare("d", "fp", session_id=None, correlation=RUN_1)
    receipts.finalize("d", "fp", DeliverResult(True, "spawned", session_id="s1"))

    replay = receipts.lookup("d", "fp", live_session_id="s1")

    assert replay.run_id == "gen-1"


def test_the_current_correlation_ignores_a_refused_delivery():
    receipts, _ = _receipts()
    receipts.prepare("a", "fp", session_id=None, correlation=RUN_1)
    receipts.finalize("a", "fp", DeliverResult(True, "spawned", session_id="s1"))
    receipts.prepare("b", "fp2", session_id=None, correlation=RUN_2)
    receipts.finalize("b", "fp2", DeliverResult(False, "rejected", reason="at_capacity"))

    delivery_id, correlation, session_id = receipts.current_correlation()

    assert (delivery_id, correlation, session_id) == ("a", RUN_1, "s1")


def test_half_a_correlation_is_no_correlation():
    assert DeliveryCorrelation.of(WORK, "") is None
    assert DeliveryCorrelation.of(None, "gen-1") is None


# --- every provider host keeps the same contract -----------------------------


@adapters
async def test_the_correlation_is_durable_before_the_provider_write(build_harness, tmp_path):
    harness = build_harness(tmp_path)
    on_disk_at_write: list[dict] = []
    harness.write_hooks.append(
        lambda: on_disk_at_write.append(dict(harness.persisted_receipts(KEY).get("d1", {})))
    )

    result = await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False, correlation=RUN_1
    )
    await settle()

    assert result.accepted
    assert on_disk_at_write, "the provider write happened"
    assert DeliveryCorrelation.from_receipt(on_disk_at_write[0]) == RUN_1
    await harness.stop()


@adapters
async def test_a_restarted_runtime_replays_the_original_correlation(build_harness, tmp_path):
    harness = build_harness(tmp_path)
    await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False, correlation=RUN_1
    )
    await settle()
    await harness.stop()

    harness.reload()
    replay = await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False, correlation=RUN_1
    )

    assert replay.accepted
    assert (replay.work_id, replay.run_id) == (WORK, "gen-1")
    assert harness.writes == ["go"]
    snapshot = harness.host.snapshot()
    assert SNAPSHOT_RUN_CORRELATION in snapshot["snapshot_capabilities"]
    worker = snapshot["workers"][KEY]
    assert (worker["work_id"], worker["run_id"]) == (WORK, "gen-1")
    assert worker["run_session_id"] == harness.record(KEY).session_id
    assert worker["run_delivery_id"] == "d1"
    await harness.stop()


@adapters
async def test_a_conflicting_run_is_refused_before_any_provider_write(build_harness, tmp_path):
    harness = build_harness(tmp_path)
    await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False, correlation=RUN_1
    )
    await settle()

    refused = await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False, correlation=RUN_2
    )

    assert refused.reason == "delivery_correlation_conflict"
    assert harness.writes == ["go"]
    assert DeliveryCorrelation.from_receipt(harness.receipts(KEY)["d1"]) == RUN_1
    await harness.stop()


@adapters
async def test_an_uncertain_delivery_keeps_its_correlation(build_harness, tmp_path):
    harness = build_harness(tmp_path)
    await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False, correlation=RUN_1
    )
    await settle()
    harness.receipts(KEY)["d1"].pop("action", None)  # killed between write and confirm

    replay = await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False, correlation=RUN_1
    )

    assert replay.action == "uncertain"
    assert (replay.work_id, replay.run_id) == (WORK, "gen-1")
    assert harness.writes == ["go"]
    await harness.stop()


@adapters
async def test_an_adopted_correlation_survives_a_runtime_restart(build_harness, tmp_path):
    harness = build_harness(tmp_path)
    await harness.host.deliver(KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False)
    await settle()
    assert "work_id" not in harness.persisted_receipts(KEY)["d1"]  # a pre-#2142 receipt

    replay = await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False, correlation=RUN_1
    )

    assert (replay.work_id, replay.run_id) == (WORK, "gen-1")
    assert DeliveryCorrelation.from_receipt(harness.persisted_receipts(KEY)["d1"]) == RUN_1
    await harness.stop()
    harness.reload()
    again = await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False
    )
    assert (again.work_id, again.run_id) == (WORK, "gen-1")
    assert harness.writes == ["go"]
    await harness.stop()


@adapters
async def test_a_delivery_that_never_began_its_write_names_no_run_after_a_crash(
    build_harness, tmp_path
):
    """A crash after `prepare()` is saved but before `begin_write()`: nothing reached the
    provider, so the restarted runtime must not report — and boot must not bind — that Run."""
    harness = build_harness(tmp_path)
    await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False, correlation=RUN_1
    )
    await settle()
    receipt = harness.receipts(KEY)["d1"]
    for field in ("action", "provider_turn_id"):
        receipt.pop(field, None)
    receipt["delivery_stage"] = "prepared_before_write"
    harness.host._save_state()
    await harness.stop()

    harness.reload()
    worker = harness.host.snapshot()["workers"][KEY]

    assert (worker["work_id"], worker["run_id"], worker["run_session_id"]) == (None, None, None)
    # The receipt itself still holds its frozen correlation for the same-id retry.
    assert DeliveryCorrelation.from_receipt(harness.persisted_receipts(KEY)["d1"]) == RUN_1
    await harness.stop()


@adapters
async def test_an_uncertain_write_still_names_its_run(build_harness, tmp_path):
    harness = build_harness(tmp_path)
    await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False, correlation=RUN_1
    )
    await settle()
    receipt = harness.receipts(KEY)["d1"]
    receipt.pop("action", None)
    receipt["delivery_stage"] = "write_may_have_started"

    assert harness.host.snapshot()["workers"][KEY]["run_id"] == "gen-1"
    await harness.stop()


@adapters
async def test_a_clean_pre_write_rejection_proves_no_participation(build_harness, tmp_path):
    """One rule decides participation for every provider (#2142 review round 3)."""
    harness = build_harness(tmp_path)
    await harness.host.deliver(KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False)
    await settle()

    # Demand the opposite of the worker's live state — idle while it is mid-turn, active
    # while it is idle — which every host refuses before anything is written.
    busy = bool(harness.host.snapshot()["workers"][KEY]["turn_active"])
    refused = await harness.host.deliver(
        KEY,
        "more",
        cwd=harness.cwd,
        delivery_id="d2",
        delegated=False,
        require_idle=busy,
        require_active=not busy,
        correlation=RUN_2,
    )

    assert not refused.accepted
    assert harness.writes == ["go"]
    receipt = harness.host.delivery_receipt(KEY, "d2")
    assert not delivery_participated(receipt)
    assert harness.host.snapshot()["workers"][KEY]["run_id"] is None
    await harness.stop()


@adapters
async def test_an_uncertain_write_proves_participation_by_the_same_rule(build_harness, tmp_path):
    harness = build_harness(tmp_path)
    await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False, correlation=RUN_1
    )
    await settle()
    receipt = harness.receipts(KEY)["d1"]
    receipt.pop("action", None)  # the write began and was never confirmed

    stored = harness.host.delivery_receipt(KEY, "d1")
    assert delivery_participated(stored)
    assert harness.host.snapshot()["workers"][KEY]["run_id"] == "gen-1"
    await harness.stop()


@pytest.mark.parametrize(
    ("outcome", "participated"),
    [
        ({"action": "spawned", "accepted": True}, True),
        ({"action": "uncertain", "accepted": True}, True),
        ({"action": "rejected", "accepted": False, "session_id": "s1"}, False),
        ({"action": "rejected", "accepted": False, "provider_write_state": "unknown"}, True),
        ({"fingerprint": "f"}, True),  # an unconfirmed, pre-stage receipt: may have written
        ({"fingerprint": "f", "delivery_stage": "write_may_have_started"}, True),
        ({"fingerprint": "f", "delivery_stage": "prepared_before_write"}, False),
        (None, False),
    ],
)
def test_the_one_participation_rule(outcome, participated):
    assert delivery_participated(outcome) is participated


@adapters
async def test_an_uncorrelated_delivery_is_unchanged(build_harness, tmp_path):
    harness = build_harness(tmp_path)

    result = await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False
    )
    await settle()

    assert result.accepted
    assert "work_id" not in harness.receipts(KEY)["d1"]
    assert harness.host.snapshot()["workers"][KEY]["work_id"] is None
    await harness.stop()


# --- the runtime: accepts it as metadata and answers with the receipt's ------


def _runtime_config(tmp_path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "claude_workers": {"enabled": True, "state_dir": str(tmp_path)},
            "keyed_workers": {"state_dir": str(tmp_path)},
            "restart_nudge": {"file": str(tmp_path / "restart-nudge.json")},
            "history": {"db_path": str(tmp_path / "budgets.db")},
            "accounts": [
                AccountConfig(
                    provider="claude_code", label="main", config_dir=str(tmp_path)
                ).model_dump()
            ],
        }
    )


async def test_the_runtime_hands_the_correlation_to_the_host_and_reports_the_receipt(tmp_path):
    app = create_runtime_app(_runtime_config(tmp_path))
    host = MagicMock()
    host.deliver = AsyncMock(return_value=DeliverResult(True, "spawned", session_id="s1"))
    host.delivery_receipt = MagicMock(return_value={"work_id": WORK, "run_id": "gen-1"})
    app.state.claude_workers.hosts["main"] = host

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://rt") as client:
        response = await client.post(
            "/workers/accounts/claude_code/main/deliver",
            json={
                "key": KEY,
                "message": "go",
                "delivery_id": "poller:d1",
                "work_id": WORK,
                "run_id": "gen-1",
            },
        )

    body = response.json()
    assert (body["work_id"], body["run_id"]) == (WORK, "gen-1")
    assert body["run_participation"] is True
    kwargs = host.deliver.await_args.kwargs
    assert kwargs["correlation"] == RUN_1
    # Metadata, not prompt: the message the host writes is exactly the caller's.
    assert host.deliver.await_args.args[1] == "go"


async def test_the_runtime_reports_no_participation_when_no_receipt_survives(tmp_path):
    """A clean pre-write failure forgets its receipt; the session id alone proves nothing."""
    app = create_runtime_app(_runtime_config(tmp_path))
    host = MagicMock()
    host.deliver = AsyncMock(
        return_value=DeliverResult(False, "rejected", reason="write failed", session_id="s1")
    )
    host.delivery_receipt = MagicMock(return_value=None)
    app.state.claude_workers.hosts["main"] = host

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://rt") as client:
        response = await client.post(
            "/workers/accounts/claude_code/main/deliver",
            json={
                "key": KEY,
                "message": "go",
                "delivery_id": "poller:d1",
                "work_id": WORK,
                "run_id": "gen-1",
            },
        )

    body = response.json()
    assert body["run_participation"] is False
    assert "work_id" not in body


async def test_the_runtime_sends_no_correlation_for_an_uncorrelated_delivery(tmp_path):
    app = create_runtime_app(_runtime_config(tmp_path))
    host = MagicMock()
    host.deliver = AsyncMock(return_value=DeliverResult(True, "spawned", session_id="s1"))
    app.state.claude_workers.hosts["main"] = host

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://rt") as client:
        response = await client.post(
            "/workers/accounts/claude_code/main/deliver",
            json={"key": KEY, "message": "go", "delivery_id": "poller:d1"},
        )

    assert "work_id" not in response.json()
    assert "correlation" not in host.deliver.await_args.kwargs


async def test_boot_restores_a_binding_the_death_kept_from_being_written(tmp_path):
    state = create_runtime_app(_runtime_config(tmp_path)).state
    host = MagicMock()
    host.snapshot = MagicMock(
        return_value={
            "workers": {
                KEY: {
                    "session_id": "s2",  # moved on since, uncorrelated
                    "work_id": WORK,
                    "run_id": "gen-1",
                    "run_session_id": "s1",
                    "run_delivery_id": "poller:d1",
                },
                "other": {"session_id": "s9", "work_id": None, "run_id": None},
            }
        }
    )
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges

    nudges.reconcile_run_bindings()
    nudges.reconcile_run_bindings()  # a second boot changes nothing

    bindings = nudges._budget_db.session_run_bindings()
    assert [(b.session_key, b.work_id, b.run_id, b.first_delivery_id) for b in bindings] == [
        ("claude_code:main:s1", WORK, "gen-1", "poller:d1")
    ]


# --- the deck: validated, ledgered, bound ------------------------------------


def test_the_deck_refuses_half_a_correlation():
    with pytest.raises(ValidationError):
        WorkerDeliverRequest(key=KEY, message="m", work_id=WORK)


@pytest.mark.parametrize(
    "field",
    [
        {"dispatch_generation": "gen-1"},
        {"phase_delivery_id": "poller:d1:phase:execute:1"},
        {"expected_source_session_id": "s0"},
        {"expected_source_version": "1:2:3"},
    ],
)
def test_the_deck_refuses_a_field_retired_with_phase_delivery(field):
    """#2194: a caller relying on a removed guard is told so, not silently ignored."""
    with pytest.raises(ValidationError, match="no longer accepted"):
        WorkerDeliverRequest(key=KEY, message="m", **field)


def test_the_deck_refuses_a_work_that_is_not_the_cards():
    with pytest.raises(ValidationError, match="canonical"):
        WorkerDeliverRequest(
            key=KEY,
            message="m",
            work_id="github_issue:eerovil/agentdeck#1",
            run_id="gen-1",
            issue_ref=KEY,
        )


@pytest.mark.parametrize(
    "work_id",
    [
        "github_issue:Eerovil/agentdeck#2142",  # another spelling of the same Work
        "github_issue:agentdeck#2142",  # no owner
        "gitlab_issue:eerovil/agentdeck#2142",  # no such source kind
        "github_issue:eerovil/agentdeck#0x10",
    ],
)
def test_the_deck_refuses_a_work_id_that_is_not_canonical(work_id):
    with pytest.raises(ValidationError, match="canonical"):
        WorkerDeliverRequest(key=KEY, message="m", work_id=work_id, run_id="gen-1")


def test_blank_correlation_fields_are_no_correlation():
    body = WorkerDeliverRequest(
        key=KEY, message="m", delivery_id="poller:d1", work_id=" ", run_id=""
    )

    assert body.correlation is None


def test_the_deck_accepts_any_spelling_of_the_cards_issue():
    body = WorkerDeliverRequest(
        key=KEY,
        message="m",
        delivery_id="poller:d1",
        work_id=WORK,
        run_id="gen-1",
        issue_ref="https://github.com/EEROVIL/agentdeck/issues/2142",
    )

    assert body.correlation == RUN_1


def _deck(tmp_path, handle):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        inject=InjectConfig(enabled=True, max_message_chars=2000),
        accounts=[
            AccountConfig(
                provider="claude_code", label="main", config_dir=str(tmp_path / "main")
            ),
            AccountConfig(provider="codex", label="alt", config_dir=str(tmp_path / "alt")),
        ],
    )
    app = create_app(config)
    app.state.runtime_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runtime"
    )
    return app


def _runtime_answering(session_id: str, seen: list[dict], *, echo: bool = True):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"workers": {}})
        body = json.loads(request.content)
        seen.append(body)
        answer = {"accepted": True, "action": "spawned", "session_id": session_id}
        if echo and body.get("work_id"):
            answer.update(work_id=body["work_id"], run_id=body["run_id"])
        return httpx.Response(200, json=answer)

    return handle


def _poller_body(run_id: str = "gen-1", delivery_id: str = "poller:d1", **extra) -> dict:
    return {
        "key": KEY,
        "message": "carry on",
        "delivery_id": delivery_id,
        "role": "kanban",
        "context_policy": "auto",
        "issue_ref": KEY,
        "work_id": WORK,
        "run_id": run_id,
        **extra,
    }


async def _post(app, path: str, body: dict) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post(f"/api/workers/accounts/{path}/deliver", json=body)


async def test_one_delivery_is_followable_from_work_to_session(tmp_path):
    seen: list[dict] = []
    app = _deck(tmp_path, _runtime_answering("s1", seen))

    response = await _post(app, "claude_code/main", _poller_body())

    assert response.json()["accepted"]
    # The runtime got the correlation as fields, never inside the message.
    assert (seen[0]["work_id"], seen[0]["run_id"]) == (WORK, "gen-1")
    assert "github_issue" not in seen[0]["message"]
    bindings = app.state.budget_db.session_run_bindings(work_id=WORK, run_id="gen-1")
    assert [(b.session_key, b.first_delivery_id) for b in bindings] == [
        ("claude_code:main:s1", "poller:d1")
    ]
    row = app.state.budget_db.get_context_delivery(
        "claude_code", "claude_code:main", KEY, "poller:d1"
    )
    assert (row.work_id, row.run_id) == (WORK, "gen-1")


async def test_a_replayed_delivery_answers_with_its_original_correlation(tmp_path):
    seen: list[dict] = []
    app = _deck(tmp_path, _runtime_answering("s1", seen))
    await _post(app, "claude_code/main", _poller_body())

    replay = await _post(app, "claude_code/main", _poller_body())

    assert len(seen) == 1  # answered from the ledger: nothing reached the runtime again
    assert (replay.json()["work_id"], replay.json()["run_id"]) == (WORK, "gen-1")


async def test_a_conflicting_work_under_one_delivery_id_never_reaches_the_runtime(tmp_path):
    seen: list[dict] = []
    app = _deck(tmp_path, _runtime_answering("s1", seen))
    await _post(app, "claude_code/main", _poller_body())
    other = "github_issue:eerovil/agentdeck#9"

    refused = await _post(
        app, "claude_code/main", {**_poller_body(), "work_id": other, "issue_ref": None}
    )

    assert refused.json()["reason"] == "delivery_correlation_conflict"
    assert len(seen) == 1


async def test_a_ledger_row_from_before_correlation_adopts_it(tmp_path):
    seen: list[dict] = []
    app = _deck(tmp_path, _runtime_answering("s1", seen))
    legacy = {k: v for k, v in _poller_body().items() if k not in {"work_id", "run_id"}}
    await _post(app, "claude_code/main", legacy)

    replay = await _post(app, "claude_code/main", _poller_body())

    assert len(seen) == 1
    assert replay.json()["run_id"] == "gen-1"
    row = app.state.budget_db.get_context_delivery(
        "claude_code", "claude_code:main", KEY, "poller:d1"
    )
    assert (row.work_id, row.run_id) == (WORK, "gen-1")


async def test_moving_account_or_provider_does_not_move_the_work_or_run(tmp_path):
    seen: list[dict] = []
    answers = iter(["s1", "t1"])

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"workers": {}})
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"accepted": True, "action": "spawned", "session_id": next(answers)}
        )

    app = _deck(tmp_path, handle)
    await _post(app, "claude_code/main", _poller_body())
    # The same Run lands on another provider's account (a headroom move).
    await _post(app, "codex/alt", _poller_body())

    bindings = app.state.budget_db.session_run_bindings(work_id=WORK, run_id="gen-1")
    assert sorted(b.session_key for b in bindings) == ["claude_code:main:s1", "codex:alt:t1"]


def _runtime_returning(answer: dict):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"workers": {}})
        return httpx.Response(200, json=answer)

    return handle


@pytest.mark.parametrize(
    "answer",
    [
        # The runtime says the receipt proves nothing, though a session id came back.
        {"accepted": False, "action": "rejected", "session_id": "s1", "run_participation": False},
        # An older runtime: a plain rejection with a session id is no participation either.
        {"accepted": False, "action": "rejected", "session_id": "s1"},
    ],
)
async def test_a_clean_rejection_binds_no_session_to_the_run(tmp_path, answer):
    app = _deck(tmp_path, _runtime_returning(answer))

    await _post(app, "claude_code/main", _poller_body())

    assert app.state.budget_db.session_run_bindings() == []


@pytest.mark.parametrize(
    "answer",
    [
        {
            "accepted": True,
            "action": "uncertain",
            "session_id": "s1",
            "work_id": WORK,
            "run_id": "gen-1",
            "run_participation": True,
        },
        {"accepted": True, "action": "uncertain", "session_id": "s1"},  # an older runtime
    ],
)
async def test_an_uncertain_write_binds_its_session(tmp_path, answer):
    app = _deck(tmp_path, _runtime_returning(answer))

    await _post(app, "claude_code/main", _poller_body())

    assert [(b.session_key, b.run_id) for b in app.state.budget_db.session_run_bindings()] == [
        ("claude_code:main:s1", "gen-1")
    ]


async def test_an_old_runtime_that_echoes_nothing_still_binds_the_requested_run(tmp_path):
    seen: list[dict] = []
    app = _deck(tmp_path, _runtime_answering("s1", seen, echo=False))

    await _post(app, "claude_code/main", _poller_body())

    assert [b.run_id for b in app.state.budget_db.session_run_bindings()] == ["gen-1"]


# --- the binding: participation, many to many -------------------------------


def _db(tmp_path) -> Db:
    return Db(tmp_path / "bindings.db")


def test_a_binding_is_idempotent_and_the_first_one_stands(tmp_path):
    db = _db(tmp_path)

    db.bind_session_run("s:a:1", WORK, "gen-1", delivery_id="poller:d1")
    db.bind_session_run("s:a:1", WORK, "gen-1", delivery_id="poller:d2")

    (binding,) = db.session_run_bindings()
    assert binding.first_delivery_id == "poller:d1"


def test_bindings_are_many_to_many(tmp_path):
    db = _db(tmp_path)
    other = "github_issue:eerovil/agentdeck#1"

    db.bind_session_run("s:a:1", WORK, "gen-1")
    db.bind_session_run("s:a:1", WORK, "gen-2")  # the same session in the next Run
    db.bind_session_run("s:a:1", other, "gen-9")  # and in another Work entirely
    db.bind_session_run("s:b:2", WORK, "gen-1")  # a second session in the first Run

    assert len(db.session_run_bindings(session_key="s:a:1")) == 3
    assert {b.session_key for b in db.session_run_bindings(run_id="gen-1")} == {
        "s:a:1",
        "s:b:2",
    }


# --- delegation: the child gets the exact current execution ----------------


def test_a_delegated_child_joins_the_run_its_parent_is_in_and_only_that(tmp_path):
    db = _db(tmp_path)
    state = AppState(db=db, budget_db=db)
    parent = "claude_code:main:parent-1"
    db.bind_session_run(parent, "github_issue:eerovil/agentdeck#1", "old-run")
    time.sleep(0.001)
    db.bind_session_run(parent, WORK, "gen-1")

    # A child names its parent by raw provider id as often as by key.
    state.mark_delegated_session("codex:alt:child-1", "parent-1")

    assert [(b.work_id, b.run_id) for b in db.session_run_bindings(
        session_key="codex:alt:child-1"
    )] == [(WORK, "gen-1")]


def test_a_child_of_a_parent_in_no_run_joins_none(tmp_path):
    db = _db(tmp_path)
    state = AppState(db=db, budget_db=db)

    state.mark_delegated_session("codex:alt:child-1", "claude_code:main:parent-1")

    assert db.session_run_bindings() == []


def test_the_binding_survives_a_restart(tmp_path):
    _db(tmp_path).bind_session_run("s:a:1", WORK, "gen-1")

    assert [b.run_id for b in _db(tmp_path).session_run_bindings()] == ["gen-1"]
