"""Slice 2 of #179: the hooks wired into the engine — validate, observe and health.

The command and release-gate hooks are still part of the protocol an extension speaks, but no
wake asks them any more: the engine holds no cards (#3732).
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, run_wake, store_for

from agentdeck.poller_engine import config, extensions, health
from agentdeck.poller_engine.extensions import Extension, Hook, Mode
from agentdeck.poller_engine.health import HealthReason

_HOOKS = frozenset(Hook)


def program(tmp_path, body: str, *, name="probe", hooks=_HOOKS, config_block=None) -> Extension:
    path = tmp_path / f"{name}.py"
    path.write_text("import json, sys\n" + body)
    return Extension(
        name=name,
        program=(sys.executable, str(path)),
        hooks=hooks,
        config=config_block or {},
    )


# --- validate-config folds into #177's collect-all report ---

_MANIFEST = f"""
contract = {config.CONTRACT}

[board]
org = "example"
project_number = 1

[[repos]]
id = "store"
holds_issues = true
holds_code = true
verification = "agentdeck_job"

[extensions.probe]
threshold = 3
"""

_OVERLAY = f"""
contract = {config.CONTRACT}

[paths]
state = "/tmp/state.json"

[checkouts]
store = "/tmp/store"
"""


def _write_config(tmp_path):
    manifest = tmp_path / "poller.toml"
    overlay = tmp_path / "overlay.toml"
    manifest.write_text(_MANIFEST)
    overlay.write_text(_OVERLAY)
    return manifest, overlay


def test_an_extension_checks_its_own_block_into_the_one_report(tmp_path):
    extension = program(
        tmp_path,
        'payload = json.load(sys.stdin)\n'
        'problems = []\n'
        'if payload["config"].get("threshold", 0) < 5:\n'
        '    problems.append({"location": "manifest.extensions.probe.threshold",\n'
        '                     "what": "must be at least 5", "fix": "raise it"})\n'
        'print(json.dumps({"problems": problems}))\n',
    )
    manifest, overlay = _write_config(tmp_path)
    draft = config.inspect(
        manifest, overlay, extension_validators=extensions.validators_for([extension])
    )
    assert not draft.ok
    assert "manifest.extensions.probe.threshold" in [p.location for p in draft.report.problems]


def test_a_happy_extension_leaves_the_report_clean(tmp_path):
    extension = program(tmp_path, 'json.load(sys.stdin)\nprint(json.dumps({}))\n')
    manifest, overlay = _write_config(tmp_path)
    draft = config.inspect(
        manifest, overlay, extension_validators=extensions.validators_for([extension])
    )
    assert draft.ok, draft.report.render()


def test_settings_nobody_could_check_are_a_problem_not_a_pass(tmp_path):
    extension = program(tmp_path, 'raise SystemExit(2)')
    manifest, overlay = _write_config(tmp_path)
    draft = config.inspect(
        manifest, overlay, extension_validators=extensions.validators_for([extension])
    )
    # Silently accepting settings nobody validated would let a broken project activate.
    problem = next(p for p in draft.report.problems if p.location == "manifest.extensions.probe")
    assert "could not be checked" in problem.what


def test_only_extensions_with_the_hook_are_asked_to_validate(tmp_path):
    extension = program(tmp_path, "raise SystemExit(1)", hooks=frozenset({Hook.HEALTH}))
    assert extensions.validators_for([extension]) == {}


# --- what an observe hook is told, and what it may leave with the engine ---

_ECHO_SETTLED = """
payload = json.load(sys.stdin)
print(json.dumps({
    "requests": [{"kind": "open-work", "payload": {
        "repo": "store", "key": "sentry-123", "title": "TypeError",
        "instruction": "fix the TypeError"}}],
    "health": {"settled": (payload.get("context") or {}).get("settled")},
}))
"""


def test_an_observe_hook_is_told_which_of_its_keys_are_already_decided(tmp_path):
    # Observe only: a health hook answering later would overwrite what observe reported.
    extension = program(tmp_path, _ECHO_SETTLED, hooks=frozenset({Hook.OBSERVE}))
    store = store_for(tmp_path)
    first = run_wake(
        store, FakePort(opened=[41]), now=_NOW, enabled_extensions=[extension],
        issue_repos={"store"},
    )
    assert first.report.extension_health["probe"]["settled"] == []

    second = run_wake(
        store, FakePort(), now=_NOW + timedelta(seconds=30), enabled_extensions=[extension],
        issue_repos={"store"},
    )
    # Its own bare key, not the engine's `ext:probe:` id: an extension that keeps asking for
    # work its source still reports needs to know what the engine would only drop (#1709).
    assert second.report.extension_health["probe"]["settled"] == ["sentry-123"]


_ECHO_CONTEXT = """
payload = json.load(sys.stdin)
print(json.dumps({"health": {"context": payload.get("context")}}))
"""


def test_an_observe_hook_is_handed_no_card_era_keys(tmp_path):
    """`cards` and `works_router` went with `chatgpt-nudge`, the last extension to read them."""
    extension = program(tmp_path, _ECHO_CONTEXT, hooks=frozenset({Hook.OBSERVE}))
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "In progress"}}})
    outcome = run_wake(store, FakePort(), now=_NOW, enabled_extensions=[extension])
    context = outcome.report.extension_health["probe"]["context"]
    assert set(context) == {"settled", "memory", "quiet"}
    assert context["quiet"] is False


# Counts its own wakes in the memory the engine keeps for it, and reports what it was handed.
_COUNTER = """
payload = json.load(sys.stdin)
memory = (payload.get("context") or {}).get("memory")
wakes = int((memory or {}).get("wakes") or 0) + 1
print(json.dumps({"memory": {"wakes": wakes}, "health": {"handed": memory}}))
"""


def test_an_observe_hook_gets_back_the_memory_it_left_on_the_last_live_wake(tmp_path):
    """#2691: an extension needing memory between wakes leaves it with the engine."""
    extension = program(tmp_path, _COUNTER, hooks=frozenset({Hook.OBSERVE}))
    store = store_for(tmp_path)
    first = run_wake(store, FakePort(), now=_NOW, enabled_extensions=[extension])
    assert first.report.extension_health["probe"]["handed"] == {}
    assert store.read()["extension_memory"] == {"probe": {"wakes": 1}}

    second = run_wake(
        store, FakePort(), now=_NOW + timedelta(seconds=30), enabled_extensions=[extension]
    )
    assert second.report.extension_health["probe"]["handed"] == {"wakes": 1}
    assert store.read()["extension_memory"] == {"probe": {"wakes": 2}}


