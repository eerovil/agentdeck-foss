"""The Poller host's side of Work-owned workspaces (#2149).

A card's worktree used to be the host wake's to create and the card record's to delete.
Now the Work owns it: the wake asks the runtime's workspace service for the Work's
claim on each repository it needs, and gets back the one path every Run, provider and
account of that Work shares. The runtime creates, adopts or reuses the worktree; this
module only asks and reads the answer.

Two answers matter to the caller:

* a usable claim — work there, and never remove it on an abandoned delivery, because
  it is the Work's and outlives this wake;
* anything else — no Work to claim for, the runtime would not adopt what is on disk
  without guessing, the claim's state is unsettled, or the runtime did not answer. No
  worktree is made here: there is no card-owned path to fall back to any more (#2195), and
  a second candidate beside an uncertain one is exactly what the claim exists to prevent.
  The delivery waits for a later wake, and ``detail`` says why.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..git_context import github_repository
from ..poller_engine.github import runtime_post

# The routes `agentdeck.work_workspace_claims` serves; a test pins the spelling.
ENSURE_PATH = "/work-workspaces/ensure"
ENSURE_TIMEOUT_S = 300

IMPLEMENTATION = "implementation"
ARTIFACT = "artifact"

_USABLE = frozenset({"created", "reused", "adopted"})


@dataclass(frozen=True)
class ClaimedWorkspace:
    """What the runtime answered for one repository workspace of one Work."""

    path: Path | None = None
    branch: str = ""
    base_ref: str = ""
    created: bool = False  # the runtime made the directory for this request
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.path is not None


def checkout_repository(checkout: Path) -> str:
    """The lower-cased ``owner/repo`` of a checkout's origin remote, or ""."""
    try:
        done = subprocess.run(
            ["git", "-C", str(checkout), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return (github_repository(done.stdout.strip()) or "").lower()


@dataclass
class WorkWorkspaces:
    """Asks the runtime for a Work's repository workspaces on behalf of one wake.

    ``legacy_worktrees(directive)`` is what the Poller card has registered, or None when
    that could not be read; the runtime adopts an existing directory only against it.
    """

    runtime_socket: str
    legacy_worktrees: Callable[[dict], list[str] | None] = lambda _directive: None
    repository_of: Callable[[Path], str] = checkout_repository
    post: Callable[..., tuple[int, object | None]] = runtime_post

    def ensure(
        self,
        directive: dict,
        *,
        purpose: str,
        checkout: Path,
        name: str,
        branch: str,
        base: str,
        repository: str = "",
    ) -> ClaimedWorkspace:
        work_id = str(directive.get("work_id") or "").strip()
        if not work_id:
            # A dispatch with no Work names nothing to claim for.
            return ClaimedWorkspace(detail="the directive names no Work")
        repository = (repository or self.repository_of(checkout)).strip().lower()
        if not repository:
            return ClaimedWorkspace(detail=f"{checkout} has no GitHub remote")
        legacy = self.legacy_worktrees(directive)
        status, answer = self.post(
            self.runtime_socket,
            ENSURE_PATH,
            {
                "work_id": work_id,
                "run_id": str(directive.get("generation") or ""),
                "repository": repository,
                "purpose": purpose,
                "checkout": str(checkout),
                "path": str(checkout / ".worktrees" / name),
                "branch": branch,
                "base_ref": base,
                "legacy_paths": legacy,
            },
            timeout=ENSURE_TIMEOUT_S,
        )
        if status != 200 or not isinstance(answer, dict):
            detail = answer.get("detail") if isinstance(answer, dict) else ""
            return ClaimedWorkspace(detail=str(detail or f"runtime answered {status or 'nothing'}"))
        outcome = str(answer.get("outcome") or "")
        detail = str(answer.get("detail") or "")
        claim = answer.get("claim")
        if outcome not in _USABLE or not isinstance(claim, dict) or not claim.get("path"):
            return ClaimedWorkspace(detail=f"{outcome or 'no outcome'}: {detail}".strip())
        return ClaimedWorkspace(
            path=Path(str(claim["path"])),
            branch=str(claim.get("branch") or branch),
            base_ref=str(claim.get("base_ref") or base),
            created=outcome == "created",
            detail=detail,
        )
