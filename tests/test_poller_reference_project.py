"""#183: the Reference Poller Project's committed configuration, and the canary scenario.

The live half of #183 — a real board, a real activation, a real worker — is an operator action.
What is checkable here is that the example a fresh operator copies is complete and honest, and
that the acceptance scenario covers what #115 said it must.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from agentdeck.poller_engine import config
from agentdeck.poller_engine.config import CONTRACT, Role

REPO = Path(__file__).resolve().parent.parent
REFERENCE = REPO / "poller" / "reference"
MANIFEST = REFERENCE / "poller.toml"
OVERLAY_TEMPLATE = REFERENCE / "overlay.example.toml"


def _raw(path: Path) -> dict:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def _filled(tmp_path, *, project_number=7) -> tuple[Path, Path]:
    """The committed example with its one placeholder filled, and a usable Overlay beside it."""
    manifest = tmp_path / "poller.toml"
    manifest.write_text(
        MANIFEST.read_text().replace("project_number = 0", f"project_number = {project_number}")
    )
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        OVERLAY_TEMPLATE.read_text().replace("~/", f"{tmp_path}/")
    )
    return manifest, overlay


# --- the committed example ---

def test_the_reference_manifest_is_on_the_engines_contract():
    assert _raw(MANIFEST)["contract"] == CONTRACT
    assert _raw(OVERLAY_TEMPLATE)["contract"] == CONTRACT


def _values(node) -> list[str]:
    """Every string value in the parsed file: what is configured, not what is explained."""
    if isinstance(node, dict):
        return [value for child in node.values() for value in _values(child)]
    if isinstance(node, list):
        return [value for child in node for value in _values(child)]
    return [node] if isinstance(node, str) else []


def test_the_reference_manifest_holds_no_secrets_paths_or_node_ids():
    # Checked over the configured values rather than the file text: a comment that mentions a
    # token is documentation, while a token in a value would be the mistake this split prevents.
    for value in _values(_raw(MANIFEST)):
        assert not value.startswith(("PVT_", "PVTSSF_")), value
        assert "/home/" not in value and not value.startswith("~/"), value
        assert "ghp_" not in value and "github_pat" not in value, value


def test_the_board_is_named_semantically_and_by_role():
    board = _raw(MANIFEST)["board"]
    assert board["org"] == "eerovil"
    roles = set(board["statuses"])
    # Every role the engine requires is named; Backlog is optional and absent here.
    for role in config.REQUIRED_ROLES:
        assert role.value.lower().replace(" ", "_") in roles, role
    assert "backlog" not in roles


def test_the_board_number_is_an_obvious_placeholder_that_fails_closed(tmp_path):
    assert _raw(MANIFEST)["board"]["project_number"] == 0
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(OVERLAY_TEMPLATE.read_text().replace("~/", f"{tmp_path}/"))
    draft = config.inspect(MANIFEST, overlay)
    # Refusing is the point: a poller must never point at a board nobody chose.
    assert not draft.ok
    assert "manifest.board.project_number" in [p.location for p in draft.report.problems]


def test_the_reference_project_validates_offline_once_its_board_is_known(tmp_path):
    manifest, overlay = _filled(tmp_path)
    draft = config.inspect(
        manifest,
        overlay,
        extension_validators={
            name: (lambda block, problems: None)
            for name in ("deploy-watch", "screenshots", "chatgpt-nudge")
        },
    )
    assert draft.ok, draft.report.render()


def test_the_repo_states_every_role_it_plays(tmp_path):
    manifest, overlay = _filled(tmp_path)
    parsed = config.inspect(manifest, overlay, extension_validators={
        name: (lambda block, problems: None)
        for name in ("deploy-watch", "screenshots", "chatgpt-nudge")
    }).manifest
    repo = parsed.repo("agentdeck")
    assert repo.holds_issues and repo.holds_code and repo.auto_deploys
    assert parsed.auto_deploy_repos() == frozenset({"agentdeck"})


def test_the_full_preset_is_enabled_because_that_is_what_the_example_demonstrates():
    capabilities = _raw(MANIFEST)["capabilities"]
    for capability in (
        "work_control",
        "guarded_release",
        "merge_review",
        "deployment_observation",
        "artifacts",
    ):
        assert capabilities[capability] is True, capability


def test_the_enabled_extensions_are_the_ones_this_project_can_actually_run():
    extensions = _raw(MANIFEST)["extensions"]
    # chatgpt-nudge is on by default for every project, so the copy-me example carries it too;
    # the public copy leaves it out of both, so there the example does not (#3623).
    nudge = (MANIFEST.parents[1] / "extensions" / "chatgpt_nudge.py").exists()
    assert set(extensions) == {"deploy-watch", "screenshots", *(["chatgpt-nudge"] * nudge)}
    # sentry-autofix needs an organization and token this project does not have, and the file
    # says so rather than shipping a block that could never validate.
    assert "sentry-autofix" in MANIFEST.read_text()


def test_deploy_watch_points_at_this_hosts_own_deploy():
    settings = _raw(MANIFEST)["extensions"]["deploy-watch"]
    assert settings["service"] == "agentdeck.service"
    assert settings["health_url"].endswith("/healthz")


def test_the_authorization_slot_shows_the_shape():
    authorization = _raw(MANIFEST)["authorization"]
    assert authorization["work"]["role"] == "triage"
    assert authorization["release"]["role"] == "maintain"


def test_the_reference_project_names_no_bot_and_says_why():
    """`bot` denies that account every tier, including approving an activation. This project's
    engine speaks with its single operator's own credential, so naming them would leave nobody
    able to approve, and naming a fictitious account would make the rule look enforced while the
    engine still posts as a human. Unset is the honest state — but only if the file says so,
    because a silently absent bot is indistinguishable from an oversight."""
    authorization = _raw(MANIFEST)["authorization"]
    assert "bot" not in authorization
    text = MANIFEST.read_text()
    assert "bot" in text, "the slot must still be documented even though it is unset"
    assert "account of its own" in text, "the file must say what would make setting it real"


def test_an_operator_of_this_project_can_actually_approve_its_activation():
    """The gate has to be passable by someone. With no bot named, the repository's own Release
    role decides — which is the whole point of stage four."""
    from agentdeck.poller_engine import authorization as auth

    policy = auth.parse_policy(_raw(MANIFEST)["authorization"])
    assert auth.holds(policy, auth.Tier.RELEASE, login="eerovil", permission="admin")
    # A drive-by with no role still cannot approve.
    assert not auth.holds(policy, auth.Tier.RELEASE, login="stranger", permission="read")


def test_the_overlay_template_asks_only_for_secrets_and_paths():
    overlay = _raw(OVERLAY_TEMPLATE)
    # `extensions` holds only chatgpt-nudge's host half: a state file path and the review program.
    assert set(overlay) <= {"contract", "paths", "checkouts", "worker", "credentials", "extensions"}
    assert set(overlay.get("extensions", {})) <= {"chatgpt-nudge"}
    # No GitHub credential at all: the engine speaks as its owner's authority (#2289), and no
    # token is ever inline in a config.
    assert not any("token" in key for key in overlay.get("credentials", {}))


def test_the_readme_states_both_prerequisites_this_host_lacks():
    text = (REFERENCE / "README.md").read_text()
    assert "read:project" in text
    assert "project_number" in text
    # And that the first write only happens after the one human gate.
    assert "single human gate" in text


# --- the canary scenario (#115) ---

def _scenario():
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    from poller_canary import SCENARIO

    return SCENARIO


def test_the_canary_covers_every_capability_115_named():
    covered = {step.capability for step in _scenario()}
    for capability in (
        "work control",
        "authorization (#110)",
        "guarded release",
        "merge repair",
        "deployment observation",
        "stop protocol",
        "reconcile",
        "artifacts",
        "observability",
    ):
        assert capability in covered, capability


def test_the_canary_is_ordered_so_each_step_can_assume_the_last():
    keys = [step.key for step in _scenario()]
    assert keys.index("inspect") < keys.index("activate")
    # Nothing real happens before the gate, and the release steps follow the work that produced it.
    assert keys.index("activate") < keys.index("work") < keys.index("merge_review")
    assert keys.index("merge_review") < keys.index("deploy")


def test_every_step_says_what_evidence_would_prove_it():
    for step in _scenario():
        assert step.what and step.evidence, step.key


def test_a_step_whose_outcome_is_unknown_is_never_recorded_as_a_pass(tmp_path):
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    from poller_canary import SCENARIO, Result, Run

    run = Run(instance="reference")
    run.results = [Result(step, "unknown") for step in SCENARIO if step.blocking]
    # An acceptance script that guessed would be worse than no script at all.
    assert run.passed is False
    assert '"passed": false' in __import__("json").dumps(run.as_json(), indent=2).lower()


def test_a_dry_run_lists_the_scenario_without_performing_anything(tmp_path):
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    from poller_canary import main

    code = main([
        "--root", str(tmp_path), "--instance", "reference",
        "--manifest", str(MANIFEST), "--dry-run",
        "--evidence", str(tmp_path / "evidence.json"),
    ])
    assert code == 0
    evidence = __import__("json").loads((tmp_path / "evidence.json").read_text())
    assert {step["outcome"] for step in evidence["steps"]} == {"skipped"}


# --- reading the evidence an instance leaves behind (#183) ---

def _canary():
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    import poller_canary

    return poller_canary


def _instance(tmp_path, **artifacts) -> object:
    """An instance directory holding exactly the artifacts a case is about."""

    base = tmp_path / "instances" / "reference"
    base.mkdir(parents=True)
    for name, payload in artifacts.items():
        path = base / name.replace("__", ".")
        if isinstance(payload, str):
            path.write_text(payload)
        else:
            path.write_text(json.dumps(payload))
    return _canary().Evidence.load(base)


def _fresh_health(**overrides) -> dict:
    from datetime import UTC, datetime

    health = {
        "cycles": 800,
        "error_streak": 0,
        "last_cycle_at": datetime.now(UTC).isoformat(),
        "totals": {"scans": 800, "scan_failures": 0},
        "last_report": {"queue_depth": 0, "worker_backend_ok": True},
    }
    health.update(overrides)
    return health


def test_an_instance_with_nothing_on_disk_is_unknown_everywhere_not_failed(tmp_path):
    """A half-provisioned instance has not failed the scenario; it has not run it."""
    canary = _canary()
    evidence = _instance(tmp_path)
    for key, verify in canary.VERIFIERS.items():
        outcome, detail = verify(evidence)
        assert outcome == "unknown", (key, outcome, detail)
        assert detail, key


def test_the_canary_reads_who_applied_the_configuration(tmp_path):
    """#2157: activation is one apply, so its evidence is the audit record it leaves."""
    canary = _canary()
    evidence = _instance(tmp_path, activation__json={
        "activated_by": "eerovil", "activated_at": "2026-09-24T10:00:00+00:00",
        "fingerprint": "abc123def456abc123",
    })
    outcome, detail = canary._verify_activate(evidence)
    assert outcome == "pass"
    assert "eerovil" in detail and "abc123def456abc1" in detail


