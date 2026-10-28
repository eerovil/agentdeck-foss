"""Worker dispatch prompts → readable session titles.

A spawned worker's first user prompt is a fixed dispatch string, and it is the
same string for every card, so agentdeck would otherwise show a wall of cards all
titled with the same boilerplate — the one useful bit, the issue reference, buried
in it and the tail truncated (#310). Three dispatch shapes exist on this host::

    Run the kanban-worker skill for example-org/shop#2728.
    Run the kanban-worker-ledger skill for example-org/ledger#244 in REVIEW mode: ...
    You are working eerovil/agentdeck#301 for the AgentDeck poller.
    ## Assignment\n- Repo: eerovil/agentdeck\n- Issue: #290 — Requested changes

We parse the reference out of any of them and resolve the real GitHub issue/PR
title via ``gh api`` (disk-cached — titles change rarely), so the card reads e.g.
``store#2728 · Fix intro text duplication`` instead. Titles a session writes for
itself still win; this is what a card falls back to, which is all a Codex worker
ever has — nothing writes an ``aiTitle`` for it.

The reference may arrive without an owner, because the poller engine names cards
``repo#number``. That is still worth showing (``agentdeck#301`` beats the
boilerplate), it just cannot be resolved against GitHub or linked, so an ownerless
ref is never handed to the resolver.

Resolution is best-effort: with no ``gh`` on PATH, no auth, or offline, we fall
back to the bare ``repo#number`` reference and never raise.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from agentdeck import gh_cli
from agentdeck.github_cache import GitHubMetadata, GitHubMetadataCache

log = logging.getLogger(__name__)

_NAME = r"[A-Za-z0-9_.-]+"
# Every shape a dispatch prompt takes on this host, most specific first. The owner
# group is optional in the poller form: the engine identifies a card as `repo#n`,
# and the adapter qualifies it only when it can read the checkout's remote.
# Each one opens a line: a prompt that only quotes a dispatch mid-line — /dream lists past
# chats as "title: You are working eerovil/agentdeck#677 …" — is not that worker, and
# reading it as one made a Work of a pull request the chat never worked on (#2960).
_LINE_START = r"(?m)^[ \t]*"
_REF_PATTERNS = (
    # "Run the kanban-worker skill for example-org/shop#2728."
    # "Run the kanban-worker-ledger skill for example-org/ledger#244 in REVIEW mode: ..."
    # A project's own variant of the skill is named `kanban-worker-<project>`.
    re.compile(
        rf"{_LINE_START}Run the kanban-worker(?:-[a-z0-9-]+)? skill for ({_NAME})/({_NAME})#(\d+)"
    ),
    # "You are working eerovil/agentdeck#301 for the AgentDeck poller." — and the
    # ownerless "You are working agentdeck#301 …" the engine's own ref produces.
    re.compile(rf"{_LINE_START}You are working (?:({_NAME})/)?({_NAME})#(\d+)\b"),
    # The assignment block a Codex kanban worker is given:
    #   ## Assignment
    #   - Repo: eerovil/agentdeck
    #   - Issue: #290 — Requested changes
    re.compile(rf"{_LINE_START}-?[ \t]*Repo:\s*({_NAME})/({_NAME})\s*\n[^\n]*?Issue:\s*#(\d+)"),
)

# Short mode tags worth surfacing — they distinguish otherwise-identical cards
# (the same issue re-dispatched for review, a merge fix, or a resume).
_MODE_RE = re.compile(r"\b(REVIEW|MERGE-FIX|MERGE-ARM|RESUMING)\b")
_MODE_LABELS = {
    "REVIEW": "review",
    "MERGE-FIX": "merge-fix",
    "MERGE-ARM": "merge-arm",
    "RESUMING": "resume",
}


def _mode_label(prompt: str) -> str | None:
    """Find the first mode tag without walking the whole prompt in regex code.

    Dispatch prompts can put the tag anywhere, so bounding the search would
    change which cards get a suffix. ``str.find`` jumps to the few literal
    candidates cheaply; the existing regex still decides word boundaries. An
    invalid first candidate falls back to one regex pass over the remaining
    suffix instead of turning repeated near-matches into a Python loop.
    """
    position = min(
        (
            position
            for token in _MODE_LABELS
            if (position := prompt.find(token)) >= 0
        ),
        default=-1,
    )
    if position < 0:
        return None
    mode_match = _MODE_RE.match(prompt, position)
    if mode_match is None:
        mode_match = _MODE_RE.search(prompt, position + 1)
    return _MODE_LABELS[mode_match.group(1)] if mode_match is not None else None


@dataclass(frozen=True)
class KanbanRef:
    repo: str
    number: int
    mode: str | None = None
    # None when the dispatch prompt named the card without its owner. Such a ref
    # still titles a card; it cannot be resolved or linked (see module docstring).
    owner: str | None = None

    @property
    def resolvable(self) -> bool:
        return bool(self.owner)

    @property
    def key(self) -> str:
        """Cache key — mode-independent (one issue, one title)."""
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def short(self) -> str:
        return f"{self.repo}#{self.number}"


@lru_cache(maxsize=2048)
def parse_ref(prompt: str | None) -> KanbanRef | None:
    """Extract the worker's issue/PR reference from its dispatch prompt, or None."""
    if not prompt:
        return None
    for pattern in _REF_PATTERNS:
        match = pattern.search(prompt)
        if match is None:
            continue
        owner, repo, number = match.group(1), match.group(2), match.group(3)
        mode = _mode_label(prompt)
        return KanbanRef(repo=repo, number=int(number), mode=mode, owner=owner or None)
    return None


