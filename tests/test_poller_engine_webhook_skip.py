"""#1673 builds #1631: the webhook-quiet skip of a timed wake's live GitHub queries.

The engine owns the policy knob and the last-full-wake ceiling; the host's two proofs
arrive collapsed into one ``webhook_quiet`` hint. Every test here holds the decision to
its one safety property: any condition that cannot be proved runs the full wake, so the
failure mode of the whole feature is always more polling, never less.

The second property, and the one #1673 stated as an acceptance condition, is that what
the skip drops is the *live GitHub queries* and nothing else. The wake still runs, so
durable state — a queued command, a delivery waiting to be retried — keeps moving at the
ordinary 30-second cadence; only discovery waits for the ceiling, because discovery is
the only thing the webhook streams are evidence about.
"""

from __future__ import annotations

import sys
from datetime import timedelta

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, run_wake, store_for

from agentdeck.poller_engine import extensions
from agentdeck.poller_engine.config import (
    DEFAULT_WEBHOOK_SKIP_S,
    Report,
    parse_manifest,
)
from agentdeck.poller_engine.cycle import WEBHOOK_QUIET_ENV
from agentdeck.poller_engine.extensions import Hook
from agentdeck.poller_engine.ownership import LabelEvent
from agentdeck.poller_engine.ports import DeployState
from agentdeck.poller_engine.reconcile import CardSnapshot

RECENT = (_NOW - timedelta(seconds=30)).isoformat()


def _quiet_state(stamp: str = RECENT) -> dict:
    return {
        "issues": {"store#5": {"status": "Todo"}},
        "ownership": {"initialized": True, "events": {"store#5": "e1"}},
        "last_live_wake": stamp,
    }


def _live_port() -> FakePort:
    return FakePort(
        labelled=["store#5"],
        label_events={"store#5": ["e1"]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
    )


def _assert_no_live_queries(port: FakePort) -> None:
    assert port.scans == 0
    assert port.labelled_reads == 0
    assert port.snapshot_batches == []


# --- the skip decision ---------------------------------------------------------------


def test_all_three_conditions_met_skips_the_live_queries(tmp_path):
    store = store_for(tmp_path, _quiet_state())
    port = _live_port()

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.quiet == "webhook-quiet"
    # Not halted: the cycle ran. A halt is how the engine says it refused to work at all,
    # and the host reads it as a deferred wake rather than one whose directives count.
    assert outcome.halted is None
    assert any("webhook streams quiet" in line for line in outcome.log)
    _assert_no_live_queries(port)
    state = store.read()
    assert state["issues"] == {"store#5": {"status": "Todo"}}
    # The stamp is the ceiling on trusting webhooks, so a skipped wake never renews it:
    # 300 seconds after the last *full* wake the skip runs out however quiet it stayed.
    assert state["last_live_wake"] == RECENT


def test_without_the_host_hint_the_wake_is_full(tmp_path):
    store = store_for(tmp_path, _quiet_state())
    port = _live_port()

    outcome = run_wake(store, port, now=_NOW)  # webhook_quiet defaults to False

    assert outcome.halted is None
    assert port.labelled_reads > 0


def test_the_knob_at_zero_disables_skipping_entirely(tmp_path):
    store = store_for(tmp_path, _quiet_state())
    port = _live_port()

    outcome = run_wake(store, port, now=_NOW, webhook_skip_s=0, webhook_quiet=True)

    assert outcome.halted is None
    assert port.labelled_reads > 0


def test_a_stale_last_full_wake_forces_a_full_wake(tmp_path):
    old = (_NOW - timedelta(seconds=DEFAULT_WEBHOOK_SKIP_S)).isoformat()
    store = store_for(tmp_path, _quiet_state(old))
    port = _live_port()

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.halted is None
    assert port.labelled_reads > 0


def test_no_recorded_full_wake_is_no_evidence(tmp_path):
    state = _quiet_state()
    del state["last_live_wake"]
    store = store_for(tmp_path, state)
    port = _live_port()

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.halted is None
    assert port.labelled_reads > 0


def test_an_unreadable_or_future_stamp_polls_rather_than_trusts(tmp_path):
    for index, stamp in enumerate(
        ("not a moment", (_NOW + timedelta(seconds=120)).isoformat())
    ):
        base = tmp_path / f"case-{index}"
        base.mkdir()
        store = store_for(base, _quiet_state(stamp))
        port = _live_port()
        outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)
        assert outcome.halted is None, stamp
        assert port.labelled_reads > 0, stamp


