"""Slice 2 of #179: the hooks wired into the engine — validate, observe, command, gate, health."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, payloads, run_wake, store_for

from agentdeck.poller_engine import config, extensions, health, queue
from agentdeck.poller_engine.cycle import extension_release_states
from agentdeck.poller_engine.extensions import Extension, Hook, Mode
from agentdeck.poller_engine.health import HealthReason
from agentdeck.poller_engine.ports import EffectKind
from agentdeck.poller_engine.reconcile import CardSnapshot

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


# --- observe proposes work, which enters the ordinary queue ---

_PROPOSER = """
json.load(sys.stdin)
print(json.dumps({"requests": [{"kind": "queue-work", "payload": {
    "repo": "store", "number": 5, "key": "sentry-123", "instruction": "fix the TypeError"}}]}))
"""


def test_a_proposal_becomes_an_ordinary_queued_instruction(tmp_path):
    extension = program(tmp_path, _PROPOSER)
    store = store_for(tmp_path)
    outcome = run_wake(
        store, FakePort(), now=_NOW, enabled_extensions=[extension]
    )
    state = store.read()
    control = state["controls"]["ext:probe:sentry-123"]
    assert control["kind"] == "claude"
    assert control["author"] == "extension:probe"
    # It runs through the same dispatch path as a human's instruction.
    assert len(outcome.directives) == 1
    assert payloads(outcome.directives[0]) == ["fix the TypeError"]


def test_the_same_proposal_is_never_queued_twice(tmp_path):
    extension = program(tmp_path, _PROPOSER)
    store = store_for(tmp_path)
    run_wake(store, FakePort(), now=_NOW, enabled_extensions=[extension])
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    second = run_wake(
        store, port, now=_NOW + timedelta(seconds=30), enabled_extensions=[extension]
    )
    assert len(queue.controls(store.read())) == 1
    assert second.report.commands_ingested == 0


_ECHO_SETTLED = """
payload = json.load(sys.stdin)
print(json.dumps({
    "requests": [{"kind": "queue-work", "payload": {
        "repo": "store", "number": 5, "key": "sentry-123",
        "instruction": "fix the TypeError"}}],
    "health": {"settled": (payload.get("context") or {}).get("settled")},
}))
"""


def test_an_observe_hook_is_told_which_of_its_keys_are_already_decided(tmp_path):
    # Observe only: a health hook answering later would overwrite what observe reported.
    extension = program(tmp_path, _ECHO_SETTLED, hooks=frozenset({Hook.OBSERVE}))
    store = store_for(tmp_path)
    first = run_wake(store, FakePort(), now=_NOW, enabled_extensions=[extension])
    assert first.report.extension_health["probe"]["settled"] == []

    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    second = run_wake(
        store, port, now=_NOW + timedelta(seconds=30), enabled_extensions=[extension]
    )
    # Its own bare key, not the engine's `ext:probe:` id: an extension that keeps asking for
    # work its source still reports needs to know what the engine would only drop (#1709).
    assert second.report.extension_health["probe"]["settled"] == ["sentry-123"]


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


def test_a_proposal_is_reported_but_never_queued_in_shadow_mode(tmp_path):
    extension = program(tmp_path, _PROPOSER)
    store = store_for(tmp_path)
    outcome = run_wake(
        store, FakePort(), now=_NOW, enabled_extensions=[extension], mode=Mode.SHADOW
    )
    assert queue.controls(store.read()) == {}
    assert outcome.directives == []


# --- the ceilings on proposed work (#371) ---

_MANY = """
json.load(sys.stdin)
print(json.dumps({"requests": [
    {"kind": "queue-work", "payload": {"repo": "store", "number": n, "key": "sentry-%d" % n,
     "instruction": "fix %d" % n}} for n in range(1, 6)
]}))
"""


def test_the_daily_cap_stops_honoring_proposals_once_it_is_spent(tmp_path):
    store = store_for(tmp_path)
    outcome = run_wake(
        store,
        FakePort(),
        now=_NOW,
        enabled_extensions=[program(tmp_path, _MANY)],
        proposal_daily_cap=2,
    )
    # The extension asked for five; the engine honored the two the project's budget allows.
    assert len(queue.controls(store.read())) == 2
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


def test_a_staged_manual_proposal_uses_the_ordinary_open_and_queue_path(tmp_path):
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
    run_wake(store, port, now=_NOW, proposal_daily_cap=2, issue_repos={"store"})

    state = store.read()
    assert len(port.opened_calls) == 1
    assert "proposal_inbox" not in state
    assert state["opened"]["ext:probe:MANUAL-1"]["number"] == 77
    assert state["controls"]["ext:probe:MANUAL-1"]["number"] == 77


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

    outcome = run_wake(store, FakePort(), now=_NOW, issue_repos={"store"})

    assert "proposal_inbox" not in store.read()
    assert any("dropped malformed or refused" in line for line in outcome.log)


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
    run_wake(store, FakePort(), now=_NOW, enabled_extensions=[program(tmp_path, _MANY)])
    # A project that never named a ceiling keeps behaving exactly as it did.
    assert len(queue.controls(store.read())) == 5


def test_a_spent_daily_budget_survives_a_restart(tmp_path):
    extension = program(tmp_path, _MANY)
    first = store_for(tmp_path)
    run_wake(first, FakePort(), now=_NOW, enabled_extensions=[extension], proposal_daily_cap=2)

    # A fresh store over the same durable state: a budget that forgets what it spent on every
    # redeploy is not a budget guard at all.
    reopened = store_for(tmp_path)
    run_wake(
        reopened,
        FakePort(),
        now=_NOW + timedelta(minutes=5),
        enabled_extensions=[extension],
        proposal_daily_cap=2,
    )
    assert len(queue.controls(reopened.read())) == 2


def test_the_day_turning_refills_the_proposal_budget(tmp_path):
    extension = program(tmp_path, _MANY)
    store = store_for(tmp_path)
    run_wake(store, FakePort(), now=_NOW, enabled_extensions=[extension], proposal_daily_cap=2)
    run_wake(
        store,
        FakePort(),
        now=_NOW + timedelta(days=1),
        enabled_extensions=[extension],
        proposal_daily_cap=2,
    )
    # Two more, not two total: what the cap refused was deferred, not dropped.
    assert len(queue.controls(store.read())) == 4


def test_the_proposal_worker_cap_leaves_slots_for_people(tmp_path):
    store = store_for(tmp_path)
    outcome = run_wake(
        store,
        FakePort(),
        now=_NOW,
        enabled_extensions=[program(tmp_path, _MANY)],
        cap=10,
        proposal_worker_cap=2,
    )
    # Five proposals queued, but only two may hold a worker at once, so an autofix flood
    # cannot occupy every slot the project has.
    assert len(queue.controls(store.read())) == 5
    assert len(outcome.directives) == 2
    assert any("proposal worker cap 2 reached" in line for line in outcome.log)


def test_a_batch_a_human_contributed_to_is_not_throttled_as_a_proposal(tmp_path):
    extension = program(
        tmp_path,
        'json.load(sys.stdin)\nprint(json.dumps({"requests": [{"kind": "queue-work",'
        ' "payload": {"repo": "store", "number": 5, "key": "sentry-1",'
        ' "instruction": "fix the TypeError"}}]}))\n',
    )
    port = FakePort(comments=[comment("c1", "/claude please look at this too")])
    outcome = run_wake(
        store_for(tmp_path),
        port,
        now=_NOW,
        enabled_extensions=[extension],
        proposal_worker_cap=0,  # no slot for proposal-only work at all, to leave no doubt
    )
    # The person asked too, so this is their request; an autofix budget must not throttle it.
    assert len(outcome.directives) == 1
    assert "fix the TypeError" in payloads(outcome.directives[0])
    assert not any("proposal worker cap" in line for line in outcome.log)


def test_a_malformed_proposal_is_ignored_without_stopping_the_wake(tmp_path):
    extension = program(
        tmp_path,
        'json.load(sys.stdin)\nprint(json.dumps({"requests": [{"kind": "queue-work",'
        ' "payload": {"repo": "store"}}]}))\n',
    )
    store = store_for(tmp_path)
    outcome = run_wake(store, FakePort(), now=_NOW, enabled_extensions=[extension])
    assert queue.controls(store.read()) == {}
    assert outcome.verdict.state is not None  # the wake still completed and reported


# --- declared commands are recognized only when their extension is enabled ---

_COMMANDER = """
payload = json.load(sys.stdin)
if payload["context"].get("declare"):
    print(json.dumps({"commands": [{"word": "/shot", "tier": "work", "help": "screenshots"}]}))