def test_an_activation_record_with_no_fingerprint_is_unknown(tmp_path):
    canary = _canary()
    unbound = _instance(tmp_path, activation__json={
        "activated_by": "eerovil", "activated_at": "2026-07-26T14:17:19+00:00",
    })
    outcome, detail = canary._verify_activate(unbound)
    assert outcome == "unknown"
    assert "no binding fingerprint" in detail


def test_a_stale_health_record_is_unknown_never_a_pass(tmp_path):
    """#107 makes freshness first-class: a stale record shows as unknown, never as current."""
    from datetime import UTC, datetime, timedelta

    canary = _canary()
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    evidence = _instance(tmp_path, state__json={
        "data": {"health": _fresh_health(last_cycle_at=old)},
    })
    outcome, detail = canary._verify_health(evidence)
    assert outcome == "unknown"
    assert "stale" in detail


def test_a_fresh_health_record_with_an_error_streak_fails(tmp_path):
    canary = _canary()
    evidence = _instance(tmp_path, state__json={
        "data": {"health": _fresh_health(error_streak=4, last_error="board unreachable")},
    })
    outcome, detail = canary._verify_health(evidence)
    assert outcome == "fail"
    assert "board unreachable" in detail


def test_a_healthy_deploy_extension_is_not_a_deploy_observation(tmp_path):
    """The distinction the step exists for: the extension being up is not the capability running."""
    canary = _canary()
    evidence = _instance(tmp_path, state__json={"data": {"health": _fresh_health(
        totals={"deploys": 0},
        last_report={"extensions": {"deploy-watch": {"state": "ok", "healthy": True}}},
    )}})
    outcome, detail = canary._verify_deploy(evidence)
    assert outcome == "unknown"
    assert "a healthy extension is not an observation" in detail


