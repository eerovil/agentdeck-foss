"""What the CLI actually hands the cycle (#183).

Every rule in this engine is tested at the `run_cycle` level, where the fixture passes the
extensions, the auto-deploy repositories and the policy by hand. That proves the rules work.
It does not prove the CLI ever supplies them — and for a long time it did not, so the whole
extension system was configured, validated, contract-tested and then never invoked.

These tests sit at the seam the unit tests cannot see: they drive `main()` and assert on what
reached `run_cycle`. GitHub is stubbed at the adapter, and nowhere else.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import runpy
import sys
from pathlib import Path

import pytest
from poller_engine_fakes import FakePort

from agentdeck.poller_engine import __main__ as cli
from agentdeck.poller_engine import config as engine_config
from agentdeck.poller_engine import cycle as cycle_module
from agentdeck.poller_engine import extensions

_MANIFEST = """
contract = 1

[board]
org = "example"
project_number = 7

[board.statuses]
todo = "Todo"
in_progress = "In Progress"
in_review = "In review"
done = "Done"
needs_more_info = "Needs More Info"
blocked = "Blocked"

[policy]
merge_method = "merge"
worker_cap = 2

[[repos]]
id = "store"
holds_issues = true
holds_code = true
verification = "agentdeck_job"
auto_deploys = true

[authorization]
release = ["eero"]
work = ["eero"]
"""

_EXTENSION = """
from pathlib import Path
Path(__file__).with_suffix(".ran").write_text("x")  # proof the engine executed this program
hook = sys.argv[1] if len(sys.argv) > 1 else ""
sys.stdin.read()
if hook == "hooks":
    print(json.dumps({"hooks": ["health", "validate-config"]}))
elif hook == "validate-config":
    print(json.dumps({"problems": []}))
elif hook == "health":
    print(json.dumps({"status": "ok", "detail": "fine"}))
else:
    sys.exit(2)
"""


_PROPOSAL_EXTENSION = """
from pathlib import Path
Path(__file__).with_suffix(".ran").write_text("x")
hook = sys.argv[1] if len(sys.argv) > 1 else ""
payload = json.loads(sys.stdin.read() or "{}")
if hook == "hooks":
    print(json.dumps({"hooks": ["health", "propose", "validate-config"]}))
elif hook == "validate-config":
    print(json.dumps({"problems": []}))
elif hook == "propose":
    ref = payload["context"]["reference"]
    print(json.dumps({"requests": [{"kind": "open-work", "payload": {
        "repo": "store", "key": ref, "title": ref, "instruction": "fix " + ref
    }}]}))
elif hook == "health":
    print(json.dumps({"health": {"state": "ok"}}))
else:
    sys.exit(2)
"""

_MALFORMED_PROPOSAL_EXTENSION = _PROPOSAL_EXTENSION.replace(
    '"repo": "store", "key": ref, "title": ref, "instruction": "fix " + ref',
    '"repo": "store", "title": ref, "instruction": "fix " + ref',
)
_QUEUE_WORK_PROPOSAL_EXTENSION = _PROPOSAL_EXTENSION.replace(
    '"kind": "open-work", "payload": {',
    '"kind": "queue-work", "payload": {"number": 5, ',
)

@pytest.fixture(autouse=True)
def _example_speaks_as_the_agent(tmp_path, monkeypatch):
    """The fixture org is mapped to a non-human authority, as every real owner must be (#2289)."""
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text('[owners]\nexample = "agent"\n', encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))


class _Discovery:
    def board(self, org, project_number):
        return engine_config.BoardSnapshot(
            project_id="PVT_1",
            status_field_id="F_1",
            status_options={
                "Todo": "o1", "In Progress": "o2", "In review": "o3",
                "Done": "o4", "Needs More Info": "o5", "Blocked": "o6",
            },
        )

    def repo_check(self, org, repo_id):
        return engine_config.RepoCheck(engine_config.Reachability.REACHABLE)


