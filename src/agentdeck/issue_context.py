"""The deck's GitHub issue read model, and the attention/issue join (#821).

Deckhand already turns a session's *registered pull requests* into live GitHub
state (``git_context.py``). This module does the same for the second population
the desk has to answer for: **issues that have not become a chat yet**.

Three rules shape everything here.

* **One reader, one cache.** Open issues and their GitHub Project memberships resolve through the
  same process-shared
  ``GitHubMetadataCache`` pull requests already use — same TTLs, same 15 s
  single-flight lease, same failure backoff. There is deliberately no second
  cache, and a worker never calls GitHub itself: it reads what the web process
  already resolved, over the local API, so its credential fence stays intact.
* **An incomplete lookup is not absence.** Carried over verbatim from
  ``git_context.py::GitContexts``. A rate-limited, timed-out, or ``gh``-less read
  makes that repository *unknown* — never "nothing pending". A backlog that
  answered a throttled read with an empty list would be worse than one that said
  it could not look, because only the second is visible.
* **Join on identity, never on prose.** A waiting chat and its issue are one item to
  the boss and two rows to the deck. They are joined on
  repository plus issue number, taken from the identities that already carry it:
  a registered issue URL, an ``agent/issue-<n>`` branch, and a registered pull
  request's head branch. Title similarity is not evidence.

Grouping is out of scope on purpose. The desk produces groups as prose at answer
time; a stored grouping taxonomy would be the boss-facing roll-up #414 rejected.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gh_cli, merge_checks, poller_ops, wayfinder_map
from .git_context import GitContext, GitContextResolver
from .github_cache import GitHubMetadata, GitHubMetadataCache
from .kanban import KanbanRef, KanbanTitleCache
from .models import AttentionReport, Session
from .poller_engine.config import role_of_status_labels
from .poller_ops import BoardView

log = logging.getLogger(__name__)

_ISSUE_FIELDS = "number,title,url,state,labels,updatedAt,assignees,createdAt"
# ``closingIssuesReferences`` is GitHub's own link from a pull request to the issues it closes,
# in any repository: the one structural link a Work card in an issues-only repository has to
# its pull request in a code repository (#2226).
_PULL_FIELDS = "number,title,url,headRefName,baseRefName,isDraft,closingIssuesReferences"
_PULL_FIELDS_TAG = hashlib.sha256(_PULL_FIELDS.encode()).hexdigest()[:8]
# One sub-issue of a parent issue (#3622): it may be in another repository.
_SUB_ISSUES_JQ = (
    '[.[] | {repository: (.repository_url | split("/") | .[-2:] | join("/")), '
    # Label names too: a wayfinder map's box shows its ticket's type even when the deck has
    # never read the ticket's own record (#3627).
    "number, title, state, labels: [.labels[]?.name]}]"
)

# A wayfinder map's ticket dependencies (#3627, keys in ``wayfinder_map``).
_BLOCKED_BY_JQ = (
    '[.[] | {repository: (.repository_url | split("/") | .[-2:] | join("/")), number}]'
)


def _issue_rows(value: Any) -> tuple[dict, ...] | None:
    """A cached sub-issue or "blocked by" list's well-formed rows; None when it is no list."""
    if not isinstance(value, list):
        return None
    return tuple(
        item
        for item in value
        if isinstance(item, dict)
        and isinstance(item.get("repository"), str)
        and isinstance(item.get("number"), int)
    )


def _body_of(value: Any) -> str | None:
    body = value.get("body") if isinstance(value, dict) else None
    return body if isinstance(body, str) else None


# ``agent/issue-<n>`` is the branch name the poller dispatches every worker on, so
# it is the identity a chat carries even before it registers a pull request.
_ISSUE_BRANCH_RE = re.compile(r"(?:^|/)issue-(\d+)(?:$|[^0-9])")
# Only an ``/issues/<n>`` link names an issue. A ``/pull/<n>`` link names a pull
# request, whose number is a different sequence — joining on it would silently
# attach a chat to an unrelated issue.
_ISSUE_LINK_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)",
    re.IGNORECASE,
)
_PULL_LINK_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)


def _no_attention_report(_session_key: str) -> None:
    return None


def issue_number_from_branch(branch: str | None) -> int | None:
    """The issue number an ``agent/issue-<n>`` branch carries, if it carries one."""
    match = _ISSUE_BRANCH_RE.search(branch or "")
    return int(match.group(1)) if match else None


def issue_reference_from_url(url: str | None) -> tuple[str, int] | None:
    """``(owner/repo, number)`` for a GitHub *issue* link, or None."""
    match = _ISSUE_LINK_RE.search(url or "")
    if match is None:
        return None
    return (f"{match.group(1)}/{match.group(2)}", int(match.group(3)))


def pull_reference_from_url(url: str | None) -> tuple[str, int] | None:
    """``(owner/repo, number)`` for a GitHub *pull request* link, or None."""
    match = _PULL_LINK_RE.search(url or "")
    if match is None:
        return None
    return (f"{match.group(1)}/{match.group(2)}", int(match.group(3)))


def identity(repository: str, number: int) -> str:
    """The join key. Case-insensitive because GitHub repository names are."""
    return f"{repository.lower()}#{number}"


@dataclass(frozen=True)
class IssueContext:
    """One GitHub issue, as much of it as the desk needs to reason about."""

    repository: str
    number: int
    title: str
    url: str
    state: str  # "open" | "closed"
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    updated_at: str | None = None
    # When it was opened; None in a list cached before the field was read (#3558).
    created_at: str | None = None

    @property
    def identity(self) -> str:
        return identity(self.repository, self.number)

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def has_label(self, label: str) -> bool:
        lowered = label.lower()
        return any(item.lower() == lowered for item in self.labels)

    def as_json(self) -> dict:
        return {
            "repository": self.repository,
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "state": self.state,
            "labels": list(self.labels),
            "assignees": list(self.assignees),
            "updated_at": self.updated_at,
            "created_at": self.created_at,
        }


def _issue(repository: str, value: object) -> IssueContext | None:
    """One normalized issue row, or None for anything unrecognised.

    Unknown shapes are skipped rather than raised: a single odd row must not turn
    a whole repository's read into a failure.
    """
    if not isinstance(value, dict):
        return None
    number, title, url, state = (
        value.get("number"),
        value.get("title"),
        value.get("url"),
        value.get("state"),
    )
    if not isinstance(number, int) or not isinstance(url, str) or not isinstance(state, str):
        return None
    labels = tuple(
        str(item.get("name"))
        for item in value.get("labels") or ()
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    )
    assignees = tuple(
        str(item.get("login"))
        for item in value.get("assignees") or ()
        if isinstance(item, dict) and isinstance(item.get("login"), str)
    )
    updated = value.get("updatedAt")
    created = value.get("createdAt")
    return IssueContext(
        repository=repository,
        number=number,
        title=title.strip() if isinstance(title, str) else "",
        url=url,
        state=state.lower(),
        labels=labels,
        assignees=assignees,
        updated_at=updated if isinstance(updated, str) else None,
        created_at=created if isinstance(created, str) else None,
    )