def test_a_deploy_gate_that_only_waited_is_not_a_deploy_observed(tmp_path):
    """`totals.deploys` counts WAITING cycles, so it cannot prove deploy-watch ever said ok."""
    canary = _canary()
    evidence = _instance(tmp_path, state__json={"data": {
        "issues": {},
        "health": _fresh_health(totals={"deploys": 42}),
    }})
    outcome, detail = canary._verify_deploy(evidence)
    assert outcome == "unknown"
    assert "no release-status of ok is recorded" in detail


def test_a_deploy_is_observed_when_the_release_recorded_its_link(tmp_path):
    canary = _canary()
    evidence = _instance(tmp_path, state__json={"data": {
        "issues": {"agentdeck#292": {"done_pending": {
            "merged": ["agentdeck#291"],
            "deploy_links": ["[agentdeck](http://127.0.0.1:8756/healthz)"],
        }}},
        # Zero waiting cycles: a deploy that reported ok on the first gate check never
        # increments the counter, and that must not read as "never observed".
        "health": _fresh_health(totals={"deploys": 0}),
    }})
    outcome, detail = canary._verify_deploy(evidence)
    assert outcome == "pass"
    assert "agentdeck#292" in detail


def test_a_finished_deploy_is_still_readable_from_the_log_after_the_record_is_pruned(tmp_path):
    """The real case: both live observations were unreadable a minute after they happened."""
    canary = _canary()
    evidence = _instance(
        tmp_path,
        cycle__log=(
            "release: agentdeck#293 merged -> awaiting deploy ['agentdeck']\n"
            "release: agentdeck#293 done\n"
            "release: agentdeck#301 merged -> awaiting deploy ['agentdeck']\n"
            "release: agentdeck#301 done\n"
        ),
        # Pruned records, and the counter at zero — a deploy that answered ok on the first gate
        # check never increments it, which is exactly what happened on both of these.
        state__json={"data": {"issues": {}, "health": _fresh_health(totals={"deploys": 0})}},
    )
    outcome, detail = canary._verify_deploy(evidence)
    assert outcome == "pass"
    assert "agentdeck#293" in detail and "agentdeck#301" in detail
    assert "log-derived" in detail


