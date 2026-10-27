"""A read-only Work projection above chats and sessions (#2138).

A Work is one underlying task. In this first slice the only supported source
is a GitHub issue, so a Work is identified by the issue's (repository, number)
under the case-insensitive ``IssueRef.identity`` rules. Nothing here is stored:
the projection is rebuilt from what the deck already holds. The durable
``work_id`` (#2140) is derived from the source alone, so it needs no lookup.

Work and chat are many-to-many. One chat can work on several issues and one
issue can have several independent chats, so neither side owns the other.
Membership is keyed by chat, not by session leg, so a chat that moved provider
or account is still one member.

Only explicit evidence joins a chat to a Work:

* an issue registered for any leg of the chat (``session_issues``);
* a leg's provider-inferred ``Session.issue_url`` that parses as a full ref;
* a leg's participation in a Run of the Work (``session_run_bindings``);
* a leg's saved delegated Work binding (``session_work_bindings``, #2207);
* a side chat started from the Work's page (``session_work_bindings``, #2388). It is a
  member with no Run, so every Run reader leaves it alone.

cwd, worktree, branch, pull request, title, account, timing and transcript
prose are never read, and neither is the parent chain. Delegation inherits
Work context once, when the child is created (or by the one-time backfill of
children older than that rule), and the result is saved on the child: its
parent's current Run, else its parent's single Work, else — when the parent
had several — an ambiguous row naming the candidates, which joins nothing.
A provider-internal subagent is not a member in its own right; it stays nested
under its parent chat.

The builder is pure. ``AppState.work_projection`` gathers the facts from one
snapshot of the store and hands them here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property

from .models import IssueRef


class WorkEvidence(StrEnum):
    """Why a chat belongs to a Work."""

    REGISTERED_ISSUE = "registered_issue"  # durable session_issues row
    ISSUE_URL = "issue_url"  # provider-inferred kanban issue on a leg
    RUN_BINDING = "run_binding"  # the leg took part in a Run of the Work
    DELEGATED = "delegated"  # saved at creation from a parent with exactly one Work
    BACKFILLED = "backfilled"  # saved once from the legacy parent rule (#2207)
    SIDE_CHAT = "side_chat"  # started from the Work's page, never part of a Run (#2388)


# A saved delegation outcome that names candidate Works but joins none (#2207).
AMBIGUOUS = "ambiguous"


GITHUB_ISSUE = "github_issue"


def work_id_for(ref: IssueRef) -> str:
    """The canonical ``work_id`` of a GitHub-issue-backed Work (#2140).

    ``github_issue:<owner/repo>#<number>`` under ``IssueRef.identity`` rules,
    so every spelling of one issue gives one id. It is derived from the issue
    alone: no database, no network, nothing a provider, account, chat or Run
    change can move. A repository rename is a source migration, not handled here.
    """
    return WorkRef.from_issue(ref).work_id


@dataclass(frozen=True, order=True)
class WorkRef:
    """A Work's identity: a GitHub issue, repository compared case-insensitively."""

    repository: str  # lower-cased "owner/repo"
    number: int

    def __post_init__(self) -> None:
        # Lower-cased here, not by each caller: a New work press built its key from the
        # name as typed and started a second agent beside the router's (#3629).
        object.__setattr__(self, "repository", self.repository.lower())

    @classmethod
    def from_issue(cls, ref: IssueRef) -> WorkRef:
        repository, number = ref.identity
        return cls(repository, number)

    @classmethod
    def from_work_id(cls, work_id: str) -> WorkRef | None:
        """The inverse of ``work_id``; None for any other source or a malformed id."""
        kind, _, key = (work_id or "").partition(":")
        repository, _, number = key.rpartition("#")
        if kind != GITHUB_ISSUE or not number.isdigit():
            return None
        ref = IssueRef.parse(f"{repository}#{number}")
        return cls.from_issue(ref) if ref is not None else None

    @property
    def source_kind(self) -> str:
        return GITHUB_ISSUE

    @property
    def source_key(self) -> str:
        return f"{self.repository}#{self.number}"

    @property
    def work_id(self) -> str:
        return f"{self.source_kind}:{self.source_key}"


@dataclass(frozen=True, order=True)
class WorkProvenance:
    """One piece of evidence for a membership.

    ``session_key`` is the leg that carried the evidence; for a saved
    delegated or backfilled binding ``parent_chat_id`` names the parent chat
    the Work was taken from when it was saved.
    """

    evidence: WorkEvidence
    session_key: str = ""
    parent_chat_id: str = ""


@dataclass(frozen=True)
class WorkMembership:
    work: WorkRef
    chat_id: str
    provenance: tuple[WorkProvenance, ...]

    @property
    def direct(self) -> bool:
        inherited = (WorkEvidence.DELEGATED, WorkEvidence.BACKFILLED)
        return any(p.evidence not in inherited for p in self.provenance)


@dataclass(frozen=True)
class Work:
    ref: WorkRef
    # A spelling of the issue for display and links, chosen deterministically.
    issue: IssueRef
    memberships: tuple[WorkMembership, ...]

    @property
    def work_id(self) -> str:
        return self.ref.work_id

    @property
    def chat_ids(self) -> tuple[str, ...]:
        return tuple(m.chat_id for m in self.memberships)


@dataclass(frozen=True)
class WorkBinding:
    """A saved delegated Work outcome for one leg (#2207).

    ``evidence`` is ``delegated``, ``backfilled`` or ``side_chat`` for a membership, or
    ``ambiguous`` for one candidate of a parent that was in several Works.
    """

    session_key: str
    work_id: str
    evidence: str
    parent_chat_id: str = ""


@dataclass(frozen=True)
class WorkChatFacts:
    """What the store already knows about one leg of a chat, gathered once per pass.

    ``chat_id`` is the chat identity (the session key when there is no durable
    chat identity); entries sharing it are folded into one chat.
    """

    chat_id: str
    registered: tuple[tuple[str, IssueRef], ...] = ()  # (leg key, ref)
    issue_urls: tuple[tuple[str, str], ...] = ()  # (leg key, raw issue_url)
    runs: tuple[tuple[str, str], ...] = ()  # (leg key, work_id) from Run bindings
    bindings: tuple[WorkBinding, ...] = ()


@dataclass(frozen=True)
class AmbiguousDelegation:
    """A delegated chat whose parent was in several Works; it joined none of them."""

    chat_id: str
    candidates: tuple[WorkRef, ...]
    parent_chat_ids: tuple[str, ...]


@dataclass(frozen=True)
class WorkProjection:
    works: tuple[Work, ...]
    ambiguous: tuple[AmbiguousDelegation, ...] = ()

    def get(self, ref: WorkRef | IssueRef) -> Work | None:
        if isinstance(ref, IssueRef):
            ref = WorkRef.from_issue(ref)
        return next((work for work in self.works if work.ref == ref), None)

    def works_for_chat(self, chat_id: str) -> tuple[WorkRef, ...]:
        return self._by_chat.get(chat_id, ())

    def works_chat_joined(self, chat_id: str) -> tuple[WorkRef, ...]:
        """The Works this chat joined by its own saved act, never by a guess or a parent.

        A registered issue, a chat started from the Work's page, or a Run: each is something
        the chat or the deck recorded about this chat itself. A provider-read issue URL and a
        Work inherited from a parent are left out (#2552).
        """
        own = (WorkEvidence.REGISTERED_ISSUE, WorkEvidence.SIDE_CHAT, WorkEvidence.RUN_BINDING)
        return tuple(
            work.ref
            for work in self.works
            for membership in work.memberships
            if membership.chat_id == chat_id
            and any(item.evidence in own for item in membership.provenance)
        )

    @cached_property
    def _by_chat(self) -> dict[str, tuple[WorkRef, ...]]:
        # Built once per projection: the backfill and the Work pages ask per chat.
        index: dict[str, list[WorkRef]] = {}
        for work in self.works:
            for membership in work.memberships:
                index.setdefault(membership.chat_id, []).append(work.ref)
        return {chat_id: tuple(refs) for chat_id, refs in index.items()}


def build_work_projection(chats: Iterable[WorkChatFacts]) -> WorkProjection:
    """Group chats into issue-backed Works from saved, explicit evidence only."""
    issues: dict[WorkRef, IssueRef] = {}
    found: dict[str, dict[WorkRef, set[WorkProvenance]]] = {}
    ambiguous: dict[str, tuple[set[WorkRef], set[str]]] = {}
    for facts in chats:
        own = found.setdefault(facts.chat_id, {})
        evidence = [(key, ref, WorkEvidence.REGISTERED_ISSUE, "") for key, ref in facts.registered]
        for key, raw in facts.issue_urls:
            ref = IssueRef.parse(raw or "")
            if ref is not None:
                evidence.append((key, ref, WorkEvidence.ISSUE_URL, ""))
        saved = [(key, work_id, WorkEvidence.RUN_BINDING, "") for key, work_id in facts.runs]
        for binding in facts.bindings:
            if binding.evidence == AMBIGUOUS:
                work = WorkRef.from_work_id(binding.work_id)
                if work is not None:
                    works, parents = ambiguous.setdefault(facts.chat_id, (set(), set()))
                    works.add(work)
                    parents.update(p for p in binding.parent_chat_id.split(",") if p)
            elif binding.evidence in (
                WorkEvidence.DELEGATED,
                WorkEvidence.BACKFILLED,
                WorkEvidence.SIDE_CHAT,
            ):
                saved.append(
                    (
                        binding.session_key,
                        binding.work_id,
                        WorkEvidence(binding.evidence),
                        binding.parent_chat_id,
                    )
                )
        for key, work_id, kind, parent in saved:
            work = WorkRef.from_work_id(work_id)
            if work is not None:
                ref = IssueRef(work.repository, work.number)
                evidence.append((key, ref, kind, parent))
        for key, ref, kind, parent in evidence:
            work = WorkRef.from_issue(ref)
            issues[work] = min(issues.get(work, ref), ref)
            own.setdefault(work, set()).add(WorkProvenance(kind, key, parent))

    members: dict[WorkRef, list[WorkMembership]] = {}
    for chat_id in sorted(found):
        for work, provenance in found[chat_id].items():
            members.setdefault(work, []).append(
                WorkMembership(work, chat_id, tuple(sorted(provenance)))
            )
    return WorkProjection(
        tuple(Work(work, issues[work], tuple(members[work])) for work in sorted(members)),
        tuple(
            AmbiguousDelegation(chat_id, tuple(sorted(works)), tuple(sorted(parents)))
            for chat_id, (works, parents) in sorted(ambiguous.items())
        ),
    )
