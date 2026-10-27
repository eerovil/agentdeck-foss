"""The Codex half of the heavy-command queue (#1346).

Codex has no ``PreToolUse`` hook, so the queue reaches its workers through
shadowing executables on their PATH. Two things are worth pinning hardest here:
a whole suite really is refused with the command to run instead, and *every*
other path — no runtime, an escape hatch, a shim that cannot even load — runs
the real program. A shim that fails closed takes this host out of service, which
is the failure it exists to prevent.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agentdeck.heavy_shim import classify, install, main

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "heavy_command_hook.py"


def _hook():
    spec = importlib.util.spec_from_file_location("heavy_command_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_both_enforcement_halves_share_one_notion_of_heavy():
    """A suite refused under Claude and started under Codex is the bug."""
    hook = _hook()

    assert hook.HEAVY is classify.HEAVY
    assert hook.EXEMPT is classify.EXEMPT
    assert hook.reason("pytest -q", "/tmp") == classify.queue_advice("pytest -q", "/tmp")


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "uv run pytest -q -m 'not browser'",
        "uv run python -m pytest -q",
        "/usr/bin/python3 -m pytest -q",
        "npm test",
        "npx playwright test",
        "make test",
        "go test ./...",
    ],
)
def test_a_whole_suite_is_heavy(command):
    assert classify.is_heavy(command)


@pytest.mark.parametrize(
    "command",
    [
        "make --version",
        "npm install",
        "go build ./...",
        "uv run python -c 'print(1)'",
        "agentdeck job submit --cwd . -- pytest -q",
        "AGENTDECK_HEAVY_SLOT_HELD=1 pytest -k one_thing",
        "echo 'x; pytest -q'",
        "printf '%s' 'agentdeck job status; pytest -q'",
        "echo 'x | npm test'",
    ],
)
def test_ordinary_work_is_not(command):
    assert not classify.is_heavy(command)


@pytest.mark.parametrize(
    "command",
    [
        "AGENTDECK_HEAVY_SLOT_HELD=1 echo ok; pytest -q",
        "agentdeck job status; pytest -q",
    ],
)
def test_an_exempt_earlier_segment_does_not_exempt_a_later_suite(command):
    assert classify.is_heavy(command)


# Seen running unqueued on this host on 2026-10-06 (#2666): each started with a
# wrapper, so a rule that only reads the first word never saw the suite inside.
WRAPPED_SUITES = [
    'timeout 1200 podman exec web bash -lc "cd /code/.worktrees/issue-501-store && '
    'source /code/activate; ./pytest.sh app/tests"',
    "podman run --rm --network container:so_redis -v /home/me/trailhead/soi:/code:z "
    "-w /code/.worktrees/issue-57 trailhead_soi:python3 bash -lc 'ln -sf docker.py "
    "soiproject/settings/__init__.py; . /code/activate && python manage.py test soi so.stocks'",
    "docker compose exec web pytest -q",
    "docker compose -f dev.yml run --rm web python -m pytest",
    "docker-compose exec web pytest",
    "podman exec -it bazaar-bazaar-1 pytest tests/",
    "nice -n 10 pytest -q",
    "env FOO=1 pytest -q",
    "nohup pytest -q",
    "bash -c 'pytest -q'",
    "python manage.py test soi",
    "./manage.py test",
    "cd x && ./pytest.sh app",
]


@pytest.mark.parametrize("command", WRAPPED_SUITES)
def test_a_suite_behind_a_wrapper_is_heavy(command):
    assert classify.is_heavy(command)


@pytest.mark.parametrize(
    "command",
    [
        "podman exec web cat pytest.ini",
        "podman ps",
        "podman logs web",
        'podman exec web bash -lc "ls tests"',
        "podman exec web python manage.py migrate",
        "podman run",
        "timeout 5 curl http://localhost",
        "timeout",
        "bash",
        "bash ./pytest.sh",
        "cat manage.py",
        "rg pytest.sh",
        "agentdeck job submit -- podman exec bazaar-bazaar-1 pytest",
        "AGENTDECK_HEAVY_SLOT_HELD=1 timeout 100 pytest -q",
        "AGENTDECK_HEAVY_SLOT_HELD=1 podman exec web pytest app",
    ],
)
def test_a_wrapper_around_ordinary_work_is_not(command):
    assert not classify.is_heavy(command)


def test_a_container_suite_from_a_pod_lane_checkout_is_told_to_drop_the_container(tmp_path):
    """The pod lane takes a bare pytest, so `podman exec …` queued as typed stays here."""
    runner = tmp_path / "helm" / "app" / "jobs" / "test" / "run.sh"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/bin/sh\n")
    store = tmp_path / "web" / ".worktrees" / "issue-501-store"
    store.mkdir(parents=True)

    advice = classify.queue_advice(WRAPPED_SUITES[0], str(store))

    assert f"agentdeck job submit --cwd {store} -- pytest <the same test arguments>" in advice
    assert "leave the container out" in advice


@pytest.mark.parametrize(
    ("command", "inside"),
    [
        (WRAPPED_SUITES[0], True),
        (WRAPPED_SUITES[1], True),
        ("cd x && timeout 9 podman exec web pytest", True),
        ("bash -c 'docker compose exec web pytest'", True),
        ("nice -n 10 pytest -q", False),
        ("pytest -q", False),
    ],
)
def test_only_a_container_command_counts_as_running_in_one(command, inside):
    """A `nice pytest` must not be told to leave out a container it never had."""
    assert classify.runs_in_container(command) is inside


def test_an_soi_container_suite_is_queued_as_typed(tmp_path):
    """soi sits under trailhead's runner but has no suite there yet (#2666 review)."""
    runner = tmp_path / "helm" / "app" / "jobs" / "test" / "run.sh"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/bin/sh\n")
    soi = tmp_path / "soi" / ".worktrees" / "issue-57"
    soi.mkdir(parents=True)

    advice = classify.queue_advice(WRAPPED_SUITES[1], str(soi))

    assert f"agentdeck job submit --cwd {soi} -- podman run --rm" in advice
    assert "leave the container out" not in advice
    assert "Only one local job runs at a time" in advice
    # The trailhead root itself names no suite either.
    assert "leave the container out" not in classify.queue_advice(WRAPPED_SUITES[0], str(tmp_path))


