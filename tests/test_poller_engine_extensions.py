"""Slice 1 of #179: the extension hook protocol, its failure containment, and the harness.

The protocol's whole point is what happens when an extension misbehaves, so most of these
tests write a real program to a temp directory and run it as a subprocess. Mocks cannot
crash, hang, or print garbage on stdout.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agentdeck.poller_engine import extensions
from agentdeck.poller_engine.extensions import (
    Extension,
    Hook,
    Mode,
    RequestKind,
    contract_test,
    honored,
    invoke,
    snapshot,
)

_ALL_HOOKS = frozenset({Hook.VALIDATE_CONFIG, Hook.HEALTH, Hook.OBSERVE, Hook.RELEASE_GATE})


def write_extension(tmp_path, body: str, name: str = "probe") -> Extension:
    """Write a real extension program and return the Extension that runs it."""
    path = tmp_path / f"{name}.py"
    path.write_text("import json, sys\n" + body)
    return Extension(
        name=name,
        program=(sys.executable, str(path)),
        hooks=_ALL_HOOKS,
        config={"setting": 1},
    )


_ECHO = """
payload = json.load(sys.stdin)
print(json.dumps({
    "health": {"state": "ok", "saw": payload["hook"], "mode": payload["mode"],
               "config": payload["config"], "cards": len(payload["context"].get("cards", []))},
}))
"""


# --- what an extension receives ---

def test_an_extension_is_given_its_settings_the_mode_and_a_snapshot(tmp_path):
    extension = write_extension(tmp_path, _ECHO)
    response = invoke(
        extension, Hook.HEALTH, context={"cards": [{"ref": "store#7"}]}, mode=Mode.LIVE
    )
    assert response.ok
    assert response.health["saw"] == "health"
    assert response.health["mode"] == "live"
    assert response.health["config"] == {"setting": 1}
    assert response.health["cards"] == 1


def test_the_snapshot_shares_nothing_with_engine_state():
    state = {"issues": {"store#7": {"pending": ["9"]}}}
    copy = snapshot(state)
    copy["issues"]["store#7"]["pending"].append("tampered")
    # The engine's own structure is untouched, because the extension only ever gets a copy.
    assert state["issues"]["store#7"]["pending"] == ["9"]


def test_the_snapshot_drops_anything_that_will_not_serialize():
    class Live:
        def __repr__(self) -> str:
            return "<live object>"

    copied = snapshot({"port": Live(), "n": 1})
    # A live object would be a hole in the boundary, so it crosses as text or not at all.
    assert copied["port"] == "<live object>"
    assert copied["n"] == 1


def test_a_hook_an_extension_does_not_implement_is_never_called(tmp_path):
    extension = write_extension(tmp_path, "raise SystemExit(1)")
    quiet = Extension(
        name=extension.name, program=extension.program, hooks=frozenset({Hook.HEALTH})
    )
    response = invoke(quiet, Hook.OBSERVE)
    assert response.ok
    assert response.requests == ()


def test_the_hook_name_is_passed_as_an_argument(tmp_path):
    extension = write_extension(
        tmp_path, 'json.load(sys.stdin)\nprint(json.dumps({"health": {"argv": sys.argv[1:]}}))\n'
    )
    response = invoke(extension, Hook.RELEASE_GATE)
    assert response.health["argv"] == ["release-gate"]


# --- requests are only ever requests ---

_ASKS = """
json.load(sys.stdin)
print(json.dumps({"requests": [
    {"kind": "queue-work", "payload": {"ref": "store#7", "instruction": "fix it"}},
    {"kind": "release-status", "payload": {"repo": "store", "status": "ok"}},
]}))
"""


def test_an_extension_may_ask_for_work_and_report_a_release_status(tmp_path):
    extension = write_extension(tmp_path, _ASKS)
    response = invoke(extension, Hook.OBSERVE)
    assert response.ok
    assert [request.kind for request in response.requests] == [
        RequestKind.QUEUE_WORK,
        RequestKind.RELEASE_STATUS,
    ]
    assert response.requests[0].payload["instruction"] == "fix it"


def test_an_unknown_request_kind_is_refused_rather_than_guessed_at(tmp_path):
    extension = write_extension(
        tmp_path, 'json.load(sys.stdin)\nprint(json.dumps({"requests": [{"kind": "merge-now"}]}))\n'
    )
    response = invoke(extension, Hook.OBSERVE)
    assert not response.ok
    assert "not a request kind" in response.degraded
    assert response.requests == ()


def test_a_malformed_request_degrades_the_whole_answer_not_half_of_it(tmp_path):
    extension = write_extension(
        tmp_path,
        'json.load(sys.stdin)\nprint(json.dumps({"requests": ['
        '{"kind": "queue-work"}, "not-an-object"]}))\n',
    )
    response = invoke(extension, Hook.OBSERVE)
    # Partially trusting a broken answer is worse than skipping it.
    assert not response.ok
    assert response.requests == ()


def test_shadow_mode_reports_requests_but_honors_none(tmp_path):
    extension = write_extension(tmp_path, _ASKS)
    response = invoke(extension, Hook.OBSERVE, mode=Mode.SHADOW)
    assert len(response.requests) == 2          # the operator can still read what would happen
    assert honored(response, Mode.SHADOW) == () # ...and nothing is acted on
    assert len(honored(response, Mode.LIVE)) == 2


def test_a_degraded_extension_has_nothing_honored(tmp_path):
    extension = write_extension(tmp_path, "raise SystemExit(3)")
    response = invoke(extension, Hook.OBSERVE)
    assert honored(response, Mode.LIVE) == ()


# --- failure containment ---

def test_a_crashing_extension_is_skipped_and_marked_degraded(tmp_path):
    extension = write_extension(tmp_path, 'raise RuntimeError("boom")')
    response = invoke(extension, Hook.HEALTH)
    assert not response.ok
    assert "probe.health" in response.degraded
    assert response.health["state"] == "degraded"


def test_a_nonzero_exit_reports_its_first_line_of_stderr(tmp_path):
    extension = write_extension(
        tmp_path, 'print("cannot reach the deploy API", file=sys.stderr)\nraise SystemExit(4)\n'
    )
    response = invoke(extension, Hook.RELEASE_GATE)
    assert not response.ok
    assert "cannot reach the deploy API" in response.degraded


def test_an_extension_that_hangs_is_timed_out(tmp_path):
    extension = write_extension(tmp_path, "import time\ntime.sleep(30)\n")
    response = invoke(extension, Hook.HEALTH, timeout=0.1)
    assert not response.ok
    assert "timed out" in response.degraded


def test_output_that_is_not_json_is_skipped(tmp_path):
    extension = write_extension(tmp_path, 'print("all good!")')
    response = invoke(extension, Hook.HEALTH)
    assert not response.ok
    assert "not JSON" in response.degraded


def test_output_that_is_not_an_object_is_skipped(tmp_path):
    extension = write_extension(tmp_path, 'print(json.dumps([1, 2, 3]))')
    response = invoke(extension, Hook.HEALTH)
    assert not response.ok
    assert "not an object" in response.degraded


def test_a_missing_program_is_skipped_not_raised(tmp_path):
    extension = Extension(
        name="ghost", program=(str(tmp_path / "absent"),), hooks=frozenset({Hook.HEALTH})
    )
    response = invoke(extension, Hook.HEALTH)
    assert not response.ok
    assert response.health["state"] == "degraded"


def test_a_runner_that_itself_explodes_does_not_take_the_cycle_with_it(tmp_path):
    def broken(argv, payload, timeout):
        raise MemoryError("the host is out of memory")

    extension = write_extension(tmp_path, _ECHO)
    response = invoke(extension, Hook.HEALTH, runner=broken)
    assert not response.ok
    assert "runner failed" in response.degraded


# --- problems fold into the shared report ---

def test_validate_config_problems_come_back_as_report_problems(tmp_path):
    extension = write_extension(
        tmp_path,
        'json.load(sys.stdin)\nprint(json.dumps({"problems": [{'
        '"location": "manifest.extensions.probe.org",'
        ' "what": "unknown org", "fix": "set it"}]}))\n',
    )
    response = invoke(extension, Hook.VALIDATE_CONFIG)
    assert response.ok
    problem = response.problems[0]
    assert problem.location == "manifest.extensions.probe.org"
    assert problem.what == "unknown org" and problem.fix == "set it"


def test_a_problem_with_missing_fields_still_says_where_it_came_from(tmp_path):
    extension = write_extension(
        tmp_path, 'json.load(sys.stdin)\nprint(json.dumps({"problems": [{}]}))\n'
    )
    response = invoke(extension, Hook.VALIDATE_CONFIG)
    assert response.ok
    assert response.problems[0].location == "manifest.extensions.probe"
    assert response.problems[0].what and response.problems[0].fix


# --- the contract-test harness ---

_GOOD_CITIZEN = """
payload = json.load(sys.stdin)
hook = payload["hook"]
answer = {"health": {"state": "ok"}}
if hook == "release-gate":
    answer["requests"] = [{"kind": "release-status", "payload": {"repo": "store", "status": "ok"}}]