def test_a_budget_deferral_outranks_the_quiet_skip(tmp_path):
    """Quota low is the fact an operator must see, and neither wake ran live queries —
    but only the budget reason says why even a forced full wake would defer."""
    store = store_for(tmp_path, _quiet_state())
    port = FakePort(graphql_deferred=True)

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.halted == "graphql-budget"
    # A deferred wake ran no live queries either, so it must not renew the ceiling.
    assert store.read()["last_live_wake"] == RECENT


# --- the last-full-wake stamp --------------------------------------------------------


def test_a_full_wake_stamps_itself_and_arms_the_next_skip(tmp_path):
    store = store_for(
        tmp_path,
        {
            "issues": {"store#5": {"status": "Todo"}},
            "ownership": {"initialized": True, "events": {"store#5": "e1"}},
        },
    )

    first = run_wake(store, _live_port(), now=_NOW, webhook_quiet=True)
    assert first.halted is None  # nothing recorded yet, so the hint was declined
    assert store.read()["last_live_wake"] == _NOW.isoformat()

    later = _NOW + timedelta(seconds=30)
    port = _live_port()
    second = run_wake(store, port, now=later, webhook_quiet=True)
    assert second.quiet == "webhook-quiet"
    _assert_no_live_queries(port)


def test_a_skipped_wake_still_publishes_health(tmp_path):
    store = store_for(tmp_path, _quiet_state())

    outcome = run_wake(store, _live_port(), now=_NOW, webhook_quiet=True)

    assert outcome.verdict is not None
    health = store.read()["health"]
    assert health["cycles"] == 1
    assert health["last_cycle_at"] == _NOW.isoformat()


def test_a_skipped_wake_keeps_labelled_discovery_for_the_next_full_wake(tmp_path):
    """The skip consumes nothing: a label event the streams never saw (webhooks are
    at-most-once) is still discovered and started by the full wake the ceiling
    guarantees, exactly as #311's discovery test starts it."""
    store = store_for(
        tmp_path,
        {
            "ownership": {
                "initialized": True,
                "events": {},
                "last_complete_scan_at": "2026-07-25T12:00:00+00:00",
            },
            "last_live_wake": RECENT,
        },
    )
    port = FakePort(
        labelled=["store#7"],
        label_events={"store#7": [LabelEvent("e7", "2026-07-25T12:00:01Z")]},
        snapshots={"store#7": CardSnapshot(status="Todo")},
    )

    skipped = run_wake(store, port, now=_NOW, webhook_quiet=True)
    assert skipped.quiet == "webhook-quiet"
    assert skipped.directives == []
    assert "store#7" not in (store.read().get("issues") or {})

    full = run_wake(
        store, port, now=_NOW + timedelta(seconds=DEFAULT_WEBHOOK_SKIP_S), webhook_quiet=True
    )
    assert full.halted is None
    assert [directive["ref"] for directive in full.directives] == ["store#7"]


# --- what the skip must not hold -----------------------------------------------------


def _queued_control_state() -> dict:
    """A command accepted on an earlier wake, on a card the engine has already read.

    Everything here is durable state: the ledger record, the card, and the live check that
    already happened. None of it needs a fresh discovery pass to move, which is exactly why
    holding it for the whole skip window was wrong.
    """
    return {
        "issues": {
            "store#5": {
                "status": "Todo",
                # Read on the last full wake, so this card is not one the engine has never
                # seen — those deliberately wait for a wake that can check their blockers.
                "last_live_check": RECENT,
            }
        },
        "controls": {
            "9": {
                "id": "9",
                "repo": "store",
                "number": 5,
                "kind": "claude",
                "payload": "carry on",
                "status": "new",
                "created_at": "t1",
            }
        },
        "ownership": {"initialized": True, "events": {"store#5": "e1"}},
        "last_live_wake": RECENT,
    }