def test_a_pytest_is_offered_only_the_lanes_this_host_has_set_up(tmp_path, monkeypatch):
    """The guard's advice follows job-lanes.toml, so a host without it is told local (#3623)."""
    runner = tmp_path / "scripts" / "kube_pytest.sh"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/bin/sh\n")

    advice = classify.queue_advice("pytest -q", str(tmp_path))
    assert "throwaway cluster pod" in advice and "--where mac" in advice

    only_kube = tmp_path / "job-lanes.toml"
    only_kube.write_text("[lanes.kube]\n", encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_JOB_LANES", str(only_kube))
    advice = classify.queue_advice("pytest -q", str(tmp_path))
    assert "throwaway cluster pod" in advice and "--where mac" not in advice

    monkeypatch.setenv("AGENTDECK_JOB_LANES", str(tmp_path / "absent.toml"))
    advice = classify.queue_advice("pytest -q", str(tmp_path))
    assert "pod" not in advice and "Only one local job runs at a time" in advice


def test_a_container_suite_with_nowhere_else_to_go_is_queued_as_typed(tmp_path):
    advice = classify.queue_advice("podman exec bazaar-bazaar-1 pytest tests/", str(tmp_path))

    assert f"agentdeck job submit --cwd {tmp_path} -- podman exec bazaar-bazaar-1 pytest" in advice
    assert "leave the container out" not in advice
    assert "Only one local job runs at a time" in advice


def test_every_program_a_heavy_command_starts_with_is_shadowed():
    """The launcher list is the coverage: a name missing here is a hole."""
    for program in (
        "pytest", "uv", "npm", "npx", "cargo", "go", "tox", "make", "podman", "docker", "timeout"
    ):
        assert program in classify.SHIMMED_PROGRAMS


def test_the_shim_installs_itself_first_on_a_workers_path(tmp_path, monkeypatch):
    monkeypatch.setenv(install.SHIM_DIR_ENV, str(tmp_path / "shim"))

    path = install.worker_path({"PATH": "/usr/bin:/bin"})

    assert path.split(os.pathsep) == [str(tmp_path / "shim"), "/usr/bin", "/bin"]
    for program in classify.SHIMMED_PROGRAMS:
        generated = tmp_path / "shim" / program
        assert os.access(generated, os.X_OK)
        text = generated.read_text()
        assert "agentdeck.heavy_shim.main" in text
        assert text.rstrip().endswith(f'{program} "$@"')


def test_installing_twice_does_not_stack_the_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(install.SHIM_DIR_ENV, str(tmp_path / "shim"))
    once = install.worker_path({"PATH": "/usr/bin"})

    assert install.worker_path({"PATH": once}) == once


def test_the_real_program_is_found_past_the_shims_own_directory(tmp_path, monkeypatch):
    shim = tmp_path / "shim"
    real = tmp_path / "bin"
    for directory in (shim, real):
        directory.mkdir()
        (directory / "pytest").write_text("#!/bin/sh\n")
        (directory / "pytest").chmod(0o755)
    monkeypatch.setenv(install.SHIM_DIR_ENV, str(shim))

    found = install.real_program("pytest", {"PATH": os.pathsep.join([str(shim), str(real)])})

    assert found == str(real / "pytest")


def test_the_real_program_skips_a_symlink_to_the_active_launcher_directory(tmp_path, monkeypatch):
    shim = tmp_path / "shim"
    alias = tmp_path / "alias"
    real = tmp_path / "bin"
    shim.mkdir()
    alias.symlink_to(shim, target_is_directory=True)
    real.mkdir()
    (shim / "pytest").write_text("#!/bin/sh\n")
    (shim / "pytest").chmod(0o755)
    (real / "pytest").write_text("#!/bin/sh\n")
    (real / "pytest").chmod(0o755)
    monkeypatch.setenv(install.SHIM_DIR_ENV, str(tmp_path / "changed-setting"))

    found = install.real_program(
        "pytest",
        {
            "PATH": os.pathsep.join([str(alias), str(real)]),
            install.ACTIVE_DIR_ENV: str(shim),
        },
    )

    assert found == str(real / "pytest")


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """A PATH with the shim in front of one fake program, and a live runtime."""
    real = tmp_path / "bin"
    real.mkdir()
    for program in ("pytest", "make"):
        marker = real / program
        marker.write_text(f'#!/bin/sh\necho "real {program} ran: $*"\n')
        marker.chmod(0o755)
    socket_path = tmp_path / "codex-runtime.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen()

    monkeypatch.setenv(install.SHIM_DIR_ENV, str(tmp_path / "shim"))
    shim = install.ensure_shim()
    assert shim is not None

    def run(command: list[str], **overrides: str) -> subprocess.CompletedProcess:
        # Named absolutely: `subprocess` resolves argv[0] against *this*
        # process's PATH, not the one it hands the child, so a bare name would
        # find the test runner's own pytest and never reach the shim.
        command = [str(shim / command[0]), *command[1:]]
        environment = {
            "PATH": os.pathsep.join([str(shim), str(real), "/usr/bin", "/bin"]),
            "PYTHONPATH": str(REPO / "src"),
            install.SHIM_DIR_ENV: str(shim),
            "AGENTDECK_CODEX_SOCKET": str(socket_path),
            "HOME": str(tmp_path),
        }
        environment.update(overrides)
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=environment,
            cwd=str(tmp_path),
            check=False,
        )

    yield run
    listener.close()