else:
    print(json.dumps({"requests": [
        {"kind": "queue-work", "payload": {"key": "shot-1", "instruction": "take a screenshot"}},
        {"kind": "comment", "payload": {"body": "on it"}},
    ]}))
"""


def _shot_comment(cid=7):
    return {
        "id": cid,
        "body": "/shot /checkout",
        "user": {"login": "eero", "type": "User"},
        "issue_url": "https://api.github.com/repos/org/store/issues/5",
        "html_url": "https://example/7",
        "created_at": "2026-07-25T11:59:00Z",
    }


def test_a_declared_command_is_routed_to_its_extension(tmp_path):
    extension = program(tmp_path, _COMMANDER)
    store = store_for(tmp_path)
    port = FakePort(comments=[_shot_comment()])
    outcome = run_wake(store, port, now=_NOW, enabled_extensions=[extension])
    state = store.read()
    assert state["controls"]["7"]["kind"] == "extension"
    # The command is finished once the extension answered: its follow-up is ordinary queued
    # work with its own identity, so leaving this in-flight would re-run it every wake.
    assert state["controls"]["7"]["status"] == "done"
    # Its proposal entered the ordinary queue and its comment went out as an effect.
    assert "ext:probe:shot-1" in state["controls"]
    assert any("on it" in str(effect.value) for effect in outcome.effects)


def test_an_unknown_command_word_is_not_a_command_at_all(tmp_path):
    store = store_for(tmp_path)
    port = FakePort(comments=[_shot_comment()])
    run_wake(store, port, now=_NOW)  # no extensions enabled
    # A project without the extension treats the word as prose, not as a command it cannot serve.
    assert queue.controls(store.read()) == {}


def test_a_command_whose_extension_breaks_is_retried_next_wake(tmp_path):
    extension = program(
        tmp_path,
        'payload = json.load(sys.stdin)\n'
        'if payload["context"].get("declare"):\n'
        '    print(json.dumps({"commands": [{"word": "/shot"}]}))\n'
        'else:\n'
        '    raise SystemExit(5)\n',
    )
    store = store_for(tmp_path)
    port = FakePort(comments=[_shot_comment()])
    outcome = run_wake(store, port, now=_NOW, enabled_extensions=[extension])
    # Left unprocessed rather than lost, and the extension is marked degraded.
    assert store.read()["controls"]["7"]["status"] == "new"
    assert outcome.report.extension_failures >= 1
    assert any("probe.command" in line for line in outcome.log)


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


# --- release-gate feeds the guarded release's normalized states ---

_GATE = """
payload = json.load(sys.stdin)
builds = payload["context"].get("builds", {})
print(json.dumps({"requests": [
    {"kind": "release-status", "payload": {"repo": repo, "status": "ok", "link": "https://ci/1"}}
    for repo in builds
]}))
"""


def test_a_release_gate_reports_normalized_states_for_the_watched_builds(tmp_path):
    extension = program(tmp_path, _GATE)
    states, degraded = extension_release_states(
        [extension], builds={"store": {"sha": "abc"}}
    )
    assert states == {"store": {"status": "ok", "link": "https://ci/1"}}
    assert degraded == []


def test_a_gate_that_speaks_its_vendors_language_contributes_nothing(tmp_path):
    extension = program(
        tmp_path,
        'json.load(sys.stdin)\nprint(json.dumps({"requests": [{"kind": "release-status",'
        ' "payload": {"repo": "store", "status": "killed"}}]}))\n',
    )
    states, degraded = extension_release_states([extension], builds={"store": {"sha": "abc"}})
    # An unrecognized state leaves the repo pending: the release waits rather than guessing.
    assert states == {}
    assert degraded == []


def test_a_skipped_gate_leaves_the_deploy_pending_and_says_which_one(tmp_path):
    extension = program(tmp_path, "raise SystemExit(1)")
    states, degraded = extension_release_states([extension], builds={"store": {"sha": "abc"}})
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


# --- opening a card for a proposal that has none (#393) ---

_OPENER = """
json.load(sys.stdin)
print(json.dumps({"requests": [{"kind": "open-work", "payload": {
    "repo": "store", "key": "WEB-7", "title": "TypeError: boom",
    "instruction": "Triage and fix Sentry WEB-7", "url": "https://sentry.invalid/WEB-7"}}]}))