def _instance(tmp_path, *, with_extension: bool, body: str = _EXTENSION) -> dict:
    """A project on disk: Manifest, Overlay, a checkout, and optionally an extension."""
    checkout = tmp_path / "checkout"
    (checkout / "poller" / "extensions").mkdir(parents=True)
    _program(tmp_path).write_text("import json, sys\n" + body)

    manifest_text = _MANIFEST
    if with_extension:
        # The Manifest allows exactly what the program declares (#2158) — the list is lifted
        # from the program's own `hooks` answer, which is valid TOML as it stands.
        declared = re.search(r'"hooks": (\[[^\]]*\])', body)
        allowed = f"hooks = {declared.group(1)}\n" if declared else ""
        manifest_text += f'\n[extensions.probe]\n{allowed}setting = "value"\n'
    manifest = tmp_path / "poller.toml"
    manifest.write_text(manifest_text)

    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        "contract = 1\n\n"
        f'[paths]\nstate = "{tmp_path / "state.json"}"\n\n'
        f'[checkouts]\nstore = "{checkout}"\n\n'
        '[worker]\naccount = "claude_code:main"\n'
    )
    return {"manifest": manifest, "overlay": overlay, "binding": tmp_path / "binding.json"}


def _program(tmp_path) -> Path:
    return tmp_path / "checkout" / "poller" / "extensions" / "probe.py"


def _ran(tmp_path) -> bool:
    """Did the engine execute the extension program at all since the last forget?"""
    return _program(tmp_path).with_suffix(".ran").exists()


def _forget_runs(tmp_path) -> None:
    """Discard the runs activation itself made, so a wake's are the only ones left."""
    _program(tmp_path).with_suffix(".ran").unlink(missing_ok=True)


@pytest.fixture
def in_process(monkeypatch):
    """Run each extension program inside this process instead of starting Python for it.

    The program's own code still runs, re-read from disk on every call, with its hook as
    ``argv[1]`` and the payload on stdin; only the interpreter start is skipped. What a real
    process adds (exit codes, a timeout, a stripped environment) is pinned by
    ``test_poller_engine_extensions.py`` and by the other wakes in this file.
    """

    def run(argv, payload, timeout):
        out, err = io.StringIO(), io.StringIO()
        saved = sys.argv, sys.stdin
        sys.argv, sys.stdin = [*argv[1:]], io.StringIO(payload)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                runpy.run_path(argv[1], run_name="__main__")
            code = 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else int(exc.code is not None)
        finally:
            sys.argv, sys.stdin = saved
        return code, out.getvalue(), err.getvalue()

    monkeypatch.setattr(extensions, "run_program", run)


def _activate(files) -> None:
    """Resolve and publish a binding, the way a completed activation leaves one."""
    first = engine_config.inspect(files["manifest"], files["overlay"])
    roots = [Path(p).expanduser() for p in (first.overlay.checkouts or {}).values()]
    installed, problems = extensions.discover(
        first.manifest.extensions,
        roots,
        allowed=cli.allowed_hooks(first.manifest),
        interpreter=sys.executable,
    )
    assert not problems, problems
    loaded, report = engine_config.resolve(
        files["manifest"], files["overlay"], _Discovery(),
        extension_validators={e.name: extensions.config_validator(e) for e in installed},
    )
    assert loaded is not None, report.render()
    engine_config.write_binding(files["binding"], loaded.binding)


def _spy(monkeypatch) -> list:
    """Record every run_cycle call without changing what it does."""
    seen: list = []
    real = cycle_module.run_cycle

    def recording(store, port, **kwargs):
        seen.append(kwargs)
        return real(store, port, **kwargs)

    monkeypatch.setattr(cycle_module, "run_cycle", recording)
    return seen


def _stub_github(monkeypatch, port=None):
    port = port or FakePort(permissions={"eero": "maintain"})
    monkeypatch.setattr(cli, "_adapter_for_draft", lambda draft: _Discovery())
    monkeypatch.setattr(cli, "_adapter", lambda loaded, paths=None: port)
    monkeypatch.setattr(cli, "_provision_steps", lambda loaded, adapter, paths: [])
    return port