print(json.dumps(answer))
"""


def test_a_well_behaved_extension_passes_the_contract_test(tmp_path):
    extension = write_extension(tmp_path, _GOOD_CITIZEN)
    assert contract_test(extension) == []


def test_the_contract_test_runs_every_declared_hook_in_shadow_mode(tmp_path):
    log = tmp_path / "calls.log"
    extension = write_extension(
        tmp_path,
        f'payload = json.load(sys.stdin)\n'
        f'open({str(log)!r}, "a").write(payload["hook"] + ":" + payload["mode"] + "\\n")\n'
        'print(json.dumps({"requests": [{"kind": "release-status", '
        '"payload": {"status": "ok"}}]}))\n',
    )
    contract_test(extension)
    lines = log.read_text().splitlines()
    assert sorted(lines) == sorted(f"{hook.value}:shadow" for hook in _ALL_HOOKS)


def test_the_contract_test_reports_a_hook_that_breaks_the_protocol(tmp_path):
    extension = write_extension(
        tmp_path,
        'payload = json.load(sys.stdin)\n'
        'if payload["hook"] == "observe":\n'
        '    raise SystemExit(9)\n'
        'print(json.dumps({"requests": [{"kind": "release-status", '
        '"payload": {"status": "ok"}}]}))\n',
    )
    problems = contract_test(extension)
    assert [problem.location for problem in problems] == ["extensions.probe.observe"]
    assert "exited 9" in problems[0].what


def test_a_release_gate_must_speak_the_engines_normalized_states(tmp_path):
    extension = write_extension(
        tmp_path,
        'json.load(sys.stdin)\nprint(json.dumps({"requests": [{"kind": "release-status", '
        '"payload": {"status": "killed"}}]}))\n',
    )
    problems = contract_test(extension)
    assert any("not a release state" in problem.what for problem in problems)


def test_a_release_gate_that_reports_nothing_is_a_problem(tmp_path):
    extension = write_extension(tmp_path, 'json.load(sys.stdin)\nprint(json.dumps({}))\n')
    problems = contract_test(extension)
    assert any("reported no release status" in problem.what for problem in problems)


def test_an_extension_that_declares_no_hooks_is_pointless_and_says_so(tmp_path):
    extension = Extension(name="idle", program=(sys.executable, "-c", "pass"))
    problems = contract_test(extension)
    assert problems and "declares no hooks" in problems[0].what


# --- the artifact exposes the harness ---

def test_the_engine_cli_can_contract_test_an_extension(tmp_path, capsys):
    from agentdeck.poller_engine.__main__ import main

    program = tmp_path / "probe.py"
    program.write_text("import json, sys\n" + _GOOD_CITIZEN)
    code = main(
        [
            "contract-test",
            "--name", "probe",
            "--hook", "health",
            "--hook", "release-gate",
            sys.executable, str(program),
        ]
    )
    assert code == 0
    assert "answers every declared hook" in capsys.readouterr().out


def test_the_engine_cli_rejects_an_unknown_hook_name(tmp_path, capsys):
    from agentdeck.poller_engine.__main__ import main

    code = main(["contract-test", "--name", "probe", "--hook", "teleport", "true"])
    assert code == 2
    assert "unknown hook" in capsys.readouterr().err


# --- discovery: finding the programs a Manifest enables (#183) ---

def _folder(tmp_path, name: str, body: str) -> Path:
    """An extension program in the conventional folder of a checkout."""
    directory = tmp_path / "checkout" / "poller" / "extensions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text("import json, sys\n" + body)
    return path


_GATE = {"deploy-watch": frozenset({Hook.HEALTH, Hook.RELEASE_GATE})}

_DECLARES = """
payload = json.load(sys.stdin)
if sys.argv[1] == "hooks":
    print(json.dumps({"hooks": ["health", "release-gate"]}))