def test_a_worker_running_a_whole_suite_is_told_to_queue_it(worker, tmp_path):
    result = worker(["pytest", "-q", "-m", "not browser"])

    assert result.returncode == install.EXIT_REFUSED
    assert "real pytest ran" not in result.stdout
    assert f"agentdeck job submit --cwd {tmp_path} -- pytest -q -m 'not browser'" in (result.stderr)
    assert "AGENTDECK_HEAVY_SLOT_HELD=1" in result.stderr


def test_an_ordinary_call_to_a_shadowed_program_just_runs(worker):
    """`make test` is a suite; `make build` is a worker doing its job."""
    result = worker(["make", "build"])

    assert result.returncode == 0
    assert result.stdout.strip() == "real make ran: build"


def test_the_escape_hatch_still_bypasses(worker):
    result = worker(["pytest", "-q"], AGENTDECK_HEAVY_SLOT_HELD="1")

    assert result.returncode == 0
    assert "real pytest ran" in result.stdout


def test_a_suite_started_by_a_shimmed_program_is_not_refused_twice(worker):
    """`jobs.py` sets the guard on a queued job's child; so does a passthrough."""
    result = worker(["pytest", "-q"], **{install.RECURSION_GUARD: "1"})

    assert result.returncode == 0
    assert "real pytest ran" in result.stdout