def test_a_queued_control_advances_on_a_quiet_wake(tmp_path):
    """The regression #1673 asked for: durable controls and their delivery must not wait
    for the 300s ceiling, because they never needed GitHub to be re-read."""
    store = store_for(tmp_path, _queued_control_state())
    port = _live_port()

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.quiet == "webhook-quiet"
    # The command progressed out of `new`, and the worker delivery it earned went out.
    assert store.read()["controls"]["9"]["status"] in {"queued", "processing"}
    assert [directive["ref"] for directive in outcome.directives] == ["store#5"]
    # ...and it did so without asking GitHub a single question.
    _assert_no_live_queries(port)


def test_a_card_the_engine_has_never_read_still_waits_for_a_full_wake(tmp_path):
    """The one thing a quiet wake must not do: start a card whose declared dependencies
    nobody checked. A full wake follows within the ceiling and starts it properly."""
    state = _queued_control_state()
    del state["issues"]["store#5"]["last_live_check"]
    state["issues"]["store#5"]["pending"] = ["9"]
    state["controls"]["9"]["status"] = "queued"
    store = store_for(tmp_path, state)
    port = _live_port()

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.directives == []
    assert any("dependencies unread this wake" in line for line in outcome.log)
    _assert_no_live_queries(port)

    later = run_wake(store, port, now=_NOW + timedelta(seconds=DEFAULT_WEBHOOK_SKIP_S))
    assert [directive["ref"] for directive in later.directives] == ["store#5"]


def test_a_quiet_wake_is_not_reported_as_a_failed_github_read(tmp_path):
    """Declining to ask is not the same fact as asking and failing. A quiet wake that
    counted scan failures would make a healthy instance look like a broken one within
    minutes, because most wakes are quiet."""
    store = store_for(tmp_path, _queued_control_state())

    outcome = run_wake(store, _live_port(), now=_NOW, webhook_quiet=True)

    assert outcome.report.scan_failures == 0
    assert outcome.report.errors == []


# --- the retired phase host events (#2162, #2154) -----------------------------------


def _legacy_phase_state(status: str) -> dict:
    """A card carrying a phase block from before #2154."""
    return {
        "issues": {
            "store#5": {
                "status": "In progress",
                "generation": "gen-1",
                "processing": ["9"],
                "last_live_check": RECENT,
                "phases": {
                    "generation": "gen-1",
                    "lane": "work",
                    "phase": "execute",
                    "attempt": 1,
                    "status": status,
                    "checkpoints": {},
                },
            }
        },
        "controls": {"9": {"id": "9", "repo": "store", "number": 5, "kind": "claude",
                           "payload": "do the thing", "status": "processing",
                           "created_at": "x"}},
        "ownership": {"initialized": True, "events": {"store#5": "e1"}},
        "last_live_wake": RECENT,
    }


def _phase_port(**kwargs) -> FakePort:
    return FakePort(
        labelled=["store#5"],
        label_events={"store#5": ["e1"]},
        snapshots={"store#5": CardSnapshot(status="In progress")},
        **kwargs,
    )


_ENDED = {"store#5": {"observed": "ended", "generation": "gen-1"}}


def test_an_old_checkpointed_phase_buys_no_full_wake_and_no_successor(tmp_path):
    """#2154 tests 8/9: a phase block left from before the retirement loads, is ignored, and
    neither forces a full wake nor mints a phase successor."""
    store = store_for(tmp_path, _legacy_phase_state("checkpointed"))
    port = _phase_port(turn_ends=_ENDED)

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.quiet == "webhook-quiet"
    assert not [d for d in outcome.directives if d["action"] in {"phase", "phase-pressure"}]
    saved = store.read()
    assert saved["issues"]["store#5"]["phases"]["status"] == "checkpointed"