def test_a_dry_run_or_a_failed_hook_never_changes_the_memory(tmp_path):
    extension = program(tmp_path, _COUNTER, hooks=frozenset({Hook.OBSERVE}))
    store = store_for(tmp_path)
    run_wake(store, FakePort(), now=_NOW, enabled_extensions=[extension])

    shadow = run_wake(
        store, FakePort(), now=_NOW + timedelta(seconds=30), enabled_extensions=[extension],
        mode=Mode.SHADOW,
    )
    assert shadow.report.extension_health["probe"]["handed"] == {"wakes": 1}
    assert store.read()["extension_memory"] == {"probe": {"wakes": 1}}

    broken = program(tmp_path, "raise SystemExit(1)", hooks=frozenset({Hook.OBSERVE}))
    run_wake(store, FakePort(), now=_NOW + timedelta(seconds=60), enabled_extensions=[broken])
    # Not seeing an answer is not an answer of "forget everything".
    assert store.read()["extension_memory"] == {"probe": {"wakes": 1}}


def test_memory_that_is_too_big_or_from_another_hook_is_refused(tmp_path):
    huge = program(
        tmp_path,
        "json.load(sys.stdin)\n"
        f"print(json.dumps({{'memory': {{'x': 'y' * {extensions.MAX_MEMORY_BYTES}}}}}))\n",
        name="huge",
    )
    response = extensions.invoke(huge, Hook.OBSERVE, context={})
    assert not response.ok and "bytes of memory" in response.degraded

    elsewhere = program(
        tmp_path, "json.load(sys.stdin)\nprint(json.dumps({'memory': {}}))\n", name="other"
    )
    response = extensions.invoke(elsewhere, Hook.HEALTH, context={})
    assert not response.ok and "memory" in response.degraded
    assert extensions.invoke(elsewhere, Hook.OBSERVE, context={}).memory == {}


_QUEUER = """
json.load(sys.stdin)
print(json.dumps({"requests": [{"kind": "queue-work", "payload": {
    "repo": "store", "number": 5, "key": "sentry-123", "instruction": "fix the TypeError"}}]}))
"""


