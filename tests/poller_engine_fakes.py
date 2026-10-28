"""Shared fakes for the Poller Engine cycle tests (#176): a recording port and store.

Not collected as tests. ``FakePort`` scripts every read the engine makes and captures
every write, so a whole wake can be driven deterministically; ``RecordingStore`` keeps
each mid-transaction publish so durability ordering can be asserted.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime

from agentdeck.poller_engine import authorization as auth
from agentdeck.poller_engine.cycle import run_cycle
from agentdeck.poller_engine.state import EnginePaths, StateStore

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)

# The fixture project's controller policy: "eero" may both work and release. A policy is
# required — an engine with no stated policy has been told nobody may steer it.
POLICY = auth.parse_policy(
    {"work": {"allow": ["eero"]}, "release": {"allow": ["eero"]}, "bot": "claude-bot"}
)


def run_wake(store, port, **kwargs):
    """One wake with the fixture's ordinary controller policy unless a test states another."""
    kwargs.setdefault("policy", POLICY)
    return run_cycle(store, port, **kwargs)


class FakePort:
    """A recording EnginePort: every read is scripted, every write is captured."""

    # The fixture's organization. Not part of the protocol — the real adapter carries it.
    org = "org"

    def __init__(
        self,
        *,
        refuse=(),
        permissions=None,
        ref_states=None,
        graphql_deferred=False,
        core_deferred=False,
        opened=None,
        marked=None,
        markers_readable=True,
        scheduled=None,
    ):
        # Issue opening (#393): `opened` scripts the numbers open_issue hands out in order, and
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
        self.marker_lookups = []
        self.refuse = set(refuse)
        self.permissions = dict(permissions or {})
        self.permission_calls = []
        # GitHub's own state per ``repo#number`` (#1230). Absent means unreadable.
        self._ref_states = dict(ref_states or {})
        self.ref_state_lookups = []
        self.graphql_deferred = graphql_deferred
        self.core_deferred = core_deferred
        self.applied = []
        self.live_query_checks = 0
        self.stale_reading_offered = None

    # reads
    def live_queries_halt(self, *, stale_reading_ok=False):
        self.live_query_checks += 1
        self.stale_reading_offered = stale_reading_ok
        if self.core_deferred:
            return "core-budget"
        if self.graphql_deferred:
            return "graphql-budget"
        return None

    def permission(self, repo, login):
        """The raw collaborator level, or None when it could not be read (uncertainty)."""
        self.permission_calls.append((repo, login))
        return self.permissions.get(login, "write")

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

    def ref_states(self, refs):
        """Issue/PR state per ref (#1230); an unscripted ref is simply unreadable."""
        self.ref_state_lookups.append(list(refs))
        return {ref: self._ref_states[ref] for ref in refs if ref in self._ref_states}

    # writes
    def apply(self, effect):
        # A kind on its own refuses every effect of that kind.
        landed = effect.kind not in self.refuse
        if landed:
            self.applied.append(effect)
        return landed

    def kinds(self):
        return [effect.kind for effect in self.applied]


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
