"""Activation is one validated configuration apply (#2157, replacing the staged machine of #180).

Each test drives the real `activate` command with GitHub stubbed at the adapter seam only, so
what is pinned is the operator-visible contract: what a dry run writes (nothing), what refuses
before anything is published, and what a successful apply leaves for the next wake to load.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from agentdeck.poller_engine import activation
from agentdeck.poller_engine.activation import ProvisionStep
from agentdeck.poller_engine.config import Reachability, RepoCheck

_NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)

_CLI_MANIFEST = """
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

[authorization]
release = ["eero"]
work = ["eero"]
"""

@pytest.fixture(autouse=True)
def _example_speaks_as_the_agent(tmp_path, monkeypatch):
    """The fixture org is mapped to a non-human authority, as every real owner must be (#2289)."""
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text('[owners]\nexample = "agent"\n', encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))



class _FakeDiscovery:
    """The read-only lookups activation needs, answered without a network.

    ``project_id`` is settable so a test can move the board between applies, and
    ``reachability`` so a test can make the required repository unreachable.
    """

    def __init__(self, project_id: str = "PVT_1") -> None:
        self.project_id = project_id
        self.reachability = Reachability.REACHABLE

    def board(self, org, project_number):
        from agentdeck.poller_engine.config import BoardSnapshot

        return BoardSnapshot(
            project_id=self.project_id,
            status_field_id="F_1",
            status_options={
                "Todo": "o1", "In Progress": "o2", "In review": "o3",
                "Done": "o4", "Needs More Info": "o5", "Blocked": "o6",
            },
        )

    def repo_check(self, org, repo_id):
        return RepoCheck(self.reachability)


class _Steps:
    """Provisioning steps that record every run, and fail on demand."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.fail = False

    def __call__(self, loaded, adapter, paths):
        def label() -> bool:
            self.runs.append("label")
            return not self.fail

        return [ProvisionStep("label:store", label)]


def _activation_cli(tmp_path, monkeypatch, discovery=None, steps=None):
    """The CLI with GitHub stubbed at the adapter seam, and nowhere else."""
    from poller_engine_fakes import FakePort

    from agentdeck.poller_engine import __main__ as cli

    manifest = tmp_path / "poller.toml"
    manifest.write_text(_CLI_MANIFEST)
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        "contract = 1\n\n"
        f'[paths]\nstate = "{tmp_path / "state.json"}"\n\n'
        f'[checkouts]\nstore = "{tmp_path / "store"}"\n\n'
        '[worker]\naccount = "claude_code:main"\n'
    )
    port = FakePort(permissions={"eero": "maintain"})
    board = discovery or _FakeDiscovery()
    monkeypatch.setattr(cli, "_adapter_for_draft", lambda draft: board)
    monkeypatch.setattr(cli, "_adapter", lambda loaded, paths=None: port)
    monkeypatch.setattr(cli, "_provision_steps", steps or (lambda loaded, adapter, paths: []))
    return cli, manifest, overlay, tmp_path / "binding.json"


def _args(manifest, overlay, binding, *extra) -> list[str]:
    return [
        "activate", "--manifest", str(manifest), "--overlay", str(overlay),
        "--binding", str(binding), "--instance", "demo", *extra,
    ]


def _apply(cli, manifest, overlay, binding, login="eero") -> int:
    return cli.main(_args(manifest, overlay, binding, "--apply", login))


def _no_cycle(*_args, **_kwargs):
    raise AssertionError("activation ran a wake")


def _state_of(tmp_path) -> dict:
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    return StateStore(EnginePaths.from_base(str(tmp_path / "state.json"))).read()


# --- a dry run shows the plan and writes nothing ---


def test_a_dry_run_shows_the_plan_and_writes_nothing(tmp_path, monkeypatch, capsys):
    steps = _Steps()
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch, steps=steps)
    before = sorted(path.name for path in tmp_path.iterdir())

    assert cli.main(_args(manifest, overlay, binding)) == 0
    shown = json.loads(capsys.readouterr().out)["dry_run"]

    assert shown["binding"]["project_id"] == "PVT_1"
    assert shown["provision"] == ["label:store"]
    assert shown["plan"]["status_backend"] == "project"
    assert "shadow" in shown, "the read-only shadow cycle is still shown as a diagnostic"
    assert steps.runs == [], "a dry run provisioned"
    assert sorted(path.name for path in tmp_path.iterdir()) == before, "a dry run wrote a file"


# --- the apply is the one gate, and it checks the config as it is now ---


def test_an_invalid_manifest_cannot_be_applied(tmp_path, monkeypatch, capsys):
    steps = _Steps()
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch, steps=steps)
    manifest.write_text(_CLI_MANIFEST.replace('org = "example"\n', ""))

    assert _apply(cli, manifest, overlay, binding) == 78
    assert not binding.exists()
    assert steps.runs == []


def test_an_unauthorized_apply_refuses_before_anything_external(tmp_path, monkeypatch, capsys):
    steps = _Steps()
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch, steps=steps)

    assert _apply(cli, manifest, overlay, binding, login="drive-by") == 77
    assert "does not hold Release authority" in capsys.readouterr().err
    assert steps.runs == [], "an unauthorized apply provisioned"
    assert not binding.exists()
    assert not (tmp_path / "activation.json").exists()


def test_a_deck_that_is_not_the_main_one_is_turned_on_with_work_authority(
    tmp_path, monkeypatch, capsys
):
    """#3755: a coworker's deck on a shared project needs Work, not Release, to turn it on."""
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    manifest.write_text(_CLI_MANIFEST + '\n[decks]\nmain = "eero"\n')  # coworker has `write`
    accounts = tmp_path / "github-authority.toml"  # the autouse fixture's file
    mapped = accounts.read_text()

    accounts.write_text(mapped + '[deck]\nperson = "eero"\n')  # the main deck needs Release
    assert _apply(cli, manifest, overlay, binding, login="coworker") == 77
    assert "does not hold Release authority" in capsys.readouterr().err

    accounts.write_text(mapped + '[deck]\nperson = "coworker"\n')
    assert _apply(cli, manifest, overlay, binding, login="coworker") == 0  # `write` is enough
    assert binding.exists()