def test_queue_work_is_refused_because_there_is_no_card_to_queue_it_on(tmp_path):
    store = store_for(tmp_path)
    outcome = run_wake(
        store, FakePort(), now=_NOW, enabled_extensions=[program(tmp_path, _QUEUER)],
        issue_repos={"store"},
    )
    state = store.read()
    assert "controls" not in state and "issues" not in state
    assert any("refused queue-work on store#5" in line for line in outcome.log)
    assert outcome.report.ok


_OPENER = """
json.load(sys.stdin)
print(json.dumps({"requests": [{"kind": "open-work", "payload": {
    "repo": "store", "key": "WEB-7", "title": "TypeError: boom",
    "instruction": "Triage and fix Sentry WEB-7", "url": "https://sentry.invalid/WEB-7"}}]}))
"""


def test_a_proposal_is_reported_but_never_opened_in_shadow_mode(tmp_path):
    store = store_for(tmp_path)
    port = FakePort(opened=[41])
    run_wake(
        store, port, now=_NOW, enabled_extensions=[program(tmp_path, _OPENER)],
        mode=Mode.SHADOW, issue_repos={"store"},
    )
    assert port.opened_calls == []
    assert "opened" not in store.read()


# --- the ceiling on proposed work (#371) ---

_MANY = """
json.load(sys.stdin)
print(json.dumps({"requests": [
    {"kind": "open-work", "payload": {"repo": "store", "key": "sentry-%d" % n,
     "title": "boom %d" % n, "instruction": "fix %d" % n}} for n in range(1, 6)
]}))
"""


def _opened(store) -> int:
    return len(store.read().get("opened") or {})


def _many_wake(store, tmp_path, now, **kwargs):
    return run_wake(
        store, FakePort(opened=list(range(41, 51))), now=now,
        enabled_extensions=[program(tmp_path, _MANY)], issue_repos={"store"}, **kwargs,
    )


def test_the_daily_cap_stops_honoring_proposals_once_it_is_spent(tmp_path):
    store = store_for(tmp_path)
    outcome = _many_wake(store, tmp_path, _NOW, proposal_daily_cap=2)
    # The extension asked for five; the engine honored the two the project's budget allows.
    assert _opened(store) == 2
    assert any("daily cap (2) reached" in line for line in outcome.log)


def test_a_staged_manual_proposal_waits_under_a_spent_daily_cap(tmp_path):
    from agentdeck.poller_engine.cycle import stage_proposals

    store = store_for(tmp_path)
    with store.transaction() as state:
        state["proposals"] = {"date": _NOW.date().isoformat(), "honored": 1}
        assert stage_proposals(
            state,
            "probe",
            (
                extensions.Request(
                    extensions.RequestKind.OPEN_WORK,
                    {"repo": "store", "key": "MANUAL-1", "title": "Manual",
                     "instruction": "fix it"},
                ),
            ),
        ) == 1

    port = FakePort(opened=[77])
    outcome = run_wake(
        store, port, now=_NOW, proposal_daily_cap=1, issue_repos={"store"}
    )

    assert port.opened_calls == []
    assert store.read()["proposal_inbox"][0]["payload"]["key"] == "MANUAL-1"
    assert any("manual requests wait" in line for line in outcome.log)


def test_a_staged_manual_proposal_uses_the_ordinary_open_path(tmp_path):
    from agentdeck.poller_engine.cycle import stage_proposals

    store = store_for(tmp_path)
    with store.transaction() as state:
        stage_proposals(
            state,
            "probe",
            (
                extensions.Request(
                    extensions.RequestKind.OPEN_WORK,
                    {"repo": "store", "key": "MANUAL-1", "title": "Manual",
                     "instruction": "fix it"},
                ),
                extensions.Request(
                    extensions.RequestKind.QUEUE_WORK,
                    {"repo": "store", "number": 5, "key": "Q-1", "instruction": "fix it"},
                ),
            ),
        )

    port = FakePort(opened=[77])
    run_wake(store, port, now=_NOW, proposal_daily_cap=2, issue_repos={"store"})

    state = store.read()
    assert len(port.opened_calls) == 1
    assert "proposal_inbox" not in state
    assert state["opened"] == {"ext:probe:MANUAL-1": {"repo": "store", "number": 77}}


