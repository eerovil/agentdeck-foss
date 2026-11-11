"""What an owned Claude worker starts without, per repository (#4252).

Every tool definition, plugin skill and plugin connector name goes out with every call of
every chat. The host cuts the ones no chat uses; a repository whose chats need one back
names its own lists in ``[claude_workers.repositories."owner/repo"]``. The worker reads
which repository it starts in from the checkout's ``origin`` remote.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

#: A remote read must never hold a spawn up for long; an unread one keeps the host's lists.
_REMOTE_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class Cut:
    """The tools and plugins one worker runs without."""

    tools: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()

    def settings(self) -> dict:
        """The part of a Claude settings file that makes this cut.

        A bare tool name in ``permissions.deny`` takes the tool out of the list the model
        is sent, and ``mcp__<server>`` takes that connector's tools; ``enabledPlugins``
        false switches a plugin, its skills and its connectors off. Checked on Claude Code
        2.1.317 against its ``init`` event.
        """
        out: dict = {}
        if self.tools:
            out["permissions"] = {"deny": list(self.tools)}
        if self.plugins:
            out["enabledPlugins"] = {name: False for name in self.plugins}
        return out


@dataclass(frozen=True)
class WorkerCuts:
    """The host's cut and each repository's own, keyed by casefolded ``owner/repo``.

    A repository's ``None`` keeps the host's list for that part.
    """

    tools: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()
    repositories: dict[str, tuple[tuple[str, ...] | None, tuple[str, ...] | None]] = field(
        default_factory=dict
    )

    def for_repository(self, repository: str | None) -> Cut:
        tools, plugins = self.repositories.get((repository or "").casefold(), (None, None))
        return Cut(
            tools=self.tools if tools is None else tools,
            plugins=self.plugins if plugins is None else plugins,
        )


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