def _cycle_argv(files) -> list[str]:
    return [
        "cycle",
        "--manifest", str(files["manifest"]),
        "--overlay", str(files["overlay"]),
        "--binding", str(files["binding"]),
    ]


def _propose_argv(files, reference="WEB-7") -> list[str]:
    return [
        "propose",
        "--manifest", str(files["manifest"]),
        "--overlay", str(files["overlay"]),
        "--binding", str(files["binding"]),
        "--extension", "probe",
        reference,
    ]


def test_propose_stages_a_pinned_extensions_request_for_the_next_wake(
    tmp_path, monkeypatch, in_process
):
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    files = _instance(tmp_path, with_extension=True, body=_PROPOSAL_EXTENSION)
    _activate(files)
    _stub_github(monkeypatch)

    assert cli.main(_propose_argv(files)) == 0

    state = StateStore(EnginePaths.from_base(tmp_path / "state.json")).read()
    assert state["proposal_inbox"] == [
        {
            "extension": "probe",
            "kind": "open-work",
            "payload": {
                "repo": "store", "key": "WEB-7", "title": "WEB-7",
                "instruction": "fix WEB-7",
            },
        }
    ]


def test_propose_is_idempotent_before_the_next_wake(tmp_path, monkeypatch, in_process, capsys):
    files = _instance(tmp_path, with_extension=True, body=_PROPOSAL_EXTENSION)
    _activate(files)
    _stub_github(monkeypatch)

    assert cli.main(_propose_argv(files)) == 0
    assert cli.main(_propose_argv(files)) == 0
    assert "already proposed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("body", "said"),
    [
        (_MALFORMED_PROPOSAL_EXTENSION, "malformed proposal request"),
        # There is no card to queue work on since #3732.
        (_QUEUE_WORK_PROPOSAL_EXTENSION, "asked to queue work on a card"),
    ],
)
def test_propose_reports_a_request_it_cannot_stage_as_failure(
    tmp_path, monkeypatch, in_process, capsys, body, said
):
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    files = _instance(tmp_path, with_extension=True, body=body)
    _activate(files)
    _stub_github(monkeypatch)

    assert cli.main(_propose_argv(files)) == 65
    captured = capsys.readouterr()
    assert said in captured.err
    assert "already proposed" not in captured.out
    state = StateStore(EnginePaths.from_base(tmp_path / "state.json")).read()
    assert "proposal_inbox" not in state


def test_a_wake_hands_the_cycle_the_extensions_the_manifest_enables(
    tmp_path, monkeypatch, in_process
):
    """Without this the extension system is inert at runtime: observe hooks propose nothing,
    declared commands are not commands, and release gates are never consulted — while every
    unit test still passes, because they hand the extensions over themselves."""
    files = _instance(tmp_path, with_extension=True)
    _activate(files)
    _stub_github(monkeypatch)
    seen = _spy(monkeypatch)

    assert cli.main(_cycle_argv(files)) == 0
    assert seen, "the cycle never ran"
    installed = seen[0]["enabled_extensions"]
    assert [extension.name for extension in installed] == ["probe"]