def test_a_shadow_wake_never_drains_the_manual_proposal_inbox(tmp_path):
    from agentdeck.poller_engine.cycle import stage_proposals

    store = store_for(tmp_path)
    with store.transaction() as state:
        stage_proposals(
            state,
            "probe",
            (
                extensions.Request(
                    extensions.RequestKind.OPEN_WORK,
                    {"repo": "store", "key": "MANUAL-1", "title": "Manual",
                     "instruction": "fix it"},
                ),
            ),
        )
    port = FakePort(opened=[77])

    run_wake(store, port, now=_NOW, mode=Mode.SHADOW, issue_repos={"store"})

    assert port.opened_calls == []
    assert store.read()["proposal_inbox"][0]["payload"]["key"] == "MANUAL-1"


def test_a_permanently_refused_manual_proposal_is_dropped(tmp_path):
    from agentdeck.poller_engine.cycle import stage_proposals

    store = store_for(tmp_path)
    with store.transaction() as state:
        stage_proposals(
            state,
            "probe",
            (
                extensions.Request(
                    extensions.RequestKind.OPEN_WORK,
                    {"repo": "not-allowed", "key": "MANUAL-1", "title": "Manual"},
                ),
            ),
        )
        # An inbox an older engine staged may still hold queue-work, which can never be honored.
        state["proposal_inbox"].append({
            "extension": "probe", "kind": "queue-work",
            "payload": {"repo": "store", "number": 5, "key": "Q-1", "instruction": "x"},
        })

    outcome = run_wake(store, FakePort(), now=_NOW, issue_repos={"store"})

    assert "proposal_inbox" not in store.read()
    assert sum("dropped malformed or refused" in line for line in outcome.log) == 2


def test_a_manual_open_proposal_without_a_stable_key_is_never_staged(tmp_path):
    from agentdeck.poller_engine.cycle import stage_proposals

    store = store_for(tmp_path)
    with store.transaction() as state:
        added = stage_proposals(
            state,
            "probe",
            (
                extensions.Request(
                    extensions.RequestKind.OPEN_WORK,
                    {"repo": "store", "title": "Manual", "instruction": "fix it"},
                ),
            ),
        )

    assert added == 0
    assert "proposal_inbox" not in store.read()


def test_no_daily_cap_means_the_project_named_no_ceiling(tmp_path):
    store = store_for(tmp_path)
    _many_wake(store, tmp_path, _NOW)
    # A project that never named a ceiling keeps behaving exactly as it did.
    assert _opened(store) == 5


def test_a_spent_daily_budget_survives_a_restart(tmp_path):
    _many_wake(store_for(tmp_path), tmp_path, _NOW, proposal_daily_cap=2)

    # A fresh store over the same durable state: a budget that forgets what it spent on every
    # redeploy is not a budget guard at all.
    reopened = store_for(tmp_path)
    _many_wake(reopened, tmp_path, _NOW + timedelta(minutes=5), proposal_daily_cap=2)
    assert _opened(reopened) == 2


def test_the_day_turning_refills_the_proposal_budget(tmp_path):
    store = store_for(tmp_path)
    _many_wake(store, tmp_path, _NOW, proposal_daily_cap=2)
    _many_wake(store, tmp_path, _NOW + timedelta(days=1), proposal_daily_cap=2)
    # Two more, not two total: what the cap refused was deferred, not dropped.
    assert _opened(store) == 4


def test_a_malformed_proposal_is_ignored_without_stopping_the_wake(tmp_path):
    extension = program(
        tmp_path,
        'json.load(sys.stdin)\nprint(json.dumps({"requests": [{"kind": "open-work",'
        ' "payload": {"repo": "store"}}]}))\n',
    )
    store = store_for(tmp_path)
    outcome = run_wake(
        store, FakePort(), now=_NOW, enabled_extensions=[extension], issue_repos={"store"}
    )
    assert "opened" not in store.read()
    assert outcome.verdict.state is not None  # the wake still completed and reported


# --- declared command words (the protocol; no wake routes them any more) ---

_COMMANDER = """
json.load(sys.stdin)
print(json.dumps({"commands": [{"word": "/shot", "tier": "work", "help": "screenshots"}]}))
"""


def test_two_extensions_cannot_claim_the_same_word(tmp_path):
    first = program(tmp_path, _COMMANDER, name="first")
    second = program(tmp_path, _COMMANDER, name="second")
    table = extensions.declared_commands([first, second])
    assert table["/shot"][0].name == "first"