def test_it_fails_open_when_the_runtime_is_not_running(worker, tmp_path):
    result = worker(["pytest", "-q"], AGENTDECK_CODEX_SOCKET=str(tmp_path / "absent"))

    assert result.returncode == 0
    assert "real pytest ran" in result.stdout


def test_it_fails_open_when_only_a_stale_runtime_socket_remains(worker, tmp_path):
    stale = tmp_path / "stale.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(stale))
    listener.close()

    result = worker(["pytest", "-q"], AGENTDECK_CODEX_SOCKET=str(stale))

    assert result.returncode == 0
    assert "real pytest ran" in result.stdout


def test_it_fails_open_when_the_shim_itself_cannot_run(tmp_path, monkeypatch, worker):
    """A launcher from a newer checkout, an interpreter without this package."""
    blind = tmp_path / "blind-python"
    # -S drops site-packages and -E drops PYTHONPATH, so this interpreter runs
    # but cannot import AgentDeck at all — which is the half-deployed deck.
    blind.write_text(f'#!/bin/sh\nexec {sys.executable} -S -E "$@"\n')
    blind.chmod(0o755)
    monkeypatch.setattr(install, "launcher_interpreter", lambda: blind)
    assert install.ensure_shim() is not None

    result = worker(["pytest", "-q"])

    assert result.returncode == 0
    assert "real pytest ran" in result.stdout


def test_an_empty_argv_asks_for_nothing_and_gets_nothing():
    assert main.main([]) == install.EXIT_NOT_FOUND