def test_the_manifests_control_labels_reach_the_adapter_and_provisioner(
    tmp_path, monkeypatch
):
    """Configured-but-unwired is exactly what this file exists to catch: the engine never
    learns the label's name, so an adapter built without it reports every card unlabelled and
    the whole standing-approval path is inert while every unit test still passes (#543)."""
    from agentdeck.poller_engine.state import EnginePaths

    files = _instance(tmp_path, with_extension=False)
    files["manifest"].write_text(
        _MANIFEST.replace(
            "project_number = 7",
            'project_number = 7\n'
            'ownership_label = ["agent", "claude"]\n'
            'automerge_label = "automerge"\n'  # retired (#2502): ignored, provisions nothing
            'spec_only_label = "spec-only"',
        )
        + "\n[capabilities]\nguarded_release = true\nmerge_review = true\n"
    )
    _activate(files)
    loaded = engine_config.load(files["manifest"], files["overlay"], files["binding"])
    adapter = cli._adapter(loaded)
    assert adapter.ownership_label == "agent"
    assert not hasattr(adapter, "automerge_label")

    paths = EnginePaths.from_base(tmp_path / "state.json")
    steps = cli._provision_steps(loaded, adapter, paths)
    created = []
    monkeypatch.setattr(
        adapter.api,
        "run",
        lambda *args: (created.append(args) or (0, "", "")),
    )
    ownership_steps = [
        step
        for step in steps
        if step.name == "label:store" or step.name.startswith("ownership-alias-label:")
    ]
    assert [step.name for step in ownership_steps] == [
        "label:store",
        "ownership-alias-label:claude:store",
    ]
    assert all(step.apply() for step in ownership_steps)
    assert [args[2] for args in created] == ["agent", "claude"]
    assert not any(step.name.startswith("automerge-label:") for step in steps)
    assert any(step.name.startswith("spec-only-label:") for step in steps)


def test_the_manifests_github_reserves_reach_the_adapter(tmp_path):
    files = _instance(tmp_path, with_extension=False)
    files["manifest"].write_text(
        _MANIFEST.replace(
            "worker_cap = 2",
            "worker_cap = 2\ngraphql_reserve = 350\ncore_reserve = 800",
        )
    )
    _activate(files)
    loaded = engine_config.load(files["manifest"], files["overlay"], files["binding"])

    adapter = cli._adapter(loaded)

    assert adapter.graphql_reserve == 350
    assert adapter.core_reserve == 800


def test_a_wake_refuses_only_the_extension_it_cannot_find(tmp_path, monkeypatch):
    """A missing program is runtime health, not a reason to stop the whole poller (#2158): the
    wake runs, the extension is loaded refused, and its health record says what is wrong."""
    files = _instance(tmp_path, with_extension=True)
    _activate(files)
    # The program disappears after activation — a checkout moved, a file deleted.
    _program(tmp_path).unlink()
    _stub_github(monkeypatch)
    seen = _spy(monkeypatch)

    assert cli.main(_cycle_argv(files)) == 0
    assert seen, "one missing extension refused an entire wake"
    assert "no program for it was found" in seen[0]["enabled_extensions"][0].refused
    assert _health_of(tmp_path)["state"] == "degraded"


# --- extension code is software, the hook list is policy (#2158) ---

def _health_of(tmp_path, name="probe") -> dict:
    from agentdeck.poller_engine import health
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    state = StateStore(EnginePaths.from_base(str(tmp_path / "state.json"))).read()
    return (health.record(state)["last_report"]["extensions"] or {}).get(name) or {}


def test_edited_extension_code_runs_on_the_next_wake_without_re_activation(
    tmp_path, monkeypatch, in_process
):
    """Case 1: fixing an extension is a deployment. The next wake runs the new code at the
    hooks the Manifest allows, and nothing asks for the project to be applied again."""
    files = _instance(tmp_path, with_extension=True)
    _activate(files)
    _program(tmp_path).write_text(_program(tmp_path).read_text() + "# a fix, deployed\n")
    _forget_runs(tmp_path)
    _stub_github(monkeypatch)
    seen = _spy(monkeypatch)

    assert cli.main(_cycle_argv(files)) == 0
    assert _ran(tmp_path), "the deployed extension was never run"
    loaded = seen[0]["enabled_extensions"][0]
    assert loaded.refused == ""
    assert loaded.hooks == frozenset({extensions.Hook.HEALTH, extensions.Hook.VALIDATE_CONFIG})
    assert _health_of(tmp_path)["state"] == "ok"
    assert _health_of(tmp_path)["source"] == str(_program(tmp_path))


def test_an_activation_records_no_extension_code(tmp_path):
    """Case 12: nothing about the program's bytes is written into new binding state."""
    files = _instance(tmp_path, with_extension=True)
    _activate(files)
    binding = json.loads(Path(files["binding"]).read_text())
    assert binding["extension_pins"] == {}
    assert "code_hash" not in Path(files["binding"]).read_text()