def issue_url(ref: KanbanRef) -> str | None:
    """GitHub URL for the referenced issue/PR, or None when the owner is unknown.

    The ``/issues/`` form redirects to ``/pull/`` when the number is a PR, so it
    works for both."""
    if not ref.owner:
        return None
    return f"https://github.com/{ref.owner}/{ref.repo}/issues/{ref.number}"


_ISSUE_URL_RE = re.compile(
    rf"^https://github\.com/({_NAME})/({_NAME})/(?:issues|pull)/(\d+)$"
)


def card_ref(issue_url: str | None) -> str | None:
    """``owner/repo#number`` (lowercased) for a session's issue URL, or None.

    The engine names a card ``repo#number`` and the deck holds the URL, so this is the one
    spelling both sides can be compared in.
    """
    match = _ISSUE_URL_RE.match((issue_url or "").strip())
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}#{match.group(3)}".lower()


def format_title(ref: KanbanRef, issue_title: str | None) -> str:
    """Card title from a ref and its (maybe-unresolved) issue title."""
    base = f"{ref.short} · {issue_title}" if issue_title else ref.short
    if ref.mode:
        base = f"{base} ({ref.mode})"
    return base


# One jq expression pulls the title and everything needed to derive the state
# badge, so a single gh call per ref covers both. ``comments`` is the comment count a
# hidden Work is brought back by (#2238), and ``labels`` say whether a Work is paused
# (#2548). ``updatedAt`` is not read here: it
# is carried so the webhook write-through in `webhook_status_cache` can compare a
# delivery against a polled row and lose when it is the older snapshot (#1681).
_ISSUE_JQ = (
    "{title, state, state_reason, updatedAt: .updated_at, closedAt: .closed_at, "
    "comments, is_pr: (.pull_request != null), "
    "merged: (.pull_request.merged_at != null), labels: [.labels[]?.name], "
    # Its own sub-issues' counts (#3622, ``work_tree``), and its GitHub parent issue only when
    # the read names one: GitHub's issue read leaves ``parent_issue_url`` out, so the link
    # comes from a ``sub_issues`` delivery and a read must not erase it (#3654).
    "subIssues: (.sub_issues_summary // null), "
    # Its first assignee (#3731): which of two decks on the repository works it.
    "assignee: (.assignee.login // null)}"
    ' + (if has("parent_issue_url") then {parent: .parent_issue_url} else {} end)'
)


def status_label(rec: dict | None) -> tuple[str, str] | None:
    """(text, kind) badge for a resolved issue/PR record, or None when unknown.

    ``kind`` is a CSS modifier: open (active), merged, done (issue closed as
    completed), dropped (closed as not-planned), closed (PR closed unmerged)."""
    state = (rec or {}).get("state")
    if not state:
        return None
    if state == "open":
        return ("open", "open")
    if rec.get("is_pr"):
        return ("merged", "merged") if rec.get("merged") else ("closed", "closed")
    if rec.get("state_reason") == "not_planned":
        return ("closed", "dropped")
    return ("closed", "done")


