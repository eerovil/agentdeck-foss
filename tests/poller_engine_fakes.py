"""Shared fakes for the Poller Engine cycle tests (#176): a recording port and store.

Not collected as tests. ``FakePort`` scripts every read the engine makes and captures
every write, so a whole wake can be driven deterministically; ``RecordingStore`` keeps
each mid-transaction publish so durability ordering can be asserted.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime

from agentdeck.poller_engine import authorization as auth
from agentdeck.poller_engine.cycle import run_cycle
from agentdeck.poller_engine.ownership import LabelRemoval
from agentdeck.poller_engine.ports import (
    ArmOutcome,
    DeployState,
    LinkedPRs,
    ScanResult,
    SnapshotBatch,
    WorktreeChanges,
)
from agentdeck.poller_engine.state import EnginePaths, StateStore

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)

# The fixture project's controller policy: "eero" may both work and release. A policy is
# required — an engine with no stated policy has been told nobody may steer it.
POLICY = auth.parse_policy(
    {"work": {"allow": ["eero"]}, "release": {"allow": ["eero"]}, "bot": "claude-bot"}
)


def _checkout_of(path):
    """The checkout a fixture worktree belongs to: ``/co/x/.worktrees/n`` -> ``/co/x``."""
    head, marker, _rest = path.partition("/.worktrees/")
    return head if marker else path


def run_wake(store, port, **kwargs):
    """One wake with the fixture's ordinary controller policy unless a test states another."""
    kwargs.setdefault("policy", POLICY)
    return run_cycle(store, port, **kwargs)


def payloads(directive):
    """The instruction texts a directive carries (messages are full command records)."""
    return [message.get("payload") for message in directive["messages"]]


def comment(
    cid,
    body,
    *,
    repo="store",
    number=5,
    author="eero",
    created="2026-07-25T11:59:00Z",
    user_type="User",
    association="MEMBER",
):
    return {
        "id": cid,
        "body": body,
        "user": {"login": author, "type": user_type},
        "author_association": association,
        "issue_url": f"https://api.github.com/repos/org/{repo}/issues/{number}",
        "html_url": f"https://github.com/org/{repo}/issues/{number}#issuecomment-{cid}",
        "created_at": created,
    }


