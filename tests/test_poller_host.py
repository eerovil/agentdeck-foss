"""Slice 2 of #178: the templated units and the installer."""

from __future__ import annotations

import os
from pathlib import Path

from agentdeck.poller_host import (
    REPO_PLACEHOLDER,
    ROOT_PLACEHOLDER,
    UNIT_NAMES,
    install,
    instance_dir,
    render_unit,
    timer_unit,
    unit_source_dir,
)

REPO = Path(__file__).resolve().parent.parent


# --- the reviewed unit templates ---

def _unit(name: str) -> str:
    return (unit_source_dir() / name).read_text()


def test_both_unit_templates_are_shipped_in_the_repo():
    for name in UNIT_NAMES:
        assert (unit_source_dir() / name).exists(), name
    assert unit_source_dir() == REPO / "systemd"


def test_the_service_runs_one_cycle_from_the_deployed_checkout():
    service = _unit("agentdeck-poller@.service")
    assert "Type=oneshot" in service
    # The wake comes from the checkout, and the wake runs the engine from that same checkout
    # (#2153): one deploy selects both, and there is no release pointer left to name.
    assert f"{ROOT_PLACEHOLDER}/instances/%i" in service
    assert f"{REPO_PLACEHOLDER}/scripts/poller_wake.py" in service
    assert "poller-engine.pyz" not in service
    assert "current/" not in service
    assert f"--root {ROOT_PLACEHOLDER} --instance %i" in service
    # The three config paths are the wake's to derive from the root and the instance id, so
    # the unit names them once (as %i) instead of three times.
    assert "--manifest" not in service
    # Per-instance working directory, so two projects never share state.
    assert f"WorkingDirectory={ROOT_PLACEHOLDER}/instances/%i" in service


