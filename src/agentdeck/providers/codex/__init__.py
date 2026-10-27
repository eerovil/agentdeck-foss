"""Codex CLI session provider."""

import os
from pathlib import Path

WEB_SEARCH_CONFIG_OVERRIDE = 'web_search="live"'
# Let AgentDeck-owned Codex chats reach the network in the workspace-write
# sandbox by default. Codex blocks outbound network in workspace-write unless
# this is set, which makes curl/pip/git push/gh either hang until timeout or
# stall on a per-command approval — so delegated review/fix/PR and web tasks
# could not push or open PRs without manual intervention. Enabling it here makes
# network the default for every AgentDeck-launched Codex process.
NETWORK_ACCESS_CONFIG_OVERRIDE = "sandbox_workspace_write.network_access=true"

# Grant the rootless-podman runtime dir ($XDG_RUNTIME_DIR, e.g. /run/user/1000) as a
# workspace-write writable root. AgentDeck-owned chats run tests inside the service
# containers via `podman exec`, but the sandbox mounts the runtime dir read-only, so
# podman fails with `chmod /run/user/<uid>/libpod: read-only file system` and Codex has
# to escalate out of the sandbox. Making it writable lets `podman exec` work in-sandbox.
_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"

# A git worktree keeps its refs, index, HEAD and logs in the MAIN repo's `.git`
# (`<repo>/.git/worktrees/<name>` plus the shared `<repo>/.git/refs`), which lives
# OUTSIDE the worktree cwd, and Codex's workspace-write sandbox makes only the cwd
# writable by default. AgentDeck-owned chats routinely run in kanban `.worktrees/<issue>`
# checkouts, so grant the user's home tree — where every repo (and its `.git`) lives — as
# a writable root. It is canonicalized because `/home` is a symlink to `/var/home` here
# and the sandbox matches on the real path git actually writes to. This is a deliberately
# broad, process-global grant (the app-server is cwd-agnostic and serves many worktrees at
# once); set AGENTDECK_WRITABLE_BASE to narrow or widen the base.
#
# It does NOT buy escalation-free git, and no writable root can: Codex's permission
# profile re-marks each workspace root's own `.git` read-only after granting the root,
# resolving a linked worktree's `.git` pointer file to the real gitdir. So a
# chat still escalates on every ref/index/lock write in its OWN checkout, while sibling
# repos and other worktrees under the grant stay writable. Widening the roots does not
# change that; see .claude/codebase/owned-agent-control.md (#709).
_WORKSPACE_BASE = os.environ.get("AGENTDECK_WRITABLE_BASE") or os.path.realpath(Path.home())

# `/tmp` and `$TMPDIR` are deliberately absent: Codex already adds them (and the thread's
# cwd) to workspace-write on its own, so a scratch worktree under `/tmp` costs no
# escalation. This list stays the deck's own minimum — restating a provider default here
# would silently paper over the release that changes it. Scratch checkouts still belong in
# the home tree, but for attribution: `models.py::resolve_session_project` reads a
# `/tmp/<name>` checkout as a project named `<name>`, filing that spend under a repository
# that does not exist (#709).
_WRITABLE_ROOTS = [root for root in (_RUNTIME_DIR, _WORKSPACE_BASE) if root]
WRITABLE_ROOTS_CONFIG_OVERRIDE = (
    "sandbox_workspace_write.writable_roots=["
    + ",".join(f'"{root}"' for root in _WRITABLE_ROOTS)
    + "]"
)

# Have an LLM review approval requests instead of a human for AgentDeck-owned chats.
# Codex's default `approval_policy = on-request` makes the model raise an approval
# prompt whenever it wants to escalate out of the workspace-write sandbox; with the
# default `approvals_reviewer = "user"` (set in the account's config.toml) each of those
# lands on the human, so delegated/deck-driven chats stall waiting for manual clicks.
# `auto_review` routes those requests to a reviewer model, letting AgentDeck chats run
# unattended. NOTE: this lets a model auto-approve commands with no human gate — the
# dashboard has no auth, and the sandbox already grants the home tree + runtime dir as
# writable roots, so an auto-approved command can touch every repo and the live account
# budget. This is a deliberate, process-global default for AgentDeck-launched Codex only.
APPROVALS_REVIEWER_CONFIG_OVERRIDE = 'approvals_reviewer="auto_review"'

# Run each command in a plain shell, not a login shell. A login shell reads the host's
# ~/.bash_profile and ~/.bashrc, which put ~/.local/bin (the real, logged-out `gh`) back
# in front of the deck's `gh` shim, so every Codex chat's bare `gh` answered "gh auth
# login" (#3350). The deck already hands Codex a complete PATH, so nothing is lost.
LOGIN_SHELL_CONFIG_OVERRIDE = "allow_login_shell=false"
