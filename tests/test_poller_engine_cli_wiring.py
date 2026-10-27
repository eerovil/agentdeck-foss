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
from datetime import UTC, datetime
from pathlib import Path

import pytest
from poller_engine_fakes import FakePort

from agentdeck.poller_engine import __main__ as cli
from agentdeck.poller_engine import config as engine_config
from agentdeck.poller_engine import cycle as cycle_module
from agentdeck.poller_engine import extensions
from agentdeck.poller_engine.ownership import LabelEvent

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

# A release gate that reports every watched build green — the extension a project trusts to say
# whether production actually took the merge.
_GATE_EXTENSION = """
from pathlib import Path
Path(__file__).with_suffix(".ran").write_text("x")
hook = sys.argv[1] if len(sys.argv) > 1 else ""
payload = json.loads(sys.stdin.read() or "{}")
if hook == "hooks":
    print(json.dumps({"hooks": ["health", "release-gate", "validate-config"]}))
elif hook == "validate-config":
    print(json.dumps({"problems": []}))
elif hook == "release-gate":
    print(json.dumps({"requests": [
        {"kind": "release-status", "payload": {"repo": repo, "status": "ok"}}
        for repo in (payload.get("context") or {}).get("builds") or {}
    ]}))
elif hook == "health":
    print(json.dumps({"health": {"state": "ok"}}))
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


def test_propose_reports_a_malformed_extension_request_as_failure(
    tmp_path, monkeypatch, in_process, capsys
):
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    files = _instance(tmp_path, with_extension=True, body=_MALFORMED_PROPOSAL_EXTENSION)
    _activate(files)
    _stub_github(monkeypatch)

    assert cli.main(_propose_argv(files)) == 65
    captured = capsys.readouterr()
    assert "malformed proposal request" in captured.err
    assert "already proposed" not in captured.out
    state = StateStore(EnginePaths.from_base(tmp_path / "state.json")).read()
    assert "proposal_inbox" not in state


def test_register_worktree_persists_immediately_for_the_active_generation(
    tmp_path, monkeypatch
):
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    state_path = tmp_path / "state.json"
    store = StateStore(EnginePaths.from_base(state_path))
    with store.transaction() as state:
        state["issues"] = {"store#5": {"generation": "gen1"}}
    path = tmp_path / "repo" / ".worktrees" / "issue-5"
    monkeypatch.setattr(cli.github, "registered_worktree", lambda value: (path.parent.parent, True))

    code = cli.main([
        "register-worktree",
        "--state", str(state_path),
        "--generation", "gen1",
        "store", "5", str(path),
    ])

    assert code == 0
    assert store.read()["issues"]["store#5"]["worktrees"] == [str(path)]


def test_register_worktree_refuses_a_stale_generation_without_mutating_state(
    tmp_path, monkeypatch
):
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    state_path = tmp_path / "state.json"
    store = StateStore(EnginePaths.from_base(state_path))
    with store.transaction() as state:
        state["issues"] = {"store#5": {"generation": "current"}}
    path = tmp_path / "repo" / ".worktrees" / "issue-5"
    monkeypatch.setattr(cli.github, "registered_worktree", lambda value: (path.parent.parent, True))

    code = cli.main([
        "register-worktree",
        "--state", str(state_path),
        "--generation", "stale",
        "store", "5", str(path),
    ])

    assert code == 75
    assert "worktrees" not in store.read()["issues"]["store#5"]


def test_register_worktree_before_creating_it_says_create_first(tmp_path, capsys):
    """A worker that registers first hears the directory is missing, and nothing is recorded."""
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    state_path = tmp_path / "state.json"
    store = StateStore(EnginePaths.from_base(state_path))
    with store.transaction() as state:
        state["issues"] = {"store#5": {"generation": "gen1"}}
    path = _sibling_worktree(tmp_path).parent / "not-made-yet"

    code = cli.main([
        "register-worktree",
        "--state", str(state_path),
        "--generation", "gen1",
        "store", "5", str(path),
    ])

    assert code == 64
    assert "create it first" in capsys.readouterr().err
    assert "worktrees" not in store.read()["issues"]["store#5"]


def _sibling_worktree(tmp_path):
    """A real worktree of an `acme/lib` checkout, on its own branch, as a worker makes one."""
    import subprocess

    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "HOME": "/x"}

    def git(*args, cwd):
        subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

    checkout = tmp_path / "lib"
    checkout.mkdir()
    git("init", "-q", "--initial-branch=main", cwd=checkout)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "0",
        cwd=checkout)
    git("remote", "add", "origin", "git@github.com:acme/lib.git", cwd=checkout)
    path = checkout / ".worktrees" / "agentdeck-5"
    git("worktree", "add", "-q", "-b", "agent/sibling", str(path), cwd=checkout)
    return path


def _register_with_claim(tmp_path, monkeypatch, answer, *, while_asking=None, already=False):
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    state_path = tmp_path / "state.json"
    store = StateStore(EnginePaths.from_base(state_path))
    path = _sibling_worktree(tmp_path)
    with store.transaction() as state:
        state["issues"] = {
            "store#5": {"generation": "gen1", **({"worktrees": [str(path)]} if already else {})}
        }
    sent = []

    def post(_socket, route, body, **_kwargs):
        sent.append((route, body))
        if while_asking:
            while_asking(store, str(path))
        return answer(str(path))

    monkeypatch.setattr(cli.github, "runtime_post", post)
    code = cli.main([
        "register-worktree",
        "--state", str(state_path),
        "--generation", "gen1",
        "--work-id", "github_issue:acme/store#5",
        "--runtime-socket", "/run/rt.sock",
        "store", "5", str(path),
    ])
    return code, store.read()["issues"]["store#5"].get("worktrees", []), sent, path


def test_a_registered_worktree_becomes_the_works_claim(tmp_path, monkeypatch):
    """#2195: nothing card-level owns a worker's sibling worktree; the Work does."""
    code, registered, sent, path = _register_with_claim(
        tmp_path,
        monkeypatch,
        lambda wanted: (200, {"outcome": "adopted", "claim": {"path": wanted}}),
    )

    assert code == 0
    assert registered == [str(path)]
    ((route, body),) = sent
    assert route == "/work-workspaces/ensure"
    assert body["work_id"] == "github_issue:acme/store#5"
    assert (body["repository"], body["purpose"], body["branch"]) == (
        "acme/lib", "implementation", "agent/sibling"
    )
    assert body["checkout"] == str(path.parent.parent)
    # The runtime adopts only what the card registered, so the registration went first.
    assert body["legacy_paths"] == [str(path)]


