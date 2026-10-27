"""The webhook-quiet port: live GitHub reads refused, everything else untouched (#1673).

#1631 decided that a timed wake may skip its **live GitHub queries** when the webhook
streams prove there is nothing new to read. #1673 says in the same breath that durable-state
reconciliation and controls still run on every wake. Those two only fit together if the skip
is narrow: the wake still happens, and what it loses is the fresh view of GitHub, not its own
memory.

So the quiet wake runs the ordinary cycle behind this wrapper. Every live GitHub read answers
with the *same* value that read already uses to say "not this wake" — ``None`` for a sweep
that could not be completed, ``deferred`` for a snapshot batch, an absent key for a state the
adapter has no grounds to claim. Nothing new is invented: those answers are the ones the
GraphQL reserve and a GitHub outage already produce, so every caller's handling of them is
code that has been exercised since long before this existed.

What is deliberately **not** wrapped is everything that never touches GitHub — the host's
dispatch receipts, turn ends, job rows, phase pressure and worktree reads — and every write.
A queued ``/claude`` command accepted on an earlier wake, a delivery waiting to be retried,
a release finalization already decided: all of those are durable state the engine holds, and
none of them needs a fresh discovery pass to move. Holding them for up to ``webhook_skip_s``
was the defect this fixes.

Deploy observation is one of those reads (#2351). ``deploy_states`` and the release-gate
extensions ask the host or the deploy system, never GitHub, and a deploy that finishes sends
GitHub nothing — so the streams stay quiet exactly when the answer changes. Refusing it here
would leave a merged card reading Deploying for up to the whole ceiling.

The one read that is *not* refused is ``live_queries_halt``: it reports the host's own budget
snapshot rather than asking GitHub anything, and the cycle has to keep seeing it.

That claim was false for two years and is now true (#2082). ``live_queries_halt`` reaches
``Gh.current_rate_limits``, which used to spend a real ``gh api user`` whenever the shared store
held no core reading from the last 30 seconds — and a quiet wake, by construction, makes no other
call that could leave one. So the cheapest wake the engine has was quietly the one paying for the
gate, on every instance.

The cycle now decides the quiet skip *before* the gate and passes ``stale_reading_ok`` into it, so
a quiet wake answers from a reading up to ``GATE_MAX_AGE_S`` old and probes only when the store is
empty outright. Only a quiet wake gets that: it is the one wake where nothing — including the
``chatgpt-nudge`` extension, which spends core quota through raw ``urllib`` and records none of it
— can lower the real budget behind the gate's back. Keep both halves, or quiet stops being free
again, or the gate starts trusting a reading it has no right to.
"""

from __future__ import annotations

from .ports import EnginePort, LinkedPRs, PrState, ScanResult, SnapshotBatch

# What a deferral caused by the quiet skip says in the cycle log, where the budget path says
# "GraphQL reserve reached". An operator reading a deferred phase has to be able to tell a
# quota problem from a deliberately cheap wake.
QUIET_REASON = "webhook-quiet wake"


class QuietPort:
    """An ``EnginePort`` that refuses live GitHub reads and delegates everything else."""

    def __init__(self, port: EnginePort) -> None:
        self._port = port

    # --- host-local reads and every write: straight through ---------------------------

    def __getattr__(self, name: str):
        # Anything not overridden below is host-local or a write, and belongs to the real
        # port. Only ever reached for attributes this class does not define, so an override
        # can never be bypassed by accident.
        return getattr(self._port, name)

    # --- live GitHub reads: the answer each caller already reads as "not this wake" -----

    def scan_comments(self, *, since: str) -> ScanResult:
        # Incomplete, so the intake cursor holds exactly where it is and no comment can be
        # skipped. The quiet cycle does not call this at all; it is refused here too so the
        # wrapper is safe whatever a later phase order does.
        return ScanResult(comments=[], complete=False, scanned_to=None)

    def permission(self, repo: str, login: str) -> str | None:
        return None  # uncertainty, which authorization never reads as a grant

    def snapshot_cards(self, cards: dict) -> SnapshotBatch:
        return SnapshotBatch(deferred=True, reason=QUIET_REASON)

    def labelled_cards(self) -> dict | None:
        return None  # a sweep that could not be completed, never "nothing is labelled"

    def ownership_actors(self, events: dict[str, str]) -> dict[str, str]:
        return {}

    def ownership_removal(self, repo: str, number: int):
        return None  # unreadable, so a hand-back waits for a wake that can read it

    def linked_prs(self, repo: str, number: int) -> LinkedPRs | None:
        return None

    def resolve_prs(self, refs: list[str]) -> LinkedPRs | None:
        return None

    def release_trees(self, refs: list[str]) -> dict[str, str | None]:
        return dict.fromkeys(refs)

    def review_diffs(self, refs: list[str]) -> dict[str, str | None]:
        return dict.fromkeys(refs)

    def pr_files(self, ref: str) -> list[str] | None:
        return None

    def ref_states(self, refs: list[str]) -> dict[str, str]:
        return {}  # absent is how this read reports a ref it could not judge

    def pr_states(self, refs: list[str]) -> dict[str, PrState] | None:
        return None

    def commit_checks(self, repo: str, sha: str) -> str | None:
        return None

    def sync_merge_parent(self, ref: str, sha: str) -> str | None:
        return None

    def scheduled_tasks(self, tracked=()):
        return None  # the scheduled-task sweep waits for a full wake (#2517)

    def find_marked_issue(self, repo: str, marker: str, *, since: str):
        # Inconclusive, so nothing opens a second issue on the strength of a read that
        # never happened.
        return (None, False)
