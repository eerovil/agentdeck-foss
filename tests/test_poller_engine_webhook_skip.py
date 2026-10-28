"""#1673 builds #1631: the webhook-quiet skip of a timed wake's live GitHub queries.

The engine owns the policy knob and the last-full-wake ceiling; the host's two proofs
arrive collapsed into one ``webhook_quiet`` hint. Every test here holds the decision to
its one safety property: any condition that cannot be proved runs the full wake, so the
failure mode of the whole feature is always more polling, never less.

The second property, and the one #1673 stated as an acceptance condition, is that what
the skip drops is the *live GitHub queries* and nothing else. The wake still runs and still
reports; only discovery (since #3732, the scheduled-task sweep and bug-report filing) waits for
the ceiling, because discovery is the only thing the webhook streams are evidence about.
"""

from __future__ import annotations

from datetime import timedelta

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, run_wake, store_for

from agentdeck.poller_engine.config import (
    DEFAULT_WEBHOOK_SKIP_S,
    Report,
    parse_manifest,
)
from agentdeck.poller_engine.cycle import WEBHOOK_QUIET_ENV

RECENT = (_NOW - timedelta(seconds=30)).isoformat()


def _quiet_state(stamp: str = RECENT) -> dict:
    return {"last_live_wake": stamp}


def _live_port() -> FakePort:
    return FakePort(scheduled={})


def _assert_no_live_queries(port: FakePort) -> None:
    assert port.scheduled_reads == 0


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
    # The stamp is the ceiling on trusting webhooks, so a skipped wake never renews it:
    # 300 seconds after the last *full* wake the skip runs out however quiet it stayed.
    assert state["last_live_wake"] == RECENT


def test_without_the_host_hint_the_wake_is_full(tmp_path):
    store = store_for(tmp_path, _quiet_state())
    port = _live_port()

    outcome = run_wake(store, port, now=_NOW)  # webhook_quiet defaults to False

    assert outcome.halted is None
    assert port.scheduled_reads > 0


def test_the_knob_at_zero_disables_skipping_entirely(tmp_path):
    store = store_for(tmp_path, _quiet_state())
    port = _live_port()

    outcome = run_wake(store, port, now=_NOW, webhook_skip_s=0, webhook_quiet=True)

    assert outcome.halted is None
    assert port.scheduled_reads > 0


def test_a_stale_last_full_wake_forces_a_full_wake(tmp_path):
    old = (_NOW - timedelta(seconds=DEFAULT_WEBHOOK_SKIP_S)).isoformat()
    store = store_for(tmp_path, _quiet_state(old))
    port = _live_port()

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.halted is None
    assert port.scheduled_reads > 0


def test_no_recorded_full_wake_is_no_evidence(tmp_path):
    state = _quiet_state()
    del state["last_live_wake"]
    store = store_for(tmp_path, state)
    port = _live_port()

    outcome = run_wake(store, port, now=_NOW, webhook_quiet=True)

    assert outcome.halted is None
    assert port.scheduled_reads > 0


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
        assert port.scheduled_reads > 0, stamp


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
    store = store_for(tmp_path)

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


# --- what the skip must not hold -----------------------------------------------------


def test_a_quiet_wake_is_not_reported_as_a_failed_github_read(tmp_path):
    """Declining to ask is not the same fact as asking and failing. A quiet wake that
    counted scan failures would make a healthy instance look like a broken one within
    minutes, because most wakes are quiet."""
    store = store_for(tmp_path, _quiet_state())

    outcome = run_wake(store, _live_port(), now=_NOW, webhook_quiet=True)

    assert outcome.quiet == "webhook-quiet"
    assert outcome.report.scan_failures == 0
    assert outcome.report.errors == []


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

    `chatgpt_nudge` (retired in #3741) listed open pull requests once per repository per wake
    on the same credential the engine is carefully saving — a floor of roughly 920 REST calls
    an hour across this host. The engine's port refuses its own reads on a quiet wake; an
    extension runs outside that port, so it has to be told the decision.
    """
    from agentdeck.poller_engine.cycle import _observe_context

    state = _quiet_state()
    assert _observe_context(state, extension="sentry-autofix", quiet=True)["quiet"] is True
    assert _observe_context(state, extension="sentry-autofix", quiet=False)["quiet"] is False
    # Absent means the wake was live: an older engine that never set it must not read as quiet.
    assert _observe_context(state, extension="sentry-autofix")["quiet"] is False


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
    assert port.scheduled_reads > 0