def test_a_wake_does_not_grant_a_hook_the_program_newly_declares(tmp_path, monkeypatch, in_process):
    """Case 3, end to end: the program adds `observe`; the Manifest never allowed it."""
    files = _instance(tmp_path, with_extension=True)
    _activate(files)
    _program(tmp_path).write_text(_program(tmp_path).read_text().replace(
        '["health", "validate-config"]', '["health", "observe", "validate-config"]'
    ))
    _stub_github(monkeypatch)
    seen = _spy(monkeypatch)

    assert cli.main(_cycle_argv(files)) == 0
    loaded = seen[0]["enabled_extensions"][0]
    assert not loaded.implements(extensions.Hook.OBSERVE)
    assert _health_of(tmp_path)["ignored_hooks"] == ["observe"]


def test_an_extension_whose_manifest_states_no_hooks_runs_refused(tmp_path, monkeypatch):
    """Case 11. Old binding pins no longer stand in for a missing hook list (#2196): the wake
    loads the extension refused, and the offline check asks for the list."""
    files = _instance(tmp_path, with_extension=True)
    manifest = files["manifest"].read_text()
    files["manifest"].write_text(manifest.replace('hooks = ["health", "validate-config"]\n', ""))
    loaded, report = engine_config.resolve(files["manifest"], files["overlay"], _Discovery(),
                                           extension_validators={"probe": lambda *_: None})
    assert loaded is not None, report.render()
    engine_config.write_binding(files["binding"], loaded.binding)
    _forget_runs(tmp_path)
    _stub_github(monkeypatch)
    seen = _spy(monkeypatch)

    assert cli.main(_cycle_argv(files)) == 0
    probe = seen[0]["enabled_extensions"][0]
    assert probe.refused
    assert not probe.implements(extensions.Hook.HEALTH)

    first = engine_config.inspect(files["manifest"], files["overlay"])
    _, problems = cli._enabled_extensions(first.manifest, first.overlay)
    assert "states no hooks" in problems[0].what


def test_a_project_with_no_extensions_still_wakes(tmp_path, monkeypatch):
    files = _instance(tmp_path, with_extension=False)
    _activate(files)
    _stub_github(monkeypatch)
    seen = _spy(monkeypatch)

    assert cli.main(_cycle_argv(files)) == 0
    assert seen[0]["enabled_extensions"] == []


def test_the_dry_run_shadow_cycle_sees_the_same_wake_inputs_and_apply_runs_none(
    tmp_path, monkeypatch, in_process, capsys
):
    """The diagnostic shadow cycle is a production-shaped wake; an apply runs no wake at all."""
    files = _instance(tmp_path, with_extension=True)
    _stub_github(monkeypatch)
    seen = _spy(monkeypatch)
    common = [
        "activate",
        "--manifest", str(files["manifest"]),
        "--overlay", str(files["overlay"]),
        "--binding", str(files["binding"]),
        "--instance", "demo",
    ]

    assert cli.main(common) == 0
    assert len(seen) == 1
    assert [e.name for e in seen[0]["enabled_extensions"]] == ["probe"]
    assert seen[0]["issue_repos"] == frozenset({"store"})

    assert cli.main([*common, "--apply", "eero"]) == 0, capsys.readouterr().err
    assert len(seen) == 1, "an apply ran a canary wake"

    published = json.loads(Path(files["binding"]).read_text())
    assert published["approval"]["activated_by"] == "eero"
    assert published["approval"]["activated_at"]
    assert "mode" not in published["approval"]


def test_activation_no_longer_offers_to_carry_an_approval(tmp_path):
    """Carrying consent existed only for engine releases (#2153); a human approves, or nothing."""
    files = _instance(tmp_path, with_extension=False)
    with pytest.raises(SystemExit):
        cli.main([*_preview_argv(files), "--carry-approval", "--fingerprint", "x"])


