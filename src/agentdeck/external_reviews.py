"""What a project's outside reviewer said about each pull request (#1820, #3623).

Read by the Work page and the release in every edition; written by the ChatGPT nudges,
which the public copy leaves out. So the record lives here and ``poller_nudge`` re-exports
it, and the public stand-in reads none.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ExternalReviews:
    """One instance's external reviewer (``review_program``, #1820), for every pull request.

    Read from the state the extension owns and persists (#1876, #1910), never recomputed:
    ``awaiting`` is a review asked for whose chat has not settled, keyed by ``owner/repo#pr``
    casefolded, with the chat as ``owner/repo#n`` ("" when the entry does not name it);
    ``given_up`` is a pull request whose misses the extension itself settled as given up.
    ``state_file`` is the file this was read from, so a reader can notice it change (#2216).

    The rest say why the reviewer will *not* ask about a pull request (#2226), each keyed the
    same way with the head it is about: ``seen`` heads it already reviewed, ``stalled`` ones
    it stopped bringing forward (with its reason), ``suppressed`` ones whose nudges stopped
    reporting back (``missing_receipts`` at the cap the extension publishes), and
    ``given_up_heads`` the head each give-up is about. ``repos`` is the repositories it
    watches, casefolded; None when its settings do not say. ``clean`` is the head each
    pull request's review closed clean about (#2254), read by ``clean_at`` and never by
    ``judge``; ``changes`` the head each review sent back for changes, with when (#2332), read
    by ``changes_at``.
    """

    state_file: Path | None
    awaiting: Mapping[str, str]
    given_up: frozenset[str]
    repos: frozenset[str] | None = None
    seen: Mapping[str, str] = field(default_factory=dict)
    stalled: Mapping[str, tuple[str, str]] = field(default_factory=dict)  # key: (head, why)
    suppressed: Mapping[str, str] = field(default_factory=dict)  # key: head
    given_up_heads: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    clean: Mapping[str, str] = field(default_factory=dict)  # key: head
    changes: Mapping[str, tuple[str, float]] = field(default_factory=dict)  # key: (head, at)

    def _recorded_at(self, repository: str, number: int, head: str, sha: str) -> bool:
        head, sha = head.strip().lower(), sha.strip().lower()
        if len(head) < 7 or len(sha) < 7:
            return False
        if self.repos is not None and repository.casefold() not in self.repos:
            return False
        return head.startswith(sha)

    def changes_at(self, repository: str, number: int, head: str) -> float | None:
        """When the reviewer sent exactly ``head`` back for changes (epoch, 0.0 unknown), or
        None when it did not (#2332). The same exact-head rule as ``clean_at``."""
        sha, at = self.changes.get(f"{repository}#{number}".casefold(), ("", 0.0))
        return at if self._recorded_at(repository, number, head, sha) else None

    def clean_at(self, repository: str, number: int, head: str) -> bool:
        """The reviewer recorded a clean review of exactly ``head`` (#2254).

        Only what the extension recorded as a clean outcome counts, never an old ``seen``:
        ``head`` has to be known (7+ characters), the repository watched, and the recorded
        head a prefix of it. A clean review of the exact head outranks a stall, a stopped
        receipt or a give-up about it, the same rule as ``judge`` (#2229).
        """
        sha = self.clean.get(f"{repository}#{number}".casefold(), "")
        return self._recorded_at(repository, number, head, sha)

    def judge(self, repository: str, number: int, head: str = "") -> tuple[str, str]:
        """``(verdict, detail)`` for one pull request, from the extension's own state.

        ``head`` is the pull request's current commit when the deck's cache knows it. The
        extension forgets an entry once the head it is about moves, so an entry about any
        other head is stale and says nothing. Verdicts: ``awaiting`` (detail: its chat),
        ``unwatched``, ``stalled`` (its reason), ``suppressed``, ``given_up`` (why),
        ``reviewed`` (the head), else ``configured``: the reviewer will ask about it.

        A review of exactly the current head is evidence the review ran, so it outranks what
        the extension recorded about getting there: a give-up or a stopped receipt about that
        head was a transport verdict the review itself has answered (#2229). Only a head the
        deck's cache knows can be exact; with none, the recorded order stands.
        """
        key = f"{repository}#{number}".casefold()

        def current(sha: str) -> bool:
            return not head or not sha or head.lower().startswith(sha.lower())

        if key in self.awaiting:
            return "awaiting", self.awaiting[key]
        if self.repos is not None and repository.casefold() not in self.repos:
            return "unwatched", ""
        if key in self.stalled and current(self.stalled[key][0]):
            return "stalled", self.stalled[key][1]
        reviewed = self.seen.get(key, "")
        if head and len(reviewed) >= 7 and head.lower().startswith(reviewed.lower()):
            return "reviewed", reviewed
        if key in self.suppressed and current(self.suppressed[key]):
            return "suppressed", ""
        if key in self.given_up and current(self.given_up_heads.get(key, ("", ""))[0]):
            return "given_up", self.given_up_heads.get(key, ("", ""))[1]
        if key in self.seen and current(self.seen[key]):
            return "reviewed", self.seen[key]
        return "configured", ""
