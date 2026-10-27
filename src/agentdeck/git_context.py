"""Authoritative git and GitHub pull-request context for Deckhand."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import gh_cli, merge_checks
from .github_cache import GitHubMetadata, GitHubMetadataCache
from .models import Session

# One GitHub remote parser for the deck and the stdlib-only poller engine (#2148).
from .poller_engine.workspace import github_repository

log = logging.getLogger(__name__)

# ``updatedAt`` is not read for rendering: it is the version the webhook write-through
# (#1627) compares against, and fetching it here keeps polled rows comparable with
# webhook-written ones instead of always losing the last-writer-wins guard.
# ``headRefOid`` is the pull request's current head commit, which is what says whether a
# finished review is about the latest commit or an older one (#2088). It rides the query
# that was already being made, so knowing it costs no extra GitHub call.
# ``additions``, ``deletions`` and ``changedFiles`` are the pull request's size, which a Work
# row shows as ``+a/−d · N files`` (#2236); the same query, so no extra call either.
# ``files`` is the per-file list the size is summed from, so tests and docs can be left out
# of it (#2331); ``gh`` stops it at 100 files, and a longer pull request shows no size.
# ``autoMergeRequest`` says GitHub merges it by itself once its required checks pass, which is
# what makes a Work worth asking ``merge_checks`` about (#2275); the same query again.
_PR_FIELDS = (
    "number,title,url,state,isDraft,mergedAt,headRefName,baseRefName,headRefOid,updatedAt,"
    "additions,deletions,changedFiles,files,autoMergeRequest"
)
_COMMIT_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _pull_cache_key(repository: str, number: int) -> str:
    """Shared-cache key for one pull request.

    One spelling, because the resolver both peeks at this key and refreshes it:
    two literals would drift and the peek would silently always miss.
    """
    return f"pr-ref:v1:{repository.lower()}:{number}"


@dataclass(frozen=True)
class PullRequestContext:
    repository: str
    number: int
    title: str
    url: str
    status: str  # "open" | "merged" | "closed"; interpret via the properties below
    draft: bool = False
    head_branch: str | None = None
    base_branch: str | None = None
    # The commit the pull request currently points at (#2088). None whenever the row this
    # was built from predates the field, which reads as "we do not know" and never as
    # "the head has not moved".
    head_sha: str | None = None

    @property
    def short_head_sha(self) -> str:
        """The head commit in the seven characters everything else on the page shows."""
        return self.head_sha[:7] if self.head_sha else ""

    @property
    def is_open(self) -> bool:
        """The PR is still open (a merged/closed PR is not)."""
        return self.status == "open"

    @property
    def is_merged(self) -> bool:
        """The PR actually merged (a PR closed without merging did not)."""
        return self.status == "merged"

    @property
    def is_terminal(self) -> bool:
        """The PR is resolved — merged or closed — so nothing is left to do.

        Kept an explicit set rather than ``not is_open`` so an unexpected status
        string is never treated as terminal by default.
        """
        return self.status in {"merged", "closed"}

    @property
    def is_reviewable(self) -> bool:
        """Open and not a draft — a human could review it now."""
        return self.is_open and not self.draft

    @property
    def display_status(self) -> str:
        """The chip label: an open draft reads ``draft``, otherwise the raw
        status. (The CSS class stays keyed on the raw ``status``.)"""
        return "draft" if self.draft and self.is_open else self.status

    def as_json(self) -> dict:
        return {
            "repository": self.repository,
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "status": self.status,
            "draft": self.draft,
            "head_branch": self.head_branch,
            "base_branch": self.base_branch,
            "head_sha": self.head_sha,
        }

    @classmethod
    def from_json(cls, value: object) -> PullRequestContext | None:
        """Rehydrate ``as_json`` output, or None for anything unrecognised."""
        if not isinstance(value, dict):
            return None
        repository, title, url, status = (
            value.get("repository"),
            value.get("title"),
            value.get("url"),
            value.get("status"),
        )
        number = value.get("number")
        if not isinstance(number, int) or not all(
            isinstance(item, str) for item in (repository, title, url, status)
        ):
            return None
        head, base = value.get("head_branch"), value.get("base_branch")
        head_sha = value.get("head_sha")
        return cls(
            repository=repository,
            number=number,
            title=title,
            url=url,
            status=status,
            draft=bool(value.get("draft")),
            head_branch=head if isinstance(head, str) else None,
            base_branch=base if isinstance(base, str) else None,
            head_sha=head_sha if isinstance(head_sha, str) and head_sha else None,
        )


@dataclass(frozen=True)
class GitContext:
    repository: str | None
    branch: str | None
    dirty: bool
    pull_requests: tuple[PullRequestContext, ...] = ()
    pulls_complete: bool = True
    # The checkout's current HEAD commit, when git could answer. This is the
    # commit a review verdict is about (#1127), so it follows the same rule as
    # ``branch``: a shared checkout sitting on another chat's branch exposes
    # neither.
    head_sha: str | None = None

    @property
    def short_head_sha(self) -> str | None:
        """The seven-character HEAD, or None when there is no HEAD to show."""
        return self.head_sha[:7] if self.head_sha else None

    @property
    def reviewable_pull(self) -> PullRequestContext | None:
        """The first reviewable PR (highest-numbered, by resolver ordering), or None."""
        return next((pull for pull in self.pull_requests if pull.is_reviewable), None)

    @property
    def is_shipped(self) -> bool:
        """Has resolved PRs and every one is terminal. An empty set is NOT
        shipped — a session with no PRs stays eligible for a finished card."""
        if not self.pulls_complete or not self.pull_requests:
            return False
        return all(pull.is_terminal for pull in self.pull_requests)

    @property
    def has_merged_pr(self) -> bool:
        """At least one PR actually merged (closed-without-merging does not count)."""
        return any(pull.is_merged for pull in self.pull_requests)

    def as_json(self) -> dict:
        return {
            "repository": self.repository,
            "branch": self.branch,
            "dirty": self.dirty,
            "pull_requests": [pull.as_json() for pull in self.pull_requests],
            "pulls_complete": self.pulls_complete,
            "head_sha": self.head_sha,
        }

    @classmethod
    def from_json(cls, value: object) -> GitContext | None:
        """Rehydrate ``as_json`` output, or None for anything unrecognised.

        A context is only worth restoring when every PR in it survives the
        round-trip: a partially decoded set would look authoritative while
        silently having lost a pull request.
        """
        if not isinstance(value, dict):
            return None
        raw_pulls = value.get("pull_requests")
        if not isinstance(raw_pulls, list):
            return None
        pulls = tuple(PullRequestContext.from_json(item) for item in raw_pulls)
        if any(pull is None for pull in pulls):
            return None
        repository, branch = value.get("repository"), value.get("branch")
        head_sha = value.get("head_sha")
        return cls(
            repository=repository if isinstance(repository, str) else None,
            branch=branch if isinstance(branch, str) else None,
            dirty=bool(value.get("dirty")),
            pull_requests=tuple(pull for pull in pulls if pull is not None),
            pulls_complete=bool(value.get("pulls_complete", True)),
            head_sha=head_sha if isinstance(head_sha, str) and head_sha else None,
        )


class GitContexts:
    """Every triaged session's authoritative git/PR context, keyed by session key.

    This store owns one rule: **an incomplete lookup is not absence.** A GitHub
    call that timed out, hit a rate limit, or ran on a host without ``gh``
    resolves to a context whose ``pulls_complete`` is False. That means "we could
    not look", never "the registered pull request vanished" — so it must not
    replace what a complete lookup already established, and must not reach the
    checkpoint a cold web process restores from. Miss that at any one site and a
    restart or a ``gh`` blip resurrects Attention Insights for shipped work.

    An incomplete context *is* accepted for a key that has no entry yet: that is
    a first lookup failing, not a downgrade, and the alternative is knowing
    nothing at all about the session.

    ``update`` is the only writer, so the rule is decided here rather than at
    each point of use. Reads go through the store too (``is_shipped``,
    ``has_merged_pr``), which spares callers from spelling out what an absent
    key means.
    """

    def __init__(self) -> None:
        self._contexts: dict[str, GitContext] = {}

    @property
    def view(self) -> Mapping[str, GitContext]:
        """A read-only mapping, safe to hand to the web layer."""
        return MappingProxyType(self._contexts)

    def get(self, key: str) -> GitContext | None:
        return self._contexts.get(key)

    def update(self, resolved: Mapping[str, GitContext]) -> bool:
        """Merge freshly resolved contexts. Returns whether anything changed."""
        changed = False
        for key, context in resolved.items():
            existing = self._contexts.get(key)
            if context == existing:
                continue
            if existing is not None and not context.pulls_complete:
                continue
            self._contexts[key] = context
            changed = True
        return changed

    def retain(self, keys: Iterable[str]) -> None:
        """Prune to the sessions that are still eligible for triage."""
        eligible = frozenset(keys)
        self._contexts = {
            key: context for key, context in self._contexts.items() if key in eligible
        }

    def is_shipped(self, key: str) -> bool:
        context = self._contexts.get(key)
        return context is not None and context.is_shipped

    def has_merged_pr(self, key: str) -> bool:
        context = self._contexts.get(key)
        return context is not None and context.has_merged_pr

    def shipped_keys(self) -> tuple[str, ...]:
        return tuple(key for key, context in self._contexts.items() if context.is_shipped)

    def as_json(self) -> dict[str, dict]:
        """Checkpoint payload — complete contexts only."""
        return {
            key: context.as_json()
            for key, context in self._contexts.items()
            if context.pulls_complete
        }

    def restore(self, payload: object) -> None:
        """Adopt a checkpoint payload, ignoring anything unreadable or incomplete."""
        if not isinstance(payload, dict):
            return
        for key, raw in payload.items():
            context = GitContext.from_json(raw)
            if context is not None and context.pulls_complete:
                self._contexts[str(key)] = context


def _pull_request(repository: str, value: dict) -> PullRequestContext | None:
    number = value.get("number")
    title = value.get("title")
    url = value.get("url")
    state = value.get("state")
    if not isinstance(number, int) or not all(
        isinstance(item, str) for item in (title, url, state)
    ):
        return None
    status = "merged" if value.get("mergedAt") else state.lower()
    return PullRequestContext(
        repository=repository,
        number=number,
        title=title.strip(),
        url=url,
        status=status,
        draft=bool(value.get("isDraft")),
        head_branch=value.get("headRefName") if isinstance(value.get("headRefName"), str) else None,
        base_branch=value.get("baseRefName") if isinstance(value.get("baseRefName"), str) else None,
        head_sha=value.get("headRefOid") if isinstance(value.get("headRefOid"), str) else None,
    )


class GitContextResolver:
    """Resolve session worktrees and their registered PRs without giving Deckhand tools.

    A session's pull requests are the ones its agent registered through the
    local API (``Session.pull_requests``) — nothing is guessed from transcript
    text or from whichever branch a shared checkout currently sits on. This
    resolver only turns those references into live GitHub state.

    Git state is cheap and refreshed with every Deckhand analysis opportunity.
    GitHub calls are cached more aggressively, with terminal PRs treated as stable.
    """

    COMMAND_TIMEOUT_S = 6.0
    MAX_PULL_REQUESTS = 10
    # How long a chat must have been silent before its pull requests stop being
    # re-read. Deliberately not "has no live process": an idle chat is one the
    # operator can resume and often does, and freezing today's work would be a
    # visible regression for a saving the terminal TTL already collects. A day
    # of silence is what actually distinguishes finished work from resting work.
    DORMANT_AFTER_S = 24 * 3600.0

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        metadata_cache: GitHubMetadataCache | None = None,
    ) -> None:
        self._git = shutil.which("git")
        self._gh = shutil.which("gh")
        self._metadata = metadata_cache or GitHubMetadataCache(cache_path)
        self._command_limit = asyncio.Semaphore(6)

    async def _run(self, *args: str, owner: str | None = None) -> tuple[int, str]:
        try:
            async with self._command_limit:
                # `gh_cli` picks the owner's non-human authority for a `gh` call and
                # refuses one it cannot place (#2289); git runs through here too.
                code, stdout = await gh_cli.run(
                    args, timeout=self.COMMAND_TIMEOUT_S, owner=owner
                )
        except (OSError, TimeoutError) as exc:
            log.debug("Deckhand context command failed: %s", exc)
            return (1, "")
        return (code, stdout.decode("utf-8", "replace"))

    async def resolve(
        self, sessions: list[Session], *, force: bool = False
    ) -> dict[str, GitContext]:
        fresh_after = time.time() if force else None
        rows = await asyncio.gather(
            *(
                self._resolve_session(
                    session, force=force, fresh_after=fresh_after
                )
                for session in sessions
            )
        )
        return {
            session.key: context
            for session, context in zip(sessions, rows, strict=True)
            if context is not None
        }

    async def _resolve_session(
        self,
        session: Session,
        *,
        force: bool = False,
        fresh_after: float | None = None,
    ) -> GitContext | None:
        branch = None
        dirty = False
        head_sha = None
        referenced = self._repository_from_session(session)
        checkout = None
        git_found = False
        if self._git is not None and session.cwd is not None:
            cwd = str(session.cwd)
            (
                (status_code, status_text),
                (remote_code, remote_text),
                (head_code, head_text),
            ) = await asyncio.gather(
                self._run(self._git, "-C", cwd, "status", "--porcelain=v1", "--branch"),
                self._run(self._git, "-C", cwd, "remote", "get-url", "origin"),
                self._run(self._git, "-C", cwd, "rev-parse", "HEAD"),
            )
            if head_code == 0:
                candidate = head_text.strip()
                # A repository with no commits yet answers with an error, and an
                # unexpected shape is not a commit: only take something that
                # looks like one.
                if _COMMIT_RE.fullmatch(candidate):
                    head_sha = candidate
            if status_code == 0:
                git_found = True
                lines = status_text.splitlines()
                if lines and lines[0].startswith("## "):
                    head = lines[0][3:].split("...", 1)[0]
                    if head and not head.startswith("HEAD "):
                        branch = head
                dirty = any(line and not line.startswith("## ") for line in lines)
            if remote_code == 0:
                checkout = github_repository(remote_text)

        # The checkout's own remote names the repository; a repo named by the
        # session's issue URL identifies work whose worktree is already gone.
        repository = checkout or referenced
        # Keep the registered spelling for display; identity dedupe is case-
        # insensitive because GitHub repository names are.
        registered = {ref.identity: ref for ref in reversed(session.pull_requests)}
        refs = [
            (ref.repository, ref.number) for ref in registered.values()
        ][: self.MAX_PULL_REQUESTS]

        pulls: dict[tuple[str, int], PullRequestContext] = {}
        pulls_complete = self._gh is not None or not refs
        if self._gh is not None and refs:
            resolved = await asyncio.gather(
                *(
                    self._pull_for_ref(
                        repo,
                        number,
                        force=force,
                        fresh_after=fresh_after,
                        cached_first=not force and self._dormant(session),
                    )
                    for repo, number in refs
                )
            )
            # Every registered PR is an explicit claim by this chat, so any
            # unresolved one makes the context incomplete rather than empty.
            pulls_complete = all(complete for _pull, complete in resolved)
            registered_pulls = tuple(pull for pull, _complete in resolved if pull is not None)
            for pull in registered_pulls:
                pulls[(pull.repository.lower(), pull.number)] = pull

            # A shared checkout may have moved to another chat's branch. Once
            # this chat registers its own PR, expose local branch/dirty state
            # only when that checkout still matches one of the PR head branches.
            if (
                registered_pulls
                and branch
                and not any(pull.head_branch == branch for pull in registered_pulls)
            ):
                branch = None
                dirty = False
                head_sha = None

        ordered = tuple(sorted(pulls.values(), key=lambda pull: pull.number, reverse=True))
        if not git_found and repository is None and not ordered:
            return None
        return GitContext(repository, branch, dirty, ordered, pulls_complete, head_sha)

    @staticmethod
    def _repository_from_session(session: Session) -> str | None:
        """Repository named by the session's structured issue link, if any.

        Used only when the checkout itself cannot answer (a worktree that has
        been removed). Conversation prose is deliberately not read: a repository
        mentioned in a message is not evidence that this chat works on it.
        """
        issue = session.primary_issue
        return issue.repository if issue is not None else None

    def _dormant(self, session: Session) -> bool:
        """Has this chat been silent long enough that its PRs cannot move?

        Unknown timestamps answer False: a chat the deck cannot date is treated
        as current, because the cost of being wrong that way is one GitHub call
        and the cost of the other way is a card frozen on stale state.
        """
        stamp = session.last_activity or session.started_at
        if stamp is None:
            return False
        return (datetime.now(UTC) - stamp).total_seconds() > self.DORMANT_AFTER_S

    def reconcile_cached_pulls_many(
        self, contexts: Mapping[str, GitContext]
    ) -> dict[str, GitContext]:
        """``reconcile_cached_pulls`` for many contexts in one cache read (#2841).

        Deckhand reconciles every held context on each pass; a query per pull request
        was a large share of a pass that runs on the web process's event loop, and so
        of the wait for a chat page opened while it ran. Only the contexts that moved
        are returned, keyed as given.
        """
        keys = {
            _pull_cache_key(pull.repository, pull.number)
            for context in contexts.values()
            for pull in context.pull_requests
        }
        if not keys:
            return {}
        held = self._metadata.peek_values(keys)
        moved: dict[str, GitContext] = {}
        for name, context in contexts.items():
            reconciled = self._reconciled(context, held)
            if reconciled is not None:
                moved[name] = reconciled
        return moved

    def reconcile_cached_pulls(self, context: GitContext) -> GitContext | None:
        """This context with its pull requests brought up to the shared cache.

        Returns None when the cache agrees, so a caller can tell "nothing moved"
        from "here is the new one" without comparing contexts itself.

        The pull-request rows are kept current for free by the webhook streams
        (#1627, #1642): a delivery rewrites ``pr-ref:v1:<repo>:<number>`` seconds
        after the pull request moves, spending no GitHub quota. Nothing consulted
        those rows again once a session's context had been resolved, so a chat's
        pill went on showing the state of its first lookup while the truth sat one
        cache read away (#1697).

        This is that read: a local peek per registered pull request, no GitHub call
        and no subprocess. A key with no row changes nothing — an absent row is not
        evidence that anything moved, and the ordinary TTL still covers it. Only
        the pull requests are reconciled: branch, dirtiness and HEAD come from the
        checkout, which no webhook can speak for.
        """
        keys = [_pull_cache_key(pull.repository, pull.number) for pull in context.pull_requests]
        held = {key: self._metadata.peek(key).value for key in keys}
        return self._reconciled(context, held)

    @staticmethod
    def _reconciled(context: GitContext, held: Mapping[str, Any]) -> GitContext | None:
        """``context`` with each pull request replaced by its row in ``held``, by cache
        key; None when no row differs."""
        moved = False
        pulls = []
        for pull in context.pull_requests:
            row = held.get(_pull_cache_key(pull.repository, pull.number))
            fresh = _pull_request(pull.repository, row) if isinstance(row, dict) else None
            if fresh is not None and fresh != pull:
                moved = True
                pulls.append(fresh)
            else:
                pulls.append(pull)
        if not moved:
            return None
        return replace(context, pull_requests=tuple(pulls))

    async def refresh_pulls(self, refs: Iterable[tuple[str, int]], *, limit: int) -> int:
        """Re-read at most ``limit`` pull requests' rows from GitHub; how many were read.

        The Work overview's bounded backfill for a row cached before it carried the pull
        request's size (#2236): the same fetch, shared cache and lease as a pill's.
        """
        wanted = list(refs)[: max(limit, 0)]
        if not wanted or self._gh is None:
            return 0
        found = await asyncio.gather(
            *(self._pull_for_ref(repository, number, force=True) for repository, number in wanted)
        )
        return sum(1 for pull, _ in found if pull is not None)

    async def refresh_merge_checks(self, refs: Iterable[tuple[str, int]], *, limit: int) -> int:
        """Read at most ``limit`` armed pull requests' required-check rows (#2275); how many
        rows are now held.

        Not forced: the shared cache's open-row TTL is the bound, so an armed pull request
        costs one GraphQL request per TTL however often the Work overview asks.
        """
        wanted = list(refs)[: max(limit, 0)]
        if not wanted or self._gh is None:
            return 0
        found = await asyncio.gather(
            *(self._merge_checks_for(repository, number) for repository, number in wanted)
        )
        return sum(1 for row in found if row is not None)

    async def _merge_checks_for(self, repository: str, number: int) -> dict | None:
        owner, _, name = repository.partition("/")
        if not owner or not name:
            return None

        async def fetch() -> GitHubMetadata | None:
            assert self._gh is not None
            code, output = await self._run(
                self._gh,
                "api",
                "graphql",
                "-f",
                f"query={merge_checks.QUERY}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"number={number}",
                owner=owner,
            )
            # Read even when gh failed: GitHub answers the rest when it refuses one field, and an
            # App installation is always refused ``branchProtectionRule`` (FORBIDDEN, #3199).
            # ``parse`` reads a refused field's null as unknown, never as an answer.
            try:
                value = json.loads(output) if output.strip() else None
            except (TypeError, ValueError):
                value = None
            row = merge_checks.parse(value, checked_at=datetime.now(UTC))
            if row is None:
                log.debug("Merge checks unavailable for %s#%s", repository, number)
                return None
            return GitHubMetadata(row, terminal=False)

        lookup = await self._metadata.resolve(merge_checks.cache_key(repository, number), fetch)
        return lookup.value if isinstance(lookup.value, dict) else None

    async def _pull_for_ref(
        self,
        repository: str,
        number: int,
        *,
        force: bool = False,
        fresh_after: float | None = None,
        cached_first: bool = False,
    ) -> tuple[PullRequestContext | None, bool]:
        # A chat silent for a day will not push to or merge anything again, and
        # the triage rotation reaches every resting chat on the deck (#791) — so
        # refreshing its pull requests spends the shared GitHub budget on
        # movement that cannot happen. Any answer already held is the answer. A
        # chat that has never been read still gets its one fetch, so a resting
        # card is never permanently unknown, and a manual refresh (``force``)
        # still reads live.
        cache_key = _pull_cache_key(repository, number)
        if cached_first:
            held = self._metadata.peek(cache_key).value
            pull = _pull_request(repository, held) if isinstance(held, dict) else None
            if pull is not None:
                return (pull, True)

        async def fetch() -> GitHubMetadata | None:
            assert self._gh is not None
            code, output = await self._run(
                self._gh,
                "pr",
                "view",
                str(number),
                "--repo",
                repository,
                "--json",
                _PR_FIELDS,
            )
            try:
                value = json.loads(output) if code == 0 else None
                pull = _pull_request(repository, value) if isinstance(value, dict) else None
                if pull is None:
                    raise ValueError("GitHub PR was unavailable")
            except (TypeError, ValueError):
                log.debug(
                    "Deckhand PR lookup unavailable for %s#%s",
                    repository,
                    number,
                )
                return None
            return GitHubMetadata(value, terminal=pull.is_terminal)

        lookup = await self._metadata.resolve(
            cache_key,
            fetch,
            force=force,
            fresh_after=fresh_after,
        )
        value = lookup.value
        pull = _pull_request(repository, value) if isinstance(value, dict) else None
        return (pull, lookup.complete and pull is not None)