class KanbanTitleCache:
    """Shared ``owner/repo#n`` → issue title + GitHub state resolver.

    The host-level metadata cache survives restarts and coordinates production
    with staging. Titles rarely change, but state does, so open items use the
    shared five-minute TTL while closed/merged items remain long-lived.
    """

    OPEN_TTL_S = GitHubMetadataCache.OPEN_TTL_S
    TERMINAL_TTL_S = GitHubMetadataCache.TERMINAL_TTL_S
    NEG_TTL_S = GitHubMetadataCache.NEGATIVE_TTL_S
    FAILURE_BACKOFF_S = GitHubMetadataCache.FAILURE_BACKOFF_S
    # Per-scan bound on new gh calls so a cold cache can't stall one scan.
    MAX_FETCH_PER_SWEEP = 8
    FETCH_TIMEOUT_S = 6.0

    def __init__(self, path: Path | None = None) -> None:
        self._metadata = GitHubMetadataCache(path)
        self._gh = shutil.which("gh")

    @staticmethod
    def _cache_key(ref: KanbanRef) -> str:
        return f"issue:v1:{ref.key.lower()}"

    def _stored(self, ref: KanbanRef, rec: dict) -> GitHubMetadata:
        """The read's record, keeping the held row's ``parent`` when the read has none.

        GitHub's issue read never carries ``parent_issue_url``; the link is on the row from
        a ``sub_issues`` delivery (``webhook_status_cache``), so a refresh keeps it (#3654)."""
        if "parent" not in rec:
            held = self._metadata.peek(self._cache_key(ref)).value
            if isinstance(held, dict) and "parent" in held:
                rec = {**rec, "parent": held["parent"]}
        return GitHubMetadata(rec, terminal=rec.get("state") == "closed")

    def _record(self, ref: KanbanRef) -> dict | None:
        if not ref.resolvable:
            return None
        value = self._metadata.peek(self._cache_key(ref)).value
        return value if isinstance(value, dict) else None

    def get(self, ref: KanbanRef) -> str | None:
        """Cached issue title if known, else None (unresolved or a cached miss)."""
        rec = self._record(ref)
        return (rec or {}).get("title") or None

    def get_status(self, ref: KanbanRef) -> tuple[str, str] | None:
        """(text, kind) GitHub-state badge for the ref, or None if unresolved."""
        return status_label(self._record(ref))

    async def resolve_missing(self, refs: list[KanbanRef], now: float) -> bool:
        """Fetch title + state for up to ``MAX_FETCH_PER_SWEEP`` stale/unknown
        refs.

        Returns True if the cache changed (so the caller can re-read it). A
        no-op — returning False — when ``gh`` is absent or nothing is stale.
        """
        if self._gh is None:
            return False
        unique = {self._cache_key(ref): ref for ref in refs if ref.resolvable}
        if not unique:
            return False

        attempted = False

        def request(ref: KanbanRef):
            async def fetch() -> GitHubMetadata | None:
                nonlocal attempted
                attempted = True
                _ref, rec = await self._fetch(ref)
                if rec is None:
                    return None
                return self._stored(ref, rec)

            return fetch

        results = await self._metadata.resolve_many(
            [(key, request(ref)) for key, ref in unique.items()],
            now=now,
            max_refreshes=self.MAX_FETCH_PER_SWEEP,
        )
        return attempted or any(result.refreshed for result in results.values())

    async def refresh(self, refs: list[KanbanRef], now: float, *, limit: int) -> int:
        """Re-read up to ``limit`` refs' records now, however fresh they are (#2226).

        For a record the deck knows is missing a field it needs — a closed issue cached
        before ``closedAt`` was read — which no TTL would ever refresh, because a closed
        record is terminal. Goes through the same shared cache and lease as
        ``resolve_missing``, so a key another process is already reading is not read
        twice. Returns how many were actually refreshed.
        """
        if self._gh is None or limit <= 0:
            return 0
        unique = {self._cache_key(ref): ref for ref in refs if ref.resolvable}
        if not unique:
            return 0

        def request(ref: KanbanRef):
            async def fetch() -> GitHubMetadata | None:
                _ref, rec = await self._fetch(ref)
                if rec is None:
                    return None
                return self._stored(ref, rec)

            return fetch

        results = await self._metadata.resolve_many(
            [(key, request(ref)) for key, ref in list(unique.items())[:limit]],
            force=True,
            now=now,
            max_refreshes=limit,
        )
        return sum(1 for result in results.values() if result.refreshed)

    async def _fetch(self, ref: KanbanRef) -> tuple[KanbanRef, dict | None]:
        assert self._gh is not None
        try:
            # Through `gh_cli`, which reads as the repository owner's non-human
            # authority (#2289). An owner with none is refused, and the card keeps its
            # bare `repo#n` name rather than a title read as somebody else (#544).
            code, out = await gh_cli.run(
                [
                    self._gh,
                    "api",
                    f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}",
                    "--jq",
                    _ISSUE_JQ,
                ],
                timeout=self.FETCH_TIMEOUT_S,
            )
        except (OSError, TimeoutError) as exc:
            log.debug("gh issue fetch failed for %s: %s", ref.key, exc)
            return (ref, None)
        if code != 0:
            return (ref, None)
        try:
            rec = json.loads(out.decode("utf-8", "replace"))
        except ValueError:
            return (ref, None)
        if not isinstance(rec, dict) or not rec.get("title"):
            return (ref, None)
        rec["title"] = str(rec["title"]).strip()
        return (ref, rec)