def test_a_drifted_instance_records_the_refusal_instead_of_going_quiet(tmp_path, monkeypatch):
    """#107's health record exists so an instance is never mistaken for healthy. Returning
    without writing anything makes a *refused* instance look merely stale, which is the one
    confusion the record is there to prevent."""
    from agentdeck.poller_engine import health
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    files = _instance(tmp_path, with_extension=False)
    _activate(files)
    _stub_github(monkeypatch)

    # Move the Manifest after activation: exactly what the fingerprint exists to catch.
    files["manifest"].write_text(_MANIFEST.replace("worker_cap = 2", "worker_cap = 3"))

    assert cli.main(_cycle_argv(files)) == 75

    state = StateStore(EnginePaths.from_base(str(tmp_path / "state.json"))).read()
    record = health.record(state)
    assert record, "the drift refusal left no health record at all"
    assert "drift" in json.dumps(record), record


def test_the_binding_is_read_from_disk_and_never_rediscovered(tmp_path, monkeypatch):
    """A wake must be cheap and must use exactly what was approved."""
    files = _instance(tmp_path, with_extension=False)
    _activate(files)
    _stub_github(monkeypatch)

    def explode(draft):
        raise AssertionError("a wake re-ran discovery")

    monkeypatch.setattr(cli, "_adapter_for_draft", explode)
    assert cli.main(_cycle_argv(files)) == 0
    assert Path(files["binding"]).exists()


def test_configuration_this_software_cannot_load_stops_that_instance_and_says_why(
    tmp_path, monkeypatch, capsys
):
    """A deploy whose engine refuses one project's files fails that instance closed (#2153).

    Nothing else is held: the refusal is this instance's own health record, which is what the
    poller page shows, rather than a deploy-wide gate.
    """
    from agentdeck.poller_engine import health
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    files = _instance(tmp_path, with_extension=False)
    _activate(files)
    _stub_github(monkeypatch)
    files["manifest"].write_text(
        files["manifest"].read_text().replace("contract = 1", "contract = 99", 1)
    )

    assert cli.main(_cycle_argv(files)) == 78
    assert "contract 99" in capsys.readouterr().err
    record = health.record(StateStore(EnginePaths.from_base(str(tmp_path / "state.json"))).read())
    assert "contract 99" in json.dumps(record), record


def test_an_instance_never_activated_is_left_without_a_health_record(tmp_path, monkeypatch):
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    files = _instance(tmp_path, with_extension=False)
    _stub_github(monkeypatch)

    assert cli.main(_cycle_argv(files)) == 78
    assert not StateStore(EnginePaths.from_base(str(tmp_path / "state.json"))).read()


def _preview_argv(files) -> list[str]:
    return [
        "activate",
        "--manifest", str(files["manifest"]),
        "--overlay", str(files["overlay"]),
        "--binding", str(files["binding"]),
        "--instance", "demo",
    ]


def test_a_preview_leaves_durable_state_exactly_as_it_found_it(tmp_path, monkeypatch, capsys):
    """#109 says nothing before the human gate changes anything.

    `_ShadowPort` intercepts `apply`, which covers every *external* write because they all leave
    through that one chokepoint. Durable state never passes through it — `store.transaction()`
    saves on clean exit — so the shadow wake runs against a scratch copy.
    """
    files = _instance(tmp_path, with_extension=False)
    _stub_github(monkeypatch, FakePort(permissions={"eero": "maintain"}))

    state = tmp_path / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    before = json.dumps({"schema_version": 1, "data": {
        "issues": {"store#5": {"status": "In progress"}},
    }}, indent=2)
    state.write_text(before)

    assert cli.main(_preview_argv(files)) == 0
    first = json.loads(capsys.readouterr().out)["dry_run"]
    assert cli.main(_preview_argv(files)) == 0
    second = json.loads(capsys.readouterr().out)["dry_run"]

    # The engine holds no cards (#3732): nothing to dispatch, nothing to write.
    assert first["shadow"] == second["shadow"] == {"would_apply": []}
    assert state.read_text() == before