def test_a_deploy_that_gated_but_never_released_is_not_a_pass(tmp_path):
    """A failed deploy ends the card Blocked, so reaching the gate proves nothing on its own."""
    canary = _canary()
    evidence = _instance(
        tmp_path,
        cycle__log="release: agentdeck#301 merged -> awaiting deploy ['agentdeck']\n",
        state__json={"data": {"issues": {}, "health": _fresh_health(totals={"deploys": 7})}},
    )
    outcome, detail = canary._verify_deploy(evidence)
    assert outcome == "unknown"
    assert "no release completed after it" in detail


def test_a_release_still_gating_on_its_deploy_is_unknown_not_a_pass(tmp_path):
    canary = _canary()
    evidence = _instance(tmp_path, state__json={"data": {
        "issues": {"agentdeck#292": {"awaiting_deploy": {"since": "2026-07-26T15:00:00+00:00"}}},
        "health": _fresh_health(totals={"deploys": 3}),
    }})
    outcome, detail = canary._verify_deploy(evidence)
    assert outcome == "unknown"
    assert "has not resolved yet" in detail


def test_authorization_needs_the_refusal_not_just_the_acceptance(tmp_path):
    """Accepted Work commands prove the grant half; only a refusal proves the bar (#110)."""
    canary = _canary()
    accepted_only = _instance(tmp_path, controls__json={"controls": {
        "1": {"id": "1", "kind": "claude", "status": "done"},
    }})
    outcome, detail = canary._verify_authorization(accepted_only)
    assert outcome == "unknown"
    assert "no refusal is on record" in detail

    both = _instance(tmp_path / "both", controls__json={"controls": {
        "1": {"id": "1", "kind": "claude", "status": "processing"},
        "2": {"id": "2", "kind": "unauthorized", "payload": "merge", "url": "https://x/2"},
    }})
    assert canary._verify_authorization(both)[0] == "pass"


def test_an_admission_refusal_is_not_an_authorization_refusal(tmp_path):
    """The false pass this step is most exposed to, seen for real on the live instance.

    `reject-unowned` and `reject-busy` are admission decisions about the *card*. They say nothing
    about who asked, and a Release Controller in good standing collects them routinely — reading
    them as proof of the authorization bar would pass the step on ordinary operational noise.
    """
    canary = _canary()
    evidence = _instance(tmp_path, controls__json={"controls": {
        "1": {"id": "1", "kind": "claude", "status": "done"},
        "2": {"id": "2", "kind": "merge", "status": "rejected", "rejection": "reject-unowned"},
        "3": {"id": "3", "kind": "merge", "status": "rejected", "rejection": "reject-busy"},
    }})
    outcome, detail = canary._verify_authorization(evidence)
    assert outcome == "unknown"
    assert "no refusal is on record" in detail


