"""The independent command ledger: accepted commands outlive worker state (#176 slice 12).

Durable state holds two very different kinds of fact. Worker state — statuses, leases,
dispatches, release progress — is a *current generation* that may legitimately be rolled
back to its backup after a crash. Accepted commands are not: once the engine has taken a
command and told the requester so, forgetting it is a silent, unrecoverable failure. A
`/claude` instruction that vanishes looks exactly like one that was never sent.

So commands live in their own file, and the store publishes it **before** the worker state.
A crash between the two can at worst repeat an idempotent state transition; it can never
forget a command whose reaction has already changed. On load, the ledger is merged back
over the state (ledger records win by comment id, and the ledger's scan cursor wins), and
any command still queued or processing whose card record was lost is reattached:

* a work instruction goes back on the card's queue, and one that was mid-flight also marks
  the card for immediate restart rather than leaving it to age out the stale window;
* a review whose pass came with its approved-heads snapshot resumes the release, and one
  without that snapshot is **re-run** — a pass with no record of what was approved is not an
  approval, and merging unknown code is the one recovery that cannot be undone;
* a release or stop command goes back to unprocessed and is re-gated from the live world,
  because its remote side effect must never be inferred from a state generation that is gone.

Pure, stdlib-only operations over the state payload.
"""

from __future__ import annotations

from datetime import UTC, datetime

# Per-card facts worth carrying in the ledger: enough to rebuild a usable record for a
# recovered command (what kind of card it is, where its work lives, what was approved),
# never the volatile lease/dispatch state, which must be re-derived from the live world.
CONTEXT_FIELDS = (
    "status",
    "repos",
    "started",
    "kind",
    "branch",
    "branches",
    "worktrees",
    "session_id",
    "review_heads",
    "review_diffs",
    "review_trees",
    # The two release bits a later wake cannot re-derive (#2101). Both have to survive a state
    # generation being rolled back to its backup, and for opposite reasons: losing the review
    # pass costs a card a second review of a diff nobody changed, and losing the green commits
    # costs it a whole rebase-and-rerun turn. Neither can be reconstructed from the live world,
    # because GitHub knows nothing about either one.
    "release_review_pass",
    "release_tests_pass",
)

_CARRIED = frozenset(CONTEXT_FIELDS) - {"status"}

_CONTROLS_KEY = "controls"
_CURSOR_KEY = "control_since"
_PROPOSAL_INBOX_KEY = "proposal_inbox"
_RECOVERABLE = frozenset({"queued", "processing"})
_RELEASE_IN_FLIGHT = frozenset(
    {"awaiting_merge", "awaiting_deploy", "done_pending", "blocked_pending", "fix", "merged"}
)


def snapshot(state: dict) -> dict:
    """The ledger payload for the current state: commands, scan cursor, and card context."""
    context = {
        key: {field: rec[field] for field in CONTEXT_FIELDS if field in rec}
        for key, rec in (state.get("issues") or {}).items()
    }
    return {
        _CONTROLS_KEY: state.get(_CONTROLS_KEY) or {},
        _CURSOR_KEY: state.get(_CURSOR_KEY),
        _PROPOSAL_INBOX_KEY: state.get(_PROPOSAL_INBOX_KEY) or [],
        "issue_context": context,
    }


def merge(state: dict, ledger: dict) -> dict:
    """Merge a loaded ledger over a state payload; returns the card context it carried.

    Ledger records win by comment id: the ledger is published first, so where the two
    disagree the ledger is the newer truth. Its scan cursor wins for the same reason — a
    rolled-back cursor would re-ingest commands the engine has already answered.
    """
    if not isinstance(ledger, dict):
        return {}
    ledger_commands = ledger.get(_CONTROLS_KEY)
    if isinstance(ledger_commands, dict) and ledger_commands:
        state.setdefault(_CONTROLS_KEY, {}).update(ledger_commands)
    cursor = ledger.get(_CURSOR_KEY)
    if cursor:
        state[_CURSOR_KEY] = cursor
    proposal_inbox = ledger.get(_PROPOSAL_INBOX_KEY)
    if isinstance(proposal_inbox, list):
        if proposal_inbox:
            state[_PROPOSAL_INBOX_KEY] = proposal_inbox
        else:
            state.pop(_PROPOSAL_INBOX_KEY, None)
    context = ledger.get("issue_context")
    return context if isinstance(context, dict) else {}


