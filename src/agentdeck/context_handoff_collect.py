"""Bounded local inputs for the pure context-handoff renderer."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_COMMAND_TIMEOUT_S = 0.75
_AGGREGATE_TIMEOUT_S = 3.5
_MAX_OUTPUT_CHARS = 8_000
_MAX_CHANGED_PATHS = 100


@dataclass(frozen=True)
class WorkspaceHandoff:
    text: str = ""
    diagnostic: str | None = None


async def collect_workspace_handoff(
    cwd: str | Path | None,
    *,
    observed_at: datetime | None = None,
) -> WorkspaceHandoff:
    """Read bounded, local-only Git facts without blocking the request loop."""
    if cwd is None:
        return WorkspaceHandoff(diagnostic="workspace unavailable")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _collect_workspace_handoff,
                Path(cwd),
                observed_at or datetime.now(UTC),
            ),
            timeout=_AGGREGATE_TIMEOUT_S,
        )
    except TimeoutError:
        return WorkspaceHandoff(diagnostic="workspace observation timed out")


def _collect_workspace_handoff(cwd: Path, observed_at: datetime) -> WorkspaceHandoff:
    if not cwd.is_dir():
        return WorkspaceHandoff(diagnostic="workspace directory unavailable")
    try:
        root = _git(cwd, "rev-parse", "--show-toplevel")
        branch = _git(cwd, "branch", "--show-current") or "detached"
        head = _git(cwd, "rev-parse", "HEAD")
        status = _git(cwd, "status", "--short", "--untracked-files=normal")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return WorkspaceHandoff(diagnostic=_diagnostic(exc))
    paths = status.splitlines()
    omitted = max(0, len(paths) - _MAX_CHANGED_PATHS)
    paths = paths[:_MAX_CHANGED_PATHS]
    lines = [
        f"Observed at {observed_at.astimezone(UTC).isoformat()}",
        "The live checkout is authoritative; re-check it before editing.",
        f"Root: {root}",
        f"Branch: {branch}",
        f"HEAD: {head}",
        "Changed paths:",
        *(paths or ["(clean)"]),
    ]
    if omitted:
        lines.append(f"... {omitted} more paths omitted")
    return WorkspaceHandoff(text="\n".join(lines)[:_MAX_OUTPUT_CHARS])


def _git(cwd: Path, *args: str) -> str:
    # Spool command output outside Python memory. A repository with an enormous
    # status cannot make the request allocate its whole output before slicing.
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        done = subprocess.run(
            ["git", "-C", str(cwd), *args],
            stdout=stdout_file,
            stderr=stderr_file,
            timeout=_COMMAND_TIMEOUT_S,
            check=False,
        )
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(_MAX_OUTPUT_CHARS).decode(errors="replace")
        stderr = stderr_file.read(_MAX_OUTPUT_CHARS).decode(errors="replace")
    if done.returncode != 0:
        raise ValueError(stderr.strip() or f"git {args[0]} failed")
    return stdout.strip()


def _diagnostic(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "workspace Git command timed out"
    text = str(exc).replace("\n", " ").strip()
    return (text or "workspace observation failed")[:240]