def test_a_program_that_does_not_exist_reports_itself(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv(install.SHIM_DIR_ENV, str(tmp_path / "shim"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    assert main.main(["cargo", "build"]) == install.EXIT_NOT_FOUND
    assert "cargo: not found" in capsys.readouterr().err


@pytest.fixture
def python_guard(tmp_path, monkeypatch):
    """A Python whose startup meets the guard, and a live runtime."""
    monkeypatch.setenv(install.SHIM_DIR_ENV, str(tmp_path / "shim"))
    guard = install.ensure_python_guard()
    assert guard is not None
    socket_path = tmp_path / "codex-runtime.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen()

    def run(arguments: list[str], **overrides: str) -> subprocess.CompletedProcess:
        environment = {
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(guard),
            "AGENTDECK_CODEX_SOCKET": str(socket_path),
            "HOME": str(tmp_path),
        }
        environment.update(overrides)
        return subprocess.run(
            [sys.executable, *arguments],
            capture_output=True,
            text=True,
            env=environment,
            cwd=str(tmp_path),
            check=False,
        )

    yield run
    listener.close()


def test_the_guard_catches_a_suite_the_shim_never_saw(python_guard, tmp_path):
    """`bash -lc` puts `~/.local/bin` back in front of the shim; this does not care."""
    entry_point = tmp_path / "pytest"
    entry_point.write_text("print('the suite started')\n")

    result = python_guard([str(entry_point), "-q"])

    assert result.returncode == install.EXIT_REFUSED
    assert "the suite started" not in result.stdout
    assert f"agentdeck job submit --cwd {tmp_path} -- pytest -q" in result.stderr


def test_the_guard_catches_python_m_pytest(python_guard, tmp_path):
    """A spelling the shim cannot see at all: no shimmed program is named."""
    result = python_guard(["-m", "pytest", "--version"])

    assert result.returncode == install.EXIT_REFUSED
    assert "python -m pytest --version" in result.stderr


def test_the_guard_leaves_ordinary_python_alone(python_guard):
    result = python_guard(["-c", "print('fine')"])

    assert result.returncode == 0
    assert result.stdout.strip() == "fine"


def test_the_guard_fails_open_without_a_runtime(python_guard, tmp_path):
    entry_point = tmp_path / "pytest"
    entry_point.write_text("print('the suite started')\n")

    result = python_guard([str(entry_point), "-q"], AGENTDECK_CODEX_SOCKET=str(tmp_path / "absent"))

    assert result.returncode == 0
    assert "the suite started" in result.stdout


def test_the_guard_honours_the_escape_hatch(python_guard, tmp_path):
    entry_point = tmp_path / "pytest"
    entry_point.write_text("print('the suite started')\n")

    result = python_guard([str(entry_point), "-q"], AGENTDECK_HEAVY_SLOT_HELD="1")

    assert result.returncode == 0
    assert "the suite started" in result.stdout


def test_the_guard_does_not_displace_a_projects_own_sitecustomize(
    python_guard, tmp_path, monkeypatch
):
    """Being first on PYTHONPATH must not silently disable someone else's."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "sitecustomize.py").write_text("print('project sitecustomize ran')\n")
    guard = install.guard_dir()

    result = python_guard(
        ["-c", "print('fine')"],
        PYTHONPATH=os.pathsep.join([str(guard), str(project)]),
    )

    assert result.returncode == 0
    assert "project sitecustomize ran" in result.stdout


def test_the_guard_runs_sitecustomize_from_the_project_root(python_guard, tmp_path):
    (tmp_path / "sitecustomize.py").write_text("print('root sitecustomize ran')\n")

    result = python_guard(
        ["-c", "print('fine')"],
        PYTHONPATH=str(install.guard_dir()) + os.pathsep,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["root sitecustomize ran", "fine"]


def test_the_guard_preserves_project_sitecustomize_module_identity(python_guard, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "sitecustomize.py").write_text(
        "import sitecustomize, sys\n"
        "from pathlib import Path\n"
        "print(__name__)\n"
        "print(sitecustomize is sys.modules[__name__])\n"
        "print(Path(__file__).resolve().parent)\n"
    )
    guard = install.guard_dir()

    result = python_guard(
        ["-c", "print('fine')"],
        PYTHONPATH=os.pathsep.join([str(guard), str(project)]),
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[:2] == ["sitecustomize", "True"]
    assert result.stdout.splitlines()[2] == str(project)


def test_the_guard_preserves_project_sitecustomize_error_diagnostics(python_guard, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "sitecustomize.py").write_text("raise RuntimeError('project boom')\n")
    guard = install.guard_dir()

    result = python_guard(
        ["-c", "print('fine')"],
        PYTHONPATH=os.pathsep.join([str(guard), str(project)]),
    )

    assert result.returncode == 0
    assert "Error in sitecustomize" in result.stderr
    assert "RuntimeError: project boom" in result.stderr


def test_the_guard_carries_the_one_definition_of_heavy(tmp_path, monkeypatch):
    monkeypatch.setenv(install.SHIM_DIR_ENV, str(tmp_path / "shim"))
    guard = install.ensure_python_guard()

    generated = (guard / "sitecustomize.py").read_text()

    source = (Path(install.__file__).parent / "classify.py").read_text()
    assert repr(source) in generated


def test_the_guard_goes_first_on_a_workers_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setenv(install.SHIM_DIR_ENV, str(tmp_path / "shim"))

    once = install.worker_pythonpath({"PYTHONPATH": "/opt/lib"})

    assert once.split(os.pathsep) == [str(install.guard_dir()), "/opt/lib"]
    assert install.worker_pythonpath({"PYTHONPATH": once}) == once


def test_worker_pythonpath_preserves_an_explicit_current_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(install.SHIM_DIR_ENV, str(tmp_path / "shim"))

    value = install.worker_pythonpath({"PYTHONPATH": os.pathsep})

    assert value.split(os.pathsep) == [str(install.guard_dir()), "", ""]