def test_a_declared_word_must_be_a_single_command_word(tmp_path):
    extension = program(
        tmp_path,
        'json.load(sys.stdin)\nprint(json.dumps({"commands": [{"word": "take a shot"}]}))\n',
    )
    assert extensions.declared_commands([extension]) == {}


# --- the release-gate protocol still normalizes what an extension reports ---

_GATE = """
payload = json.load(sys.stdin)
builds = payload["context"].get("builds", {})
print(json.dumps({"requests": [
    {"kind": "release-status", "payload": {"repo": repo, "status": "ok", "link": "https://ci/1"}}
    for repo in builds
]}))
"""


def _release_states(extension):
    return extensions.release_states([extension], context={"builds": {"store": {"sha": "abc"}}})


def test_a_release_gate_reports_normalized_states_for_the_watched_builds(tmp_path):
    extension = program(tmp_path, _GATE)
    states, degraded = _release_states(extension)
    assert states == {"store": {"status": "ok", "link": "https://ci/1"}}
    assert degraded == []


def test_a_gate_that_speaks_its_vendors_language_contributes_nothing(tmp_path):
    extension = program(
        tmp_path,
        'json.load(sys.stdin)\nprint(json.dumps({"requests": [{"kind": "release-status",'
        ' "payload": {"repo": "store", "status": "killed"}}]}))\n',
    )
    states, degraded = _release_states(extension)
    # An unrecognized state leaves the repo pending: the release waits rather than guessing.
    assert states == {}
    assert degraded == []


def test_a_skipped_gate_leaves_the_deploy_pending_and_says_which_one(tmp_path):
    extension = program(tmp_path, "raise SystemExit(1)")
    states, degraded = _release_states(extension)
    assert states == {}
    assert degraded == ["probe"]


# --- health ---

def test_extension_health_lands_in_the_wake_s_record(tmp_path):
    extension = program(
        tmp_path,
        'json.load(sys.stdin)\nprint(json.dumps({"health": {"state": "ok", "watching": 2}}))\n',
    )
    store = store_for(tmp_path)
    outcome = run_wake(store, FakePort(), now=_NOW, enabled_extensions=[extension])
    recorded = health.record(store.read())["last_report"]["extensions"]
    assert recorded["probe"]["state"] == "ok"
    assert recorded["probe"]["watching"] == 2
    assert outcome.verdict.state.value == "ok"


def test_a_skipped_extension_degrades_the_instance_without_failing_the_cycle(tmp_path):
    extension = program(tmp_path, "raise SystemExit(9)", hooks=frozenset({Hook.HEALTH}))
    store = store_for(tmp_path)
    outcome = run_wake(store, FakePort(), now=_NOW, enabled_extensions=[extension])
    assert outcome.halted is None                       # the wake ran to completion
    assert outcome.report.extension_failures == 1
    assert HealthReason.EXTENSION_DEGRADED in outcome.verdict.reasons
    assert outcome.verdict.state.value == "degraded"    # degraded, never failing (#112)


def test_a_wake_with_no_extensions_asks_nothing_and_stays_ok(tmp_path):
    store = store_for(tmp_path)
    outcome = run_wake(store, FakePort(), now=_NOW)
    assert outcome.report.extension_health == {}
    assert outcome.verdict.state.value == "ok"


# --- observe proposes issues, which the engine opens exactly once (#393) ---

def test_a_proposal_gets_its_labelled_issue_opened(tmp_path):
    store = store_for(tmp_path)
    port = FakePort(opened=[41])
    outcome = run_wake(
        store, port, now=_NOW,
        enabled_extensions=[program(tmp_path, _OPENER)], issue_repos={"store"},
    )
    assert [call["repo"] for call in port.opened_calls] == ["store"]
    assert port.opened_calls[0]["title"] == "TypeError: boom"
    # The marker travels in the body: it is the only way a repeat can recognize its own work.
    assert "<!-- poller-opened: ext:probe:WEB-7 -->" in port.opened_calls[0]["body"]
    assert "Triage and fix Sentry WEB-7" in port.opened_calls[0]["body"]
    # From here the deck starts its Work from the label; the engine queues nothing.
    state = store.read()
    assert state["opened"]["ext:probe:WEB-7"] == {"repo": "store", "number": 41}
    assert "controls" not in state and "issues" not in state
    assert any("probe opened store#41 for WEB-7" in line for line in outcome.log)


