"""A side chat started from a Work's page (#2388): where it runs, on which account, and
what its first message says.

A side chat belongs to the Work but is never a Run: it is linked by a ``side_chat``
``session_work_bindings`` row once it exists (``AppState.finalize_work_side_chats``), so
the Run trace, the Poller and every other Run reader never see it. Everything here is a
read; the route in ``web/routes_works.py`` makes the one write.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import IssueRef, PullRequestRef
from .work import WorkRef
from .work_workspace import RepositoryRole, WorkspaceProbe, WorkWorkspace


def work_checkout(workspace: WorkWorkspace | None, work: WorkRef) -> str:
    """The Work's own checkout on this host, or "" when it has none.

    The code repository the workspace leads with, else the Work's own repository, else any
    other code repository; within it a registered or claimed worktree first, then a
    directory one of its chats ran in. Artifact repositories are never a chat's folder, and
    a path that no longer exists is history, not a checkout.
    """
    if workspace is None:
        return ""
    code = [item for item in workspace.repositories if RepositoryRole.ARTIFACT not in item.roles]
    ranked = sorted(
        code,
        key=lambda item: (
            item.repository != workspace.primary,
            item.repository != work.repository,
        ),
    )
    for repo in ranked:
        for item in (*repo.worktrees, *repo.cwds):
            seen = item.observation
            if seen.readable:
                return seen.toplevel or seen.path
    return ""


def local_repository_checkout(
    work: WorkRef | str, candidates: Iterable[str], probe: WorkspaceProbe | None = None
) -> str:
    """A main checkout of the Work's repository (or the ``owner/repo`` given) on this host,
    or "" when there is none.

    Only a directory holding a ``.git`` directory (not a linked worktree) whose ``origin``
    is that repository and which is its own top level counts. This runs git, so a caller
    keeps it off the event loop.
    """
    repository = (work if isinstance(work, str) else work.repository).lower()
    probe = probe or WorkspaceProbe()
    seen: set[str] = set()
    for raw in candidates:
        if not raw or raw in seen:
            continue
        seen.add(raw)
        try:
            path = Path(raw).expanduser().resolve()
            if not (path / ".git").is_dir():
                continue
        except (OSError, RuntimeError):
            continue
        found = probe.observe(str(path))
        if found.readable and found.repository == repository and (found.toplevel == str(path)):
            return str(path)
    return ""


def prefilled_account(chats: Sequence[Any], sessions: Any, candidates: Sequence[str]) -> str:
    """The account of the Work's most recently active chat when it can start one, else the
    first account that can (the new-chat form's default); "" when none can."""
    if not candidates:
        return ""
    floor = datetime.min.replace(tzinfo=UTC)
    for chat in sorted(chats, key=lambda item: item.last_activity or floor, reverse=True):
        session = sessions.get(chat.session_key)
        account = getattr(session, "account_key", "") if session is not None else ""
        if account in candidates:
            return account
        break
    return candidates[0]


def work_pull_requests(workspace: WorkWorkspace | None) -> tuple[PullRequestRef, ...]:
    """Every pull request the Work's workspace lists, once each, in its order."""
    found: dict[tuple[str, int], PullRequestRef] = {}
    for repo in workspace.repositories if workspace is not None else ():
        for pull in repo.pull_requests:
            ref = PullRequestRef.parse(pull.url) or (
                PullRequestRef(pull.repository, pull.number) if pull.repository else None
            )
            if ref is not None:
                found.setdefault(ref.identity, ref)
    return tuple(found.values())


def context_line(issue: IssueRef, pulls: Sequence[PullRequestRef]) -> str:
    """The one line ahead of the person's text: which Work this is, never the issue body."""
    names = [
        f"PR #{pull.number}"
        if pull.repository.lower() == issue.repository.lower()
        else f"PR {pull.repository}#{pull.number}"
        for pull in pulls
    ]
    tail = f" ({', '.join(names)})" if names else ""
    return f"Context: Work for {issue.repository}#{issue.number}{tail}."


def first_message(issue: IssueRef, pulls: Sequence[PullRequestRef], text: str) -> str:
    return f"{context_line(issue, pulls)}\n\n{text.strip()}"
