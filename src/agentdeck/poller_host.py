"""Installing the poller on a host: its unit files and its directories (#178).

The templated timer and oneshot service live in ``systemd/`` as real files with placeholders
for the host's poller root and the checkout, so what is installed is the reviewed file with a
path substituted, not a string built in code. The installer is idempotent and writes only
inside the two directories it is given — it must never touch a pre-existing poller
installation on the same host.

There is no engine to install: the unit runs the wake script, and the wake runs the engine,
both from the same deployed AgentDeck checkout (#2153).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

ROOT_PLACEHOLDER = "@POLLER_ROOT@"
REPO_PLACEHOLDER = "@REPO@"
UNIT_NAMES = ("agentdeck-poller@.service", "agentdeck-poller@.timer")
DEFAULT_ROOT = "~/.local/share/agentdeck/poller"
DEFAULT_UNIT_DIR = "~/.config/systemd/user"


def instance_dir(root: Path, instance_id: str) -> Path:
    """Per-project state, logs and lock live under the root, one directory per project."""
    return root / "instances" / instance_id


# --- units and directories ---

def unit_source_dir() -> Path:
    """Where the reviewed unit templates live in this repository."""
    return Path(__file__).resolve().parent.parent.parent / "systemd"


def render_unit(template: str, root: Path, repo: Path | None = None) -> str:
    """Substitute the host's paths into a unit template, leaving systemd's own %i alone.

    ``repo`` is where the wake script and its virtualenv live. It defaults to this checkout,
    which is the answer on every host that installs from a checkout — the only one so far.
    """
    return template.replace(ROOT_PLACEHOLDER, str(root)).replace(
        REPO_PLACEHOLDER, str(repo or _repo_root())
    )


def _repo_root() -> Path:
    """The checkout this module was imported from."""
    return Path(__file__).resolve().parent.parent.parent


def install(
    root: str | Path,
    unit_dir: str | Path,
    *,
    instances: Iterable[str] = (),
    source_dir: Path | None = None,
    repo: Path | None = None,
) -> list[Path]:
    """Create the host layout and install the unit templates. Idempotent; returns what it wrote.

    Only the named instances' directories are created, and only the two templated units are
    written — an installer that reached further could disturb a poller
    already running on this host, which is explicitly out of scope for this effort.
    """
    poller_root = Path(root).expanduser()
    written: list[Path] = []
    for instance_id in instances:
        instance_dir(poller_root, instance_id).mkdir(parents=True, exist_ok=True)

    units = Path(unit_dir).expanduser()
    units.mkdir(parents=True, exist_ok=True)
    source = source_dir or unit_source_dir()
    for name in UNIT_NAMES:
        rendered = render_unit((source / name).read_text(), poller_root, repo)
        target = units / name
        if not target.exists() or target.read_text() != rendered:
            target.write_text(rendered)
        written.append(target)
    return written


def timer_unit(instance_id: str) -> str:
    return f"agentdeck-poller@{instance_id}.timer"