def test_the_same_error_never_opens_a_second_card(tmp_path):
    extension = program(tmp_path, _OPENER)
    store = store_for(tmp_path)
    first = FakePort(opened=[41])
    run_wake(store, first, now=_NOW, enabled_extensions=[extension], issue_repos={"store"})

    second = FakePort(opened=[42])
    run_wake(
        store, second, now=_NOW + timedelta(minutes=1),
        enabled_extensions=[extension], issue_repos={"store"},
    )

    assert second.opened_calls == [], "a second issue for one error is the failure mode"
    assert list(store.read()["opened"]) == ["ext:probe:WEB-7"]


def test_a_creation_interrupted_before_it_was_recorded_looks_before_creating_again(tmp_path):
    """The crash window: the issue exists on GitHub but durable state does not know its number.

    Creating again here would file a duplicate, which is the one outcome worse than doing nothing.
    """
    extension = program(tmp_path, _OPENER)
    store = store_for(tmp_path)
    # A port whose creation "fails" leaves the intent behind, exactly as a crash would.
    stalled = FakePort(opened=[])
    run_wake(store, stalled, now=_NOW, enabled_extensions=[extension], issue_repos={"store"})
    assert "ext:probe:WEB-7" in store.read()["opening"]
    assert "opened" not in store.read() or not store.read()["opened"]

    # Next wake: the issue is there after all, carrying the marker.
    recovered = FakePort(
        opened=[99],
        marked={"<!-- poller-opened: ext:probe:WEB-7 -->": 41},
    )
    run_wake(
        store, recovered, now=_NOW + timedelta(minutes=1),
        enabled_extensions=[extension], issue_repos={"store"},
    )

    assert recovered.opened_calls == [], "it must adopt the issue it already filed"
    assert store.read()["opened"]["ext:probe:WEB-7"]["number"] == 41
    assert "opening" not in store.read()


def test_an_unreadable_lookup_defers_rather_than_risking_a_duplicate(tmp_path):
    """"Could not look" and "is not there" differ by exactly one duplicated issue."""
    extension = program(tmp_path, _OPENER)
    store = store_for(tmp_path)
    run_wake(
        store, FakePort(opened=[]), now=_NOW,
        enabled_extensions=[extension], issue_repos={"store"},
    )

    blind = FakePort(opened=[99], markers_readable=False)
    run_wake(
        store, blind, now=_NOW + timedelta(minutes=1),
        enabled_extensions=[extension], issue_repos={"store"},
    )

    assert blind.marker_lookups, "it must at least try to look"
    assert blind.opened_calls == [], "an unreadable lookup must not license a creation"
    # The intent survives, so a later wake can still resolve it.
    assert "ext:probe:WEB-7" in store.read()["opening"]


def test_opening_a_card_counts_against_the_daily_ceiling(tmp_path):
    """With one issue per error, "proposals per day" and "issues per day" are the same number —
    and it is the second one an operator feels."""
    many = """
json.load(sys.stdin)
print(json.dumps({"requests": [
    {"kind": "open-work", "payload": {"repo": "store", "key": "WEB-%d" % n,
     "title": "boom %d" % n, "instruction": "fix %d" % n}} for n in range(1, 6)
]}))
"""
    store = store_for(tmp_path)
    port = FakePort(opened=[41, 42, 43, 44, 45])
    outcome = run_wake(
        store, port, now=_NOW, enabled_extensions=[program(tmp_path, many)],
        proposal_daily_cap=2, issue_repos={"store"},
    )
    assert len(port.opened_calls) == 2, "the ceiling must bound the issues opened"
    assert any("daily cap (2) reached" in line for line in outcome.log)


def test_an_issue_is_never_opened_in_a_repo_the_project_does_not_hold_issues_in(tmp_path):
    """Opening an issue is a real GitHub write, so where one may appear is the project's decision.

    A mistyped `project_to_repo` value would otherwise file a labelled issue into an unrelated
    repository in the same org — which the deck would then start a Work on.
    """
    store = store_for(tmp_path)
    port = FakePort(opened=[41])
    outcome = run_wake(
        store, port, now=_NOW,
        enabled_extensions=[program(tmp_path, _OPENER)],
        issue_repos={"abacus"},          # the proposal names "store"
    )
    assert port.opened_calls == []
    assert "opened" not in store.read()
    assert any("refused to open an issue in 'store'" in line for line in outcome.log)