def test_an_unreachable_required_repository_fails_before_publish(tmp_path, monkeypatch, capsys):
    board = _FakeDiscovery()
    board.reachability = Reachability.DENIED
    steps = _Steps()
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch, board, steps)

    assert _apply(cli, manifest, overlay, binding) == 78
    assert steps.runs == []
    assert not binding.exists()


def test_an_earlier_dry_run_does_not_matter_because_apply_resolves_again(
    tmp_path, monkeypatch, capsys
):
    """There is no preview fingerprint to carry: the apply describes the board as it is now."""
    board = _FakeDiscovery()
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch, board)
    assert cli.main(_args(manifest, overlay, binding)) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]["binding"]["project_id"] == "PVT_1"

    board.project_id = "PVT_2"
    assert _apply(cli, manifest, overlay, binding) == 0, capsys.readouterr().err
    assert json.loads(binding.read_text())["project_id"] == "PVT_2"


def test_the_old_approval_arguments_are_gone(tmp_path, monkeypatch, capsys):
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    for extra in (["--fingerprint", "abc"], ["--carry-approval"], ["--plan", "{}"]):
        with pytest.raises(SystemExit):
            cli.main(_args(manifest, overlay, binding, "--apply", "eero", *extra))
    assert not binding.exists()


# --- a successful apply publishes, and nothing more ---


def test_a_successful_apply_publishes_a_binding_the_next_wake_can_load(
    tmp_path, monkeypatch, capsys
):
    from agentdeck.poller_engine import config as engine_config
    from agentdeck.poller_engine import cycle as engine_cycle

    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    # No canary: the next normal wake is the first real use.
    monkeypatch.setattr(engine_cycle, "run_cycle", _no_cycle)

    assert _apply(cli, manifest, overlay, binding) == 0, capsys.readouterr().err

    loaded = engine_config.load(manifest, overlay, binding)
    assert loaded.binding.project_id == "PVT_1"
    assert loaded.binding.status_option_ids["Todo"] == "o1"
    assert loaded.binding.approval["activated_by"] == "eero"


def test_the_audit_record_is_small_and_names_who_applied_what(tmp_path, monkeypatch, capsys):
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    assert _apply(cli, manifest, overlay, binding) == 0, capsys.readouterr().err

    record = json.loads((tmp_path / "activation.json").read_text())
    fingerprint = json.loads(binding.read_text())["fingerprint"]
    assert record["instance"] == "demo"
    assert record["activated_by"] == "eero"
    assert record["activated_at"]
    assert record["config_fingerprint"] == fingerprint["manifest"]
    assert record["binding_fingerprint"] == fingerprint["binding"]
    assert record["fingerprint"] == fingerprint["digest"]
    # No stage history, no preview, no canary, no health confirmation.
    for gone in ("history", "preview_fingerprint", "shadow", "canary", "health", "active_at"):
        assert gone not in record, gone