def _blank(repo: str, now: datetime) -> dict:
    return {
        "status": "Todo",
        "repos": [repo],
        "started": False,
        "manual_start": False,
        "kind": "issue",
        "branch": "",
        "pending": [],
        "processing": [],
        "generation": "",
        "started_at": now.astimezone(UTC).isoformat(),
    }


def recover(state: dict, context: dict | None = None, *, now: datetime) -> list[str]:
    """Reattach live commands whose card record was lost; returns the cards it recovered.

    Only commands the engine had already accepted (queued or processing) are recovered:
    anything new, done, cancelled or rejected needs no rescue. A command already present on
    its card is left alone, so this is idempotent across wakes.
    """
    context = context if isinstance(context, dict) else {}
    recovered: list[str] = []
    commands = state.get(_CONTROLS_KEY) or {}
    if not commands:
        # Nothing was ever accepted: a load must not invent containers it does not need.
        return recovered
    cards = state.setdefault("issues", {})
    for cid, control in commands.items():
        status = control.get("status")
        if status not in _RECOVERABLE:
            continue
        repo, number = control.get("repo"), control.get("number")
        if not repo or number is None:
            continue
        key = f"{repo}#{number}"
        carried = context.get(key) if isinstance(context.get(key), dict) else {}
        fresh = key not in cards
        rec = cards.setdefault(key, {**_blank(repo, now), **{
            field: value for field, value in carried.items() if field in frozenset(CONTEXT_FIELDS)
        }})
        for field, value in carried.items():
            if field in _CARRIED:
                rec.setdefault(field, value)
        if fresh:
            recovered.append(key)
        if _already_attached(rec, cid):
            continue
        _reattach(rec, control, str(cid), status, now=now)
    return recovered


def _already_attached(rec: dict, cid: object) -> bool:
    cid = str(cid)
    attached = {str(value) for value in (rec.get("pending") or [])}
    attached |= {str(value) for value in (rec.get("processing") or [])}
    attached |= {str(rec.get("merge_control")), str(rec.get("stop_control"))}
    return cid in attached


def _reattach(rec: dict, control: dict, cid: str, status: str, *, now: datetime) -> None:
    kind = control.get("kind")
    if kind == "claude":
        rec.setdefault("pending", []).append(cid)
        control["status"] = "queued"
        if status == "processing":
            # It was mid-flight: its worker context is gone, so restart it now rather than
            # counting it as active work until the stale window expires.
            rec["started"] = True
            rec["recovery_action"] = "pr" if rec.get("kind") == "pr" else "resume"
        return
    if kind == "merge-review":
        if rec.get("mode") == "merge" or any(
            rec.get(field) for field in _RELEASE_IN_FLIGHT
        ):
            # The worker-state generation is newer than this detached command: reaching a
            # merge/deploy watch proves the review already completed. Re-running it would
            # overwrite the active release and let a late verdict hand the card back while
            # GitHub's native auto-merge request remains armed (#954).
            rec["mode"] = "merge"
            rec["started"] = True
            if any(rec.get(field) for field in ("awaiting_merge", "awaiting_deploy", "fix")):
                rec["status"] = "In progress"
            active = rec.get("merge_control")
            if active:
                # A newer release command already owns the watch; this detached review is
                # merely stale ledger residue and must not replace it.
                control["status"] = "done"
            else:
                # The handback may have popped mode and merge_control while leaving the watch.
                # Reattach the command to that newer release state instead of rerunning review.
                rec["merge_control"] = cid
            return
        if control.get("review_passed") and rec.get("review_heads"):
            rec.update(
                status="In progress",
                started=True,
                mode="merge",
                merge_control=cid,
                started_at=now.astimezone(UTC).isoformat(),
            )
            return
        # A pass without the snapshot of what was approved is not an approval.
        control.pop("review_passed", None)
        control["status"] = "queued"
        rec.update(
            status="Todo",
            started=True,
            mode="merge-review",
            merge_control=cid,
            recovery_action="merge-review",
        )
        return
    # Release and stop commands are re-gated from the live world: their remote side effect
    # must never be inferred from a state generation that no longer exists.
    control["status"] = "new"
