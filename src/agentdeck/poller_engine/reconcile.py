"""Lifecycle reconciliation (#176 slice 6).

Every wake the engine re-derives each managed card's truth from the live world
(issue open/closed, ownership label, board status, quiet age, worker markers)
rather than trusting its own memory. That self-heal is what makes the engine
survive a crashed worker, a hand-edited board, a lost dispatch, or a restart in
the middle of a turn.

Two invariants live here:

* **Never abandon a live worker, never wait forever on a dead one.** A card whose
  board status moved off *In progress* is only accepted as finished once the
  worker's terminal marker proves it, or once the persistent Runtime reports that
  Run's execution over — the engine holds while the worker may still be writing
  (#1091). Whether a worker is alive, dead or wedged is the Runtime's fact, never a
  reading of how quiet the GitHub issue has been (#2159): a wedged worker is stopped
  by the Runtime and then handled as a reportless ending.
* **A card is forgotten only after everything it holds is actually given back.**
  Worktrees are durably registered when created and removal is checked; a failed
  removal keeps the record alive as retryable cleanup instead of leaking the
  worktree silently (the #106 gap-2 fix — the reference engine ignored the
  ``git worktree remove`` result and deleted the record regardless). The ownership
  label is not something the engine gives back: it never removes it (#2497), so a
  released card keeps wearing the label a person put on it.

Pure, stdlib-only decisions over one card's record plus a ``CardSnapshot`` the
caller fetched. Nothing here performs GitHub I/O: the caller acts on the returned
decision (edit labels, set status, remove worktrees, emit a directive) and reports
the outcome back through ``record_cleanup``. The one filesystem read is worktree
path resolution (``paths.resolve``), because a recorded path is only useful if two
records of the same directory compare equal (#1095).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from . import paths, stop

STALE_SECONDS = stop.STALE_SECONDS

# How long a card its own worker closed with ``resolved`` keeps its record and worktree, so that
# worker can still ``reopen`` it when the person reading the report asks for more (#2522).
REOPEN_HOLD = timedelta(hours=24)

# Board statuses this module reasons about by role (#108: the Manifest names
# statuses by role, so these are role names, not board option ids).
IN_PROGRESS = "In progress"
TODO = "Todo"
DONE = "Done"

# Modes owned by the guarded-release state machine (slice 7), not by this module.
_RELEASE_MODES = frozenset({"merge", "merge-review"})


class ReconcileDecision(Enum):
    CLEANUP = "cleanup"                          # issue closed → release ownership + resources
    CLEANUP_RETRY = "cleanup-retry"              # record kept alive purely to retry cleanup
    RELEASED_DONE = "released-done"              # board reached Done → release the card
    IDLE_RELEASE = "idle-release"                # no worker to stop → hand the card back to Todo
    TERMINAL_RECOVERED = "terminal-recovered"    # worker's marker names the status to publish
    WORKER_ENDED = "worker-ended"                # terminal proven → adopt the live status
    AWAIT_TERMINAL = "await-terminal"            # status moved, no marker → hold until run ends
    STOP_RELEASED = "stop-released"              # the Runtime proved the stopped Run safe
    STOP_ENDED = "stop-ended"                    # the stopped turn had already ended on its own
    STOP_WAITING = "stop-waiting"                # stopping card, the Runtime has not proved it
    RECOVER_LOST_WORKER = "recover-lost-worker"  # recovered command lost its worker → restart now
    RELEASE_GATED = "release-gated"              # the release state machine owns this card
    ACTIVE = "active"                            # a worker is running; leave it alone
    IDLE = "idle"                                # owned, idle, ready for dispatch


@dataclass(frozen=True)
class CardSnapshot:
    """The live facts about one card that the caller fetched this wake.

    ``owned`` is whether the ownership (``claude``) label is present, ``status`` the
    live board status, and ``terminal_marker`` the status a worker's terminal comment
    asked for (``None`` when the worker has posted no terminal result under the
    current lease). ``review_verdict`` is the merge review's lease-bound report from the
    same live snapshot; keeping it here makes an unreadable card unknown rather than
    indistinguishable from a reviewer that ended without reporting.

    ``stopped_terminal_marker`` is the status a terminal comment bound to the
    *stopped* generation asked for — the worker that was already finishing when the
    stop arrived, reporting under the lease the stop invalidated (#299). It is the
    proof that turn is over, which is what a stop is waiting for.

    ``execution_stopped`` is the persistent Runtime's outcome for the stopped Run, read from
    the Runtime itself: a stop was asked of it and its execution can no longer write (#2159,
    #2200). It is keyed by the stopped generation, so an older stop cannot release a later
    stop of the same card (#935).

    ``run_ended`` is the Runtime's report that the current generation's execution ended,
    died or was stopped as wedged, read the same way. It is how a card whose board status
    moved while its worker was still running learns the worker is gone.

    ``quiet_seconds`` is how long the issue has been silent. It is only ever reported in the
    cycle log: GitHub silence says nothing about whether a worker process is alive (#2159).

    ``title`` and ``body`` are the issue as it reads right now. Nothing in the
    lifecycle decisions here depends on them; they are carried because the wake
    already reads the card and the title repair (#338) must not cost a second query.

    ``blockers`` names the cards this one declares as blocking that the port could **not
    show as closed** (#640) — an open blocker, one it could not read, one that does not
    exist. Empty means nothing is in the way. Like ``owned`` it is a derived fact: a
    project may declare a dependency through GitHub's native issue dependencies or the
    written ``Blocked by: #12`` convention, and the engine reasons about neither format,
    only about whether something is still holding the card (``blockers.py``).

    ``dependencies`` is the same list without the state filter: every dependency the card
    declares this wake, closed ones included, so ``blockers`` is always a subset of it. The
    port reads both out of one answer, so this costs nothing extra — and it is the only way
    to tell a card that was held by a dependency which has since closed from a card somebody
    parked in *Blocked* by hand, which is what makes a blocked card recoverable however it
    got there (#1588). Empty means the card declares no dependency at all.

    ``comments`` is the card's human comments as the wake read them, each with the id, author
    and timestamp that identify it, and ``comments_omitted`` how many older ones did not fit
    the bound. They are carried for the same reason ``title`` and ``body`` are — the snapshot
    query already reads comment bodies to find markers — and for one more: a requester who
    asks for something in plain prose rather than a ``/claude`` command has still asked for
    it, so the authoritative input bundle has to be able to see and fence that comment (#1473).
    """

    open: bool = True
    owned: bool = True
    spec_only: bool = False
    # The issue carries the Manifest's ``paused_label`` (#2548): nothing new starts on it.
    paused: bool = False
    blockers: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    status: str = TODO
    quiet_seconds: float = 0.0
    title: str = ""
    body: str = ""
    comments: tuple[dict, ...] = ()
    comments_omitted: int = 0
    terminal_marker: str | None = None
    review_verdict: str | None = None
    stopped_terminal_marker: str | None = None
    # The words of the current lease's terminal comment, without its marker and footer: what
    # the worker told a person when it finished (#2332). Read from the same comments the
    # marker is, so it costs nothing; ``None`` when there is no such comment.
    terminal_note: str | None = None
    execution_stopped: bool = False
    run_ended: bool = False
    # When the comment carrying ``terminal_marker`` / ``stopped_terminal_marker`` was posted, as
    # GitHub stamps it: the moment that Run said it was over. Empty when there is no such
    # comment or it could not be read. A `/claude stop` written before it was aimed at that
    # Run; one written after it was aimed at an idle card (#2448).
    terminal_at: str = ""
    stopped_terminal_at: str = ""
    # The chat answers (#2348) whose comments are among the comments read: what a retry reads
    # before it posts again, so an uncertain write is never repeated once it landed.
    chat_answer_ids: frozenset[str] = frozenset()
    # When the issue was last closed, as GitHub stamps it; empty while open or unread. A
    # person's `/claude` reopens an issue closed *before* it was written, never one closed after
    # (#2417).
    closed_at: str = ""



# --- durable host-resource registration (the #106 gap-2 fix) ---

def register_worktree(rec: dict, path: str) -> None:
    """Durably record a worktree the moment it is created, so cleanup can never miss it.

    The path is resolved before it is stored (#1095). Whichever spelling of an ostree host's
    ``/home`` the dispatcher happened to hand over, the record holds the one directory
    identity, so a later comparison — a dedup, a reaper's claimed set — cannot read a live
    worktree as unclaimed. Anything already recorded is re-resolved here too, so a card
    written before this rule existed stops accumulating a second spelling of its own tree.
    """
    registered = paths.resolve_all(rec.get("worktrees"))
    resolved = paths.resolve(path)
    if resolved not in registered:
        registered.append(resolved)
    rec["worktrees"] = registered


def register_generation_worktrees(
    state: dict, repo: str, number: int, generation: str, worktree_paths: list[str]
) -> bool:
    """Attach host worktrees only to the card generation that created them, all or none.

    The caller holds the state transaction lock.  Refusing a missing or superseded
    generation prevents an old worker from giving the current card cleanup authority over
    an unrelated path.

    A delivery's worktrees are registered as one act because a partial success is the worst
    outcome available: the accepted paths become the engine's to clean while the rejected
    ones sit on disk owned by nobody, and the host cannot tell from the refusal which is
    which.  Either the whole set is durable, or the host still holds all of it.
    """
    rec = (state.get("issues") or {}).get(f"{repo}#{int(number)}")
    if not isinstance(rec, dict) or not generation or rec.get("generation") != generation:
        return False
    for path in worktree_paths:
        register_worktree(rec, path)
    return True


def worktrees(rec: dict) -> list[str]:
    """The host paths that must be released before this card may be forgotten.

    Resolved on the way out as well as in, so a record written before that rule — the
    ``/home`` and ``/var/home`` mixture already sitting in a live ``state.json`` — reads back
    as one path per directory instead of two.
    """
    return paths.resolve_all(rec.get("worktrees"))


def begin_cleanup(rec: dict) -> list[str]:
    """Mark the card as pending removal and return the host paths to release first."""
    rec["forget_pending"] = True
    return worktrees(rec)


def abandon_dispatch(rec: dict) -> dict | None:
    """Drop a stopping card's outstanding dispatch and return it so its receipt can be cleared.

    A stop invalidated the generation that dispatch was minted under, so awaiting or
    retrying it would only deliver work the engine has already cancelled.
    """
    if not rec.get("stopping"):
        return None
    return rec.pop("dispatch", None)


def record_cleanup(rec: dict, *, failed: list[str] | None = None) -> bool:
    """Record a cleanup attempt; True when the card is fully released and may be forgotten.

    The worktrees still on the host in ``failed`` are retained on the record, so the next
    wake retries them (``CLEANUP_RETRY``) instead of forgetting a card that is still holding
    a checkout. ``cleanup_label`` is cleared for records written before #2497, when a refused
    label removal was retried here too.
    """
    remaining = paths.resolve_all(failed)
    if remaining:
        rec["worktrees"] = remaining
    else:
        rec.pop("worktrees", None)
    rec.pop("cleanup_label", None)
    if remaining:
        rec["cleanup_attempts"] = int(rec.get("cleanup_attempts") or 0) + 1
        return False
    rec.pop("cleanup_attempts", None)
    rec.pop("forget_pending", None)
    return True


# --- the per-card lifecycle decision ---

def _is_active(rec: dict) -> bool:
    return rec.get("status") == IN_PROGRESS or bool(rec.get("processing"))


def _arm_release(rec: dict) -> None:
    """Queue an idle hand-back."""
    rec["release_pending"] = True


def _awaiting_start(rec: dict) -> bool:
    """Has this card never run, with queued work still waiting to be run? (#1663)

    Not every card is authorized by the ownership label. One the engine seeded from a queued
    instruction — a ``/claude`` comment, an engine-authored brief — is authorized by
    that instruction, and the engine puts the label on when it *spawns*, not when it queues. So
    a card that has not started yet has never worn one, and reading that absence as somebody
    handing the card back cancels the very command the card exists to serve.

    It went unnoticed for as long as it did because such a card almost always starts on the
    same wake it is created, and wears the label by the time anything reconciles it. The cases
    that do not are the ones that were being thrown away: a card held by an open blocker
    (#640), and one whose dispatch was deferred by the worker cap or by host capacity. The
    first is the worse failure, because the gate's own comment promises the card starts on its
    own once the blocker closes, and after the release there was nothing left to start —
    neither the record that remembered what was blocking it nor the queued brief, and the
    command ledger then refuses to queue that brief a second time (#1663).

    A card whose ownership label a person really did remove is unaffected: it had started, so
    it is either active above or has no queue left waiting.
    """
    return not rec.get("started") and bool(rec.get("pending"))


def hands_back_on_missing_label(rec: dict) -> bool:
    """Would a missing label hand this card back? Not while it has never started (#1663)."""
    return _is_active(rec) or not _awaiting_start(rec)


def reopen_held(rec: dict, *, now: datetime) -> bool:
    """Whether this card's own ``resolved`` finish may still be taken back (#2522).

    True only inside ``REOPEN_HOLD`` of the finish and for the lease that wrote it.
    ``worker_control.finish`` writes the stamp, and ``reopen`` and any new dispatch drop it.
    """
    stamp = rec.get("resolved_finish")
    if not isinstance(stamp, dict):
        return False
    generation = str(stamp.get("generation") or "")
    if not generation or generation != str(rec.get("generation") or ""):
        return False
    try:
        at = datetime.fromisoformat(str(stamp.get("at") or ""))
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    age = now.astimezone(UTC) - at.astimezone(UTC)
    return age < REOPEN_HOLD


def reconcile_card(
    rec: dict,
    snapshot: CardSnapshot,
    *,
    now: datetime,
) -> ReconcileDecision:
    """Decide one card's fate from its record plus the live snapshot (see module docstring).

    Mutations are limited to durable bookkeeping the decision implies (adopting the
    live status, arming a stop or a release). Every outward effect — labels, board
    status, worktree removal, dispatch — belongs to the caller.
    """
    if snapshot.owned:
        # Remember that the card was seen wearing the ownership label, so that the label going
        # away later can be read as a person taking the card back (#2053). Recorded before any
        # decision returns, because every wake that sees the label is evidence of the claim,
        # whatever the card's fate that wake. The next dispatch drops it (``cycle._emit``), so
        # a card released and later started again cannot inherit a stale one.
        rec["label_claim"] = True

    if rec.get("forget_pending"):
        return ReconcileDecision.CLEANUP_RETRY

    if rec.get("release_pending"):
        return ReconcileDecision.IDLE_RELEASE

    if not snapshot.open and rec.get("dispatch"):
        # A generation is minted before delivery. Until the host acknowledges that spawn,
        # there is no worker to protect and a closed card must not wait on an acknowledgement
        # that can no longer lead to useful work.
        return ReconcileDecision.CLEANUP

    live_status = snapshot.status or rec.get("status") or TODO
    stopping = bool(rec.get("stopping"))
    mode = rec.get("mode")

    if mode in _RELEASE_MODES and not stopping:
        return ReconcileDecision.RELEASE_GATED

    was_in_progress = rec.get("status") == IN_PROGRESS
    if was_in_progress and live_status == IN_PROGRESS and not stopping:
        if snapshot.terminal_marker:
            # The worker finished but its status write did not land — publish it.
            return ReconcileDecision.TERMINAL_RECOVERED
        if not snapshot.open and rec.get("generation") and not snapshot.run_ended:
            # Closing an issue is not proof that its worker stopped. In particular, GitHub
            # closes an issue as soon as a linked PR merges, even while a review worker may
            # still be writing fixes in the issue worktree. Keep the lease and checkout alive
            # until the worker reports; ownership removal remains the explicit safe-stop path.
            #
            # Only while the Runtime still has that turn running (#2822). Once it reports the
            # turn over, nothing is left to report: holding the card waited for a report that
            # could never come, so a card closed by hand stayed In progress for good. It falls
            # through to the cleanup below instead.
            return ReconcileDecision.ACTIVE

    if was_in_progress and live_status != IN_PROGRESS:
        if not stopping and not snapshot.terminal_marker:
            # Someone (or something) moved the card while the worker may still be
            # writing. Holding is the safe read: never finish a live worker's card.
            # It holds only until the Runtime reports that Run's execution over (#1091,
            # #2159). This state has no dispatch of its own to make, so a card stuck on it
            # just spends a worker slot until a person notices. Once the Runtime says the
            # worker is gone, take the branch the card would have taken anyway —
            # ``WORKER_ENDED`` while the issue is open, ``CLEANUP`` once it is closed —
            # giving back the slot, the label and the worktrees.
            held = snapshot.open or rec.get("generation")
            if held and not snapshot.run_ended:
                return ReconcileDecision.AWAIT_TERMINAL
        rec["status"] = live_status
        if stopping:
            return _stop_outcome(rec, snapshot)
        if snapshot.open or snapshot.terminal_marker:
            return ReconcileDecision.WORKER_ENDED

    rec["status"] = live_status

    label_removed = not snapshot.owned and rec.get("label_claim")
    # A person's own instruction or start is a new Run, never this one taken back, and a held
    # Done card would refuse to start it: release it as before and let that start.
    asked = rec.get("pending") or rec.get("manual_start") or rec.get("recovery_action")
    held = (
        live_status == DONE and reopen_held(rec, now=now) and not (label_removed or asked)
    )

    if not snapshot.open:
        if held:
            # Closed by its own worker's ``resolved`` a moment ago (#2522). The person reading
            # that report in the worker's chat may still ask it to carry on, and ``reopen``
            # needs this record and the worktree to put the same Run back to work. A label
            # taken off still releases at once: that is a person taking the card back.
            return ReconcileDecision.IDLE
        return ReconcileDecision.CLEANUP

    if live_status == DONE:
        if held and snapshot.terminal_marker is None:
            # A ``reopen`` caught between its steps: its ``resumed`` comment is the newest
            # marker and the issue is open again, but the board write has not landed yet. The
            # worker's repeat finishes it. A person reopening the issue by hand leaves the
            # ``resolved`` marker newest, and that card is released as before.
            return ReconcileDecision.IDLE
        return ReconcileDecision.RELEASED_DONE

    if stopping and live_status == IN_PROGRESS:
        return _stop_outcome(rec, snapshot)

    if not snapshot.owned and rec.get("label_claim"):
        # The ownership label was removed by hand: a safe stop, not a kill.
        #
        # Only a card that was *seen wearing* the label can be handed back by it (#2053). The
        # engine stopped writing the label onto issues at dispatch, so a card started by a
        # `/claude` comment has never worn one and its absence says nothing — reading it as a
        # hand-back would stop every comment-started worker on its next wake. `label_claim` is
        # set below from what the live snapshot actually shows, so it is the label's own
        # presence and disappearance that is being read here, not a guess about who put it on.
        if _is_active(rec):
            stop.request_stop(rec, now=now)
            return _stop_outcome(rec, snapshot)
        if _awaiting_start(rec):
            # Never started, and still holding queued work: the missing label is the engine's
            # own doing, not a hand-back, so the card keeps waiting for the wake that can
            # start it (#1663).
            return ReconcileDecision.IDLE
        _arm_release(rec)
        return ReconcileDecision.IDLE_RELEASE

    if rec.get("recovery_action") and not rec.get("dispatch"):
        # A command recovered from the independent ledger has no live worker context:
        # restart it now instead of counting it as active work for the stale window.
        rec["status"] = TODO
        return ReconcileDecision.RECOVER_LOST_WORKER

    if mode in _RELEASE_MODES:
        return ReconcileDecision.RELEASE_GATED

    if live_status == IN_PROGRESS:
        # However long the issue has been quiet. Whether this Run's worker is still alive is
        # the Runtime's to report, and a reportless ending it reports is handled by the wake's
        # reportless path, not guessed here from GitHub silence (#2159).
        return ReconcileDecision.ACTIVE

    return ReconcileDecision.IDLE


def _stop_outcome(rec: dict, snapshot: CardSnapshot) -> ReconcileDecision:
    """Map the stop protocol's verdict onto a reconcile decision."""
    decision = stop.reconcile_stop(
        rec,
        stopped=snapshot.execution_stopped,
        ended=bool(snapshot.stopped_terminal_marker),
    )
    if decision is stop.StopDecision.ENDED:
        return ReconcileDecision.STOP_ENDED
    if decision is stop.StopDecision.RELEASED:
        return ReconcileDecision.STOP_RELEASED
    return ReconcileDecision.STOP_WAITING


# --- worker slots ---

# A card holds a worker slot whenever a worker may still be alive on it. Note
# ``AWAIT_TERMINAL`` counts: the reference engine skipped that case when sizing
# recovery, which could admit work past the cap while a worker was in fact still writing.
HOLDS_A_WORKER = frozenset(
    {
        ReconcileDecision.ACTIVE,
        ReconcileDecision.AWAIT_TERMINAL,
        ReconcileDecision.STOP_WAITING,
    }
)


def active_workers(decisions: dict[str, ReconcileDecision]) -> int:
    """How many cards still occupy a worker slot after this wake's decisions."""
    return sum(1 for decision in decisions.values() if decision in HOLDS_A_WORKER)


def reload_action(rec: dict) -> str:
    """The dispatch action that resumes a stale card's work under a fresh lease."""
    if rec.get("mode") == "merge-review":
        return "merge-review"
    return "pr" if rec.get("kind") == "pr" else "resume"


def begin_cold_reload(rec: dict, *, now: datetime) -> None:
    """Restart the card's clock for a revived Run, so its recovery cannot re-arm every wake."""
    rec["started_at"] = now.astimezone(UTC).isoformat()