def _shot_controls(**overrides) -> dict:
    """The two records a real `/shot` leaves: the extension command, and the work it requested."""
    controls = {
        "1": {"id": "1", "kind": "extension", "payload": "/shot", "status": "done"},
        "2": {"id": "2", "kind": "claude", "status": "done",
              "payload": "Take screenshots of /, /pollers using this project's screenshot "
                         "procedure, and attach them."},
    }
    for key, patch in overrides.items():
        controls[key].update(patch)
    return {"controls": controls}


def test_a_shot_is_read_through_the_extension_control_not_a_shot_kind(tmp_path):
    """`/shot` is parsed as an `extension` control; there is no "shot" command kind at all."""
    canary = _canary()
    evidence = _instance(tmp_path, controls__json=_shot_controls())
    outcome, detail = canary._verify_artifacts(evidence)
    assert outcome == "pass"
    assert "completed" in detail


def test_a_shot_whose_worker_procedure_has_not_finished_is_not_a_pass(tmp_path):
    """Accepting the word is not taking the screenshot — the derived work is the capability."""
    canary = _canary()
    evidence = _instance(tmp_path, controls__json=_shot_controls(**{"2": {"status": "processing"}}))
    outcome, detail = canary._verify_artifacts(evidence)
    assert outcome == "unknown"
    assert "none has completed yet" in detail


def test_a_shot_that_raised_no_worker_work_is_not_a_pass(tmp_path):
    canary = _canary()
    evidence = _instance(tmp_path, controls__json={"controls": {
        "1": {"id": "1", "kind": "extension", "payload": "/shot", "status": "done"},
    }})
    outcome, detail = canary._verify_artifacts(evidence)
    assert outcome == "unknown"
    assert "requested no worker-side work" in detail


def test_a_step_proved_only_from_the_log_says_so(tmp_path):
    """cycle.log is a convenience channel, not the observable surface (#107)."""
    canary = _canary()
    evidence = _instance(
        tmp_path,
        cycle__log="reconcile: agentdeck#288 recovered terminal status In review\n",
        state__json={"data": {"issues": {}}},
    )
    outcome, detail = canary._verify_recovery(evidence)
    assert outcome == "pass"
    assert "log-derived" in detail


def test_a_stop_the_runtime_has_not_proved_is_not_a_pass(tmp_path):
    canary = _canary()
    evidence = _instance(tmp_path, state__json={"data": {"issues": {
        "agentdeck#9": {"stopping": True, "stop_requested_at": "2026-07-26T14:00:00+00:00"},
    }}})
    outcome, detail = canary._verify_stop(evidence)
    assert outcome == "unknown"
    assert "has not proved it yet" in detail


def test_unreadable_artifacts_load_as_empty_rather_than_crashing_the_run(tmp_path):
    """An instance mid-write must not make the acceptance script explode."""
    canary = _canary()
    evidence = _instance(tmp_path, state__json="{ this is not json", activation__json="")
    assert evidence.state == {}
    assert evidence.activation == {}
    assert canary._verify_health(evidence)[0] == "unknown"


def _carried(prior_steps, this_run):
    """Run `carry_forward` over hand-built results and return them keyed by step."""
    canary = _canary()
    results = []
    for step in canary.SCENARIO:
        if step.key in this_run:
            outcome, detail = this_run[step.key]
            results.append(canary.Result(step, outcome, detail, "now"))
    canary.carry_forward(results, {"steps": prior_steps})
    return {r.step.key: r for r in results}


def test_a_pass_an_earlier_run_recorded_is_carried_when_this_run_cannot_see_it(tmp_path):
    """The deploy proof lives in a card record that is pruned once the card finishes."""
    out = _carried(
        [{"key": "deploy", "outcome": "pass", "detail": "deploy observed on agentdeck#301",
          "at": "2026-07-26T16:09:40+00:00"}],
        {"deploy": ("unknown", "no release has ever gated on a deploy")},
    )
    assert out["deploy"].outcome == "pass"
    assert out["deploy"].carried_from == "2026-07-26T16:09:40+00:00"
    # The evidence file must never claim the observation happened now.
    assert out["deploy"].at == "2026-07-26T16:09:40+00:00"
    assert "carried from" in out["deploy"].detail


def test_a_failure_is_never_carried_and_never_overwritten(tmp_path):
    """A capability that broke must not be papered over by a run from before it broke."""
    out = _carried(
        [{"key": "health", "outcome": "pass", "detail": "was fine then", "at": "earlier"},
         {"key": "deploy", "outcome": "fail", "detail": "the gate failed", "at": "earlier"}],
        {"health": ("fail", "error streak 4"), "deploy": ("unknown", "nothing on record")},
    )
    assert out["health"].outcome == "fail", "a prior pass must not overwrite a live failure"
    assert out["deploy"].outcome == "unknown", "a prior failure must not be carried as a pass"