class FakePort:
    """A recording EnginePort: every read is scripted, every write is captured."""

    # The fixture's organization, matching the issue URLs `comment` builds. Not part of the
    # protocol — the real adapter carries it, and reference normalization reads it (#1230).
    org = "org"

    def __init__(
        self,
        *,
        comments=None,
        complete=True,
        scanned_to=NOW,
        snapshots=None,
        label_events=None,
        unreadable_label_events=(),
        labelled=None,
        labelled_complete=True,
        linked=None,
        review_diffs=None,
        release_trees=None,
        budget=True,
        acks=(),
        deferrals=None,
        ack_worktrees=None,
        ack_sessions=None,
        refuse=(),
        permissions=None,
        actors=None,
        pr_states=None,
        commit_checks=None,
        sync_parents=None,
        ref_states=None,
        review_verdicts=None,
        arm=None,
        cancel=None,
        deploys=None,
        turn_ends=None,
        dirty_counts=None,
        changed_paths=None,
        full_accounts=(),
        graphql_deferred=False,
        core_deferred=False,
        opened=None,
        marked=None,
        markers_readable=True,
        jobs=None,
        removals=None,
        scheduled=None,
    ):
        # Card opening (#393): `opened` scripts the numbers open_issue hands out in order, and
        # `marked` is what a marker lookup finds — the two together are how an interrupted
        # creation is exercised without a real GitHub.
        self._opened = list(opened or [])
        self.opened_calls = []
        # Scheduled tasks (#2517): the ``scheduled`` issues the sweep finds, keyed
        # ``repo#number``. The default empty dict is a board with no tasks; None is an
        # unreadable sweep. ``list_opened`` scripts the numbers `open_list_issue` hands out.
        self.scheduled = {} if scheduled is None else scheduled
        self.scheduled_reads = 0
        self.list_opened = []
        self.list_opened_calls = []
        self._marked = dict(marked or {})
        self.markers_readable = markers_readable
        # The host's job rows by id, the way the queue file holds them (#2154).
        self.jobs = dict(jobs or {})
        # The latest ownership-label removal per card (#2417). ``None`` scripts an unreadable
        # history; a card left out reads as one whose history shows no removal.
        self.removals = dict(removals or {})
        self.removal_reads: list[str] = []
        self.cancelled_jobs: list[str] = []
        self.marker_lookups = []
        self.comments = list(comments or [])
        self.complete = complete
        self.scanned_to = scanned_to
        self.snapshots = dict(snapshots or {})
        self._label_events = dict(label_events or {})
        self.unreadable_label_events = set(unreadable_label_events)
        # Discovery defaults to "nothing labelled that you do not already know", so a test about
        # something else is not quietly given extra cards. A test about intake says what is out
        # there; `labelled_complete=False` is the sweep that could not be read.
        self._labelled = list(labelled or [])
        self._labelled_complete = labelled_complete
        self.linked = dict(linked or {})
        # Pull requests GitHub would resolve that no card links, and whether the resolve read
        # fails outright — the two things a named release target can run into (#1445).
        self.resolvable: dict[str, str | None] = {}
        self.resolve_unreadable = False
        self._review_diffs = dict(review_diffs or {})
        # Changed paths per pull request (#2729); a missing ref reads as unreadable.
        self.files: dict[str, list[str]] = {}
        self._release_trees = dict(release_trees or {})
        self.budget = budget
        # Accounts the deck's usage cache positively reports over their ceiling (#802).
        self.full_accounts = set(full_accounts)
        self.acks = set(acks)
        self.deferrals = dict(deferrals or {})
        self.ack_worktrees = dict(ack_worktrees or {})
        self.ack_sessions = dict(ack_sessions or {})
        self.refuse = set(refuse)
        self.permissions = dict(permissions or {})
        self.permission_calls = []
        self.actors = dict(actors or {})
        self._pr_states = dict(pr_states or {})
        # GitHub's CI verdict per ``(repo, sha)`` (#2364). Absent means unreadable.
        self._commit_checks = dict(commit_checks or {})
        self.commit_check_reads = []
        # GitHub's own branch-update merge commits (#2793): ``{sha: first parent}``, with ``""``
        # for a commit that is not one; an unscripted commit is unreadable.
        self.sync_parents = dict(sync_parents or {})
        self.sync_parent_reads = []
        # What GitHub says about a blocker a worker named (#1230). Absent means unreadable.
        self._ref_states = dict(ref_states or {})
        self.ref_state_lookups = []
        self.review_verdicts = dict(review_verdicts or {})
        self.arm = dict(arm or {})
        self.cancel = dict(cancel or {})
        self.cancelled_auto_merges = []
        self.deploys = dict(deploys or {})
        # Per card: the host's turn-end observation the engine reads (#279).
        self.turn_ends = dict(turn_ends or {})
        # What ``waiting_on_person`` answers per card (#2887): True, False or None (unreadable).
        self.waiting: dict = {}
        self.waiting_reads: list[str] = []
        # Per worktree path: its dirty-file count; an unscripted path reads as unreadable.
        self.dirty_counts = dict(dirty_counts or {})
        # Per worktree path, either a `WorktreeChanges` or the paths alone as a shorthand: a
        # test that never mentions a path gets `None`, the unreadable answer (#1736). The
        # shorthand reads as local edits, which is the case with no inherited history at all
        # — a test about a branch's commits builds the `WorktreeChanges` itself (#2065).
        self.changed_paths = {
            str(path): value if isinstance(value, WorktreeChanges) or value is None
            else WorktreeChanges(checkout=_checkout_of(str(path)), local=tuple(value))
            for path, value in (changed_paths or {}).items()
        }
        self.changed_path_reads = []
        self.graphql_deferred = graphql_deferred
        self.core_deferred = core_deferred
        self.applied = []
        # One issue's whole comment history, oldest first (`issue_comments`, #2845), keyed by
        # card; a key in `unreadable_issue_comments` answers None, the read that failed.
        self.issue_comment_history = {}
        self.unreadable_issue_comments = set()
        self.scans = 0
        self.since = None
        self.armed = []
        self.updated_branches = []
        self.discarded = []
        self.snapshot_leases = []
        self.snapshot_batches = []
        self.live_query_checks = 0
        self.stale_reading_offered = None
        self.labelled_reads = 0
        self.ownership_actor_requests = []

    # reads
    def dispatch_ok(self):
        return self.budget

    def account_full(self, account):
        """Which accounts the deck's cached usage puts over their ceiling (#802)."""
        return account in self.full_accounts

    def live_queries_halt(self, *, stale_reading_ok=False):
        self.live_query_checks += 1
        self.stale_reading_offered = stale_reading_ok
        if self.core_deferred:
            return "core-budget"
        if self.graphql_deferred:
            return "graphql-budget"
        return None

    def issue_comments(self, repo, number):
        key = f"{repo}#{int(number)}"
        if key in self.unreadable_issue_comments:
            return None
        return list(self.issue_comment_history.get(key, []))

    def scan_comments(self, *, since):
        self.scans += 1
        self.since = since
        return ScanResult(list(self.comments), self.complete, self.scanned_to)

    def permission(self, repo, login):
        """The raw collaborator level, or None when it could not be read (uncertainty)."""
        self.permission_calls.append((repo, login))
        return self.permissions.get(login, "write")

    def snapshot_cards(self, cards):
        self.snapshot_batches.append(list(cards))
        if self.graphql_deferred:
            return SnapshotBatch(deferred=True)
        for key, request in cards.items():
            self.snapshot_leases.append(
                (key, request.generation, request.stop_generation, request.stop_requested_at)
            )
        return SnapshotBatch(cards={
            key: replace(
                self.snapshots[key],
                review_verdict=self.review_verdicts.get(
                    key, self.snapshots[key].review_verdict
                ),
            )
            for key in cards
            if key in self.snapshots
        })

    def labelled_cards(self):
        self.labelled_reads += 1
        if not self._labelled_complete:
            return None
        if self.unreadable_label_events & set(self._labelled):
            return None
        return {
            key: list(self._label_events.get(key, []))
            for key in self._labelled
        }

    def ownership_actors(self, events):
        """Who added the exact event; the fixture's ordinary controller unless overridden."""
        self.ownership_actor_requests.append(dict(events))
        return {key: self.actors.get(key, "eero") for key in events}

    def ownership_removal(self, repo, number):
        key = f"{repo}#{number}"
        self.removal_reads.append(key)
        return self.removals[key] if key in self.removals else LabelRemoval()

    def linked_prs(self, repo, number):
        return self.linked.get(f"{repo}#{number}", LinkedPRs())

    def resolve_prs(self, refs):
        """Classify named refs against every card's linked set, as GitHub would (#1445).

        ``resolvable`` is the fake's stand-in for "GitHub knows this pull request": a ref no
        card links and nobody declared resolvable is a typo, and one listed there but linked
        nowhere is a real pull request automatic discovery simply never found.
        """
        if self.resolve_unreadable:
            return None
        openings, merged, withheld = {}, [], []
        for ref in refs:
            for linked in self.linked.values():
                if ref in linked.open:
                    openings[ref] = linked.open[ref]
                    break
                if ref in linked.merged:
                    merged.append(ref)
                    break
                if ref in linked.withheld:
                    withheld.append(ref)
                    break
            else:
                if ref in self.resolvable:
                    openings[ref] = self.resolvable[ref]
        # A step marker is read from the same pull request body as discovery reads (#2730).
        marked = {ref for linked in self.linked.values() for ref in linked.step}
        steps = [ref for ref in (*openings, *merged) if ref in marked]
        return LinkedPRs(open=openings, merged=merged, withheld=withheld, step=steps)

    def release_trees(self, refs):
        return {ref: self._release_trees.get(ref) for ref in refs}

    def review_diffs(self, refs):
        return {ref: self._review_diffs.get(ref) for ref in refs}

    def pr_files(self, ref):
        return self.files.get(ref)

    def dispatch_acknowledged(self, repo, number, dispatch):
        return dispatch["id"] in self.acks

    def dispatch_deferred(self, repo, number, dispatch):
        return self.deferrals.get(dispatch["id"])

    def find_marked_issue(self, repo, marker, *, since):
        self.marker_lookups.append((repo, marker, since))
        if not self.markers_readable:
            return None, False
        return self._marked.get(marker), True

    def open_issue(self, repo, title, body, *, labels=()):
        self.opened_calls.append({"repo": repo, "title": title, "body": body})
        if labels:
            self.opened_calls[-1]["labels"] = tuple(labels)
        if not self._opened:
            return None
        return self._opened.pop(0)

    def open_list_issue(self, repo, title, body):
        self.list_opened_calls.append({"repo": repo, "title": title, "body": body})
        if not self.list_opened:
            return None
        return self.list_opened.pop(0)

    def scheduled_tasks(self, tracked=()):
        self.scheduled_reads += 1
        self.scheduled_tracked = tuple(tracked)
        return None if self.scheduled is None else dict(self.scheduled)

    def turn_ended(self, repo, number, generation):
        """What the host observed about this lease's turn; nothing, unless a test says so."""
        return self.turn_ends.get(f"{repo}#{number}") if generation else None

    def waiting_on_person(self, repo, number, generation):
        """Whether the Run waits on a prompt; not waiting, unless a test says so (#2887)."""
        self.waiting_reads.append(f"{repo}#{number}")
        return self.waiting.get(f"{repo}#{number}", False) if generation else None

    def verification_jobs(self):
        return [dict(job) for job in self.jobs.values()]

    def cancel_job(self, job_id):
        """Cancel a job the fake queue holds; ``cancel_fails`` makes every cancel undelivered."""
        self.cancelled_jobs.append(job_id)
        if getattr(self, "cancel_fails", False) or job_id not in self.jobs:
            return False
        self.jobs[job_id] = {**self.jobs[job_id], "state": "cancelled"}
        return True

    def dispatch_worktrees(self, repo, number, dispatch):
        """What the host reported creating for this delivery; nothing unless a test says so."""
        return list(self.ack_worktrees.get(dispatch["id"], ()))

    def dispatch_session_url(self, repo, number, dispatch):
        """The chat the host said took this delivery; nothing unless a test says so."""
        return self.ack_sessions.get(dispatch["id"], "")

    def worktree_changed_paths(self, paths):
        self.changed_path_reads.append(list(paths))
        return {path: self.changed_paths.get(path) for path in paths}

    def worktree_dirty_counts(self, paths):
        """Dirty-file count per registered path (#787); unscripted paths are unreadable."""
        return {path: self.dirty_counts.get(path) for path in paths}

    def ref_states(self, refs):
        """Issue/PR state per ref (#1230); an unscripted ref is simply unreadable."""
        self.ref_state_lookups.append(list(refs))
        return {ref: self._ref_states[ref] for ref in refs if ref in self._ref_states}

    def pr_states(self, refs):
        if self._pr_states is None:
            return None
        missing = [ref for ref in refs if ref not in self._pr_states]
        if missing:
            return None
        return {ref: self._pr_states[ref] for ref in refs}

    def commit_checks(self, repo, sha):
        self.commit_check_reads.append((repo, sha))
        return self._commit_checks.get((repo, sha))

    def sync_merge_parent(self, ref, sha):
        self.sync_parent_reads.append((ref, sha))
        return self.sync_parents.get(sha)

    def arm_auto_merge(self, ref):
        self.armed.append(ref)
        return self.arm.get(ref, ArmOutcome.ARMED)

    def cancel_auto_merge(self, ref):
        self.cancelled_auto_merges.append(ref)
        return self.cancel.get(ref, True)

    def deploy_states(self, builds):
        return {
            repo: self.deploys.get(repo, DeployState(status="pending"))
            for repo in builds
        }

    # writes
    def apply(self, effect):
        # A kind on its own refuses every effect of that kind; a `(kind, value)` pair refuses
        # just that one write. The board status is the case that needs the pair: a dispatch and
        # the stop it follows both publish one, and a test about a refused dispatch must not
        # refuse the stop's hand-back too.
        pair = (effect.kind, effect.value) if isinstance(effect.value, str) else None
        landed = effect.kind not in self.refuse and (pair is None or pair not in self.refuse)
        if landed:
            self.applied.append(effect)
            if effect.kind.value == "update-branch":
                self.updated_branches.append(f"{effect.repo}#{effect.number}")
            if effect.kind.value == "discard-receipts":
                self.discarded.append((f"{effect.repo}#{effect.number}", effect.value))
        return landed

    def kinds(self):
        return [effect.kind for effect in self.applied]

    def ownership_edits(self):
        """Landed effects that add or remove the ownership label. The engine makes none (#2497)."""
        return [effect for effect in self.applied if effect.kind.value.endswith("-ownership")]


class RecordingStore(StateStore):
    """A store that keeps every mid-transaction publish, so ordering can be asserted."""

    def __init__(self, paths):
        super().__init__(paths)
        self.publishes = []

    def checkpoint(self, payload):
        self.publishes.append(copy.deepcopy(payload))
        super().checkpoint(payload)


def store_for(tmp_path, initial=None) -> RecordingStore:
    store = RecordingStore(EnginePaths.from_base(tmp_path / "state.json"))
    if initial:
        with store.transaction() as state:
            state.update(copy.deepcopy(initial))
        store.publishes.clear()
    return store