def test_naming_no_issue_repos_permits_no_openings(tmp_path):
    """A project that has not said where issues live has not consented to any being created."""
    store = store_for(tmp_path)
    port = FakePort(opened=[41])
    run_wake(store, port, now=_NOW, enabled_extensions=[program(tmp_path, _OPENER)])
    assert port.opened_calls == []


_SPEC_OPENER = """
json.load(sys.stdin)
print(json.dumps({"requests": [{"kind": "open-work", "payload": {
    "repo": "store", "key": "reports-1", "title": "Singer reports", "spec_only": True,
    "instruction": "Write a spec for these reports"}}]}))
"""


def test_a_spec_only_proposal_opens_under_the_label(tmp_path):
    """#2874: the issue is labelled spec-only in the creating write, so its Work writes a spec."""
    store = store_for(tmp_path)
    port = FakePort(opened=[41])
    run_wake(
        store, port, now=_NOW, enabled_extensions=[program(tmp_path, _SPEC_OPENER)],
        issue_repos={"store"}, spec_only_label="spec-only",
    )
    assert port.opened_calls[0]["labels"] == ("spec-only",)
    assert store.read()["opened"]["ext:probe:reports-1"]["number"] == 41


def test_a_spec_only_proposal_is_refused_where_the_project_names_no_label(tmp_path):
    """Opened without the label, the issue would be built without anyone approving a spec."""
    store = store_for(tmp_path)
    port = FakePort(opened=[41])
    outcome = run_wake(
        store, port, now=_NOW, enabled_extensions=[program(tmp_path, _SPEC_OPENER)],
        issue_repos={"store"},
    )
    assert port.opened_calls == []
    assert "opened" not in store.read()
    assert any("refused a spec-only issue" in line for line in outcome.log)


def test_an_ordinary_proposal_opens_with_no_extra_label(tmp_path):
    store = store_for(tmp_path)
    port = FakePort(opened=[41])
    run_wake(
        store, port, now=_NOW, enabled_extensions=[program(tmp_path, _OPENER)],
        issue_repos={"store"}, spec_only_label="spec-only",
    )
    assert "labels" not in port.opened_calls[0]


def test_the_real_sentry_extension_opens_real_issues_through_a_real_cycle(tmp_path):
    """The one test where both halves of #393 meet.

    `sentry_autofix.py` and the engine's open-work path were changed together and everything else
    exercises them apart: the extension against `invoke`, the engine against a synthetic program.
    A payload mismatch between them would pass both and fail in production.
    """
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    issues = [{"id": "1", "shortId": "WEB-9", "title": "TypeError: boom",
               "permalink": "https://sentry.invalid/WEB-9"}]
    body = _json.dumps(issues).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    # A short poll, so ``shutdown`` returns at once rather than after the default 0.5 s.
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    try:
        token = tmp_path / "sentry-token"
        token.write_text("secret\n")
        real = Extension(
            name="sentry-autofix",
            program=(
                sys.executable,
                str(Path(__file__).resolve().parent.parent
                    / "poller" / "extensions" / "sentry_autofix.py"),
            ),
            hooks=frozenset({Hook.OBSERVE}),
            config={
                "host": f"http://127.0.0.1:{server.server_address[1]}",
                "org": "example",
                "token_file": str(token),
                "projects": [{"id": "web", "repo": "store"}],
            },
        )
        store = store_for(tmp_path)
        port = FakePort(opened=[77])
        outcome = run_wake(
            store, port, now=_NOW, enabled_extensions=[real], issue_repos={"store"}
        )
    finally:
        server.shutdown()

    assert [call["repo"] for call in port.opened_calls] == ["store"]
    assert port.opened_calls[0]["title"].startswith("WEB-9: ")
    assert "<!-- poller-opened: ext:sentry-autofix:WEB-9 -->" in port.opened_calls[0]["body"]
    assert store.read()["opened"]["ext:sentry-autofix:WEB-9"]["number"] == 77
    assert outcome.report.ok


def test_a_proposal_is_never_acknowledged_with_a_reaction(tmp_path):
    """#370: an extension's proposal has no comment to react to, so reacting fails the wake."""
    port = FakePort(opened=[41])
    outcome = run_wake(
        store_for(tmp_path), port, now=_NOW,
        enabled_extensions=[program(tmp_path, _OPENER)], issue_repos={"store"},
    )

    assert outcome.report.errors == []
    assert outcome.report.ok