def _pull(repository: str, value: object) -> dict | None:
    """One open pull request, in the shape ``PullRequestContext.as_json`` uses.

    Same shape on purpose: a joined item's pull requests come from two sources —
    a live chat's git context and this list — and a reader must not have to know
    which one a row came from. Unknown shapes are skipped, not raised.
    """
    if not isinstance(value, dict):
        return None
    number, url = value.get("number"), value.get("url")
    if not isinstance(number, int) or not isinstance(url, str):
        return None
    head = value.get("headRefName")
    base = value.get("baseRefName")
    title = value.get("title")
    closes = tuple(
        identity(f"{owner}/{name}", ref["number"])
        for ref in value.get("closingIssuesReferences") or ()
        if isinstance(ref, dict)
        and isinstance(ref.get("number"), int)
        and isinstance(repo := ref.get("repository"), dict)
        and isinstance(name := repo.get("name"), str)
        and isinstance(account := repo.get("owner"), dict)
        and isinstance(owner := account.get("login"), str)
    )
    row = {
        "repository": repository,
        "number": number,
        "title": title.strip() if isinstance(title, str) else "",
        "url": url,
        # Only open pull requests are listed, so the status is not a guess.
        "status": "open",
        "draft": bool(value.get("isDraft")),
        "head_branch": head if isinstance(head, str) else None,
        "base_branch": base if isinstance(base, str) else None,
    }
    if closes:
        row["closes"] = closes
    return row


@dataclass(frozen=True)
class RepositoryIssues:
    """One repository's issues and whether the deck actually managed to look.

    ``complete`` false means *unknown*. ``issues`` may still hold the last value
    the shared cache trusted, which is historical evidence, not a current answer.
    """

    repository: str
    issues: tuple[IssueContext, ...] = ()
    complete: bool = True
    reason: str = ""  # why the read is incomplete, for the desk to say out loud
    # The list is capped, so a full page means there may be more open issues
    # behind it. Absence from a truncated list proves nothing (#1033).
    truncated: bool = False
    # A list was actually read, fresh or minutes old. Separate from ``complete``,
    # which also goes false on staleness: only the background sweep refreshes
    # these, so requiring freshness would leave a closed issue's card on the
    # front page for most of every cycle (#1035, the same call as #1016).
    present: bool = False
    # How long ago the list was read. ``present`` says a list exists; this says
    # whether it is still young enough to be evidence about what is *not* on it
    # (#1115). None when nothing was read.
    age_s: float | None = None

    def as_json(self) -> dict:
        return {
            "repository": self.repository,
            "issues": [issue.as_json() for issue in self.issues],
            "complete": self.complete,
            "reason": self.reason,
            "truncated": self.truncated,
            "present": self.present,
            "age_s": self.age_s,
        }

    def decides_closure(self, horizon_s: float) -> bool:
        """Whether absence from this list may be read as *the issue is closed*.

        A list read within ``horizon_s`` may; an older one may not. Unknown age
        counts as too old, because the only way to not know is to have failed
        the read.
        """
        if not self.present or self.truncated:
            return False
        return self.age_s is not None and self.age_s <= horizon_s