def test_what_this_run_can_still_read_always_wins(tmp_path):
    out = _carried(
        [{"key": "repair", "outcome": "pass", "detail": "stale", "at": "earlier"}],
        {"repair": ("pass", "read live from the fix record")},
    )
    assert out["repair"].carried_from == ""
    assert out["repair"].detail == "read live from the fix record"


def test_a_carried_pass_keeps_its_original_timestamp_across_repeated_carries(tmp_path):
    """Carrying a carried result must not launder its age into the present."""
    out = _carried(
        [{"key": "deploy", "outcome": "pass", "detail": "[carried from T0] observed",
          "at": "T0", "carried_from": "T0"}],
        {"deploy": ("unknown", "pruned")},
    )
    assert out["deploy"].carried_from == "T0"


def test_the_canary_reads_the_instances_own_manifest_by_default(tmp_path):
    """The committed Manifest carries a placeholder the offline check refuses on purpose."""

    canary = _canary()
    base = tmp_path / "instances" / "reference"
    base.mkdir(parents=True)
    (base / "poller.toml").write_text("# the instance's own, with a real project_number\n")
    code = canary.main([
        "--root", str(tmp_path), "--instance", "reference", "--dry-run",
        "--evidence", str(tmp_path / "evidence.json"),
    ])
    assert code == 0
    assert json.loads((tmp_path / "evidence.json").read_text())["instance"] == "reference"


# --- the board-creation helper (#183) ---

@pytest.fixture(autouse=True)
def _board_credential_is_stated(monkeypatch):
    """The board helper refuses to run on the host's own login (#2289)."""
    monkeypatch.setenv("GH_TOKEN", "test-board-token")


def _board_script():
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    import create_poller_board

    return create_poller_board


def test_the_board_helper_asks_for_every_status_role_the_engine_moves_through():
    module = _board_script()
    wanted = {name.casefold() for name in module.REQUIRED_OPTIONS}
    for role in config.REQUIRED_ROLES:
        assert role.value.casefold() in wanted, role
    # Backlog is optional, because a board without an intake column still works.
    assert Role.BACKLOG.value in module.OPTIONAL_OPTIONS
    assert Role.BACKLOG.value.casefold() not in wanted


def test_the_helper_adds_only_what_is_missing_and_never_renames_a_column():
    module = _board_script()
    field = {
        "id": "F_1",
        "options": [
            {"name": "Todo", "color": "BLUE"},
            {"name": "In Progress", "color": "YELLOW"},  # the board's own spelling
            {"name": "Done", "color": "GREEN"},
            {"name": "Icebox", "color": "GRAY"},          # something the operator added
        ],
    }
    options, additions = module.ensure_options(
        field, wanted=module.REQUIRED_OPTIONS, keep_optional=False
    )
    names = [option["name"] for option in options]
    # Existing columns survive exactly as they are — the Manifest maps a spelling onto a role, so
    # "In Progress" must not be rewritten to "In progress", and a column the engine does not know
    # about must not be dropped.
    assert names[:4] == ["Todo", "In Progress", "Done", "Icebox"]
    assert set(additions) == {"In review", "Needs More Info", "Blocked"}


def test_a_complete_board_needs_no_changes():
    module = _board_script()
    field = {
        "id": "F_1",
        "options": [{"name": name, "color": "GRAY"} for name in module.REQUIRED_OPTIONS],
    }
    _options, additions = module.ensure_options(
        field, wanted=module.REQUIRED_OPTIONS, keep_optional=False
    )
    assert additions == []