def test_a_finished_verification_job_is_credited_on_a_quiet_wake(tmp_path):
    """#2154 test 3: verification evidence attaches to the Run from local host state alone, so
    the quiet cadence credits it without any live GitHub read."""
    store = store_for(tmp_path, _legacy_phase_state("active"))
    port = _phase_port(
        jobs={"job-7": {
            "id": "job-7", "purpose": "verification", "run_id": "gen-1",
            "state": "done", "result_class": "success",
            "tested_workspace": _clean_tree("c0ffee"),
            "verified_workspace": _clean_tree("c0ffee"),
            "tested_workspaces": {"unresolved:/w": _clean_tree("c0ffee")},
            "verified_workspaces": {"unresolved:/w": _clean_tree("c0ffee")},
        }},
    )

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.quiet == "webhook-quiet"
    _assert_no_live_queries(port)
    assert "c0ffee" in store.read()["issues"]["store#5"]["release_tests_pass"]


def _clean_tree(head: str) -> dict:
    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    return {
        "root": "/w", "head": head, "staged_sha256": empty, "tracked_diff_sha256": empty,
        "untracked_sha256": empty, "untracked_count": 0,
    }


def _deploying_state() -> dict:
    """A card merged and waiting for its production deploy, one quiet wake after a full one."""
    return {
        "issues": {
            "store#5": {
                "status": "In progress", "mode": "merge", "merge_control": "3",
                "merged": ["store#7"], "started": True,
                "awaiting_deploy": {"builds": {"store": {"sha": "sha1", "status": "pending"}},
                                    "since": RECENT},
            }
        },
        "controls": {"3": {"id": "3", "repo": "store", "number": 5, "kind": "merge",
                           "status": "processing", "created_at": "t1"}},
        "ownership": {"initialized": True, "events": {}},
        "last_live_wake": RECENT,
    }


def test_an_awaited_deploy_is_confirmed_on_a_quiet_wake(tmp_path):
    """#2351: a finished deploy sends GitHub nothing, so the streams stay quiet after it.

    The deploy gate reads the host, not GitHub, so it has to run on the quiet cadence. Were it
    held for a full wake, a card would read Deploying for up to the whole ceiling after
    production already runs the merge.
    """
    store = store_for(tmp_path, _deploying_state())
    port = FakePort(deploys={"store": DeployState(status="ok", link="http://127.0.0.1/healthz")})

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.quiet == "webhook-quiet"
    _assert_no_live_queries(port)
    assert "store#5" not in store.read()["issues"]
    assert any("127.0.0.1/healthz" in str(effect.value) for effect in port.applied)


def test_a_still_pending_deploy_is_read_again_on_the_next_quiet_wake(tmp_path):
    store = store_for(tmp_path, _deploying_state())
    port = FakePort(deploys={"store": DeployState(status="pending")})

    first = run_wake(store, port, now=_NOW, webhook_quiet=True)
    assert first.quiet == "webhook-quiet"
    assert first.report.deploys == 1
    assert store.read()["issues"]["store#5"]["awaiting_deploy"]

    port.deploys = {"store": DeployState(status="ok")}
    second = run_wake(store, port, now=_NOW + timedelta(seconds=30), webhook_quiet=True)
    assert second.quiet == "webhook-quiet"
    _assert_no_live_queries(port)
    assert "store#5" not in store.read()["issues"]


_LIVE_GATE = """
payload = json.load(sys.stdin)
if sys.argv[1] == "hooks":
    print(json.dumps({"hooks": ["release-gate"]}))
else:
    print(json.dumps({"requests": [{"kind": "release-status", "payload": {
        "repo": "store", "status": "ok", "link": "http://127.0.0.1/healthz"}}]}))
"""


def test_a_release_gate_extension_is_asked_on_a_quiet_wake(tmp_path):
    """The deploy-watch extension is how agentdeck observes its deploys: it must run quietly too."""
    folder = tmp_path / "checkout" / "poller" / "extensions"
    folder.mkdir(parents=True)
    (folder / "deploy_watch.py").write_text("import json, sys\n" + _LIVE_GATE)
    installed, problems = extensions.discover(
        {"deploy-watch": {}},
        [tmp_path / "checkout"],
        allowed={"deploy-watch": frozenset({Hook.RELEASE_GATE})},
        interpreter=sys.executable,
    )
    assert problems == []
    store = store_for(tmp_path, _deploying_state())
    port = FakePort()  # the host adapter reports nothing, so only the extension can finish it

    outcome = run_wake(
        store, port, now=_NOW, webhook_quiet=True, enabled_extensions=installed
    )

    assert outcome.quiet == "webhook-quiet"
    _assert_no_live_queries(port)
    assert "store#5" not in store.read()["issues"]