@pytest.mark.parametrize(
    "answer, reason",
    [
        (
            lambda _wanted: (200, {"outcome": "reused", "claim": {"path": "/w/lib/.worktrees/x"}}),
            "already has its acme/lib worktree at /w/lib/.worktrees/x",
        ),
        (lambda _wanted: (0, None), "the runtime did not claim it"),
        (lambda _wanted: (200, {"outcome": "legacy", "claim": None}), "legacy"),
    ],
    ids=["second worktree of one repository", "runtime unreachable", "not adoptable"],
)
def test_a_worktree_the_runtime_will_not_claim_is_not_left_on_the_card(
    tmp_path, monkeypatch, capsys, answer, reason
):
    code, registered, _sent, _path = _register_with_claim(tmp_path, monkeypatch, answer)

    assert code == 65
    assert registered == []
    assert reason in capsys.readouterr().err


def test_a_refused_claim_never_erases_a_newer_generations_registration(tmp_path, monkeypatch):
    """#2215 review: the take-back is fenced like the registration it undoes.

    While the runtime was being asked, a newer generation took the card and registered the
    same path. The stale command's refusal must leave that registration alone.
    """
    from agentdeck.poller_engine import reconcile

    def newer_generation_takes_over(store, path):
        with store.transaction() as state:
            state["issues"]["store#5"]["generation"] = "gen2"
            assert reconcile.register_generation_worktrees(state, "store", 5, "gen2", [path])

    code, registered, _sent, path = _register_with_claim(
        tmp_path,
        monkeypatch,
        lambda _wanted: (0, None),
        while_asking=newer_generation_takes_over,
    )

    assert code == 65
    assert registered == [str(path)]


def test_a_refused_claim_never_takes_back_a_registration_it_did_not_add(tmp_path, monkeypatch):
    code, registered, _sent, path = _register_with_claim(
        tmp_path, monkeypatch, lambda _wanted: (0, None), already=True
    )

    assert code == 65
    assert registered == [str(path)]


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
    assert adapter.accepted_ownership_labels == ("agent", "claude")
    assert not hasattr(adapter, "automerge_label")
    assert adapter.spec_only_label == "spec-only"

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