def test_a_dry_run_reports_what_it_would_do_without_touching_anything(capsys):
    module = _board_script()
    code = module.main(["--owner", "nobody-at-all", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "would create a board" in out


class _FakeBoard:
    """A board behind the four `gh` calls the helper makes, recording every write.

    The helper's whole job is to leave a used board better than it found it, so the fake models
    the one behaviour that makes that hard: replacing the Status option set clears every card's
    Status, exactly as `updateProjectV2Field` does on real GitHub.
    """

    def __init__(self, options, assigned):
        self.options = [dict(option) for option in options]
        self.assigned = dict(assigned)
        self.created = []
        self.option_writes = 0

    def __call__(self, *args, stdin=None):
        if args[:2] == ("project", "view"):
            return 0, __import__("json").dumps(
                {"number": int(args[2]), "url": f"https://example/{args[2]}"}
            ), ""
        if args[:2] == ("project", "create"):
            self.created.append(args)
            return 1, "", "should not create"
        if args[:2] == ("project", "field-list"):
            # Real `gh` returns each option's id alongside its name; restoring a card needs it.
            options = [
                {"id": f"o{i}", **option} for i, option in enumerate(self.options)
            ]
            return 0, __import__("json").dumps(
                {"fields": [{"name": "Status", "id": "F_1", "options": options}]}
            ), ""
        if args[:2] == ("api", "graphql"):
            return self._graphql(args, stdin)
        raise AssertionError(f"unexpected gh call: {args}")

    def _graphql(self, args, stdin):
        import json as _json

        body = _json.loads(stdin) if stdin else {"query": args[3].split("=", 1)[1]}
        query = body.get("query", "")
        if "items(first:100)" in query:  # read_assignments
            return 0, _json.dumps({"data": {"user": {"projectV2": {
                "id": "P_1",
                "field": {"id": "F_1", "options": [
                    {"id": f"o{i}", "name": option["name"]}
                    for i, option in enumerate(self.options)
                ]},
                "items": {"nodes": [
                    {"id": item, "fieldValueByName": {"name": name}}
                    for item, name in self.assigned.items()
                ]},
            }}}}), ""
        if "updateProjectV2Field" in query:  # the destructive replacement
            self.option_writes += 1
            self.options = [dict(option) for option in body["variables"]["o"]]
            self.assigned = dict.fromkeys(self.assigned, None)  # GitHub clears every card
            return 0, "{}", ""
        if "updateProjectV2ItemFieldValue" in query:  # putting a card back
            fields = dict(
                arg.split("=", 1) for arg in args if isinstance(arg, str) and "=" in arg
            )
            index = int(fields["o"].removeprefix("o"))
            self.assigned[fields["i"]] = self.options[index]["name"]
            return 0, "{}", ""
        raise AssertionError(f"unexpected graphql: {query[:60]}")


def test_completing_a_board_by_number_never_creates_a_second_one(
    monkeypatch, capsys, tmp_path
):
    """A board is identified by the number the Manifest stores. Matching on title instead means
    renaming the board in the UI silently makes a duplicate on the next run."""
    module = _board_script()
    board = _FakeBoard(
        [{"name": "Todo"}, {"name": "In Progress"}, {"name": "Done"}],
        {"card-1": "Todo"},
    )
    monkeypatch.setattr(module, "_gh", board)
    monkeypatch.chdir(tmp_path)

    # The title deliberately does not match the board's real name.
    code = module.main(["--owner", "eerovil", "--project-number", "1"])

    assert code == 0
    assert board.created == []
    assert "completing board #1" in capsys.readouterr().out


def test_adding_a_column_puts_every_card_back_where_it_was(monkeypatch, capsys, tmp_path):
    """Replacing the option set clears every card's Status, so the helper must save the
    assignments first and restore them — otherwise adding a column silently empties the board."""
    module = _board_script()
    board = _FakeBoard(
        [{"name": "Todo"}, {"name": "In Progress"}, {"name": "Done"}],
        {"card-1": "Todo", "card-2": "In Progress"},
    )
    monkeypatch.setattr(module, "_gh", board)
    monkeypatch.chdir(tmp_path)  # the saved-status record lands here, not in the repo

    code = module.main(["--owner", "eerovil", "--project-number", "1"])
    out = capsys.readouterr().out

    assert code == 0
    assert board.option_writes == 1
    assert "added: In review, Needs More Info, Blocked" in out
    # Every card is back on the column it was on, by name — not merely "not crashed".
    assert board.assigned == {"card-1": "Todo", "card-2": "In Progress"}
    assert "restored 2 card status(es)" in out


def test_an_unreadable_board_is_never_written_to(monkeypatch, capsys):
    """If the current assignments cannot be read, writing would destroy data that could not be
    put back. Refuse instead — the fail-closed rule the whole engine is built on."""
    module = _board_script()
    board = _FakeBoard([{"name": "Todo"}], {"card-1": "Todo"})

    def unreadable(*args, stdin=None):
        if args[:2] == ("api", "graphql") and stdin and "items(first:100)" in stdin:
            return 1, "", "boom"
        return board(*args, stdin=stdin)

    monkeypatch.setattr(module, "_gh", unreadable)

    code = module.main(["--owner", "eerovil", "--project-number", "1"])

    assert code == 1
    assert board.option_writes == 0  # nothing was written
    assert "refusing rather than writing blind" in capsys.readouterr().err


def test_the_reference_project_names_the_label_it_owns_cards_with():
    """This project owns cards with `agent` or `claude`, preserving the former as canonical.

    The value matters less than the fact that the file states it: a copy-me example that relied
    on engine defaults would teach the next project that these labels are not theirs to choose.
    """
    board = _raw(MANIFEST)["board"]
    assert board["ownership_label"] == ["agent", "claude"]
    assert board["spec_only_label"] == "spec-only"
    text = MANIFEST.read_text()
    # And that extending a live project's ownership is additive, with stable write behavior.
    assert "aliases without abandoning old cards" in text
    assert "first entry remains canonical" in text


def test_the_reference_project_names_the_template_title_it_repairs(tmp_path):
    """#338: this project's issue template pre-fills `Requested changes`, so the Manifest
    says so and the engine can put a real title on a card filed without one."""
    manifest, overlay = _filled(tmp_path)
    loaded = config.inspect(manifest, overlay, extension_validators={
        name: (lambda block, problems: None)
        for name in ("deploy-watch", "screenshots", "chatgpt-nudge")
    })
    assert loaded.ok, loaded.report.render()
    assert loaded.manifest.placeholder_titles == ("Requested changes",)

    template = REPO / ".github" / "ISSUE_TEMPLATE" / "issue-for-agent-to-fix.md"
    # The value is only right while it is what the template actually writes.
    assert "title: Requested changes" in template.read_text()


def _stop_control(cid: str, status: str, *, number: int = 295) -> dict:
    return {
        "id": cid, "repo": "agentdeck", "number": number, "kind": "stop",
        "payload": "", "body": "/claude stop", "status": status,
        "created_at": "2026-07-26T15:39:24Z",
    }


def test_a_stop_that_landed_is_a_pass_even_though_its_ack_file_is_gone(tmp_path):
    """Finalizing a stop discards the card's receipts, including the stop-ack.

    Reading only `stop-acks/` therefore reported a capability exercised twice, successfully,
    as never exercised at all — which is what kept the acceptance run from passing while the
    protocol was demonstrably working (agentdeck#295 and #300).
    """
    canary = _canary()
    evidence = _instance(tmp_path, controls__json={"controls": {
        "1": _stop_control("1", "done", number=295),
        "2": _stop_control("2", "done", number=300),
    }}, state__json={"data": {"issues": {}}})

    outcome, detail = canary._verify_stop(evidence)

    assert outcome == "pass"
    assert "agentdeck#295" in detail and "agentdeck#300" in detail
    assert "durable command ledger" in detail


def test_a_leftover_stop_ack_file_is_not_evidence_of_anything(tmp_path):
    """#2159 retired the stop-ack: a file an older engine left behind proves no stop."""
    canary = _canary()
    base = tmp_path / "instances" / "reference"
    (base / "stop-acks").mkdir(parents=True)
    (base / "stop-acks" / "one.ack").write_text("{}")
    evidence = canary.Evidence.load(base)

    outcome, detail = canary._verify_stop(evidence)

    assert outcome == "unknown"
    assert "stop-acks" not in detail


def test_a_stop_that_was_only_refused_is_not_a_completed_protocol(tmp_path):
    """`already-stopping` proves the command path, not the stop landing."""
    canary = _canary()
    evidence = _instance(tmp_path, controls__json={"controls": {
        "1": {**_stop_control("1", "rejected"), "rejection": "already-stopping"},
    }}, state__json={"data": {"issues": {}}})

    outcome, detail = canary._verify_stop(evidence)

    assert outcome == "unknown"
    assert "none of which completed" in detail
    assert "rejected" in detail


def test_a_stop_proved_only_from_the_log_says_so(tmp_path):
    canary = _canary()
    evidence = _instance(
        tmp_path,
        cycle__log="reconcile: agentdeck#295 stop confirmed\n",
        state__json={"data": {"issues": {}}},
    )

    outcome, detail = canary._verify_stop(evidence)

    assert outcome == "pass"
    assert "log-derived" in detail


def test_an_instance_that_never_stopped_anything_still_says_so(tmp_path):
    canary = _canary()
    evidence = _instance(tmp_path, state__json={"data": {"issues": {}}})

    assert canary._verify_stop(evidence) == ("unknown", "no stop has ever been requested")


def test_the_board_helper_refuses_without_an_explicit_credential(monkeypatch, capsys):
    monkeypatch.delenv("GH_TOKEN", raising=False)

    assert _board_script().main(["--owner", "acmecorp", "--dry-run"]) == 2
    assert "never used" in capsys.readouterr().err