# --- the manifest knob ---------------------------------------------------------------


def _manifest_raw(**policy) -> dict:
    return {
        "contract": 1,
        "board": {"org": "org", "project_number": 1},
        "repos": [
            {
                "id": "store", "holds_issues": True, "holds_code": True,
                "verification": "agentdeck_job",
            }
        ],
        "policy": policy,
    }


def test_the_knob_defaults_on_at_300_seconds():
    report = Report()
    manifest = parse_manifest(_manifest_raw(), report)
    assert report.ok, report.render()
    assert manifest.webhook_skip_s == DEFAULT_WEBHOOK_SKIP_S == 900


def test_the_knob_is_configurable_and_zero_means_off():
    report = Report()
    assert parse_manifest(_manifest_raw(webhook_skip_s=600), report).webhook_skip_s == 600
    assert parse_manifest(_manifest_raw(webhook_skip_s=0), report).webhook_skip_s == 0
    assert report.ok, report.render()


def test_a_malformed_knob_is_reported_and_defaulted():
    for value in (-1, True, "fast", 4.5):
        report = Report()
        manifest = parse_manifest(_manifest_raw(webhook_skip_s=value), report)
        locations = [problem.location for problem in report.problems]
        assert "manifest.policy.webhook_skip_s" in locations, value
        assert manifest.webhook_skip_s == DEFAULT_WEBHOOK_SKIP_S, value


def test_the_quiet_hint_rides_the_documented_environment_variable():
    """The host script and the pinned artifact must agree on one spelling; a rename here
    silently degrades every wake to full polling, so the name is pinned."""
    assert WEBHOOK_QUIET_ENV == "AGENTDECK_POLLER_WEBHOOK_QUIET"


# --- what the observe hook is told (the extension's own reads) ------------------------


def test_an_observe_hook_is_told_whether_this_wake_asked_github_anything(tmp_path):
    """An extension's GitHub reads are its own, so the quiet skip could never reach them.

    `chatgpt_nudge` lists open pull requests once per repository per wake on the same
    credential the engine is carefully saving — a floor of roughly 920 REST calls an hour
    across this host, while the fleet sat at 4,900 against a 5,000 ceiling. The engine's
    port refuses its own reads on a quiet wake; the extension runs outside that port, so it
    has to be told the decision.
    """
    from agentdeck.poller_engine.cycle import _observe_context

    state = _quiet_state()
    assert _observe_context(state, extension="chatgpt-nudge", quiet=True)["quiet"] is True
    assert _observe_context(state, extension="chatgpt-nudge", quiet=False)["quiet"] is False
    # Absent means the wake was live: an older engine that never set it must not read as quiet.
    assert _observe_context(state, extension="chatgpt-nudge")["quiet"] is False
    assert _observe_context(state, extension="chatgpt-nudge")["works_router"] is False
    assert _observe_context(state, works_router=True)["works_router"] is True


def test_the_hook_is_told_the_engines_decision_not_the_hosts_hint(tmp_path):
    """The hint says the streams look silent; the engine still forces a full wake once
    `webhook_skip_s` has elapsed. An extension reading the raw hint would skip exactly the
    wake that exists to bound trusting an at-most-once channel."""
    stale = (_NOW - timedelta(seconds=DEFAULT_WEBHOOK_SKIP_S * 2)).isoformat()
    store = store_for(tmp_path, _quiet_state(stale))
    port = _live_port()

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    # The host said quiet, the ceiling overrode it, and the wake was live. `quiet` is falsy
    # rather than None on a live wake, which is exactly why the context passes `bool(...)`.
    assert not outcome.quiet
    assert port.scans > 0