def test_a_reconfiguration_replaces_the_binding_and_keeps_one_previous(
    tmp_path, monkeypatch, capsys
):
    board = _FakeDiscovery()
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch, board)
    assert _apply(cli, manifest, overlay, binding) == 0
    first = json.loads((tmp_path / "activation.json").read_text())

    board.project_id = "PVT_2"
    assert _apply(cli, manifest, overlay, binding) == 0, capsys.readouterr().err
    assert json.loads(binding.read_text())["project_id"] == "PVT_2"
    record = json.loads((tmp_path / "activation.json").read_text())
    assert record["previous"]["fingerprint"] == first["fingerprint"]
    assert "previous" not in record["previous"], "the record grew into a history"
    assert not list(tmp_path.glob(".binding.json.*")), "a temporary file was left behind"


# --- provisioning is idempotent, and a failure publishes nothing ---


def test_a_provisioning_retry_just_runs_the_steps_again(tmp_path, monkeypatch, capsys):
    steps = _Steps()
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch, steps=steps)
    steps.fail = True

    assert _apply(cli, manifest, overlay, binding) == 75
    assert "provisioning failed: label:store" in capsys.readouterr().err
    assert not binding.exists(), "a first activation published before provisioning landed"
    assert not (tmp_path / "activation.json").exists()

    steps.fail = False
    assert _apply(cli, manifest, overlay, binding) == 0, capsys.readouterr().err
    assert steps.runs == ["label", "label"]
    assert binding.exists()


def test_a_failed_provision_leaves_the_active_binding_selected(tmp_path, monkeypatch, capsys):
    board, steps = _FakeDiscovery(), _Steps()
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch, board, steps)
    assert _apply(cli, manifest, overlay, binding) == 0
    active_binding = binding.read_text()
    active_record = (tmp_path / "activation.json").read_text()

    board.project_id = "PVT_2"
    steps.fail = True
    assert _apply(cli, manifest, overlay, binding) == 75

    assert binding.read_text() == active_binding
    assert (tmp_path / "activation.json").read_text() == active_record


def test_every_provision_step_runs_even_after_one_fails():
    ran: list[str] = []

    def boom() -> bool:
        raise RuntimeError("label API down")

    failed = activation.provision([
        ProvisionStep("a", boom),
        ProvisionStep("b", lambda: ran.append("b") or True),
    ])
    assert [step.name for step in failed] == ["a"]
    assert "label API down" in failed[0].detail
    assert ran == ["b"]


# --- the record is atomic, and an old one still reads ---


def test_the_audit_view_reads_the_record_an_apply_writes():
    record = {
        "instance": "demo", "contract": 1,
        "activated_at": "2026-09-25T04:14:23+00:00", "activated_by": "eero",
        "config_fingerprint": "m-1", "binding_fingerprint": "b-1", "fingerprint": "d-1",
    }
    assert activation.audit_view(record) == record
    assert activation.audit_view(None)["activated_by"] == ""


def test_a_record_in_the_staged_machine_shape_is_no_longer_read():
    """The pre-#2157 field names were read until every instance was applied again (#2196)."""
    view = activation.audit_view({
        "approved_by": "eero", "active_at": "2026-07-26T14:18:00+00:00",
        "fingerprint": {"manifest": "m-1", "binding": "b-1", "digest": "d-1"},
    })
    assert view["activated_by"] == view["activated_at"] == view["fingerprint"] == ""


# --- after the apply, normal loading and health fail closed ---


def test_drift_after_an_apply_still_fails_closed_on_the_next_wake(tmp_path, monkeypatch, capsys):
    from agentdeck.poller_engine import config as engine_config

    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    assert _apply(cli, manifest, overlay, binding) == 0
    manifest.write_text(_CLI_MANIFEST.replace("worker_cap = 2", "worker_cap = 3"))

    with pytest.raises(engine_config.DriftError):
        engine_config.load(manifest, overlay, binding)