else:
    print(json.dumps({"health": {"state": "ok"}}))
"""


def _shared(tmp_path, name: str, body: str) -> Path:
    """An extension program in a folder shared across instances, not in any checkout."""
    directory = tmp_path / "shared-extensions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text("import json, sys\n" + body)
    return path


def test_a_shared_extensions_folder_serves_a_project_that_vendors_nothing(tmp_path):
    """#377: a project whose own checkout holds no extensions can still use the host's."""
    path = _shared(tmp_path, "deploy_watch", _DECLARES)
    found, problems = extensions.discover(
        {"deploy-watch": {"service": "x.service"}},
        [tmp_path / "checkout"],                      # exists, holds no poller/extensions
        allowed=_GATE,
        extra_dirs=[tmp_path / "shared-extensions"],
        interpreter=sys.executable,
    )
    assert problems == []
    loaded = found[0]
    assert loaded.name == "deploy-watch"
    assert loaded.hooks == frozenset({Hook.HEALTH, Hook.RELEASE_GATE})
    # Which file resolved is visible, so a reader can tell shared from vendored (#2158).
    assert loaded.source == str(path)


def test_a_checkouts_own_copy_still_wins_over_the_shared_one(tmp_path):
    """Adding a shared folder can never change what an existing installation resolves."""
    vendored = _folder(tmp_path, "deploy_watch", _DECLARES)
    _shared(tmp_path, "deploy_watch", _DECLARES)
    found, _ = extensions.discover(
        {"deploy-watch": {}},
        [tmp_path / "checkout"],
        allowed=_GATE,
        extra_dirs=[tmp_path / "shared-extensions"],
        interpreter=sys.executable,
    )
    assert found[0].source == str(vendored)