"""


def test_a_proposal_with_no_card_gets_one_opened_and_the_work_queued(tmp_path):
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
    # From here it is an ordinary card, so the instruction rides the normal dispatch path.
    control = queue.controls(store.read())["ext:probe:WEB-7"]
    assert control["number"] == 41 and control["repo"] == "store"
    assert len(outcome.directives) == 1
    assert payloads(outcome.directives[0]) == ["Triage and fix Sentry WEB-7"]


def test_the_same_error_never_opens_a_second_card(tmp_path):
    extension = program(tmp_path, _OPENER)
    store = store_for(tmp_path)
    first = FakePort(opened=[41])
    run_wake(store, first, now=_NOW, enabled_extensions=[extension], issue_repos={"store"})

    second = FakePort(opened=[42], snapshots={"store#41": CardSnapshot(status="In progress")})
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
        snapshots={"store#41": CardSnapshot(status="Todo")},
    )
    run_wake(
        store, recovered, now=_NOW + timedelta(minutes=1),
        enabled_extensions=[extension], issue_repos={"store"},
    )

    assert recovered.opened_calls == [], "it must adopt the issue it already filed"
    assert store.read()["opened"]["ext:probe:WEB-7"]["number"] == 41
    assert queue.controls(store.read())["ext:probe:WEB-7"]["number"] == 41


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
    assert len(port.opened_calls) == 2, "the ceiling must bound cards opened, not just controls"
    assert any("daily cap (2) reached" in line for line in outcome.log)


def test_a_card_is_never_opened_in_a_repo_the_project_does_not_hold_issues_in(tmp_path):
    """Opening a card is a real GitHub write, so where one may appear is the project's decision.

    A mistyped `project_to_repo` value would otherwise file a labelled issue into an unrelated
    repository in the same org — which this engine would then adopt and dispatch a worker onto.
    """
    store = store_for(tmp_path)
    port = FakePort(opened=[41])
    outcome = run_wake(
        store, port, now=_NOW,
        enabled_extensions=[program(tmp_path, _OPENER)],
        issue_repos={"abacus"},          # the proposal names "store"
    )
    assert port.opened_calls == []
    assert queue.controls(store.read()) == {}
    assert any("refused to open a card in 'store'" in line for line in outcome.log)


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


def test_a_spec_only_proposal_opens_under_the_label_and_dispatches_a_spec_worker(tmp_path):
    """#2874: the card is labelled in the creating write, and its first dispatch is a spec."""
    store = store_for(tmp_path)
    port = FakePort(opened=[41])
    outcome = run_wake(
        store, port, now=_NOW, enabled_extensions=[program(tmp_path, _SPEC_OPENER)],
        issue_repos={"store"}, spec_only_label="spec-only",
    )
    assert port.opened_calls[0]["labels"] == ("spec-only",)
    assert store.read()["issues"]["store#41"]["spec_only"] is True
    (directive,) = outcome.directives
    assert directive["spec_only"] is True
    assert payloads(directive) == ["Write a spec for these reports"]


