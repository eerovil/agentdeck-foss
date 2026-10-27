"""Safe one-shot injection for completed non-interactive Codex sessions."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from ... import gh_shim, github_identity
from ...models import Account, InjectResult, Session
from . import (
    APPROVALS_REVIEWER_CONFIG_OVERRIDE,
    LOGIN_SHELL_CONFIG_OVERRIDE,
    NETWORK_ACCESS_CONFIG_OVERRIDE,
    WEB_SEARCH_CONFIG_OVERRIDE,
    WRITABLE_ROOTS_CONFIG_OVERRIDE,
    transcripts,
)


def is_injectable_rollout(path: Path, kind: str | None) -> bool:
    return kind == "exec" and transcripts.last_turn_complete(path)


async def _terminate_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await process.wait()


async def inject_session(
    account: Account,
    session: Session,
    path: Path,
    message: str,
    *,
    timeout_s: float,
    images: list[Path] | None = None,
    process_factory=None,
) -> InjectResult:
    """Resume one completed ``codex exec`` session and wait for its turn."""
    meta = transcripts.transcript_meta(path)
    if not is_injectable_rollout(path, meta.kind):
        return InjectResult(False, "session is not a completed Codex exec turn")
    if session.cwd is None or not session.cwd.is_dir():
        return InjectResult(False, "session working directory no longer exists")

    factory = process_factory or asyncio.create_subprocess_exec
    env = os.environ.copy()
    env["CODEX_HOME"] = str(account.root)
    # Every GitHub transport goes through the owner rule, never the host login (#2289):
    # `gh` through the shim, `git` through AgentDeck's helper.
    env["PATH"] = gh_shim.worker_path(env)
    github_identity.configure_github_git(env)
    try:
        args = [
            "codex",
            "exec",
            "resume",
            session.session_id,
            "--config",
            WEB_SEARCH_CONFIG_OVERRIDE,
            "--config",
            NETWORK_ACCESS_CONFIG_OVERRIDE,
            "--config",
            WRITABLE_ROOTS_CONFIG_OVERRIDE,
            "--config",
            APPROVALS_REVIEWER_CONFIG_OVERRIDE,
            "--config",
            LOGIN_SHELL_CONFIG_OVERRIDE,
        ]
        for image in images or []:
            args.extend(("-i", str(image)))
        args.extend((
            "-",
            "--json",
            "--skip-git-repo-check",
        ))
        process = await factory(
            *args,
            cwd=str(session.cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return InjectResult(False, f"could not start Codex: {exc}")

    try:
        await asyncio.wait_for(process.communicate((message + "\n").encode()), timeout=timeout_s)
    except TimeoutError:
        await _terminate_group(process)
        return InjectResult(False, "Codex turn timed out")
    except asyncio.CancelledError:
        await _terminate_group(process)
        raise
    if process.returncode != 0:
        return InjectResult(False, "Codex exited without completing the turn")
    return InjectResult(True)


async def start_session(
    account: Account,
    cwd: Path,
    message: str,
    *,
    timeout_s: float,
    images: list[Path] | None = None,
    effort: str | None = None,
    process_factory=None,
) -> InjectResult:
    """Start one persisted non-interactive Codex session."""
    if not cwd.is_dir():
        return InjectResult(False, "working directory does not exist")
    factory = process_factory or asyncio.create_subprocess_exec
    env = os.environ.copy()
    env["CODEX_HOME"] = str(account.root)
    # Every GitHub transport goes through the owner rule, never the host login (#2289):
    # `gh` through the shim, `git` through AgentDeck's helper.
    env["PATH"] = gh_shim.worker_path(env)
    github_identity.configure_github_git(env)
    try:
        args = [
            "codex",
            "exec",
            "--config",
            WEB_SEARCH_CONFIG_OVERRIDE,
            "--config",
            NETWORK_ACCESS_CONFIG_OVERRIDE,
            "--config",
            WRITABLE_ROOTS_CONFIG_OVERRIDE,
            "--config",
            APPROVALS_REVIEWER_CONFIG_OVERRIDE,
            "--config",
            LOGIN_SHELL_CONFIG_OVERRIDE,
        ]
        if effort:
            args.extend(("--config", f'model_reasoning_effort="{effort}"'))
        for image in images or []:
            args.extend(("-i", str(image)))
        args.extend((
            "--json",
            "--skip-git-repo-check",
            "-",
        ))
        process = await factory(
            *args,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return InjectResult(False, f"could not start Codex: {exc}")
    try:
        await asyncio.wait_for(process.communicate((message + "\n").encode()), timeout=timeout_s)
    except TimeoutError:
        await _terminate_group(process)
        return InjectResult(False, "Codex turn timed out")
    except asyncio.CancelledError:
        await _terminate_group(process)
        raise
    if process.returncode != 0:
        return InjectResult(False, "Codex exited without completing the turn")
    return InjectResult(True)