def test_a_release_gated_on_a_broken_extension_does_not_proceed(tmp_path, monkeypatch):
    """Case 8. The project named this extension as its source of deploy truth, so a wake that
    cannot run it has no evidence the deploy went green — and must wait, not fall back to the
    host as though no gate had ever been enabled."""
    from agentdeck.poller_engine.ports import DeployState
    from agentdeck.poller_engine.reconcile import CardSnapshot
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    files = _instance(tmp_path, with_extension=True, body=_GATE_EXTENSION)
    _activate(files)
    # A deployed build that no longer answers at all.
    _program(tmp_path).write_text("raise SystemExit(1)\n")

    store = StateStore(EnginePaths.from_base(str(tmp_path / "state.json")))
    with store.transaction() as state:
        state.update(_watching_a_deploy())
    # The host would happily call this deploy green. Only the project's own gate may say so.
    port = FakePort(
        permissions={"eero": "maintain"},
        snapshots={"store#5": CardSnapshot(status="In progress")},
        deploys={"store": DeployState(status="ok", link="https://ci/1")},
    )
    _stub_github(monkeypatch, port)

    assert cli.main(_cycle_argv(files)) == 0
    rec = store.read()["issues"]["store#5"]
    assert rec.get("awaiting_deploy"), "the release finished without its gate"
    assert rec["awaiting_deploy"]["builds"]["store"]["status"] == "pending"


def _watching_a_deploy() -> dict:
    """A card merged and waiting on production, the state the deploy gate reads."""
    since = datetime.now(UTC).isoformat()
    return {
        "issues": {
            "store#5": {
                "status": "In progress",
                "mode": "merge",
                "started": True,
                "started_at": since,
                "generation": "gen1",
                "merge_control": "3",
                "merged": ["store#7"],
                "awaiting_deploy": {
                    "builds": {"store": {"sha": "sha1", "status": "pending"}},
                    "since": since,
                },
            }
        },
        "controls": {
            "3": {
                "id": "3", "repo": "store", "number": 5, "kind": "merge",
                "payload": None, "status": "processing", "created_at": "t1",
            }
        },
    }


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
    assert seen[0]["auto_deploy_repos"] == frozenset({"store"})

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


# --- worker control interface (#361) ---

def _worker_argv(files, *operation: str) -> list[str]:
    return [
        "worker-control",
        "--manifest", str(files["manifest"]),
        "--overlay", str(files["overlay"]),
        "--binding", str(files["binding"]),
        *operation,
    ]


def test_worker_control_cli_uses_the_activated_instance_adapter(tmp_path, monkeypatch, capsys):
    from agentdeck.poller_engine.reconcile import CardSnapshot
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    files = _instance(tmp_path, with_extension=False)
    _activate(files)
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    _stub_github(monkeypatch, port)
    store = StateStore(EnginePaths.from_base(tmp_path / "state.json"))
    with store.transaction() as state:
        state["issues"] = {"store#5": {"generation": "gen1", "status": "In progress"}}

    body = tmp_path / "body.md"
    body.write_text("Ready for review")
    assert cli.main(_worker_argv(
        files, "finish", "store", "5", "gen1", "in_review", "--body-file", str(body)
    )) == 0
    assert [effect.kind.value for effect in port.applied] == ["comment", "set-status"]
    assert "finished -> In review" in capsys.readouterr().out


def test_worker_control_cli_fails_closed_on_a_stale_generation(tmp_path, monkeypatch, capsys):
    from agentdeck.poller_engine.reconcile import CardSnapshot
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    files = _instance(tmp_path, with_extension=False)
    _activate(files)
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    _stub_github(monkeypatch, port)
    store = StateStore(EnginePaths.from_base(tmp_path / "state.json"))
    with store.transaction() as state:
        state["issues"] = {"store#5": {"generation": "gen1", "status": "In progress"}}

    assert cli.main(_worker_argv(files, "enabled", "store", "5", "old")) == 1
    # The CLI prints the specific reason, and a superseded generation is the one case the
    # word "stale" belongs to (#1651).
    assert "stale: this generation has been superseded" in capsys.readouterr().out
    assert port.applied == []