def test_an_extension_in_neither_place_says_both_places_were_looked_in(tmp_path):
    _, problems = extensions.discover(
        {"deploy-watch": {}},
        [tmp_path / "checkout"],
        allowed=_GATE,
        extra_dirs=[tmp_path / "shared-extensions"],
        interpreter=sys.executable,
    )
    assert len(problems) == 1
    # The fix line has to name the shared folder, or the reader only learns half the story.
    assert "shared extensions folder" in problems[0].what
    assert "overlay.paths.extensions" in problems[0].fix


def test_an_enabled_extension_is_loaded_from_the_conventional_folder(tmp_path):
    _folder(tmp_path, "deploy_watch", _DECLARES)
    found, problems = extensions.discover(
        {"deploy-watch": {"service": "x.service"}},
        [tmp_path / "checkout"],
        allowed=_GATE,
        interpreter=sys.executable,
    )
    assert problems == []
    loaded = found[0]
    assert loaded.name == "deploy-watch"
    assert loaded.hooks == frozenset({Hook.HEALTH, Hook.RELEASE_GATE})
    assert loaded.config == {"service": "x.service"}
    assert loaded.refused == "" and loaded.ignored_hooks == frozenset()


def test_a_program_nobody_enabled_is_never_loaded(tmp_path):
    _folder(tmp_path, "deploy_watch", _DECLARES)
    _folder(tmp_path, "sentry_autofix", _DECLARES)
    found, _problems = extensions.discover(
        {"deploy-watch": {}}, [tmp_path / "checkout"], allowed=_GATE, interpreter=sys.executable
    )
    assert [loaded.name for loaded in found] == ["deploy-watch"]


