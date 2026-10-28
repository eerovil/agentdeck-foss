"""Where a joined poller project's file lives, and whether it moved on (#3755).

``agentdeck-project join`` (``project_join``) records the repository, path and blob it applied
in the instance's ``source.json``. This half has no setup code in it, so the Pollers page can
say when that file changed without importing the setup command.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

SOURCE_FILE = "source.json"
ROOT = "~/.local/share/agentdeck/poller"


@dataclass(frozen=True)
class Source:
    """Where an instance's project file lives, and the blob that was last applied."""

    repository: str
    path: str
    sha: str


def read_source(instance_dir: Path) -> Source | None:
    try:
        raw = json.loads((instance_dir / SOURCE_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    repository, path, sha = (raw.get(key) for key in ("repository", "path", "sha"))
    if not all(isinstance(value, str) and value for value in (repository, path, sha)):
        return None
    return Source(repository, path, sha)


def write_source(instance_dir: Path, source: Source) -> None:
    body = {"repository": source.repository, "path": source.path, "sha": source.sha}
    (instance_dir / SOURCE_FILE).write_text(json.dumps(body, indent=2) + "\n")


# --- the Pollers page -------------------------------------------------------------------------


async def changed_sources(root: str | Path) -> dict[str, str]:
    """Each joined instance whose shared file moved on since it was applied, with the
    repository holding it. One cached ``gh`` read per joined instance; an unanswered read
    shows nothing."""
    from . import gh_cli

    found: dict[str, str] = {}
    instances = Path(root).expanduser() / "instances"
    if not instances.is_dir():
        return found
    for instance in sorted(path for path in instances.iterdir() if path.is_dir()):
        source = read_source(instance)
        if source is None:
            continue
        try:
            code, out = await gh_cli.run(
                ["gh", "api", f"repos/{source.repository}/contents/{source.path}", "--jq", ".sha"],
                timeout=15,
            )
        except (OSError, TimeoutError):
            continue
        current = out.decode(errors="replace").strip() if code == 0 else ""
        if current and current != source.sha:
            found[instance.name] = source.repository
    return found