def test_worker_control_cli_confirms_a_reportless_handoff(tmp_path, monkeypatch, capsys):
    from agentdeck.poller_engine.reconcile import CardSnapshot
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    files = _instance(tmp_path, with_extension=False)
    _activate(files)
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    _stub_github(monkeypatch, port)
    store = StateStore(EnginePaths.from_base(tmp_path / "state.json"))
    with store.transaction() as state:
        state["issues"] = {
            "store#5": {
                "generation": "gen2",
                "reportless_resolved": "gen1",
                "status": "In progress",
            }
        }

    assert cli.main(_worker_argv(files, "enabled", "store", "5", "gen1")) == 1
    assert "handed to a new worker after a turn ended without a report" in capsys.readouterr().out
    assert port.applied == []


def _preview_argv(files) -> list[str]:
    return [
        "activate",
        "--manifest", str(files["manifest"]),
        "--overlay", str(files["overlay"]),
        "--binding", str(files["binding"]),
        "--instance", "demo",
    ]


def test_a_preview_leaves_durable_state_exactly_as_it_found_it(tmp_path, monkeypatch, capsys):
    """#109 says nothing before the human gate changes anything. It used to, invisibly.

    `_ShadowPort` intercepts `apply`, which covers every *external* write because they all leave
    through that one chokepoint. Durable state never passes through it — `store.transaction()`
    saves on clean exit — so the shadow wake advanced the ownership ledger and consumed the very
    starts it was reporting.
    """
    files = _instance(tmp_path, with_extension=False)
    port = FakePort(
        permissions={"eero": "maintain"},
        labelled=["store#5"],
        label_events={"store#5": ["e1"]},
    )
    _stub_github(monkeypatch, port)

    state = tmp_path / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    before = json.dumps({"schema_version": 1, "data": {
        "ownership": {"initialized": True, "events": {}},
        "issues": {},
    }}, indent=2)
    state.write_text(before)

    assert cli.main(_preview_argv(files)) == 0
    shown = json.loads(capsys.readouterr().out)["dry_run"]
    # The preview still saw the real world and reported the start it would make.
    assert shown["shadow"]["would_dispatch"] == ["store#5"]
    # And changed nothing: byte-for-byte, including the ownership ledger it walked.
    assert state.read_text() == before


def test_previewing_twice_reports_the_same_thing_both_times(tmp_path, monkeypatch, capsys):
    """The symptom that exposed it: the second preview came back empty, and the fingerprint
    moved, purely because the first run had eaten the starts."""
    files = _instance(tmp_path, with_extension=False)
    port = FakePort(
        permissions={"eero": "maintain"},
        labelled=["store#5"],
        label_events={"store#5": ["e1"]},
    )
    _stub_github(monkeypatch, port)
    state = tmp_path / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"schema_version": 1, "data": {
        "ownership": {"initialized": True, "events": {}}, "issues": {},
    }}))

    assert cli.main(_preview_argv(files)) == 0
    first = json.loads(capsys.readouterr().out)["dry_run"]
    assert cli.main(_preview_argv(files)) == 0
    second = json.loads(capsys.readouterr().out)["dry_run"]

    assert first["shadow"] == second["shadow"] == {
        # Dispatch writes the board status and nothing else: the card is claimed in the
        # engine's own state, so no label edit is ever previewed (#2053).
        "would_apply": ["set-status"],
        "would_dispatch": ["store#5"],
    }


def test_preview_names_labelled_cards_that_would_be_adopted_without_dispatch(
    tmp_path, monkeypatch, capsys
):
    files = _instance(tmp_path, with_extension=False)
    port = FakePort(
        permissions={"eero": "maintain"},
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("old", "2026-07-20T12:00:00Z")]},
    )
    _stub_github(monkeypatch, port)
    state = tmp_path / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"schema_version": 1, "data": {
        "ownership": {
            "initialized": True,
            "events": {},
            "last_complete_scan_at": "2026-07-25T11:59:00+00:00",
        },
        "issues": {},
    }}))

    assert cli.main(_preview_argv(files)) == 0
    shown = json.loads(capsys.readouterr().out)["dry_run"]

    assert shown["shadow"]["would_dispatch"] == []
    assert shown["shadow"]["would_suppress"] == ["store#5"]
