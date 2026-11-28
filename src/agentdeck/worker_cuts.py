"""What an owned Claude worker starts without, per repository (#4252).

Every tool definition, plugin skill and plugin connector name goes out with every call of
every chat. The host cuts the ones no chat uses; a repository whose chats need one back
names its own lists in ``[claude_workers.repositories."owner/repo"]``. The worker reads
which repository it starts in from the checkout's ``origin`` remote.

A Work's own chat can start without more (#4727): the connectors and skills its repository
never uses, which a chat a person starts keeps, and a copy of the account's own global
``CLAUDE.md`` that the CLI would load a second time.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: A remote read must never hold a spawn up for long; an unread one keeps the host's lists.
_REMOTE_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class Cut:
    """The tools, plugins, connectors, skills and guide files one worker runs without."""

    tools: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()
    connectors: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    guides: tuple[str, ...] = ()

    def settings(self) -> dict:
        """The part of a Claude settings file that makes this cut.

        A bare tool name in ``permissions.deny`` takes the tool out of the list the model
        is sent, and ``mcp__<server>`` takes that connector's tools; ``enabledPlugins``
        false switches a plugin, its skills and its connectors off. Checked on Claude Code
        2.1.317 against its ``init`` event.

        ``deniedMcpServers`` drops a connector whole, by the name the CLI shows for it
        (``claude.ai Figma``): its tool names and its instruction text, which a denied
        ``mcp__<server>`` still loads. ``skillOverrides`` ``off`` takes a skill out of the
        list, and ``claudeMdExcludes`` stops a guide file loading. Those three checked on
        Claude Code 2.1.332, each read from a ``--settings`` file (#4727).
        """
        out: dict = {}
        if self.tools:
            out["permissions"] = {"deny": list(self.tools)}
        if self.plugins:
            out["enabledPlugins"] = {name: False for name in self.plugins}
        if self.connectors:
            out["deniedMcpServers"] = [{"serverName": name} for name in self.connectors]
        if self.skills:
            out["skillOverrides"] = {name: "off" for name in self.skills}
        if self.guides:
            out["claudeMdExcludes"] = list(self.guides)
        return out


@dataclass(frozen=True)
class RepositoryCut:
    """One repository's own lists; ``None`` keeps the host's list for that part."""

    tools: tuple[str, ...] | None = None
    plugins: tuple[str, ...] | None = None
    work_connectors: tuple[str, ...] | None = None
    work_skills: tuple[str, ...] | None = None


@dataclass(frozen=True)
class WorkerCuts:
    """The host's cut and each repository's own, keyed by casefolded ``owner/repo``.

    ``work_connectors`` and ``work_skills`` reach only a Work's own chat (#4727): a person
    who starts a chat in the same checkout may ask for any skill or connector by name.
    """

    tools: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()
    work_connectors: tuple[str, ...] = ()
    work_skills: tuple[str, ...] = ()
    repositories: dict[str, RepositoryCut] = field(default_factory=dict)

    def for_repository(self, repository: str | None, *, work: bool = False) -> Cut:
        own = self.repositories.get((repository or "").casefold(), RepositoryCut())
        connectors = self.work_connectors if own.work_connectors is None else own.work_connectors
        skills = self.work_skills if own.work_skills is None else own.work_skills
        if repository is None and self.repositories:
            # A Work's checkout always has a repository, so none here is a failed read:
            # cut nothing a repository's own list may have kept.
            work = False
        return Cut(
            tools=self.tools if own.tools is None else own.tools,
            plugins=self.plugins if own.plugins is None else own.plugins,
            connectors=connectors if work else (),
            skills=skills if work else (),
        )


def repeated_home_guides(config_root: Path, home: Path | None = None) -> tuple[str, ...]:
    """The home folder's global guide, when an account with its own folder would load it twice.

    An account whose folder is not ``~/.claude`` loads its own ``CLAUDE.md`` as the global
    guide, and then ``~/.claude/CLAUDE.md`` again as a project guide of the home folder,
    because every checkout under the home folder has it as an ancestor (#4727). Only a
    byte-for-byte copy is named: a home guide that says something else still loads, and
    so does the one file both names point at.
    """
    home_guide = (home or Path.home()) / ".claude" / "CLAUDE.md"
    own_guide = config_root / "CLAUDE.md"
    try:
        resolved = home_guide.resolve()
        if resolved == own_guide.resolve() or home_guide.read_bytes() != own_guide.read_bytes():
            return ()
    except (OSError, RuntimeError):  # RuntimeError: a guide that links to itself
        return ()
    # The CLI matches the path it walked, which is the resolved one on a host whose home
    # folder is a symlink; naming both costs nothing.
    return tuple(dict.fromkeys((str(home_guide), str(resolved))))


def repository_of(cwd: str) -> str | None:
    """The GitHub repository a worker starting in ``cwd`` works in, or None.

    Read from git's own config, with ``url.*.insteadOf`` aliases expanded, so it costs no
    GitHub call; a directory outside a checkout, or one whose ``origin`` is not on GitHub,
    gets the host's lists.
    """
    # Imported here: config loads this module, and the poller engine's package is heavy.
    from .poller_engine.workspace import github_repository

    try:
        done = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=_REMOTE_TIMEOUT_S,
            check=False,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return github_repository(done.stdout or "")