def test_an_apply_lifts_a_hold_the_old_activation_left(tmp_path, monkeypatch, capsys):
    """The staged machine paused a first activation it rolled back; nothing else lifts that."""
    from agentdeck.poller_engine import health
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    store = StateStore(EnginePaths.from_base(str(tmp_path / "state.json")))
    with store.transaction() as state:
        health.set_paused(state, True, note="activation rolled back at canary: held off")

    assert _apply(cli, manifest, overlay, binding) == 0, capsys.readouterr().err
    assert not health.is_paused(_state_of(tmp_path))


def test_an_apply_never_lifts_a_pause_an_operator_placed(tmp_path, monkeypatch, capsys):
    from agentdeck.poller_engine import health
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    store = StateStore(EnginePaths.from_base(str(tmp_path / "state.json")))
    with store.transaction() as state:
        health.set_paused(state, True, note="holding this while I look at the board")

    assert _apply(cli, manifest, overlay, binding) == 0, capsys.readouterr().err
    state = _state_of(tmp_path)
    assert health.is_paused(state)
    assert health.pause_note(state) == "holding this while I look at the board"


def test_no_state_machine_canary_or_carry_approval_is_left():
    for gone in (
        "State", "ORDER", "Attempt", "preview", "approve", "canary", "confirm_health",
        "roll_back", "needs_rollback", "carry_approval", "PLAN_INTENT_KEYS",
    ):
        assert not hasattr(activation, gone), gone


# --- a rate-limited repository read says wait, not grant (#707) ---


class _ThrottledRepos(_FakeDiscovery):
    """The board reads fine and the repository read is rate-limited — the live #707 shape."""

    def __init__(self, blob: str) -> None:
        super().__init__()
        from agentdeck.poller_engine.github import Gh, GitHubAdapter

        class _ThrottledGh(Gh):
            def run(self, *args):
                return (1, "", blob)

        # The real adapter, so this crosses the seam between classification and wording.
        self._adapter = GitHubAdapter(
            org="acme",
            issue_repos=("store",),
            project_number=7,
            api=_ThrottledGh(),
            host=_ThrottledGh(),
        )

    def repo_check(self, org, repo_id):
        return self._adapter.repo_check(org, repo_id)


def test_a_rate_limited_dry_run_tells_the_reader_to_wait_not_to_grant(
    tmp_path, monkeypatch, capsys
):
    throttled = _ThrottledRepos("gh: API rate limit exceeded for user ID 42. (HTTP 403)")
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch, discovery=throttled)

    assert cli.main(_args(manifest, overlay, binding)) == 78  # nothing activates on a guess
    printed = capsys.readouterr().err
    assert "manifest.repos.store: GitHub could not answer right now" in printed
    assert "API rate limit exceeded" in printed
    assert "transient" in printed
    assert "credentials" not in printed
    assert "grant access" not in printed


def _live(tmp_path) -> tuple[bytes | None, ...]:
    def read(name):
        path = tmp_path / name
        return path.read_bytes() if path.exists() else None

    return tuple(read(name) for name in ("poller.toml", "binding.json", "activation.json"))


def test_an_apply_is_selected_by_one_link_through_current(tmp_path, monkeypatch, capsys):
    import os

    board = _FakeDiscovery()
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch, board)
    for project in ("PVT_1", "PVT_2", "PVT_3"):
        board.project_id = project
        assert _apply(cli, manifest, overlay, binding) == 0, capsys.readouterr().err

    for name in ("poller.toml", "binding.json", "activation.json"):
        assert os.readlink(tmp_path / name) == f"current/{name}", name
    assert os.readlink(tmp_path / "current").startswith("generations/")
    assert json.loads(binding.read_text())["project_id"] == "PVT_3"
    # The selected generation and the one before it; older ones are dropped.
    assert len(list((tmp_path / "generations").iterdir())) == 2


def test_a_crash_before_the_switch_leaves_the_old_set_selected(tmp_path, monkeypatch, capsys):
    """Nothing a reader opens changes until the one rename, so there is nothing to roll back."""
    from agentdeck.poller_engine import config as engine_config

    board = _FakeDiscovery()
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch, board)
    assert _apply(cli, manifest, overlay, binding) == 0
    before = _live(tmp_path)

    def crash(*_args, **_kwargs):
        raise OSError("killed before the switch")

    board.project_id = "PVT_2"
    monkeypatch.setattr(engine_config, "_switch_current", crash)
    assert _apply(cli, manifest, overlay, binding) == 74
    assert "nothing was published" in capsys.readouterr().err
    assert _live(tmp_path) == before
    assert engine_config.load(manifest, overlay, binding).binding.project_id == "PVT_1"