def test_an_enabled_extension_with_no_program_is_a_problem(tmp_path):
    found, problems = extensions.discover(
        {"ghost": {}},
        [tmp_path / "checkout"],
        allowed={"ghost": frozenset({Hook.HEALTH})},
        interpreter=sys.executable,
    )
    # Not a silent omission: a project that activates believing something is watching, when
    # nothing is, is worse than one that refuses.
    assert found == []
    assert "no program for it was found" in problems[0].what


def test_a_program_that_will_not_say_what_it_implements_is_a_problem(tmp_path):
    _folder(tmp_path, "mute", 'raise SystemExit(1)')
    found, problems = extensions.discover(
        {"mute": {}},
        [tmp_path / "checkout"],
        allowed={"mute": frozenset({Hook.HEALTH})},
        interpreter=sys.executable,
    )
    assert found == []
    assert "would not say which hooks" in problems[0].what


def test_a_hook_this_engine_does_not_know_refuses_the_whole_extension(tmp_path):
    _folder(
        tmp_path,
        "future",
        'json.load(sys.stdin)\nprint(json.dumps({"hooks": ["health", "teleport"]}))\n',
    )
    found, problems = extensions.discover(
        {"future": {}},
        [tmp_path / "checkout"],
        allowed={"future": frozenset({Hook.HEALTH})},
        interpreter=sys.executable,
    )
    # Partially loading an extension that expects a hook the engine will never call would be
    # worse than refusing it.
    assert found == []
    assert problems


def test_declared_hooks_are_none_when_the_answer_is_unusable(tmp_path):
    for body in ('print("not json")', 'print(json.dumps([]))', 'print(json.dumps({}))'):
        path = _folder(tmp_path, "odd", body)
        assert extensions.declared_hooks((sys.executable, str(path))) is None


# --- the hook allow-list: a project approves policy, not code bytes (#2158) ---

_MARKS_THAT_IT_RAN = """
from pathlib import Path
Path(__file__).with_suffix(".ran").write_text("x")
json.load(sys.stdin)
if sys.argv[1] == "hooks":
    print(json.dumps({"hooks": ["health", "release-gate"]}))
else:
    print(json.dumps({"health": {"state": "ok"}}))
"""


def _discovered(tmp_path, *, allowed=_GATE, wake=False, extra_dirs=()):
    return extensions.discover(
        {"deploy-watch": {}},
        [tmp_path / "checkout"],
        allowed=allowed,
        extra_dirs=extra_dirs,
        interpreter=sys.executable,
        wake=wake,
    )


def test_changed_extension_code_still_runs_at_the_hooks_it_was_allowed(tmp_path):
    """Case 1 and 2: editing the program is a deployment, not a policy change. Nothing is
    re-approved, and the allow-list that was in force stays in force."""
    path = _folder(tmp_path, "deploy_watch", _MARKS_THAT_IT_RAN)
    before, _ = _discovered(tmp_path, wake=True)
    path.write_text(path.read_text() + "# a bug fix, deployed like any other code\n")

    found, problems = _discovered(tmp_path, wake=True)

    assert problems == []
    assert found[0].refused == ""
    assert found[0].hooks == before[0].hooks == frozenset({Hook.HEALTH, Hook.RELEASE_GATE})
    assert invoke(found[0], Hook.HEALTH).ok
    assert path.with_suffix(".ran").exists()