def test_a_spec_only_proposal_is_refused_where_the_project_names_no_label(tmp_path):
    """Opened without the label, the card would be built without anyone approving a spec."""
    store = store_for(tmp_path)
    port = FakePort(opened=[41])
    outcome = run_wake(
        store, port, now=_NOW, enabled_extensions=[program(tmp_path, _SPEC_OPENER)],
        issue_repos={"store"},
    )
    assert port.opened_calls == []
    assert queue.controls(store.read()) == {}
    assert any("refused a spec-only card" in line for line in outcome.log)


def test_an_ordinary_proposal_opens_with_no_extra_label(tmp_path):
    store = store_for(tmp_path)
    port = FakePort(opened=[41])
    outcome = run_wake(
        store, port, now=_NOW, enabled_extensions=[program(tmp_path, _OPENER)],
        issue_repos={"store"}, spec_only_label="spec-only",
    )
    assert "labels" not in port.opened_calls[0]
    assert not outcome.directives[0].get("spec_only")


def test_the_real_sentry_extension_opens_real_cards_through_a_real_cycle(tmp_path):
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
    assert len(outcome.directives) == 1


def test_a_proposal_is_never_acknowledged_with_a_reaction(tmp_path):
    """#370: an extension's proposal has no comment to react to, so reacting fails the wake.

    ``_queue_proposal`` synthesizes the control id ``ext:<name>:<key>``. Reacting to that asks
    GitHub for a comment that does not exist, the effect does not land, and the wake reports a
    failure. A first activation's canary refuses a failed wake, so an instance whose canary
    happened to honor a proposal could never come up at all.
    """
    extension = program(tmp_path, _PROPOSER)
    port = FakePort()
    outcome = run_wake(store_for(tmp_path), port, now=_NOW, enabled_extensions=[extension])

    reactions = [e for e in port.applied if e.kind is EffectKind.REACT]
    assert reactions == []
    assert outcome.report.errors == []
    assert outcome.report.ok


def test_a_person_s_comment_is_still_acknowledged(tmp_path):
    """The counterpart: skipping the proposal's reaction must not silence a human's."""
    port = FakePort(comments=[comment(9, "/claude fix it")])
    outcome = run_wake(store_for(tmp_path), port, now=_NOW)

    reactions = [e for e in port.applied if e.kind is EffectKind.REACT]
    assert reactions and {str(e.value["comment"]) for e in reactions} == {"9"}
    assert outcome.report.errors == []


def test_queued_work_on_an_existing_card_cannot_mark_it_spec_only(tmp_path):
    """Only the opening path, which put the label on, may say a card is a spec (#2874)."""
    extension = program(tmp_path, """
json.load(sys.stdin)
print(json.dumps({"requests": [{"kind": "queue-work", "payload": {
    "repo": "store", "number": 5, "key": "k", "instruction": "do it", "spec_only": True}}]}))
""")
    store = store_for(tmp_path)
    outcome = run_wake(
        store, FakePort(), now=_NOW, enabled_extensions=[extension],
        issue_repos={"store"}, spec_only_label="spec-only",
    )
    assert "spec_only" not in queue.controls(store.read())["ext:probe:k"]
    assert not any(directive.get("spec_only") for directive in outcome.directives)