@dataclass(frozen=True)
class RepositoryPulls:
    """One repository's open pull requests, read the same way its issues are.

    Kept beside the issue list rather than derived from the chats working an
    issue: a worker's git context disappears when its chat ends, and a pull
    request left open by a finished worker is precisely the one still waiting
    for the operator (#1012).
    """

    repository: str
    pulls: tuple[dict, ...] = ()
    complete: bool = True
    reason: str = ""

    def as_json(self) -> dict:
        return {
            "repository": self.repository,
            "pulls": [dict(pull) for pull in self.pulls],
            "complete": self.complete,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class IssueBacklog:
    """Every declared repository's issues plus the board they are tracked on.

    ``complete`` is the honest headline: false whenever any repository, or the
    board itself, could not be read. A caller that needs to say "nothing is
    pending" must check it first.
    """

    repositories: tuple[RepositoryIssues, ...] = ()
    board: BoardView = field(default_factory=BoardView)
    pulls: tuple[RepositoryPulls, ...] = ()

    @property
    def complete(self) -> bool:
        return self.board.complete and all(entry.complete for entry in self.repositories)

    @property
    def pulls_complete(self) -> bool:
        """Whether every declared repository's open pull requests were read.

        Separate from ``complete`` on purpose: an unread pull request list makes
        the review side of the board strip unknown, but says nothing about the
        issue and card reads every other caller of this backlog depends on.
        """
        return all(entry.complete for entry in self.pulls)

    # How old a repository's issue list may be and still be read as the answer
    # to "what is open here". Freshness (five minutes) is far too strict — only
    # a background sweep refreshes these, so a card would sit on the front page
    # for most of every cycle after its issue closed (#1035). But "a list was
    # read at some point" is far too loose: on a deck whose refresher was off,
    # the lists went days old, every card filed since read as closed, and a whole
    # instance disappeared from the board strip (#1115). An hour is well past any
    # normal refresh cycle and well short of a list that has stopped being
    # evidence.
    CLOSURE_HORIZON_S = 3600.0

    def knows_open_issues(self, repository: str) -> bool:
        """Whether "not in this repository's open list" means *closed*.

        Only when the list was read whole, was not cut off by the cap, and is
        recent enough to still describe the repository: an absence from a
        partial, truncated or long-rotten list is unknown, and treating it as
        closed would quietly retire live work (#1033, #1115).
        """
        lowered = repository.lower()
        return any(
            entry.repository.lower() == lowered
            and entry.decides_closure(self.CLOSURE_HORIZON_S)
            for entry in self.repositories
        )

    @property
    def expired_issue_reads(self) -> tuple[str, ...]:
        """Repositories whose issue list is too old to say what is still open.

        Named rather than counted, because this is the honesty flag a caller
        shows instead of a confident answer it cannot support.
        """
        return tuple(
            entry.repository
            for entry in self.repositories
            if entry.present
            and not entry.decides_closure(self.CLOSURE_HORIZON_S)
            and not entry.truncated
        )

    def open_pull(self, repository: str, number: int) -> dict | None:
        """One open pull request by identity, or None when it is not open.

        None is also what an unread list gives, which is why the caller checks
        ``pulls_complete`` before reading absence as "not open".
        """
        lowered = repository.lower()
        for entry in self.pulls:
            if entry.repository.lower() != lowered:
                continue
            for pull in entry.pulls:
                if pull.get("number") == number:
                    return pull
        return None

    def knows_pulls_for(self, repository: str) -> bool:
        """Whether this repository's open pull request list was actually read."""
        lowered = repository.lower()
        return any(
            entry.repository.lower() == lowered and entry.complete
            for entry in self.pulls
        )

    def pulls_by_issue(self) -> dict[str, tuple[dict, ...]]:
        """Open pull requests keyed by the issue their head branch names.

        The branch is the only structural link between a card and its pull
        request once the chat that opened it is gone, and it is the same rule
        ``session_references`` already joins on.
        """
        found: dict[str, list[dict]] = {}
        for entry in self.pulls:
            for pull in entry.pulls:
                number = issue_number_from_branch(pull.get("head_branch"))
                if number is None:
                    continue
                found.setdefault(identity(entry.repository, number), []).append(pull)
        return {key: tuple(rows) for key, rows in found.items()}

    def pulls_for_work(self) -> dict[str, tuple[dict, ...]]:
        """Open pull requests keyed by the issue they belong to, for a Work card (#2226).

        GitHub's own closing reference, which reaches across repositories — an
        ``owner/issues`` card's pull request lives in ``owner/store`` — or, only for a pull
        request that closes nothing, the head branch, which names an issue number only and so
        only speaks for the pull request's own repository. A pull request that closes
        ``issues#453`` from branch ``issue-453`` is never also ``store#453``'s (#2231 review).
        ``pulls_by_issue`` keeps the branch rule alone for its other readers.
        """
        found: dict[str, dict[str, dict]] = {}
        for entry in self.pulls:
            for pull in entry.pulls:
                keys = list(pull.get("closes") or ())
                # LEGACY(epic:work): the branch rule, for pull requests whose body closes
                # nothing; ``work_facts`` records which of the two found each one.
                number = issue_number_from_branch(pull.get("head_branch"))
                if number is not None and not keys:
                    keys.append(identity(entry.repository, number))
                for key in keys:
                    found.setdefault(key, {}).setdefault(str(pull.get("url")), pull)
        return {key: tuple(rows.values()) for key, rows in found.items()}

    @property
    def unknown(self) -> tuple[str, ...]:
        return tuple(
            entry.repository for entry in self.repositories if not entry.complete
        )

    @property
    def issues(self) -> tuple[IssueContext, ...]:
        return tuple(issue for entry in self.repositories for issue in entry.issues)

    def issue(self, repository: str, number: int) -> IssueContext | None:
        key = identity(repository, number)
        return next((issue for issue in self.issues if issue.identity == key), None)

    def as_json(self) -> dict:
        return {
            "complete": self.complete,
            "unknown": list(self.unknown),
            "board_complete": self.board.complete,
            "pulls_complete": self.pulls_complete,
            "repositories": [entry.as_json() for entry in self.repositories],
            "pulls": [entry.as_json() for entry in self.pulls],
        }


@dataclass(frozen=True)
class InstanceBoardIssue:
    """One cached GitHub Project issue and its Project status."""

    repository: str
    number: int
    status: str
    title: str = ""
    url: str = ""

    @property
    def identity(self) -> str:
        return f"{self.repository}#{self.number}"

    @property
    def short_identity(self) -> str:
        """A compact identity for the instance's already-scoped board."""
        return f"{self.repository.rsplit('/', 1)[-1]}#{self.number}"


@dataclass(frozen=True)
class InstanceBoard:
    """One Poller Instance's board, without flattening same-card identities."""

    instance: str
    issues: tuple[InstanceBoardIssue, ...] = ()
    complete: bool = True
    unknown: tuple[str, ...] = ()

    @property
    def status_counts(self) -> tuple[tuple[str, int], ...]:
        """Status filters in stable, case-insensitive display order."""
        counts: dict[str, int] = {}
        for issue in self.issues:
            status = issue.status or "Status unknown"
            counts[status] = counts.get(status, 0) + 1
        return tuple(sorted(counts.items(), key=lambda item: item[0].casefold()))


class IssueResolver:
    """Resolve the declared repositories' open issues through the shared cache.

    Scope comes from configuration that already exists rather than from a new
    list to keep in sync: the repositories every installed Poller Instance's
    manifest marks ``holds_issues = true``. That bounds the query, and it keeps
    "name your scope" meaningful — a repository the deck cannot see is reported
    as unknown by name.
    """

    COMMAND_TIMEOUT_S = 10.0
    MAX_ISSUES = 100

    def __init__(
        self,
        *,
        poller_root: str | Path = "~/.local/share/agentdeck/poller",
        cache_path: Path | None = None,
        metadata_cache: GitHubMetadataCache | None = None,
    ) -> None:
        self._poller_root = poller_root
        self._gh = shutil.which("gh")
        self._metadata = metadata_cache or GitHubMetadataCache(cache_path)
        self._command_limit = asyncio.Semaphore(4)

    def read_board(self) -> BoardView:
        return poller_ops.read_board(self._poller_root)

    def cached_issues(self, refs: Iterable[tuple[str, int]]) -> dict[tuple[str, int], dict]:
        """Each issue's last cached record (``kanban._ISSUE_JQ``'s shape), keyed by the
        ``(repository, number)`` asked for, in one cache read and never a GitHub call. An
        issue the deck has never resolved is absent (#2219). A failed read raises: the Work
        overview reads a missing record as an open, unpaused issue (#3553)."""
        keys = {
            f"issue:v1:{repository.lower()}#{number}": (repository, number)
            for repository, number in refs
        }
        values = self._metadata.peek_values(keys, strict=True)
        return {keys[key]: value for key, value in values.items() if isinstance(value, dict)}

    async def refresh_issue_records(
        self, refs: Iterable[tuple[str, int]], *, limit: int
    ) -> int:
        """Re-read at most ``limit`` issues' own records from GitHub (#2226).

        The bounded backfill for records the Work overview cannot use: a closed issue cached
        before ``closedAt`` was recorded, which its terminal TTL would keep for a day. The
        same fetch, shared cache and lease as the pills' title resolver.
        """
        wanted = []
        for repository, number in refs:
            owner, _, repo = repository.partition("/")
            if owner and repo:
                wanted.append(KanbanRef(repo=repo, number=number, owner=owner))
        if not wanted or limit <= 0:
            return 0
        titles = KanbanTitleCache(self._metadata.path)
        return await titles.refresh(wanted, time.time(), limit=limit)

    async def refresh_stale_issue_records(self, refs: Iterable[tuple[str, int]]) -> bool:
        """Re-read the issues whose cached record has gone stale; whether any read (#2947).

        Through the cache's own TTL and lease, so an open record is read at most once per
        ``OPEN_TTL_S`` across processes, and a fresh one costs no GitHub call.
        """
        wanted = []
        for repository, number in refs:
            owner, _, repo = repository.partition("/")
            if owner and repo:
                wanted.append(KanbanRef(repo=repo, number=number, owner=owner))
        if not wanted:
            return False
        titles = KanbanTitleCache(self._metadata.path)
        return await titles.resolve_missing(wanted, time.time())

    async def refresh_tree_records(self, refs: Iterable[tuple[str, int]], *, limit: int) -> int:
        """Re-read the parent issues read longer than ``OPEN_TTL_S`` ago, closed ones too, and
        any never read; how many were read (#3622).

        A parent's sub-issue count moves without its own issue changing, and GitHub's count
        lags a link by seconds: a read just after one can keep the old count. A closed parent's
        day-long TTL would keep that answer, so a tree's parents are read on the open TTL
        whatever their state, through the same lease. At most ``limit`` per call."""
        due = []
        for repository, number in refs:
            age = self._metadata.peek(f"issue:v1:{repository.lower()}#{number}").age_s
            if age is None or age >= self._metadata.OPEN_TTL_S:
                due.append((repository, number))
        return await self.refresh_issue_records(due, limit=limit) if due else 0

    def cached_pulls(self, refs: Iterable[tuple[str, int]]) -> dict[tuple[str, int], dict]:
        """Each pull request's last cached row (``git_context._PR_FIELDS``' shape), keyed by
        the ``(repository, number)`` asked for, in one cache read and never a GitHub call. A
        pull request the deck has never resolved is absent (#2226). A failed read raises."""
        keys = {
            f"pr-ref:v1:{repository.lower()}:{number}": (repository, number)
            for repository, number in refs
        }
        values = self._metadata.peek_values(keys, strict=True)
        return {keys[key]: value for key, value in values.items() if isinstance(value, dict)}

    def stale_pull_rows(
        self, refs: Iterable[tuple[str, int, str]], older_than_s: float
    ) -> list[tuple[str, int, str]]:
        """The ``(repository, number, head)`` refs whose cached row was read from GitHub, or
        written by a webhook, at least ``older_than_s`` ago (#3217); local reads only. A pull
        request with no row is left to whatever resolves it first."""
        found = []
        for ref in refs:
            age = self._metadata.peek(f"pr-ref:v1:{ref[0].lower()}:{ref[1]}").age_s
            if age is not None and age >= older_than_s:
                found.append(ref)
        return found

    def cached_merge_checks(
        self, refs: Iterable[tuple[str, int]]
    ) -> dict[tuple[str, int], dict]:
        """Each armed pull request's last required-check row (``merge_checks.parse``), keyed
        like ``cached_pulls``, in one cache read and never a GitHub call (#2275). A failed
        read raises."""
        keys = {
            merge_checks.cache_key(repository, number): (repository, number)
            for repository, number in refs
        }
        values = self._metadata.peek_values(keys, strict=True)
        return {keys[key]: value for key, value in values.items() if isinstance(value, dict)}

    async def refresh_merge_checks(self, refs: Iterable[tuple[str, int]], *, limit: int) -> int:
        """Re-read at most ``limit`` armed pull requests' required checks once their cached
        row has aged out (#2275); ``git_context``'s fetch, through this shared cache."""
        resolver = GitContextResolver(metadata_cache=self._metadata)
        return await resolver.refresh_merge_checks(refs, limit=limit)

    async def refresh_pull_rows(self, refs: Iterable[tuple[str, int]], *, limit: int) -> int:
        """Re-read at most ``limit`` pull requests' cached rows from GitHub (#2236).

        The Work overview's backfill for a row cached before it carried the pull request's
        size; ``git_context``'s own fetch, through this resolver's shared cache.
        """
        resolver = GitContextResolver(metadata_cache=self._metadata)
        return await resolver.refresh_pulls(refs, limit=limit)

    def cached(self) -> IssueBacklog:
        """The last shared issue lists, without refreshing or calling GitHub."""
        board = self.read_board()
        repositories = []
        for repository in board.issue_repositories:
            lookup = self._metadata.peek(f"issue-list:v1:{repository.lower()}")
            rows = lookup.value
            if not isinstance(rows, list):
                repositories.append(
                    RepositoryIssues(
                        repository,
                        (),
                        False,
                        "GitHub issues are not cached",
                    )
                )
                continue
            issues = tuple(
                issue
                for issue in (_issue(repository, item) for item in rows)
                if issue is not None
            )
            ordered = tuple(sorted(issues, key=lambda issue: issue.number, reverse=True))
            repositories.append(
                RepositoryIssues(
                    repository,
                    ordered,
                    lookup.complete,
                    "GitHub issue cache is stale" if not lookup.complete else "",
                    truncated=len(ordered) >= self.MAX_ISSUES,
                    present=True,
                    age_s=lookup.age_s,
                )
            )
        return IssueBacklog(
            repositories=tuple(repositories),
            board=board,
            pulls=tuple(
                self._cached_pulls(repository)
                for repository in board.issue_repositories
            ),
        )

    def _pull_cache_key(self, repository: str) -> str:
        # The field list is part of the key: a row cached before a field was added must never
        # be served as if it had one. ``closingIssuesReferences`` went missing for ten minutes
        # after #2227 deployed, and every card it should have joined said "waiting" (#2228).
        return f"pull-list:v1:{_PULL_FIELDS_TAG}:{repository.lower()}"

    def _cached_pulls(self, repository: str) -> RepositoryPulls:
        lookup = self._metadata.peek(self._pull_cache_key(repository))
        rows = lookup.value
        if not isinstance(rows, list):
            return RepositoryPulls(
                repository, (), False, "GitHub pull requests are not cached"
            )
        pulls = tuple(
            pull for pull in (_pull(repository, row) for row in rows) if pull is not None
        )
        # A list that is merely past its five-minute TTL is still an answer, and
        # only the periodic refresh loop fills it — reporting stale as
        # unknown would leave the strip saying "partial" two thirds of the time,
        # which teaches the operator to ignore the word (#1016). Absence is the
        # unknown this flag exists for.
        return RepositoryPulls(repository, pulls, True, "")

    def cached_instance_boards(self) -> dict[str, InstanceBoard]:
        """Cached Project rows plus durable fallbacks, without refreshing GitHub."""
        boards = {}
        for scope in self.read_board().scopes:
            if scope.boardless:
                boards[scope.instance] = self._cached_boardless_board(scope)
                continue
            lookup = self._metadata.peek(self._project_cache_key(scope))
            value = lookup.value
            rows = value.get("rows") if isinstance(value, dict) else None
            complete = (
                lookup.complete
                and isinstance(value, dict)
                and value.get("incomplete") is not True
            )
            boards[scope.instance] = self._instance_board_from_project(
                scope,
                rows if isinstance(rows, list) else (),
                complete=complete,
            )
        return boards

    async def resolve_instance_boards(
        self, *, force: bool = False
    ) -> dict[str, InstanceBoard]:
        """Every installed Project's complete issue inventory through the shared cache."""
        board = self.read_board()
        fresh_after = time.time() if force else None
        if self._gh is None:
            return self.cached_instance_boards()
        resolved = await asyncio.gather(
            *(
                self._project_board(scope, force=force, fresh_after=fresh_after)
                for scope in board.scopes
            )
        )
        return {entry.instance: entry for entry in resolved}

    async def _project_board(
        self,
        scope: poller_ops.BoardScope,
        *,
        force: bool,
        fresh_after: float | None,
    ) -> InstanceBoard:
        if not scope.usable:
            return self._instance_board_from_project(scope, (), complete=False)
        if scope.boardless:
            return await self._boardless_board(scope, force=force, fresh_after=fresh_after)
        if scope.project_number <= 0:
            return self._instance_board_from_project(scope, (), complete=False)

        async def fetch() -> GitHubMetadata | None:
            assert self._gh is not None
            query = (
                "query($owner:String!,$number:Int!,$cursor:String){"
                "repositoryOwner(login:$owner){"
                "... on Organization{projectV2(number:$number){...BoardItems}}"
                "... on User{projectV2(number:$number){...BoardItems}}}}"
                # Closed issues are dropped below anyway, and on a long-lived board they are
                # nearly all of it: agentdeck's is 1135 items and 23 open, twelve pages a
                # read instead of one (#2384). The state check below still decides.
                "fragment BoardItems on ProjectV2{items(first:100,after:$cursor,"
                'query:"is:open"){'
                "nodes{content{... on Issue{number title url state repository{nameWithOwner}}}"
                'fieldValueByName(name:"Status")'
                "{... on ProjectV2ItemFieldSingleSelectValue{name}}}"
                "pageInfo{hasNextPage endCursor}}}"
            )
            rows: list[dict] = []
            cursor: str | None = None
            incomplete = False
            while True:
                args = [
                    self._gh,
                    "api",
                    "graphql",
                    "-f",
                    "query=" + query,
                    "-f",
                    "owner=" + scope.org,
                    "-F",
                    f"number={scope.project_number}",
                ]
                if cursor is not None:
                    args.extend(("-f", "cursor=" + cursor))
                code, output = await self._run(*args, owner=scope.org)
                if code != 0:
                    # Warned, not debugged: a query GitHub refuses fails the same way on
                    # every read, and a board that never loads is otherwise silent (#2384).
                    log.warning(
                        "Project board unavailable for %s: %s",
                        scope.instance,
                        output.strip()[:500] or f"gh exited {code}",
                    )
                    return None
                try:
                    value = json.loads(output)
                    if not isinstance(value, dict):
                        return None
                    incomplete = incomplete or bool(value.get("errors"))
                    connection = value["data"]["repositoryOwner"]["projectV2"]["items"]
                    nodes = connection["nodes"]
                    page_info = connection["pageInfo"]
                except (KeyError, TypeError, ValueError):
                    return None
                if not isinstance(nodes, list) or not isinstance(page_info, dict):
                    return None
                for node in nodes:
                    row, malformed = self._project_issue_row(node)
                    incomplete = incomplete or malformed
                    if row is not None:
                        rows.append(row)
                if page_info.get("hasNextPage") is not True:
                    break
                next_cursor = page_info.get("endCursor")
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                    incomplete = True
                    break
                cursor = next_cursor
            return GitHubMetadata(
                {"rows": rows, "incomplete": incomplete}, terminal=False
            )

        lookup = await self._metadata.resolve(
            self._project_cache_key(scope),
            fetch,
            force=force,
            fresh_after=fresh_after,
        )
        value = lookup.value
        rows = value.get("rows") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            rows = []
        complete = (
            lookup.complete
            and isinstance(value, dict)
            and value.get("incomplete") is not True
        )
        return self._instance_board_from_project(scope, rows, complete=complete)

    @staticmethod
    def _project_cache_key(scope: poller_ops.BoardScope) -> str:
        return f"project-board:v1:{scope.org.casefold()}:{scope.project_number}"

    @staticmethod
    def _project_issue_row(node: object) -> tuple[dict | None, bool]:
        """One Project item issue, plus whether an issue-shaped row was malformed."""
        if not isinstance(node, dict):
            return (None, True)
        content = node.get("content")
        if content is None:
            return (None, False)  # draft issue
        if not isinstance(content, dict):
            return (None, True)
        repository = content.get("repository")
        if not isinstance(repository, dict):
            return (None, False)  # pull request or another non-Issue content type
        name_with_owner = repository.get("nameWithOwner")
        number = content.get("number")
        url = content.get("url")
        state = content.get("state")
        if (
            not isinstance(name_with_owner, str)
            or not isinstance(number, int)
            or isinstance(number, bool)
            or not isinstance(url, str)
            or not isinstance(state, str)
        ):
            return (None, True)
        status = node.get("fieldValueByName")
        return (
            {
                "repository": name_with_owner,
                "number": number,
                "title": content.get("title") if isinstance(content.get("title"), str) else "",
                "url": url,
                "state": state,
                "status": status.get("name") if isinstance(status, dict) else "",
            },
            False,
        )

    async def _boardless_board(
        self,
        scope: poller_ops.BoardScope,
        *,
        force: bool,
        fresh_after: float | None,
    ) -> InstanceBoard:
        """A boardless instance's inventory: its repositories' issues, statuses from labels.

        There is no Project to page through (#1481), and there is no second read either: the
        repository issue lists the backlog already fetches carry every label, and one of those
        labels *is* the card's status. So this shares the backlog's cache entry rather than
        spending a GitHub read of its own.
        """
        for repository in scope.issue_repositories:
            await self._issues_for_repository(repository, force=force, fresh_after=fresh_after)
        return self._cached_boardless_board(scope)

    def _cached_boardless_board(self, scope: poller_ops.BoardScope) -> InstanceBoard:
        """The same inventory from cache alone, for the paths that never call GitHub."""
        rows: list[dict] = []
        complete = True
        for repository in scope.issue_repositories:
            lookup = self._metadata.peek(f"issue-list:v1:{repository.lower()}")
            value = lookup.value
            if not isinstance(value, list):
                complete = False
                continue
            if not lookup.complete:
                complete = False
            for row in value:
                built = self._boardless_row(repository, row)
                if built is None:
                    complete = False
                    continue
                rows.append(built)
        return self._instance_board_from_project(scope, rows, complete=complete)

    @staticmethod
    def _boardless_row(repository: str, row: object) -> dict | None:
        """One cached issue as a board row, its status read off its `status:*` label."""
        if not isinstance(row, dict):
            return None
        number = row.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            return None
        role = role_of_status_labels(
            str(item.get("name") or "")
            for item in row.get("labels") or []
            if isinstance(item, dict)
        )
        return {
            "repository": repository,
            "number": number,
            "title": str(row.get("title") or ""),
            "url": str(row.get("url") or ""),
            "state": str(row.get("state") or ""),
            "status": role.value if role else "",
        }

    @staticmethod
    def _instance_board_from_project(
        scope: poller_ops.BoardScope, rows: Sequence[object], *, complete: bool
    ) -> InstanceBoard:
        repositories = {repository.casefold() for repository in scope.issue_repositories}
        issues: list[InstanceBoardIssue] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                complete = False
                continue
            repository = row.get("repository")
            number = row.get("number")
            if (
                not isinstance(repository, str)
                or repository.casefold() not in repositories
                or not isinstance(number, int)
                or isinstance(number, bool)
                or str(row.get("state") or "").casefold() != "open"
            ):
                continue
            issue = InstanceBoardIssue(
                repository=repository,
                number=number,
                status=str(row.get("status") or ""),
                title=str(row.get("title") or ""),
                url=str(row.get("url") or ""),
            )
            issues.append(issue)
            seen.add(identity(repository, number))
        return InstanceBoard(
            instance=scope.instance,
            issues=tuple(
                sorted(issues, key=lambda row: (row.repository.casefold(), -row.number))
            ),
            complete=complete and scope.complete,
            unknown=() if complete else scope.issue_repositories,
        )

    async def sub_issues(self, repository: str, number: int) -> tuple[dict, ...] | None:
        """An issue's sub-issues, oldest first, through the shared cache (#3622); None when
        GitHub could not be read, never an empty list for that.

        Each is ``{"repository", "number", "title", "state"}``. Kept a day (#3739): a
        ``sub_issues`` or child's ``issues`` delivery marks the row out of date, and the day
        only covers a lost delivery. Shared across processes like every other row here."""

        async def fetch() -> GitHubMetadata | None:
            if self._gh is None:
                return None
            code, output = await self._run(
                self._gh,
                "api",
                f"repos/{repository}/issues/{number}/sub_issues?per_page=100",
                "--jq",
                _SUB_ISSUES_JQ,
            )
            if code != 0:
                return None
            try:
                value = json.loads(output)
            except (TypeError, ValueError):
                return None
            return GitHubMetadata(value, terminal=True) if isinstance(value, list) else None

        lookup = await self._metadata.resolve(
            wayfinder_map.cache_key(wayfinder_map.SUB_ISSUES_KEY, repository, number), fetch
        )
        found = _issue_rows(lookup.value)
        if found is not None:
            self._link_children(repository, number, found)
        return found

    def held_sub_issues(self, repository: str, number: int) -> tuple[tuple[dict, ...] | None, bool]:
        """``sub_issues``' held value without asking GitHub, and whether it is fresh (#3739):
        ``(None, False)`` when none is held."""
        return self._held(wayfinder_map.SUB_ISSUES_KEY, repository, number, _issue_rows)

    def held_blocked_by(self, repository: str, number: int) -> tuple[tuple[dict, ...] | None, bool]:
        """``blocked_by``' held value without asking GitHub, and whether it is fresh."""
        return self._held(wayfinder_map.BLOCKED_BY_KEY, repository, number, _issue_rows)

    def held_issue_body(self, repository: str, number: int) -> tuple[str | None, bool]:
        """``issue_body``' held value without asking GitHub, and whether it is fresh."""
        return self._held(wayfinder_map.ISSUE_BODY_KEY, repository, number, _body_of)

    def _held(self, prefix: str, repository: str, number: int, parse) -> tuple[Any, bool]:
        lookup = self._metadata.peek(wayfinder_map.cache_key(prefix, repository, number))
        value = parse(lookup.value) if lookup.value is not None else None
        return (value, lookup.complete) if value is not None else (None, False)

    def _link_children(self, repository: str, number: int, children: tuple[dict, ...]) -> None:
        """Write this issue as ``parent`` on each listed child's held row (#3654).

        No GitHub issue read or ``issues`` delivery carries ``parent_issue_url``, so a row
        that lost its link, or whose ``sub_issues`` delivery was dropped, gets it back from
        the parent's list. Only held rows change, keeping their age (``revise``)."""
        url = f"https://api.github.com/repos/{repository}/issues/{number}"
        for child in children:
            key = f"issue:v1:{child['repository'].lower()}#{child['number']}"
            held = self._metadata.peek(key).value
            if isinstance(held, dict) and held.get("parent") != url:
                self._metadata.revise(key, {**held, "parent": url})

    async def blocked_by(self, repository: str, number: int) -> tuple[dict, ...] | None:
        """The issues an issue is blocked by (GitHub's native dependencies), through the
        shared cache (#3627); None when GitHub could not be read.

        Each is ``{"repository", "number"}``. Kept a day (#3739); an ``issue_dependencies``
        delivery marks the row out of date (``webhook_status_cache``)."""
        return _issue_rows(
            await self._cached_api(
                wayfinder_map.cache_key(wayfinder_map.BLOCKED_BY_KEY, repository, number),
                f"repos/{repository}/issues/{number}/dependencies/blocked_by?per_page=100",
                _BLOCKED_BY_JQ,
            )
        )

    async def issue_body(self, repository: str, number: int) -> str | None:
        """An issue's body, through the shared cache (#3627); None when unreadable.

        A wayfinder map's body names its destination and its fog. Kept a day (#3739); an
        ``issues`` delivery marks the row out of date (``webhook_status_cache``)."""
        return _body_of(
            await self._cached_api(
                wayfinder_map.cache_key(wayfinder_map.ISSUE_BODY_KEY, repository, number),
                f"repos/{repository}/issues/{number}",
                '{body: (.body // "")}',
            )
        )

    async def _cached_api(self, key: str, path: str, jq: str) -> Any:
        """One ``gh api`` read as JSON through the shared cache, or None when it failed.

        Stored with the day-long TTL (#3739): its webhooks mark the row out of date, so the
        TTL is only the backstop for a delivery GitHub never sent."""

        async def fetch() -> GitHubMetadata | None:
            if self._gh is None:
                return None
            code, output = await self._run(self._gh, "api", path, "--jq", jq)
            if code != 0:
                return None
            try:
                value = json.loads(output)
            except (TypeError, ValueError):
                return None
            return GitHubMetadata(value, terminal=True)

        return (await self._metadata.resolve(key, fetch)).value

    async def _run(self, *args: str, owner: str | None = None) -> tuple[int, str]:
        try:
            async with self._command_limit:
                code, stdout = await gh_cli.run(
                    args, timeout=self.COMMAND_TIMEOUT_S, owner=owner
                )
        except (OSError, TimeoutError) as exc:
            log.debug("Issue read command failed: %s", exc)
            return (1, "")
        return (code, stdout.decode("utf-8", "replace"))

    async def resolve(self, *, force: bool = False) -> IssueBacklog:
        board = self.read_board()
        repositories = board.issue_repositories
        fresh_after = time.time() if force else None
        if self._gh is None:
            # No ``gh`` on this host is exactly the "could not look" case, and it
            # is the one most likely to be mistaken for an empty backlog.
            return IssueBacklog(
                repositories=tuple(
                    RepositoryIssues(repository, (), False, "gh is not installed")
                    for repository in repositories
                ),
                board=board,
                pulls=tuple(
                    RepositoryPulls(repository, (), False, "gh is not installed")
                    for repository in repositories
                ),
            )
        resolved = await asyncio.gather(
            *(
                self._issues_for_repository(
                    repository, force=force, fresh_after=fresh_after
                )
                for repository in repositories
            ),
            *(
                self._pulls_for_repository(
                    repository, force=force, fresh_after=fresh_after
                )
                for repository in repositories
            ),
        )
        count = len(repositories)
        return IssueBacklog(
            repositories=tuple(resolved[:count]),
            board=board,
            pulls=tuple(resolved[count:]),
        )

    async def _pulls_for_repository(
        self,
        repository: str,
        *,
        force: bool = False,
        fresh_after: float | None = None,
    ) -> RepositoryPulls:
        """One repository's open pull requests, through the same shared cache.

        One list call per repository per refresh, on the background pass that was
        already reading that repository's issues — never on a page render, which
        is why the dashboard can show a finished worker's still-open PR without
        spending quota (#1012).
        """

        async def fetch() -> GitHubMetadata | None:
            assert self._gh is not None
            code, output = await self._run(
                self._gh,
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "open",
                "--limit",
                str(self.MAX_ISSUES),
                "--json",
                _PULL_FIELDS,
            )
            if code != 0:
                log.debug("Pull request list unavailable for %s", repository)
                return None
            try:
                value = json.loads(output)
            except (TypeError, ValueError):
                log.debug("Pull request list unreadable for %s", repository)
                return None
            if not isinstance(value, list):
                return None
            # Never terminal: a list of open pull requests changes on every push.
            return GitHubMetadata(value, terminal=False)

        lookup = await self._metadata.resolve(
            self._pull_cache_key(repository),
            fetch,
            force=force,
            fresh_after=fresh_after,
        )
        value = lookup.value
        if not isinstance(value, list):
            return RepositoryPulls(
                repository, (), False, "GitHub pull requests could not be read"
            )
        pulls = tuple(
            pull for pull in (_pull(repository, row) for row in value) if pull is not None
        )
        if not lookup.complete:
            return RepositoryPulls(
                repository, pulls, False, "GitHub pull request read was stale"
            )
        return RepositoryPulls(repository, pulls, True)

    async def _issues_for_repository(
        self,
        repository: str,
        *,
        force: bool = False,
        fresh_after: float | None = None,
    ) -> RepositoryIssues:
        async def fetch() -> GitHubMetadata | None:
            assert self._gh is not None
            code, output = await self._run(
                self._gh,
                "issue",
                "list",
                "--repo",
                repository,
                "--state",
                "open",
                "--limit",
                str(self.MAX_ISSUES),
                "--json",
                _ISSUE_FIELDS,
            )
            if code != 0:
                log.debug("Issue list unavailable for %s", repository)
                return None
            try:
                value = json.loads(output)
            except (TypeError, ValueError):
                log.debug("Issue list unreadable for %s", repository)
                return None
            if not isinstance(value, list):
                return None
            # An open issue list is never terminal: it changes whenever anything
            # is filed, closed, or relabelled.
            return GitHubMetadata(value, terminal=False)

        lookup = await self._metadata.resolve(
            f"issue-list:v1:{repository.lower()}",
            fetch,
            force=force,
            fresh_after=fresh_after,
        )
        value = lookup.value
        if not isinstance(value, list):
            return RepositoryIssues(repository, (), False, "GitHub issues could not be read")
        issues = tuple(
            issue
            for issue in (_issue(repository, item) for item in value)
            if issue is not None
        )
        ordered = tuple(sorted(issues, key=lambda issue: issue.number, reverse=True))
        truncated = len(ordered) >= self.MAX_ISSUES
        if not lookup.complete:
            return RepositoryIssues(
                repository,
                ordered,
                False,
                "GitHub issue read was stale",
                truncated,
                True,
                lookup.age_s,
            )
        return RepositoryIssues(
            repository, ordered, True, "", truncated, True, lookup.age_s
        )


# --- the join -----------------------------------------------------------


@dataclass(frozen=True)
class SessionRef:
    """One chat's side of a joined item — identity and waiting-ness only.

    Deliberately thin: the desk already has the session list and the Deckhand
    view, so this says *which* chats belong to the item and how they were
    matched, not everything known about them.
    """

    key: str
    title: str
    waiting: bool = False
    attention: str | None = None  # the session's own "waiting"/"finished" report
    status: str = ""
    branch: str | None = None
    matched_on: str = ""  # "issue" | "issue_url" | "branch" | "pull_request"

    def as_json(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "waiting": self.waiting,
            "attention": self.attention,
            "status": self.status,
            "branch": self.branch,
            "matched_on": self.matched_on,
        }


@dataclass(frozen=True)
class PendingItem:
    """One unit of pending work: the issue and its chats."""

    repository: str
    number: int
    issue: IssueContext | None = None
    sessions: tuple[SessionRef, ...] = ()
    pull_requests: tuple[dict, ...] = ()
    ownership_label: str = ""
    ownership_labels: tuple[str, ...] = ()
    # The repository's open list was read whole and does not carry this number,
    # so the issue is closed — as opposed to simply not being readable (#1033).
    issue_closed: bool = False

    @property
    def identity(self) -> str:
        return identity(self.repository, self.number)

    @property
    def owned_by_poller(self) -> bool:
        """The issue carries its repository's ownership label, when the issue was readable."""
        labels = self.ownership_labels or (
            (self.ownership_label,) if self.ownership_label else ()
        )
        if self.issue is not None and labels:
            return any(self.issue.has_label(label) for label in labels)
        return False

    @property
    def owned_by_label(self) -> bool:
        """The live issue is open and carries an ownership label (#866)."""
        labels = self.ownership_labels or (
            (self.ownership_label,) if self.ownership_label else ()
        )
        if self.issue is None or not self.issue.is_open or not labels:
            return False
        return any(self.issue.has_label(label) for label in labels)

    @property
    def waiting(self) -> bool:
        return any(session.waiting for session in self.sessions)

    def as_json(self) -> dict:
        return {
            "repository": self.repository,
            "number": self.number,
            "identity": self.identity,
            "issue": self.issue.as_json() if self.issue else None,
            "sessions": [session.as_json() for session in self.sessions],
            "pull_requests": list(self.pull_requests),
            "ownership_label": self.ownership_label,
            "issue_closed": self.issue_closed,
            "owned_by_poller": self.owned_by_poller,
            "waiting": self.waiting,
        }


@dataclass(frozen=True)
class PendingView:
    """The joined answer to "what is pending", with its own honesty flag."""

    items: tuple[PendingItem, ...] = ()
    complete: bool = True
    unknown: tuple[str, ...] = ()
    board_complete: bool = True
    pulls_complete: bool = True
    # Repositories whose issue list was read too long ago to say what is still
    # open there. Their items stay on the list — an unread list is not a closed
    # issue — and this names why the answer is only part of one (#1115).
    expired_issue_reads: tuple[str, ...] = ()

    def as_json(self) -> dict:
        return {
            "complete": self.complete,
            "unknown": list(self.unknown),
            "board_complete": self.board_complete,
            "pulls_complete": self.pulls_complete,
            "expired_issue_reads": list(self.expired_issue_reads),
            "items": [item.as_json() for item in self.items],
        }


def session_references(
    session: Session, context: GitContext | None
) -> tuple[tuple[str, int, str], ...]:
    """Every ``(repository, number, matched_on)`` this chat identifies itself with.

    Three identities, strongest first — a registered issue link, then the
    branch of a registered pull request, then the checkout's own branch. All
    three are structural. Conversation prose is never read: a repository or issue
    mentioned in a message is not evidence that this chat works on it.
    """
    found: dict[tuple[str, int], str] = {}

    linked = issue_reference_from_url(session.issue_url)
    if linked is not None:
        found[(linked[0].lower(), linked[1])] = "issue_url"

    for issue in session.issues:
        found.setdefault((issue.repository.lower(), issue.number), "issue")

    for pull in (context.pull_requests if context else ()):
        number = issue_number_from_branch(pull.head_branch)
        if number is not None:
            found.setdefault((pull.repository.lower(), number), "pull_request")

    if context and context.repository and context.branch:
        number = issue_number_from_branch(context.branch)
        if number is not None:
            found.setdefault((context.repository.lower(), number), "branch")

    return tuple(
        (repository, number, matched)
        for (repository, number), matched in found.items()
    )


def join_pending(
    backlog: IssueBacklog,
    sessions: Sequence[Session],
    contexts: Mapping[str, GitContext] | None = None,
    *,
    attention_report_for: Callable[[str], AttentionReport | None] | None = None,
    repositories: Iterable[str] | None = None,
) -> PendingView:
    """Join open issues, their pull requests, and the chats working them into one list.

    An item is created by any of these populations, so work that has not become
    a chat yet and a chat whose issue read failed both appear. ``repositories`` narrows
    the join to a declared scope; by default it is every repository the backlog covers.
    """
    contexts = contexts or {}
    if attention_report_for is None:
        attention_report_for = _no_attention_report
    scope = {
        item.lower()
        for item in (
            repositories
            if repositories is not None
            else backlog.board.issue_repositories
        )
    }

    spelling: dict[str, str] = {}
    issues: dict[str, IssueContext] = {}
    for issue in backlog.issues:
        spelling.setdefault(issue.repository.lower(), issue.repository)
        issues[issue.identity] = issue

    session_rows: dict[str, list[SessionRef]] = {}
    session_recency: dict[str, float] = {}
    pulls: dict[str, list[dict]] = {}
    for session in sessions:
        stamp = session.started_at or session.last_activity
        session_recency[session.key] = stamp.timestamp() if stamp is not None else 0.0
        context = contexts.get(session.key)
        for repository, number, matched in session_references(session, context):
            if scope and repository not in scope:
                continue
            key = identity(repository, number)
            spelling.setdefault(repository, repository)
            report = attention_report_for(session.key)
            session_rows.setdefault(key, []).append(
                SessionRef(
                    key=session.key,
                    title=session.display_title,
                    waiting=session.is_waiting
                    or getattr(report, "state", None) == "waiting",
                    attention=getattr(report, "state", None),
                    status=session.display_state,
                    branch=context.branch if context else None,
                    matched_on=matched,
                )
            )
            for pull in (context.pull_requests if context else ()):
                # A project may keep its issues in one repository and its code in
                # others — one project files every card in `.../issues` while the pull
                # requests land in `shop`, `ledger` and friends. A pull request
                # elsewhere still belongs to this item when the chat names the
                # issue explicitly *and* the branch names the same number: two
                # structural facts, neither inferred from prose (#1020).
                if pull.repository.lower() != repository and not (
                    matched in ("issue", "issue_url")
                    and issue_number_from_branch(pull.head_branch) == number
                ):
                    continue
                rows = pulls.setdefault(key, [])
                if all(row["number"] != pull.number for row in rows):
                    rows.append(pull.as_json())

    # The repository's own open pull requests, joined by head branch. A chat's
    # git context is the fresher source, so it wins on number collisions; this
    # fills the gap it leaves behind when a worker chat ends (#1012).
    for key, rows in backlog.pulls_by_issue().items():
        repo_key = key.partition("#")[0]
        if scope and repo_key not in scope:
            continue
        spelling.setdefault(repo_key, repo_key)
        known = pulls.setdefault(key, [])
        for pull in rows:
            if all(row["number"] != pull["number"] for row in known):
                known.append(dict(pull))

    keys = set(issues) | set(session_rows) | set(pulls)
    items: list[PendingItem] = []
    for key in keys:
        repo_key, _, raw_number = key.partition("#")
        repository = spelling.get(repo_key, repo_key)
        items.append(
            PendingItem(
                repository=repository,
                number=int(raw_number),
                issue=issues.get(key),
                sessions=tuple(
                    sorted(
                        session_rows.get(key, ()),
                        key=lambda row: session_recency.get(row.key, 0.0),
                        reverse=True,
                    )
                ),
                pull_requests=tuple(pulls.get(key, ())),
                ownership_label=backlog.board.ownership_label(repository),
                ownership_labels=backlog.board.ownership_labels(repository),
                issue_closed=(
                    key not in issues and backlog.knows_open_issues(repository)
                ),
            )
        )
    items.sort(key=lambda item: (item.repository.lower(), -item.number))
    return PendingView(
        items=tuple(items),
        complete=backlog.complete,
        unknown=backlog.unknown,
        board_complete=backlog.board.complete,
        pulls_complete=backlog.pulls_complete,
        expired_issue_reads=tuple(
            repository
            for repository in backlog.expired_issue_reads
            if not scope or repository.lower() in scope
        ),
    )