def test_an_extension_that_declares_a_new_hook_does_not_gain_it(tmp_path):
    """Case 3: a software update cannot expand what a project allowed."""
    _folder(tmp_path, "deploy_watch", _MARKS_THAT_IT_RAN.replace(
        '["health", "release-gate"]', '["health", "release-gate", "observe", "command"]'
    ))
    found, problems = _discovered(tmp_path, wake=True)
    assert problems == []
    assert found[0].hooks == frozenset({Hook.HEALTH, Hook.RELEASE_GATE})
    assert found[0].ignored_hooks == frozenset({Hook.OBSERVE, Hook.COMMAND})
    assert not found[0].implements(Hook.OBSERVE)
    assert invoke(found[0], Hook.OBSERVE) == extensions.Response(ok=True)  # never called


def test_an_allowed_hook_the_program_no_longer_implements_is_refused_clearly(tmp_path):
    """Case 4: a problem at activation, a refused extension at a wake — never a silent gap."""
    _folder(tmp_path, "deploy_watch", _MARKS_THAT_IT_RAN.replace(
        '["health", "release-gate"]', '["health"]'
    ))
    found, problems = _discovered(tmp_path)
    assert found == []
    assert "does not implement release-gate, which the Manifest allows" in problems[0].what

    found, problems = _discovered(tmp_path, wake=True)
    assert problems == []
    assert "does not implement release-gate" in found[0].refused
    # It keeps the hooks it was allowed, so a missing release gate is still known as one.
    assert found[0].implements(Hook.RELEASE_GATE)


def test_an_extension_with_no_allow_list_is_a_problem_offline(tmp_path):
    """Activation requires explicit hooks: nothing else says what the project approved."""
    _folder(tmp_path, "deploy_watch", _MARKS_THAT_IT_RAN)
    found, problems = _discovered(tmp_path, allowed={})
    assert found == []
    assert "states no hooks" in problems[0].what
    assert "hooks = [...]" in problems[0].fix


def test_a_wake_refuses_a_missing_or_mute_extension_instead_of_the_whole_wake(tmp_path):
    for body in (None, "raise SystemExit(3)"):
        if body is not None:
            _folder(tmp_path, "deploy_watch", body)
        found, problems = _discovered(tmp_path, wake=True)
        assert problems == []
        assert found[0].refused
        assert found[0].hooks == frozenset({Hook.HEALTH, Hook.RELEASE_GATE})


def test_a_refused_extension_answers_no_hook_at_all(tmp_path):
    """One guard on the one chokepoint, rather than a check at each call site to forget."""
    extension = write_extension(tmp_path, _ECHO)
    refused = Extension(
        name=extension.name,
        program=extension.program,
        hooks=_ALL_HOOKS,
        refused="it would not say which hooks it implements",
    )
    for hook in Hook:
        response = invoke(refused, hook)
        assert not response.ok, hook
        assert response.health["reason"] == "it would not say which hooks it implements"


def test_a_refused_release_gate_leaves_its_release_pending(tmp_path):
    """Case 8: missing evidence is never success."""
    _folder(tmp_path, "deploy_watch", "raise SystemExit(1)")
    found, _ = _discovered(tmp_path, wake=True)
    states, degraded = extensions.release_states(found, context={"builds": {}})
    assert states == {} and degraded == ["deploy-watch"]