@pytest.mark.parametrize("plain", ["binding.json", "activation.json"])
def test_an_instance_with_plain_files_is_refused_and_nothing_is_written(
    tmp_path, monkeypatch, capsys, plain
):
    """Plain-file conversion was removed with #2196. Publishing past a plain binding or record
    would leave readers on it while `current` named the new set, so the apply refuses first."""
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    (tmp_path / "binding.json").write_text("{}")
    (tmp_path / plain).write_text("{}")
    before = _live(tmp_path)

    assert _apply(cli, manifest, overlay, binding) == 78
    assert "not on the generations layout" in capsys.readouterr().err
    assert _live(tmp_path) == before
    assert not (tmp_path / "generations").exists()
    assert not (tmp_path / "current").exists()


def test_a_never_applied_instance_may_hold_its_manifest_as_a_plain_file(
    tmp_path, monkeypatch, capsys
):
    """Before the first apply nothing reads it, so the apply replaces it with a link."""
    import os

    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    assert not manifest.is_symlink() and not binding.exists()
    assert _apply(cli, manifest, overlay, binding) == 0, capsys.readouterr().err
    assert os.readlink(manifest) == "current/poller.toml"
    assert os.readlink(binding) == "current/binding.json"


def test_write_binding_rewrites_the_binding_inside_its_generation(
    tmp_path, monkeypatch, capsys
):
    """`write_binding` goes through the link: a plain file would detach the binding from the
    Manifest it was published with."""
    from agentdeck.poller_engine import config as engine_config

    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    assert _apply(cli, manifest, overlay, binding) == 0
    loaded = engine_config.read_binding(binding)
    engine_config.write_binding(binding, loaded)
    assert binding.is_symlink()
    assert engine_config.load(manifest, overlay, binding).binding.project_id == "PVT_1"


def test_the_audit_is_built_and_committed_inside_the_apply_lock(tmp_path):
    """Two applies cannot interleave: publish B, then commit A's audit over it."""
    import fcntl

    from agentdeck.poller_engine import config as engine_config

    candidate = tmp_path / "poller.toml"
    candidate.write_text("manifest")
    binding = tmp_path / "binding.json"
    held: list[bool] = []

    def audit_for(previous):
        with open(tmp_path / "binding.json.lock", "a") as other:
            try:
                fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                held.append(False)
                fcntl.flock(other.fileno(), fcntl.LOCK_UN)
        return b"{}"

    engine_config.publish(
        binding, _resolved_for(candidate),
        manifest_path=candidate, candidate_path=candidate,
        audit_path=tmp_path / "activation.json", audit_for=audit_for,
    )
    assert held == [True]
    assert (tmp_path / "activation.json").read_bytes() == b"{}"


def test_an_old_hold_that_cannot_be_lifted_is_not_success(tmp_path, monkeypatch, capsys):
    from agentdeck.poller_engine import health
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    store = StateStore(EnginePaths.from_base(str(tmp_path / "state.json")))
    with store.transaction() as state:
        health.set_paused(state, True, note="activation rolled back at canary: held off")
    real = health.set_paused

    def fail(*_args, **_kwargs):
        raise OSError("state unwritable")

    monkeypatch.setattr(health, "set_paused", fail)
    assert _apply(cli, manifest, overlay, binding) == 74
    assert "hold is still on" in capsys.readouterr().err

    monkeypatch.setattr(health, "set_paused", real)
    assert _apply(cli, manifest, overlay, binding) == 0, capsys.readouterr().err
    assert not health.is_paused(_state_of(tmp_path))


# --- the apply installs the chosen Manifest beside its binding (#2157, after #2153) ---


def _candidate(tmp_path, text: str = _CLI_MANIFEST):
    candidate = tmp_path / "committed" / "poller.toml"
    candidate.parent.mkdir(exist_ok=True)
    candidate.write_text(text)
    return candidate


def test_an_apply_installs_the_candidate_manifest_with_its_binding(tmp_path, monkeypatch, capsys):
    from agentdeck.poller_engine import config as engine_config

    cli, installed, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    installed.write_text(_CLI_MANIFEST.replace("worker_cap = 2", "worker_cap = 1"))
    candidate = _candidate(tmp_path)

    assert _apply(cli, candidate, overlay, binding) == 0, capsys.readouterr().err

    assert installed.read_bytes() == candidate.read_bytes()
    # The wake reads the installed pair, and it loads without drift.
    assert engine_config.load(installed, overlay, binding).manifest.worker_cap == 2