def _directives(name: str) -> list[str]:
    """The unit's actual directives, ignoring its explanatory comments."""
    return [
        line.strip()
        for line in _unit(name).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_a_poller_instance_does_not_depend_on_the_dashboard_or_the_runtime():
    directives = _directives("agentdeck-poller@.service")
    for line in directives:
        assert "agentdeck.service" not in line
        assert "agentdeck-codex.service" not in line
        assert not line.startswith(("Requires=", "BindsTo=", "PartOf=", "Wants="))


def test_the_timer_wakes_every_two_minutes_and_never_replays_a_missed_tick():
    timer = _unit("agentdeck-poller@.timer")
    assert "OnUnitInactiveSec=120" in timer
    # The engine's comment-scan overlap is sized in wakes, and the /pollers page judges
    # freshness in wakes, so both must follow the timer or every instance reads late.
    from agentdeck import poller_ops
    from agentdeck.config import PollerConfig
    from agentdeck.poller_engine import health, queue

    assert queue.WAKE_INTERVAL_S == 120
    assert health.DEFAULT_INTERVAL_S == poller_ops.DEFAULT_INTERVAL_S == 120
    assert PollerConfig().interval_s == 120
    # Replaying a backlog of wakes after downtime would spawn against a stale world.
    assert "Persistent=false" in timer
    assert "Unit=agentdeck-poller@%i.service" in timer
    assert "WantedBy=timers.target" in timer


def test_rendering_substitutes_the_root_but_leaves_systemds_own_specifiers():
    rendered = render_unit(_unit("agentdeck-poller@.service"), Path("/srv/poller"))
    assert ROOT_PLACEHOLDER not in rendered
    assert "/srv/poller/instances/%i" in rendered
    assert "--root /srv/poller" in rendered
    # %i is systemd's, not ours: it must survive rendering untouched.
    assert "/srv/poller/instances/%i" in rendered


def test_unit_names_follow_the_instance_template():
    assert timer_unit("agentdeck") == "agentdeck-poller@agentdeck.timer"


# --- the installer ---

def test_installing_creates_the_instances_and_the_units_and_no_engine_slots(tmp_path):
    root = tmp_path / "poller"
    units = tmp_path / "systemd"
    written = install(root, units, instances=["agentdeck", "trailhead"])

    assert instance_dir(root, "agentdeck").is_dir()
    assert instance_dir(root, "trailhead").is_dir()
    # Nothing to release into: the engine deploys with the checkout (#2153).
    assert sorted(path.name for path in root.iterdir()) == ["instances"]
    assert [path.name for path in written] == list(UNIT_NAMES)
    assert str(root) in (units / "agentdeck-poller@.service").read_text()


def test_installing_twice_changes_nothing(tmp_path):
    root, units = tmp_path / "poller", tmp_path / "systemd"
    install(root, units, instances=["agentdeck"])
    before = {
        path.name: (path.read_text(), path.stat().st_mtime_ns)
        for path in units.iterdir()
    }
    install(root, units, instances=["agentdeck"])
    after = {
        path.name: (path.read_text(), path.stat().st_mtime_ns)
        for path in units.iterdir()
    }
    assert after == before


def test_installing_rewrites_a_unit_whose_root_changed(tmp_path):
    units = tmp_path / "systemd"
    install(tmp_path / "one", units)
    install(tmp_path / "two", units)
    service = (units / "agentdeck-poller@.service").read_text()
    assert str(tmp_path / "two") in service
    assert str(tmp_path / "one") not in service


def test_installing_writes_nothing_outside_the_directories_it_was_given(tmp_path):
    root, units = tmp_path / "poller", tmp_path / "systemd"
    other = tmp_path / "existing-poller"
    other.mkdir()
    (other / "state.json").write_text("{}")
    install(root, units, instances=["agentdeck"])
    # A pre-existing installation on the same host is untouched.
    assert (other / "state.json").read_text() == "{}"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["existing-poller", "poller", "systemd"]


# --- the scripts themselves ---

def test_the_installer_script_works_end_to_end(tmp_path):
    """Run the real scripts, not just the modules they call."""
    import subprocess
    import sys

    root = tmp_path / "poller"
    units = tmp_path / "systemd"
    installed = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "install_poller_host.py"),
         "--root", str(root), "--unit-dir", str(units), "--instance", "agentdeck"],
        capture_output=True, text=True, check=True, cwd=REPO,
    )
    assert "installed" in installed.stdout
    assert "systemctl --user enable --now agentdeck-poller@agentdeck.timer" in installed.stdout

    service = (units / "agentdeck-poller@.service").read_text()
    assert f"--root {root}" in service
    assert f"{REPO}/scripts/poller_wake.py" in service


# --- the engine is the checkout's own (#2153) ---

def _checkout_copy(tmp_path: Path, marker: str) -> Path:
    """A copy of this checkout's package, as a different deployed version would have it.

    The whole package, not just the engine: the engine imports the deck's shared modules (#2597).
    """
    import shutil

    def link_or_copy(source: str, target: str) -> None:
        # Linked, bytecode and all: compiling the whole package afresh in each copy
        # was most of this test's time. A link cannot cross filesystems, so copy then.
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)

    checkout = tmp_path / marker
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "poller_engine.py", checkout / "scripts")
    shutil.copytree(
        REPO / "src" / "agentdeck", checkout / "src" / "agentdeck", copy_function=link_or_copy
    )
    main = checkout / "src" / "agentdeck" / "poller_engine" / "__main__.py"
    text = main.read_text()
    main.unlink()  # its own file, never a write through a link into this checkout
    main.write_text(text.replace(
        '"""The engine\'s command line:', f'"""{marker}. The engine\'s command line:', 1
    ))
    return checkout


def test_the_engine_launcher_runs_the_engine_of_the_checkout_it_sits_in(tmp_path):
    """Whichever version a deploy (or a rollback) selects answers, never an installed copy.

    Two checkouts on one host, each launched with the same interpreter — whose installed
    `agentdeck` is neither of them — and each runs its own engine.
    """
    import subprocess
    import sys

    for marker in ("release-one", "release-two"):
        launcher = _checkout_copy(tmp_path, marker) / "scripts" / "poller_engine.py"
        done = subprocess.run(
            [sys.executable, str(launcher), "--help"],
            capture_output=True, text=True, check=True, cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert marker in done.stdout