def test_an_unknown_request_kind_still_fails_closed(tmp_path):
    """Case 7: the engine's request vocabulary is the boundary, whatever build is deployed."""
    extension = write_extension(
        tmp_path, 'json.load(sys.stdin)\nprint(json.dumps({"requests": [{"kind": "merge"}]}))\n'
    )
    response = invoke(extension, Hook.OBSERVE)
    assert not response.ok and "not a request kind" in response.degraded
    assert honored(response, Mode.LIVE) == ()


def test_a_shared_extension_update_reaches_every_project_without_reactivation(tmp_path):
    """Case 9: several projects share one program with their own settings and allow-lists."""
    path = _shared(tmp_path, "deploy_watch", _MARKS_THAT_IT_RAN)
    path.write_text(path.read_text() + "# fixed once, for everyone\n")
    narrow = {"deploy-watch": frozenset({Hook.HEALTH})}
    for allowed in (_GATE, narrow):
        found, problems = _discovered(
            tmp_path, allowed=allowed, wake=True, extra_dirs=[tmp_path / "shared-extensions"]
        )
        assert problems == [] and found[0].refused == ""
        assert found[0].hooks == allowed["deploy-watch"]
        assert found[0].source == str(path)


def test_a_vendored_extension_update_follows_the_same_policy(tmp_path):
    """Case 10: a project's own copy is software too; its allow-list is still the Manifest's."""
    vendored = _folder(tmp_path, "deploy_watch", _MARKS_THAT_IT_RAN)
    _shared(tmp_path, "deploy_watch", _MARKS_THAT_IT_RAN)
    vendored.write_text(vendored.read_text().replace(
        '["health", "release-gate"]', '["health", "release-gate", "propose"]'
    ))
    found, problems = _discovered(
        tmp_path, wake=True, extra_dirs=[tmp_path / "shared-extensions"]
    )
    assert problems == [] and found[0].refused == ""
    assert found[0].source == str(vendored)
    assert found[0].ignored_hooks == frozenset({Hook.PROPOSE})


def test_a_settings_check_never_runs_a_program_that_was_not_allowed_validate_config(tmp_path):
    """The allow-list binds the activation-time settings check too: running validate-config
    runs the program, so a Manifest that did not allow it gets a problem, never a quiet call."""
    path = _folder(tmp_path, "deploy_watch", _MARKS_THAT_IT_RAN.replace(
        '["health", "release-gate"]', '["health", "release-gate", "validate-config"]'
    ))
    found, problems = _discovered(tmp_path)          # allowed: health, release-gate only
    assert problems == []
    path.with_suffix(".ran").unlink()                # discovery asked it which hooks it has

    reported: list = []
    extensions.config_validator(found[0])({"service": "x"}, reported)

    assert not path.with_suffix(".ran").exists(), "an unallowed validate-config was run"
    assert reported[0].location == "manifest.extensions.deploy-watch.hooks"
    assert "does not allow validate-config" in reported[0].what

    # Allowed, it runs as before.
    allowed = {"deploy-watch": frozenset({Hook.HEALTH, Hook.RELEASE_GATE, Hook.VALIDATE_CONFIG})}
    found, _ = _discovered(tmp_path, allowed=allowed)
    path.with_suffix(".ran").unlink()
    reported = []
    extensions.config_validator(found[0])({"service": "x"}, reported)
    assert path.with_suffix(".ran").exists() and reported == []


def test_the_reference_extensions_declare_exactly_what_they_implement():
    """A declaration that drifts from the implementation is worse than none."""
    root = Path(__file__).resolve().parent.parent
    expected = {
        "sentry-autofix": {Hook.VALIDATE_CONFIG, Hook.OBSERVE, Hook.PROPOSE, Hook.HEALTH},
        # `/shot` went with the cards it queued work on (#3741).
        "screenshots": {Hook.VALIDATE_CONFIG, Hook.HEALTH},
    }
    for name, hooks in expected.items():
        program = extensions.program_for(name, [root])
        assert extensions.declared_hooks((sys.executable, str(program))) == hooks, name
