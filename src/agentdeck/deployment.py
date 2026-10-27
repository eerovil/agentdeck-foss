"""Read the deploy script's durable release record and its refusal marker."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")

# The deploy script writes one line; the pill shows it in a 238px panel. Cap it so
# a runaway reason cannot stretch the topbar or the panel.
_REASON_MAX = 200
_READ_MAX = 4096


@dataclass(frozen=True)
class DeploymentStatus:
    frontend_sha: str | None
    runtime_sha: str | None
    frontend_subject: str | None
    runtime_subject: str | None
    blocked_reason: str | None = None
    # A committed poller Manifest the deploy could not apply (#2555). Not a refusal:
    # the code deployed, but a project still runs its older settings.
    poller_note: str | None = None

    @property
    def available(self) -> bool:
        # A blocked deploy is exactly the state nobody can see anywhere else, so it
        # shows even on a host that has never recorded a release (#747).
        return (
            self.frontend_sha is not None
            or self.runtime_sha is not None
            or self.blocked_reason is not None
            or self.poller_note is not None
        )

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None

    @property
    def in_sync(self) -> bool:
        return (
            self.frontend_sha is not None
            and self.runtime_sha is not None
            and self.frontend_sha == self.runtime_sha
        )


def deploy_state_dir() -> Path:
    """Match ``scripts/deploy-live.sh`` without coupling the web app to the script."""
    configured = os.environ.get("AGENTDECK_DEPLOY_STATE")
    if configured:
        return Path(configured).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home).expanduser() / "agentdeck" / "deploy"
    return Path.home() / ".local" / "share" / "agentdeck" / "deploy"


def skills_sha(state_dir: Path | None = None) -> str | None:
    """The commit the deploy last installed bundled skills at, or None where no deploy
    recorded one (#3584). ``scripts/deploy-live.sh`` writes it after the web reload."""
    return _read_sha((state_dir or deploy_state_dir()) / "skills_sha")


def deploy_repo_dir() -> Path:
    """Return the checkout whose commits the deploy markers reference."""
    configured = os.environ.get("AGENTDECK_REPO")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2]


def is_deploy_checkout(path: str | Path | None) -> bool:
    """True when ``path`` resolves to the live deploy checkout itself.

    That directory is ``agentdeck.service``'s working tree: an agent editing it
    leaves the unattended deploy refusing every tick, and parallel workers have
    silently reverted uncommitted files there (#786). Resolved on both sides so
    a symlinked spelling (``/home/me`` vs ``/var/home/me``) still matches;
    subdirectories are deliberately not matched — ``.worktrees/*`` under the
    repo is exactly where work is supposed to happen.
    """
    if not path:
        return False
    try:
        return Path(path).expanduser().resolve() == deploy_repo_dir().resolve()
    except OSError:
        return False


def _read_sha(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value if _SHA_RE.fullmatch(value) else None


def _read_blocked_reason(path: Path) -> str | None:
    """Read the deploy script's refusal marker, if the last run left one.

    The reason is script output, not user input, but it reaches the topbar of every
    page — so only the first line survives, control characters are dropped, and the
    length is capped. Jinja escapes the rest.

    Read bounded rather than whole: unlike the sha markers this file is free-form
    text, and it is reread on every render of every page.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            raw = handle.read(_READ_MAX)
    except OSError:
        return None
    first = next((line for line in raw.splitlines() if line.strip()), "")
    reason = "".join(ch for ch in first if ch.isprintable()).strip()
    if not reason:
        return None
    return reason[: _REASON_MAX - 1] + "…" if len(reason) > _REASON_MAX else reason


@lru_cache(maxsize=32)
def _commit_subject(repo: str, sha: str) -> str | None:
    """Resolve one durable deploy marker to its single-line commit subject."""
    try:
        result = subprocess.run(
            ["git", "show", "-s", "--format=%s", sha],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    subject = result.stdout.strip() if result.returncode == 0 else ""
    return subject or None


def deployment_status() -> DeploymentStatus:
    """Return current state on every render so deferred runtime deploys become visible."""
    state_dir = deploy_state_dir()
    frontend_sha = _read_sha(state_dir / "web_sha")
    runtime_sha = _read_sha(state_dir / "runtime_sha")
    repo = str(deploy_repo_dir())
    return DeploymentStatus(
        frontend_sha=frontend_sha,
        runtime_sha=runtime_sha,
        frontend_subject=_commit_subject(repo, frontend_sha) if frontend_sha else None,
        runtime_subject=_commit_subject(repo, runtime_sha) if runtime_sha else None,
        blocked_reason=_read_blocked_reason(state_dir / "blocked"),
        poller_note=_read_blocked_reason(state_dir / "manifest_failed"),
    )