def test_a_dry_run_names_the_manifest_it_would_install_and_installs_nothing(
    tmp_path, monkeypatch, capsys
):
    cli, installed, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    old = _CLI_MANIFEST.replace("worker_cap = 2", "worker_cap = 1")
    installed.write_text(old)
    candidate = _candidate(tmp_path)

    assert cli.main(_args(candidate, overlay, binding)) == 0
    shown = json.loads(capsys.readouterr().out)["dry_run"]
    assert shown["installs_manifest"] == str(installed)
    assert installed.read_text() == old
    assert not binding.exists()


def test_a_failed_provision_installs_no_manifest(tmp_path, monkeypatch, capsys):
    steps = _Steps()
    steps.fail = True
    cli, installed, overlay, binding = _activation_cli(tmp_path, monkeypatch, steps=steps)
    old = _CLI_MANIFEST.replace("worker_cap = 2", "worker_cap = 1")
    installed.write_text(old)

    assert _apply(cli, _candidate(tmp_path), overlay, binding) == 75
    assert installed.read_text() == old
    assert not binding.exists()


def _resolved_for(candidate) -> object:
    import hashlib

    from agentdeck.poller_engine.config import ResolvedBinding

    return ResolvedBinding(
        project_id="PVT_1", status_field_id="F_1", status_option_ids={},
        fingerprint={"manifest": hashlib.sha256(candidate.read_bytes()).hexdigest()},
    )


@pytest.mark.parametrize("same_path", [True, False])
def test_a_manifest_edited_during_the_apply_is_refused_and_nothing_is_published(
    tmp_path, monkeypatch, capsys, same_path
):
    """Checked inside the publish lock on every apply, including one whose candidate *is* the
    installed file — otherwise the binding would describe bytes that are no longer there."""
    cli, installed, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    candidate = installed if same_path else _candidate(tmp_path)
    before = installed.read_bytes()

    def edit(loaded, adapter, paths):
        def step() -> bool:
            candidate.write_text(_CLI_MANIFEST.replace("worker_cap = 2", "worker_cap = 4"))
            return True

        return [ProvisionStep("edit", step)]

    monkeypatch.setattr(cli, "_provision_steps", edit)
    assert _apply(cli, candidate, overlay, binding) == 78
    assert "changed after it was resolved" in capsys.readouterr().err
    assert not binding.exists()
    assert not (tmp_path / "activation.json").exists()
    if not same_path:
        assert installed.read_bytes() == before


def test_a_load_waits_while_an_apply_holds_the_pair(tmp_path, monkeypatch, capsys):
    import threading

    from agentdeck.poller_engine import config as engine_config

    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch)
    assert _apply(cli, manifest, overlay, binding) == 0
    loaded: list = []
    with engine_config.config_lock(binding, exclusive=True):
        reader = threading.Thread(
            target=lambda: loaded.append(engine_config.load(manifest, overlay, binding))
        )
        reader.start()
        reader.join(0.3)
        assert reader.is_alive(), "a load read the files while an apply was replacing them"
    reader.join(5)
    assert loaded and loaded[0].binding.project_id == "PVT_1"


def test_an_apply_that_cannot_take_the_instance_lock_publishes_nothing(
    tmp_path, monkeypatch, capsys
):
    """The apply's atomicity rests on the lock, so an unusable lock refuses instead of running
    unlocked; a reader in the same position still loads, because it fails closed on its own."""
    from agentdeck.poller_engine import config as engine_config

    board = _FakeDiscovery()
    cli, manifest, overlay, binding = _activation_cli(tmp_path, monkeypatch, board)
    assert _apply(cli, manifest, overlay, binding) == 0
    active = (binding.read_bytes(), (tmp_path / "activation.json").read_bytes())
    lock = tmp_path / "binding.json.lock"
    lock.unlink()
    lock.mkdir()  # a lock path that cannot be opened as a file

    board.project_id = "PVT_2"
    assert _apply(cli, manifest, overlay, binding) == 74
    assert "nothing was published" in capsys.readouterr().err
    assert (binding.read_bytes(), (tmp_path / "activation.json").read_bytes()) == active

    assert engine_config.load(manifest, overlay, binding).binding.project_id == "PVT_1"
    with pytest.raises(OSError):
        with engine_config.config_lock(binding, exclusive=True):
            pass
