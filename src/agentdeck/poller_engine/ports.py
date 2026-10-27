"""The engine's I/O boundary: one port in, declared effects out (#176 slice 9).

The engine is deterministic and stdlib-only, so it performs no I/O itself. Everything
it needs to know arrives through a single ``EnginePort`` the host implements, and
everything it wants to change leaves as a declared ``Effect`` applied through that
same port. Two consequences make the engine testable and safe:

* **One seam, three consumers.** The app-side adapter (real GitHub + the async
  worker client), the parity/conformance harness, and the canary all implement this
  one port. Nothing else has to be faked.
* **Writes are declarative and reported.** ``apply`` returns whether the change
  actually landed, so a refused label edit or a failed status write becomes retryable
  durable state rather than an assumption the engine records as done.

Worker delivery stays a *directive*, not a port write: the engine records the send
intent durably, then the host performs the real delivery and writes the ack receipt
(see ``dispatch.py``). That asymmetry is deliberate — it is what makes delivery
at-most-once across a crash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from .ownership import LabelEvent, LabelRemoval
from .reconcile import CardSnapshot
from .schedules import TaskIssue


class EffectKind(Enum):
    SET_STATUS = "set-status"                # publish a board status by role name
    ADD_OWNERSHIP = "add-ownership"          # add the ownership label (no emitter since #2053)
    REMOVE_WORKTREES = "remove-worktrees"    # release host worktrees for a card
    COMMENT = "comment"                      # post an engine comment (bot-marked)
    REACT = "react"                          # set a command comment's reaction
    UPDATE_BRANCH = "update-branch"          # bring a PR branch up to date with its base
    CLOSE_ISSUE = "close-issue"              # close a finished card's issue
    REOPEN_ISSUE = "reopen-issue"            # reopen a card that must be visibly blocked
    SET_TITLE = "set-title"                  # replace a template placeholder title (#338)
    DISCARD_RECEIPTS = "discard-receipts"    # drop acknowledgement receipts (see below)
    SET_BODY = "set-body"                    # rewrite an engine-owned issue's body (#2517)
    PIN_ISSUE = "pin-issue"                  # pin an engine-owned issue in its repository (#2517)


class ArmOutcome(Enum):
    """What happened when native auto-merge was requested for one PR."""

    ARMED = "armed"                    # armed, or already mergeable — observe from here
    ALREADY_MERGED = "already-merged"  # merged in the meantime; nothing to arm
    UNAVAILABLE = "unavailable"        # the repository does not allow auto-merge
    FAILED = "failed"                  # the request was refused for another reason


@dataclass(frozen=True)
class Effect:
    """One outward change the engine wants made, applied through the port.

    ``DISCARD_RECEIPTS`` carries ``{"generation": ..., "dispatch_id": ...}`` to drop one
    specific acknowledgement, or ``None`` to drop every receipt the card has. A receipt is
    a host-side artifact keyed by card and lease, so an abandoned lease's receipt must be
    discarded — otherwise a later lease, or a later card at the same number, could read a
    stale acknowledgement as its own.
    """

    kind: EffectKind
    repo: str
    number: int
    value: object = None


@dataclass(frozen=True)
class CommentBody:
    """Comment prose plus navigation resolved from the current locked card record."""

    body: str
    session_url: str = ""
    pull_request_url: str = ""
    pull_request_ref: str = ""

    def __str__(self) -> str:
        return self.body

    @classmethod
    def from_record(
        cls,
        body: str,
        rec: dict | None,
        *,
        include_session: bool = True,
        pull_request_ref: str = "",
    ) -> CommentBody:
        record = rec if isinstance(rec, dict) else {}
        links = record.get("comment_links")
        links = links if isinstance(links, dict) else {}
        session_url = (
            str(links.get("session_url") or "")
            if include_session
            and links.get("session_generation") == record.get("generation")
            else ""
        )
        return cls(
            body=str(body),
            session_url=session_url,
            # The pull request belongs to the card, not one worker generation. A revive keeps
            # working the same PR, so its link remains useful while the new session is unknown.
            pull_request_url=str(links.get("pull_request_url") or ""),
            pull_request_ref=str(pull_request_ref),
        )


@dataclass(frozen=True)
class WorktreeCleanup:
    """Per-path result for a destructive host cleanup effect.

    A boolean can only say that *something* failed.  Keeping the exact failed paths lets
    the engine forget successful removals while retrying the remainder.

    ``retained`` is the other outcome, and it is not a failure: each pair is a path the host
    deliberately did **not** remove, with the reason a person should read (#1718).  A worktree
    still holding uncommitted work is somebody's unfinished progress, and no release is worth
    deleting it; one no Work claims is not the card's to delete at all (#2195).  Keeping
    that separate from ``failed`` matters, because a path reported pending is retried on
    every wake for as long as the card exists (#1189) — while a retained one is given back
    to the operator reaper, which is where an unclaimed directory belongs.
    """

    failed: tuple[str, ...] = ()
    retained: tuple[tuple[str, str], ...] = ()

    @property
    def landed(self) -> bool:
        return not self.failed


@dataclass(frozen=True)
class WorktreeChanges:
    """What one registered worktree has changed, and whose files those are (#1736).

    ``checkout`` is the shared checkout the paths are relative to, because a path on its own
    is only half of an identity: two cards working in two different repositories both change
    ``README.md`` and collide over nothing.

    The committed half is reported **per commit** rather than as one flat set, because a
    branch's commits are not all its own work (#2065). A worker that cut its branch from a
    master newer than the clone's last fetch carries somebody else's already-merged commits,
    and every other card cut from the same base carries exactly the same ones — so measuring
    the branch as a whole made two unrelated cards look like they were writing the same dozen
    files. Keeping the commit each path came from lets the overlap plan drop the history two
    cards hold in common and compare only what each of them actually wrote.

    ``local`` is the modified and untracked work in front of the worker right now, which is
    always that card's own. ``paths`` is the two halves together: everything this worktree
    would carry into a pull request.
    """

    checkout: str = ""
    local: tuple[str, ...] = ()
    commits: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        """Every path this worktree has changed, committed and local together."""
        found = set(self.local)
        for _commit, paths in self.commits:
            found.update(paths)
        return tuple(sorted(found))


@dataclass(frozen=True)
class LinkedPRs:
    """The pull requests GitHub links as closing one issue.

    ``open`` maps each open PR's ``repo#number`` to its head SHA (``None`` when the SHA
    could not be read — an unreadable head must not be treated as unchanged), and
    ``merged`` lists the already-merged ones, which still gate on their deploy.

    ``withheld`` names the open PRs whose author declared them not a release target — a draft,
    or a body carrying the release opt-out marker. They are deliberately absent from ``open``,
    so nothing reviews, waits on, or merges them; they are listed only so a card can say "the
    author held this back" instead of the misleading "no pull request exists".

    ``closed_unmerged`` names the linked PRs that were closed without merging. Nothing waits on
    them either, but their absence is not the same as their never having existed: "nothing is
    open" would otherwise read as "everything shipped", and a card whose second pull request was
    closed unmerged would look exactly like a finished release (#2069).

    ``step`` names the open and merged PRs whose body marks them a *step* of the card rather
    than its completion (#2730). They are released like any other, but merging them hands the
    card back to its worker instead of finishing it.
    """

    open: dict[str, str | None] = field(default_factory=dict)
    merged: list[str] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    closed_unmerged: list[str] = field(default_factory=list)
    step: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PrState:
    """One linked PR as probed this wake; ``failed_steps`` names the CI steps that failed.

    ``check_state`` is normalized to ``green``/``pending``/``failing``/``unknown`` and
    ``merge_state`` to GitHub's mergeability vocabulary (``CLEAN``/``BEHIND``/``DIRTY``/…),
    so no CI vendor's own terms reach the engine.

    ``auto_merge`` is whether GitHub still holds a native auto-merge request for this PR.
    It defaults to True because only the merge watch reads it, and only about refs it armed
    itself: a probe that cannot say keeps waiting rather than declaring a live release dead.

    ``behind_by`` is how many commits the base branch has that this pull request's head does
    not — a fact about the branches themselves, read from the compare endpoint (#2101). It
    exists because ``merge_state`` cannot answer that question on most repositories: GitHub
    reports ``BEHIND`` only where branch protection *requires* branches to be up to date, so
    on a repository without that protection an arbitrarily stale pull request still reads
    ``CLEAN``. ``None`` means the comparison could not be read, and is never up to date.
    """

    ref: str = ""
    state: str = "OPEN"  # OPEN | MERGED | CLOSED (closed-unmerged)
    head: str | None = None
    behind_by: int | None = None
    base: str | None = None  # the base branch's name, so a rebase turn can say onto what
    check_state: str = "green"
    merge_state: str = "CLEAN"
    failed_steps: list[str] = field(default_factory=list)
    auto_merge: bool = True
    merge_commit: str | None = None
    merged_at: str | None = None  # when GitHub merged it, not when a wake noticed (#815)


@dataclass(frozen=True)
class DeployState:
    """A production deploy as the deploy extension (#179) reports it."""

    status: str = "pending"  # ok | bad | pending
    link: str | None = None


@dataclass(frozen=True)
class ScanResult:
    """A comment scan: what was found, whether it was complete, and how far it reached.

    ``complete`` is false when any repository could not be scanned; the cursor then
    holds so a permission or API failure can never skip a command.
    """

    comments: list[dict] = field(default_factory=list)
    complete: bool = True
    scanned_to: datetime | None = None


@dataclass(frozen=True)
class CardQuery:
    """One card requested from the bounded live-snapshot batch."""

    repo: str
    number: int
    generation: str = ""
    stop_generation: str = ""
    stop_requested_at: str = ""


@dataclass(frozen=True)
class SnapshotBatch:
    """Readable cards plus whether the whole batch was deferred rather than read.

    ``reason`` is what a deferred batch is called in the cycle log. It defaults to the
    shared GraphQL reserve because that was the only thing that ever deferred one; a
    webhook-quiet wake (#1673) defers batches too, and an operator has to be able to tell
    a quota problem from a deliberately cheap wake without reading the code.
    """

    cards: dict[str, CardSnapshot] = field(default_factory=dict)
    deferred: bool = False
    reason: str = "GraphQL reserve reached"


@runtime_checkable
class EnginePort(Protocol):
    """Everything the engine cannot do for itself (see module docstring)."""

    def dispatch_ok(self) -> bool:
        """Compatibility hook; model usage no longer blocks dispatch (#1497)."""

    def account_full(self, account: str) -> bool:
        """Compatibility hook; current adapters always return False (#1497)."""

    def live_queries_halt(self, *, stale_reading_ok: bool = False) -> str | None:
        """Why GitHub forbids live reads, optionally followed by ``: <reading source>``.

        ``stale_reading_ok`` says this wake is webhook-quiet, so an older budget reading may
        answer rather than a fresh probe being bought for it (#2082).
        """

    def scan_comments(self, *, since: str) -> ScanResult:
        """Fetch candidate command comments created at or after ``since``."""

    def permission(self, repo: str, login: str) -> str | None:
        """This login's collaborator level on the repository, or None if it could not be read.

        The engine decides what a level *means* (``authorization.py``): the port only reports
        what GitHub said, because an authorization rule is an engine invariant and must not
        depend on an adapter getting it right. ``None`` is uncertainty, never a denial.
        """

    def snapshot_cards(self, cards: dict[str, CardQuery]) -> SnapshotBatch:
        """Fetch due cards in bounded batches; omit unreadable cards without shrinking state."""

    def labelled_cards(self) -> dict[str, list[LabelEvent]] | None:
        """Current labelled cards and their ownership-event facts, oldest first.

        This is how work is *found*. Ownership adoption can only classify a card the engine
        already knows, and comments only introduce the cards someone commented on, so without
        this a labelled issue is invisible however long it waits (#311).

        None means the sweep could not be completed, which must not be read as "nothing is
        labelled": the caller holds the ownership ledger rather than advancing past cards it
        could not see.
        """

    def ownership_actors(self, events: dict[str, str]) -> dict[str, str]:
        """Who created each exact ownership event, keyed by card, for authorization."""

    def ownership_removal(self, repo: str, number: int) -> LabelRemoval | None:
        """The latest removal of an ownership label from this issue, or None if unreadable.

        Asked only when a card that was seen wearing the label has lost it while a person's
        instruction is still live on it: which came last decides whether the removal is a
        hand-back or the instruction is (#2417). None is uncertainty, and the engine holds the
        hand-back for a wake that can read it rather than guessing.
        """

    def linked_prs(self, repo: str, number: int) -> LinkedPRs | None:
        """The card's linked PRs, or None when discovery was incomplete this wake."""

    def resolve_prs(self, refs: list[str]) -> LinkedPRs | None:
        """Classify pull requests a person named, in the shape ``linked_prs`` returns.

        This is discovery replaced rather than filtered, because the whole point of naming a
        release target is to reach one automatic discovery got wrong or never found. A ref that
        names no pull request at all is absent from every field, so the caller can tell a typo
        from a draft; None still means "could not read this wake", never "no such PR".
        """

    def release_trees(self, refs: list[str]) -> dict[str, str | None]:
        """Prospective merge-tree identities for the named PRs; unreadable values are None."""

    def review_diffs(self, refs: list[str]) -> dict[str, str | None]:
        """Effective-diff identities for the named PRs; unreadable values are None."""

    def pr_files(self, ref: str) -> list[str] | None:
        """The paths one pull request changes, or None when they could not be read (#2729)."""

    def dispatch_acknowledged(self, repo: str, number: int, dispatch: dict) -> bool:
        """Has the host written the receipt for this exact dispatch?"""

    def dispatch_deferred(self, repo: str, number: int, dispatch: dict) -> str | None:
        """Why the host positively deferred this exact dispatch, or None.

        A deferral proves no worker received the delivery. Unlike an uncertain send, the same
        lease and delivery id may therefore be retried without spending a delivery attempt.
        """

    def turn_ended(self, repo: str, number: int, generation: str) -> dict | None:
        """How the Runtime says this lease's Run ended, or None if it proves no ending.

        The persistent Runtime can see a turn end and the engine cannot; it is asked directly,
        by the lease's generation as the Run id (#2200). What the Runtime must not do is decide
        the *outcome*: silence is not `in_review`. So this carries one fact (``observed``, the
        CLI's ``subtype``, ``refused`` and the ``account``) and the engine decides what it means
        by combining it with the marker only it can see (#279).
        """

    def waiting_on_person(self, repo: str, number: int, generation: str) -> bool | None:
        """Is this lease's Run stopped on a prompt only a person can answer? (#2887)

        The Runtime's ``waiting_human``: a question, a permission or approval request, or a
        blocked command the worker cannot get past by itself. ``None`` when the Runtime could
        not be asked, which proves neither answer.
        """

    def verification_jobs(self) -> list[dict] | None:
        """The host's job rows that may be verification evidence, or None if unreadable.

        Only the rows; which of them prove anything for which Run is ``verification``'s rule,
        never the port's (#2154).
        """

    def cancel_job(self, job_id: str) -> bool:
        """Cancel one queued or running job; True only when the queue's owner answered (#2350).

        A host-local write, so a quiet wake may make it too. False is "not done yet", never
        "done": the engine tries again on a later wake.
        """

    def dispatch_worktrees(self, repo: str, number: int, dispatch: dict) -> list[str]:
        """The host worktrees this delivery's receipt reported, or empty when it reported none.

        The engine registers what it is told and nothing else. A worktree it was never told
        about is left alone: releasing a card is not a licence to delete a directory whose path
        merely looks right.
        """

    def dispatch_session_url(self, repo: str, number: int, dispatch: dict) -> str:
        """The public link of the chat this delivery's receipt says took it, or ``""``.

        Recorded by the host from the runtime's answer (#2155), so the card's next dispatch can
        continue the same chat (#2127) without the worker having registered anything.
        """

    def worktree_dirty_counts(self, paths: list[str]) -> dict[str, int | None]:
        """Uncommitted-file count per registered worktree path; ``None`` when unreadable.

        Read-only observation for the human a parked card is handed to (#787): a silent
        worker's actual progress often sits uncommitted in its worktree, and reporting only
        the silence hides it. Only paths the engine registered are ever asked about, and an
        unreadable path degrades to ``None`` rather than blocking the park.
        """

    def worktree_changed_paths(self, paths: list[str]) -> dict[str, WorktreeChanges | None]:
        """What each registered worktree has changed, or ``None`` where it cannot be read.

        The evidence behind writer serialization (#1736). Only paths the engine registered
        are ever asked about, the same eligibility rule ``worktree_dirty_counts`` uses, and an
        unreadable worktree degrades to ``None`` for that path alone — which the overlap plan
        reads as "no proven collision" rather than as a hold, because a broken checkout must
        not be able to stop every card that shares a file with it.
        """

    def ref_states(self, refs: list[str]) -> dict[str, str]:
        """GitHub's own state for each ``repo#number``, whether it is an issue or a PR (#1230).

        ``OPEN``, ``CLOSED`` or ``MERGED`` — the vocabulary ``blockers.is_resolved`` already
        reads, so a blocker the engine is waiting on is judged by exactly the rule a declared
        dependency is. A ref that could not be read is simply **absent** from the answer
        rather than reported as open: the engine fails closed on ignorance here, and saying
        "open" would be a claim the adapter has no grounds for.

        Separate from ``pr_states`` because that one probes CI and mergeability for a release
        and is undefined for an issue. A blocker is usually a pull request but never has to
        be, and all this needs to know is whether it is out of the way.
        """

    def pr_states(self, refs: list[str]) -> dict[str, PrState] | None:
        """Probe each ``repo#number``, or None when any could not be read this wake."""

    def commit_checks(self, repo: str, sha: str) -> str | None:
        """The CI verdict GitHub holds for exactly this commit (#2364), or None if unreadable.

        ``green`` (every reported check passed), ``pending``, ``failing``, or ``none`` when no
        check was reported at all — which is never green: a repository whose CI is the
        verification has not verified a commit its CI never reported on.
        """

    def sync_merge_parent(self, ref: str, sha: str) -> str | None:
        """The head GitHub's own branch update merged the base into to make ``sha`` (#2793).

        ``""`` when ``sha`` is not such a merge commit, None when it could not be read.
        """

    def arm_auto_merge(self, ref: str) -> ArmOutcome:
        """Request native gated auto-merge for one PR."""

    def cancel_auto_merge(self, ref: str) -> bool:
        """Disable native auto-merge; true also when no request remains."""

    def deploy_states(self, builds: dict[str, str]) -> dict[str, DeployState]:
        """Normalized production-deploy state per repository, keyed by the merged commit."""

    def apply(self, effect: Effect) -> bool | WorktreeCleanup:
        """Perform one outward change; cleanup may report failed paths for exact retry."""

    def find_marked_issue(
        self, repo: str, marker: str, *, since: str
    ) -> tuple[int | None, bool]:
        """``(number, conclusive)`` for an issue carrying this marker, created at/after ``since``.

        Read by listing recent issues rather than through GitHub's search index, which lags: an
        issue created seconds ago can be reported absent, and "absent" is precisely what this is
        asked to establish before a second one is created.

        ``conclusive`` is separate from the number because "not found" and "could not establish
        that" must not collapse into one answer — they differ by exactly one duplicated issue. It
        is false both when the read failed and when the listing never reached back as far as
        ``since``: a busy repository can push the issue being looked for out of one page, and a
        page that is entirely newer than the intent proves nothing about whether it exists.
        """

    def open_issue(
        self, repo: str, title: str, body: str, *, labels: tuple[str, ...] = ()
    ) -> int | None:
        """Open an issue and return its number, or None when the write did not land.

        ``labels`` go on beside the ownership label in the same write, such as the spec-only
        label an ``open-work`` proposal asked for (#2874).

        The only port method that *creates* rather than mutates, and the only one whose failure
        cannot be distinguished from success by looking at its return value alone — which is why
        the caller writes a durable intent first and reconciles through ``find_marked_issue``.
        """

    def open_list_issue(self, repo: str, title: str, body: str) -> int | None:
        """Open an issue **without** the ownership label, or None when the write did not land.

        For an issue the engine keeps up to date itself, such as the scheduled-task list
        (#2517): labelling it would hand it to a worker. The same intent-then-look rule as
        ``open_issue`` applies to the caller.
        """

    def scheduled_tasks(self, tracked: tuple[str, ...] = ()) -> dict[str, TaskIssue] | None:
        """Every issue carrying the ``scheduled`` label in this project's issue repositories.

        Keyed ``repo#number``. Open tasks are all listed, and the most recently updated closed
        ones beside them. Every ``tracked`` task is also read directly, so a tracked task absent
        from the answer is one GitHub says is unlabelled or gone — never one a bounded page
        merely left out. None means the sweep could not be read whole, which must never be read
        as "there are no tasks" (#2517).
        """
