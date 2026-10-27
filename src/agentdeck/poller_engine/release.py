"""Guarded release: admission, approval binding, repair policy, deploy gate (#176 slice 7).

Releasing is the only thing the engine does that a human cannot undo, so every step
is gated and every irreversible step is durably *intended* before it becomes
visible. These invariants live here:

* **Admission is narrow.** A release command is accepted only for an open, engine-
  owned card that is genuinely idle (no worker, no queued instruction, not
  stopping), resting in *In review* — or in the optional *Ready to merge* column where a
  project made one (#1099) — with a discovered linked-PR set. Anything else
  is rejected with a reason the caller can explain, rather than merged optimistically.
  A reason has to be *true*: "I cannot decide this yet" is a defer, not a refusal, and
  is never dressed up as one of the refusals (#294).
* **An approval is bound to the exact PR diff that was approved.** ``/merge review`` snapshots
  the open linked-PR set, every head SHA, and each PR's effective diff. A poller-created branch
  sync remains approved only while that diff is identical; a changed diff, a changed PR set, or
  an unverifiable check hands the work back (or defers). Older in-flight records keep their
  prospective-tree or strict head fence. Queued instructions supersede a pass, since the
  requester has since asked for more work.
* **One repair attempt, and code changes go back to the human who approved the diff.** A
  red or conflicting PR gets a single ``merge-fix`` worker. If it pushed and the blockers
  remain, the attempt is spent and the card is blocked. If it cleared them by changing
  code, a *diff-bound* approval hands back for review; lint/update-only repairs — and red
  CI that cleared with no push at all — keep merging. "Changed code" is the PR's own
  effective diff, not its head SHA (#1436): a repair whose whole push is a merge of the
  current base moves the head without changing a line the PR contributes, so handing it
  back would ask for a second review of a byte-identical diff.
* **Arming auto-merge is not the end of judging CI, and it earns one more attempt.** Checks
  keep running after the release arms GitHub's native auto-merge, so a PR can go red *after*
  the watch begins. The watch used to see only open/closed/merged, so red CI and slow CI
  looked identical and both spent the full merge timeout before parking with a reason that
  named nothing. Now the watch classifies each armed PR like ``plan_release`` does, and a
  repairable blocker cancels the auto-merge request and returns the card to assessment with
  a fresh repair attempt — once per release, so a check that flaps cannot loop. What that
  second attempt then costs is unchanged: the same ``assess_fix`` rules decide whether a
  code-changing repair hands back or keeps merging.
* **A release is finished or explicitly parked, never silently abandoned.** Waiting
  on checks, merges, or a production deploy is bounded by a timeout that parks the card
  (keeping the merges already made), and the terminal Done/parked transitions are
  written as durable pending intents first, so a wake that dies mid-transition
  resumes it instead of losing it. Where it parks is the caller's to choose: *Blocked*, or
  the optional *Ready to merge* column, which exists so approved work whose release did not
  land is one pile a person can retry rather than two columns meaning other things (#1099).

Pure, stdlib-only decisions over one card's record. Linked-PR discovery, check and
merge-state probing, branch updates, the native auto-merge request, comments, and
deploy observation are all I/O the caller (and the CI/deploy extension, #179)
performs and feeds back in as plain values.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum

MERGE_TIMEOUT_SECONDS = 45 * 60

IN_PROGRESS = "In progress"
IN_REVIEW = "In review"

# A failing "branch is up to date with the base" check means the PR is *behind*, not
# broken: its red CI ran against stale code and cannot be trusted until it updates.
UPTODATE_STEP = "Check_up_to_date"


class ReleaseAdmission(Enum):
    ADMIT_MERGE = "admit-merge"                    # release now
    ADMIT_REVIEW = "admit-review"                  # review first, then release on a pass
    REJECT_CLOSED = "reject-closed"                # the issue/PR is closed
    REJECT_UNOWNED = "reject-unowned"              # the engine does not own this card
    REJECT_BUSY = "reject-busy"                    # a worker or queued instruction owns it
    # Not a refusal: the engine simply has no record of this card yet. An owned card is
    # seeded by discovery, so the answer arrives on a later wake — deciding now would
    # publish a verdict about a card the engine has not met (#294).
    DEFER_NO_RECORD = "defer-no-record"
    REJECT_STATUS = "reject-status"                # not in the release-eligible status
    REJECT_NO_LINKED_PR = "reject-no-linked-pr"    # nothing linked to release
    REJECT_NOTHING_TO_REVIEW = "reject-nothing-to-review"  # review asked for, no open PR


class ReviewOutcome(Enum):
    RELEASE = "release"    # approved, still current, nothing queued → continue into the merge
    HANDBACK = "handback"  # changes needed, approval stale, or superseded by queued work
    DEFER = "defer"        # freshness could not be verified this wake; decide later


class PrBlocker(Enum):
    BEHIND = "behind"      # behind the base branch → update first, then re-judge CI
    DIRTY = "dirty"        # conflicts with the base branch → a rebase job
    RED_CODE = "red-code"  # genuine red CI on current code
    RED_LINT = "red-lint"  # red CI, but only lint steps failed
    PENDING = "pending"    # checks still running
    READY = "ready"        # green and mergeable


class ReleaseStep(Enum):
    UPDATE_BEHIND = "update-behind"  # a linked PR is behind its base → update, then re-judge
    REPAIR = "repair"                # a real blocker → spend the single repair attempt
    WAIT_CHECKS = "wait-checks"      # checks still running → wait (bounded)
    ARM_MERGE = "arm-merge"          # every linked PR is ready → arm native auto-merge


class FixOutcome(Enum):
    UPDATE_BEHIND = "update-behind"  # went behind mid-fix → update branches, keep waiting
    EXHAUSTED = "exhausted"          # the worker pushed and blockers remain → block
    WAITING = "waiting"              # the one attempt is still in flight
    UNCHANGED = "unchanged"          # the worker ended, pushed nothing, still red → block now
    HANDBACK = "handback"            # cleared by a real code change → the approver reviews it
    KEEP_MERGING = "keep-merging"    # lint/update-only fix, or transient red that cleared


class DeployOutcome(Enum):
    DONE = "done"            # every watched deploy reached a good terminal state
    FAILED = "failed"        # a deploy failed — a human owns a broken production deploy
    TIMED_OUT = "timed-out"  # no terminal state within the limit
    WAITING = "waiting"      # still deploying


# --- admission ---

def admit(
    rec: dict | None,
    *,
    review_first: bool,
    issue_open: bool,
    owned: bool,
    live_status: str | None,
    linked_open: int,
    linked_merged: int,
    eligible_statuses: tuple[str, ...] = (IN_REVIEW,),
) -> ReleaseAdmission:
    """Decide whether a ``/merge`` (or ``/merge review``) command may start (see module docstring).

    ``linked_open``/``linked_merged`` are the counts the caller's linked-PR discovery
    returned; discovery failure is the caller's problem to retry, not a rejection.

    ``rec is None`` — no record of this card at all — is deliberately *not* folded into
    ``REJECT_BUSY``: nothing is happening is the opposite of something is happening, and
    an operator told `busy` reads it as transient and retries. It is a defer, because an
    owned card the engine has no record of is one discovery has not seeded yet (#294).

    ``eligible_statuses`` is more than one column only where a project made the optional
    *Ready to merge* column (#1099). A release attempt that did not merge rests there, and
    the whole point of that column is that ``/merge`` can be tried again from it — without
    it, the engine's own repair-exhausted advice ("fix it and ``/merge`` again") named an
    action it would then refuse, because the card it had just written to *Blocked* no longer
    sat in the one admissible column.
    """
    if not issue_open:
        return ReleaseAdmission.REJECT_CLOSED
    if not owned:
        return ReleaseAdmission.REJECT_UNOWNED
    if rec is None:
        return ReleaseAdmission.DEFER_NO_RECORD
    if is_busy(rec):
        return ReleaseAdmission.REJECT_BUSY
    if live_status not in eligible_statuses:
        return ReleaseAdmission.REJECT_STATUS
    if not linked_open and not linked_merged:
        return ReleaseAdmission.REJECT_NO_LINKED_PR
    if review_first and not linked_open:
        # A plain /merge can still publish and deploy-verify already-merged work.
        return ReleaseAdmission.REJECT_NOTHING_TO_REVIEW
    return ReleaseAdmission.ADMIT_REVIEW if review_first else ReleaseAdmission.ADMIT_MERGE


def is_busy(rec: dict) -> bool:
    """True while a worker, a queued instruction, an active mode, or a stop owns the card."""
    return bool(
        rec.get("status") == IN_PROGRESS
        or rec.get("mode")
        or rec.get("stopping")
        or rec.get("pending")
        or rec.get("processing")
    )


# --- approval bound to the approved diff ---

def arm_review(
    rec: dict,
    *,
    heads: dict[str, str],
    review_diffs: dict[str, str | None] | None = None,
    release_trees: dict[str, str | None] | None = None,
    control_id: str,
    now: datetime,
) -> None:
    """Record a review request together with the exact open-PR set and head SHAs it covers."""
    clear_post_arm_repair(rec)
    rec.update(
        mode="merge-review",
        merge_control=control_id,
        started=True,
        review_heads=dict(heads),
        started_at=now.astimezone(UTC).isoformat(),
    )
    trees = {ref: tree for ref, tree in (release_trees or {}).items() if tree}
    if trees:
        rec["review_trees"] = trees
    else:
        rec.pop("review_trees", None)
    diffs = {ref: digest for ref, digest in (review_diffs or {}).items() if digest}
    if diffs:
        rec["review_diffs"] = diffs
    else:
        rec.pop("review_diffs", None)


def arm_rebase_round(
    rec: dict, refs, *, round_id: str, sources: dict[str, str] | None = None
) -> None:
    """Note that the round of work being dispatched is the engine's rebase of these references.

    Only a note that *this round* is running, and it vouches for nothing on its own. At dispatch
    the rebase has not happened yet, so the head it will produce does not exist to be recorded;
    a marker that stood for "this reference may reuse its review" from here until something
    spent it could not tell the rebase's own head from a push that landed a moment later.

    ``round_id`` is what stops it outliving the round that armed it. A rebase whose verification
    is cancelled, parked or exhausted ends without ever producing a head, and a free-standing
    field would sit there until the *next* round's clean head claimed it — an ordinary worker's
    push recorded as though the engine had produced it (#2122).

    ``sources`` is the head each reference was at when the engine asked for the rebase, read
    from the rebase control's own id. It is what lets the head this round produces be written
    down as coming *from* that head (#2453); a round with none records its head exactly as
    before, and no lineage.
    """
    kept = sorted({str(ref) for ref in refs if ref})
    if kept and round_id:
        entry: dict = {"refs": kept, "round": str(round_id)}
        known = _sources(sources, kept)
        if known:
            entry["from"] = known
        rec["rebase_round_pending"] = entry


def _sources(value: object, refs) -> dict[str, str]:
    """The ``{reference: head}`` a rebase round started from, kept only for its own references."""
    if not isinstance(value, dict):
        return {}
    wanted = {str(ref) for ref in refs}
    return {
        str(ref): str(head).lower()
        for ref, head in value.items()
        if ref and head and str(ref) in wanted
    }


def take_pending_rebase_round(rec: dict, *, round_id: str) -> dict[str, str]:
    """Which references a finishing rebase round was for — only if it is that same round.

    Answered as ``{reference: the head it was rebased from}``, with ``""`` where the round did
    not record one. A different round asking gets nothing and leaves the marker alone: it is
    not this round's to spend, and its own head is not engine-owned provenance.
    """
    value = rec.get("rebase_round_pending")
    if not isinstance(value, dict):
        return {}
    if not round_id or str(value.get("round") or "") != str(round_id):
        return {}
    rec.pop("rebase_round_pending", None)
    return _round_refs(value)


def _round_refs(entry: dict) -> dict[str, str]:
    refs = entry.get("refs")
    if not isinstance(refs, list):
        return {}
    known = _sources(entry.get("from"), [ref for ref in refs if ref])
    return {str(ref): known.get(str(ref), "") for ref in refs if ref}


def retire_pending_rebase_round(rec: dict) -> None:
    """The processing round ended without producing a head, so its provenance dies with it.

    Every ending goes through one place — done, cancelled, parked, superseded — so this is a
    rule about the round's lifetime rather than a guard bolted onto each way it can end.
    """
    rec.pop("rebase_round_pending", None)


#: How many ended rebase rounds may still be waiting for their suite at once (#2449).
_REBASE_RUNS_KEPT = 5


def keep_rebase_round_for_run(rec: dict, *, run_id: str) -> None:
    """A rebase round ended before its suite did: let that suite's job bind its head (#2449).

    The worker queues its suite and ends its turn, so the round's own job almost never finishes
    inside the round, and the pending marker died with it — every clean rebase then read as an
    unreviewed push. This keeps the round's references under the Run it ran as, in a field of
    its own that ``arm_rebase_round`` never writes, so a verify round dispatched meanwhile
    cannot overwrite it. It still vouches for nothing by itself: only a job of that same Run
    binds, and it binds the exact head it tested, so a later push still loses the review.

    A verify round is not kept: its head is never the engine's move.
    """
    value = rec.get("rebase_round_pending")
    if not isinstance(value, dict) or not run_id or rec.get("verify_round_pending"):
        return
    refs = value.get("refs")
    kept = sorted({str(ref) for ref in refs if ref}) if isinstance(refs, list) else []
    if not kept:
        return
    runs = [
        entry for entry in rebase_round_runs(rec) if entry["run_id"] != str(run_id)
    ]
    entry: dict = {"refs": kept, "run_id": str(run_id)}
    known = _sources(value.get("from"), kept)
    if known:
        entry["from"] = known
    runs.append(entry)
    rec["rebase_round_runs"] = runs[-_REBASE_RUNS_KEPT:]


def rebase_round_runs(rec: dict) -> list[dict]:
    """The ended rebase rounds still waiting for their own suite, as ``{refs, run_id[, from]}``."""
    value = rec.get("rebase_round_runs")
    if not isinstance(value, list):
        return []
    runs: list[dict] = []
    for entry in value:
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("refs"), list)
            and entry.get("run_id")
        ):
            continue
        refs = [str(ref) for ref in entry["refs"] if ref]
        kept: dict = {"refs": refs, "run_id": str(entry["run_id"])}
        known = _sources(entry.get("from"), refs)
        if known:
            kept["from"] = known
        runs.append(kept)
    return runs


def take_rebase_round_for_run(rec: dict, *, run_id: str) -> dict[str, str]:
    """Which references an ended rebase round of this Run was for, spending the entry.

    The same ``{reference: rebased-from head}`` answer as ``take_pending_rebase_round``.
    """
    runs = rebase_round_runs(rec)
    found = [entry for entry in runs if run_id and entry["run_id"] == str(run_id)]
    if not found:
        return {}
    left = [entry for entry in runs if entry["run_id"] != str(run_id)]
    if left:
        rec["rebase_round_runs"] = left
    else:
        rec.pop("rebase_round_runs", None)
    return _round_refs(found[0])


def rebase_round_sources(rec: dict, ref: str) -> set[str]:
    """The heads a rebase round of ``ref`` still waiting to bind its output started from (#2453).

    Covers the round that is running and the ended rounds whose suite has not landed. The head
    such a round produces has no provenance yet, but the head it started from is known: a verdict
    on that head can be kept there, and carried only once the round's own suite binds its output.
    """
    entries = [rec.get("rebase_round_pending"), *rebase_round_runs(rec)]
    found: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            refs = entry.get("refs") if isinstance(entry.get("refs"), list) else []
            source = _sources(entry.get("from"), refs).get(str(ref))
            if source:
                found.add(source)
    return found


def retire_rebase_round_runs(rec: dict) -> None:
    """Fresh work superseded every rebase round still waiting on its suite (#2449)."""
    rec.pop("rebase_round_runs", None)


#: How many of the engine's own rebases a card remembers the lineage of (#2453). A review takes
#: about as long as a suite, so this has to span a few rebases during one review, not history.
_REBASE_LINEAGE_KEPT = 8


def note_rebase_lineage(rec: dict, *, ref: str, source: str, head: str, now: datetime) -> None:
    """Remember that the engine's rebase round turned ``source`` into ``head`` on ``ref`` (#2453).

    The carry-over marker (``rebase_round``) says the same thing but is spent the first time
    the review bit is read, and an outside reviewer's verdict on the pre-rebase head routinely
    arrives after that. This outlives it so that verdict can still be traced to the head the
    pull request sits on now. Written only where the marker is bound — by the rebase round's
    own suite — so it vouches for exactly what the marker vouched for, and nothing a push made.
    """
    source = str(source or "").strip().lower()
    head = str(head or "").strip().lower()
    if not ref or not source or not head or source == head:
        return
    edges = [
        edge for edge in rebase_lineage(rec)
        if not (edge["ref"] == str(ref) and edge["from"] == source)
    ]
    edges.append(
        {"ref": str(ref), "from": source, "to": head, "at": now.astimezone(UTC).isoformat()}
    )
    rec["rebase_lineage"] = edges[-_REBASE_LINEAGE_KEPT:]


def rebase_lineage(rec: dict) -> list[dict]:
    """The engine's remembered rebases, oldest first, as ``{ref, from, to, at}``."""
    value = rec.get("rebase_lineage")
    if not isinstance(value, list):
        return []
    return [
        {
            "ref": str(edge["ref"]),
            "from": str(edge["from"]).lower(),
            "to": str(edge["to"]).lower(),
            "at": str(edge.get("at") or ""),
        }
        for edge in value
        if isinstance(edge, dict) and edge.get("ref") and edge.get("from") and edge.get("to")
    ]


def lineage_reaches(rec: dict, *, ref: str, start: str, end: str) -> bool:
    """Do the engine's own rebases of ``ref`` lead from ``start`` to ``end``, and only those?

    Every hop has to be a remembered rebase whose output the next hop started from, so a push
    anywhere along the way breaks the chain. A reference is matched exactly: lineage is keyed
    by the canonical reference the round recorded, and the caller resolves to that.
    """
    here = str(start or "").strip().lower()
    goal = str(end or "").strip().lower()
    if not here or not goal or here == goal:
        return False
    moves = {edge["from"]: edge["to"] for edge in rebase_lineage(rec) if edge["ref"] == str(ref)}
    seen = {here}
    for _ in range(_REBASE_LINEAGE_KEPT):
        here = moves.get(here, "")
        if not here or here in seen:
            return False
        if here == goal:
            return True
        seen.add(here)
    return False


def retire_rebase_lineage(rec: dict) -> None:
    """Fresh work replaced what those rebases carried, so none of them may bridge a verdict."""
    rec.pop("rebase_lineage", None)


def want_release_recheck(rec: dict) -> None:
    """A head just went green: read this card's release bits on this wake or the next (#2449).

    An In-review card is otherwise re-read only every ``terminal_recheck_s``, and on a busy
    base that is long enough for another merge to leave a green, reviewed branch behind again.
    """
    rec["release_recheck"] = True


def release_recheck_wanted(rec: dict) -> bool:
    return bool(rec.get("release_recheck"))


def settle_release_recheck(rec: dict) -> None:
    """The bits were read (or the card is out of their scope); nothing is owed any more."""
    rec.pop("release_recheck", None)


def note_rebase_round(rec: dict, moves: dict) -> None:
    """Record the exact head each engine-owned move produced, as ``{reference: head}``.

    Two things record it: the engine's branch sync of a behind pull request, which reads the
    resulting head back in the same wake, and the rebase-and-rerun round, whose head is the
    commit the worker's own suite ran on. Neither is an author pushing code, which is the
    distinction ``_content_unchanged`` needs before it will judge a moved head by its
    contributed diff — a digest says what a change contains, never where in the file it now
    sits, so reusing it for a push would let an identical block move to another function with
    the review still green (#1436, #2122).

    Bound to the head and not merely to the reference, because a reference can move twice. The
    engine rebases A from H0 to H1, an author pushes H2 to the same A, and a marker naming only
    A cannot tell them apart; one naming H1 admits H1 and nothing else, so the push invalidates
    the review exactly as any push does. A head recorded for the wrong reference is harmless
    for the same reason — the live head has to equal it, and two pull requests never share a
    commit — so over-recording can cost a review but can never skip one.

    An entry with no head is not recorded at all: a move whose result could not be read
    vouches for nothing, which is the direction that costs a review rather than skipping one.
    """
    kept = rebase_round(rec)
    kept.update({str(ref): str(head) for ref, head in (moves or {}).items() if ref and head})
    if kept:
        rec["rebase_round"] = kept


def rebase_round(rec: dict) -> dict[str, str]:
    """Which heads the engine itself produced, as ``{reference: head}``, read without spending.

    The freshness check asks this on the way to computing the very bit that decides whether the
    marker may be spent at all, so reading cannot consume it.

    A record written before this was head-bound holds a list or a bare ``True``. Either is read
    as vouching for *nothing* rather than for something: strict in the direction that costs one
    review rather than the one that skips it.
    """
    value = rec.get("rebase_round")
    if not isinstance(value, dict):
        return {}
    return {str(ref): str(head) for ref, head in value.items() if ref and head}


def take_rebase_round(rec: dict) -> dict[str, str]:
    """Spend the marker: one engine head move buys at most one look at the review bit."""
    moved = rebase_round(rec)
    rec.pop("rebase_round", None)
    return moved


#: How long an engine base merge whose result has not shown up yet stays confirmable (#2793).
_BRANCH_SYNC_KEPT = timedelta(hours=1)


def note_branch_sync(rec: dict, *, ref: str, before: str | None, now: datetime) -> None:
    """Record that the engine merged the base into ``ref`` at head ``before`` (#2793).

    No head is recorded as the engine's move here, whatever the read straight after the update
    shows. GitHub lands the merge a moment after accepting it, so that read can still show
    ``before``; and a head that has already changed may be an author's push that raced the
    merge. Either way the merge waits for ``_confirm_branch_syncs`` to bind it to GitHub's own
    merge commit on top of ``before``. An unknown ``before`` vouches for nothing.
    """
    before = str(before or "").strip().lower()
    if not before:
        return
    pending = {str(key): dict(value) for key, value in branch_syncs_pending(rec).items()}
    pending[str(ref)] = {"from": before, "at": now.astimezone(UTC).isoformat()}
    rec["branch_sync_pending"] = pending


def branch_syncs_pending(rec: dict) -> dict[str, dict]:
    """The engine's base merges still waiting to be seen, as ``{reference: {from, at}}``."""
    value = rec.get("branch_sync_pending")
    if not isinstance(value, dict):
        return {}
    return {
        str(ref): {"from": str(entry["from"]).lower(), "at": str(entry.get("at") or "")}
        for ref, entry in value.items()
        if isinstance(entry, dict) and entry.get("from")
    }


def branch_sync_expired(entry: dict, *, now: datetime) -> bool:
    """Has this pending merge waited too long to still speak for the head that follows it?"""
    try:
        at = datetime.fromisoformat(str(entry.get("at") or ""))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return now - at > _BRANCH_SYNC_KEPT


def settle_branch_sync(rec: dict, ref: str) -> None:
    pending = branch_syncs_pending(rec)
    pending.pop(str(ref), None)
    if pending:
        rec["branch_sync_pending"] = pending
    else:
        rec.pop("branch_sync_pending", None)


def note_verify_asked(rec: dict, heads: dict[str, str | None]) -> None:
    """Remember the head set the engine last asked a verify turn for (#2364).

    Read by the deck only: once that turn has ended with no green run of these heads, nothing
    will ask again, so the Work stops saying the deck is on it and asks a person instead.
    """
    rec["verify_asked"] = {str(ref): str(sha) for ref, sha in heads.items() if sha}


def arm_verify_round(rec: dict, *, round_id: str) -> None:
    """Note that the round being dispatched is the engine's final verification turn (#2253).

    Read by ``keep_rebase_round_for_run``: a verify round's head is never the engine's move, so
    its suite must not bind a rebase round's provenance.
    """
    if round_id:
        rec["verify_round_pending"] = {"round": str(round_id)}


def end_verify_round(rec: dict) -> None:
    """The verify round ended, however it ended. Not matched to the round id: a steer that
    landed mid-turn changes the id and still ends the round."""
    rec.pop("verify_round_pending", None)


def retire_verify_round(rec: dict) -> None:
    """Fresh work superseded the verify round, so drop its marker."""
    rec.pop("verify_round_pending", None)


def admits_verdict(rec: dict) -> bool:
    """May this card's current worker report a merge-review verdict?

    Only the armed ``merge-review`` turn a control comment started. A verdict from an ordinary
    worker would ship a diff no approval covers.
    """
    return rec.get("mode") == "merge-review"


def review_ready(rec: dict) -> bool:
    """Is an armed review waiting for its worker? (A control comment armed it.)"""
    return rec.get("mode") == "merge-review" and bool(rec.get("merge_control"))


def heads_unchanged(
    rec: dict,
    *,
    open_refs: set[str] | None,
    heads: dict[str, str | None],
    review_diffs: dict[str, str | None] | None = None,
    release_trees: dict[str, str | None] | None = None,
    rebase_moved: dict[str, str] | None = None,
) -> bool | None:
    """Is the reviewed release content still exactly what would be released?

    New snapshots identify the effective PR diff, so an unrelated base-branch change and
    the merge commit that syncs it do not manufacture a second review. A changed diff is
    always stale. Older snapshots retain their prospective-tree check for changed heads,
    and snapshots without either identity retain the former strict head-only behavior.

    ``None`` means freshness could not be verified; an unverifiable approval defers.
    """
    return _content_unchanged(
        rec.get("review_heads"),
        expected_trees=rec.get("review_trees"),
        expected_diffs=rec.get("review_diffs"),
        open_refs=open_refs,
        heads=heads,
        review_diffs=review_diffs,
        release_trees=release_trees,
        # Deliberately ungated, unlike the standing review pass (#2122). This is the approval
        # a human's `/merge review` bound to one diff, and #1436 settled what a moved head
        # costs it: a repair or a base sync whose whole push contributes no new line keeps
        # merging, because handing it back asks for a second review of an identical diff.
        # That rule was written for a different requester and is not this card's to reverse;
        # what *this* card loosened is the digest, and only the release bit reads it. Every
        # One provenance rule, both callers (#2122). This used to pass the live heads as their
        # own provenance, which made every push look engine-owned and turned #1436's narrow
        # exception — an engine repair or base sync that contributes no new line keeps merging
        # — into a blanket one. #1436 is kept by *recording* what the engine produced instead:
        # its base sync reads the resulting head back and names it, so a pass that survives two
        # syncs still survives, and a head the engine did not produce does not.
        rebase_moved=rebase_moved,
    )


def _content_unchanged(
    expected: object,
    *,
    expected_trees: object,
    expected_diffs: object,
    open_refs: set[str] | None,
    heads: dict[str, str | None],
    review_diffs: dict[str, str | None] | None = None,
    release_trees: dict[str, str | None] | None = None,
    rebase_moved: dict[str, str] | None = None,
) -> bool | None:
    """Compare one content snapshot against the live world (see ``heads_unchanged``).

    Split out from ``heads_unchanged`` because the release-passed bit (#2101) asks the very
    same question of a *different* snapshot: not "is the approved diff still current" but "is
    the reviewed diff still current". One implementation so the two can never disagree about
    what "the same content" means — which is the whole basis for carrying a clean review
    across a rebase that changed no line the pull request contributes.

    ``rebase_moved`` — the heads the engine itself produced, per reference — is what keeps that
    basis honest, and it is the difference between the two things a moved head can mean. It
    names heads and not references because a reference can move twice: the engine rebases A to
    H1 and an author then pushes H2 to the same A, and only the head tells those apart. Per
    reference as well as per head, because a card can ship several pull requests. The digest
    ignores *where* in the base a change sits, which is right for a rebase and wrong for a
    push: an author who moves an
    identical block to a different function has changed what the code does while contributing
    the same lines, and nothing else here would notice. A moved head therefore only gets the
    digest comparison when this wake knows the move came from the engine's own rebase round;
    any other new head is unreviewed, full stop. A head that has *not* moved is compared by
    digest regardless, because that is the base-moved-underneath case — the one #2122 is
    actually about — and no push happened at all.
    """
    if not isinstance(expected, dict) or not expected:
        return None
    if open_refs is None:
        return None
    if open_refs != set(expected):
        return False
    diffs = review_diffs or {}
    trees = release_trees or {}
    for ref, approved_head in expected.items():
        current = heads.get(ref)
        if not current:
            return None
        approved_diff = (
            expected_diffs.get(ref) if isinstance(expected_diffs, dict) else None
        )
        if approved_diff:
            if current != approved_head and (rebase_moved or {}).get(ref) != current:
                # Only the digest shortcut is gated. The legacy prospective-merge-tree
                # fallback below needs no provenance: a tree pins where every line sits as
                # well as what it says, so a relocation changes it.
                return False
            current_diff = diffs.get(ref)
            if not current_diff:
                return None
            if current_diff != approved_diff:
                return False
            continue
        if current == approved_head:
            continue
        approved_tree = expected_trees.get(ref) if isinstance(expected_trees, dict) else None
        if not approved_tree:
            return False
        current_tree = trees.get(ref)
        if not current_tree:
            return None
        if current_tree != approved_tree:
            return False
    return True


def consume_approval(rec: dict) -> None:
    """Spend the review approval — only after every release request has been accepted."""
    rec.pop("review_heads", None)
    rec.pop("review_diffs", None)
    rec.pop("review_trees", None)


def classify_verdict(
    verdict: str, *, heads_state: bool | None, has_pending: bool
) -> ReviewOutcome:
    """Decide a review verdict's meaning without changing anything.

    A ``pass`` releases only when nothing is queued *and* the approved diff is still
    current. Queued instructions supersede the approval: the requester has asked for
    more work since approving, so the card goes back to review rather than shipping.
    Separating the decision from the mutation lets a caller apply the outward change
    first and only commit the record once it landed.
    """
    if verdict == "pass" and not has_pending:
        if heads_state is None:
            return ReviewOutcome.DEFER
        if heads_state is True:
            return ReviewOutcome.RELEASE
    return ReviewOutcome.HANDBACK


def begin_merge(rec: dict, *, now: datetime) -> None:
    """Move an approved card from review into the release itself."""
    rec.update(
        mode="merge",
        status=IN_PROGRESS,
        started=True,
        started_at=now.astimezone(UTC).isoformat(),
    )


def handback(rec: dict, *, rests_in: str = IN_REVIEW) -> None:
    """Return the card to review: the release is off, and the approval is spent."""
    rec.pop("mode", None)
    rec.pop("fix", None)
    # The named release target belonged to the command that has just been answered (#1445).
    rec.pop("release_only", None)
    rec.pop("review_heads", None)
    rec.pop("review_diffs", None)
    rec.pop("review_trees", None)
    # A bits-driven release that handed back is over; the bits themselves survive, and are
    # re-read from the live world next wake like any other wake (#2101).
    rec.pop("auto_release", None)
    rec["status"] = rests_in


def beyond_recall(rec: dict) -> bool:
    """Is this release past the point where handing the card back can still undo it?

    A live merge watch, a live deploy watch, or a merge that already happened. Each one says
    the release got further than whatever is now asking to take it back, so the release is
    authoritative and the request is answered with words rather than with a hand-back.

    Named because two callers need the same question and must not drift: ``_handback`` refuses
    here, and a caller deciding whether its own failure was terminal or merely this wake's
    needs to tell those apart — a refusal for *this* reason is final, while a refusal because
    an effect did not land is worth another wake (#2108).
    """
    return bool(rec.get("awaiting_merge") or rec.get("awaiting_deploy") or rec.get("merged"))


# --- the review objection (#2108) ---

# One accepted `changes-needed` against a release the bits started is a small piece of work with
# its own life, not a branch of the command that raised it: the reviewer's word is spent only
# once the release is off *and* the card has been told. Writing it down as a record with a state
# is what makes it survive a wake — the command is handed back until the record is gone, and the
# release stands still for exactly as long as the record exists.
_OBJECTION = "review_objection"
# Take the release back: it is still stoppable, so the card goes back to review and nothing merges.
OBJECTION_RECALL = "recalling"
# Too late to stop: all that is left is to say so, and that still has to reach the card.
OBJECTION_NOTIFY = "too-late-notify"


def objection(rec: dict) -> dict | None:
    """The unfinished review objection on this card, or None when nothing is owed."""
    held = rec.get(_OBJECTION)
    return dict(held) if isinstance(held, dict) else None


def aim_objection(rec: dict, *, state: str, now: datetime) -> dict:
    """Write down what the objection still owes, keeping when it was first raised.

    The state is decided afresh on every wake rather than frozen at the first one, because a
    release can pass beyond recall between two attempts: an intent stuck on ``recalling`` would
    then ask for a hand-back that can no longer happen and never finish. What is durable is the
    obligation, not the plan for discharging it.
    """
    held = objection(rec) or {}
    since = str(held.get("since") or now.astimezone(UTC).isoformat())
    rec[_OBJECTION] = {"state": state, "since": since}
    return rec[_OBJECTION]


def settle_objection(rec: dict) -> None:
    """Nothing is owed any more: the release is off, or the card has been told it was too late."""
    rec.pop(_OBJECTION, None)


# --- blocker classification ---

def classify_pr(
    *,
    check_state: str,
    merge_state: str,
    failed_steps: list[str] | None = None,
    uptodate_step: str = UPTODATE_STEP,
    behind_by: int | None = None,
) -> PrBlocker:
    """Classify one linked PR's blocker from probed check + merge state.

    Mergeability is judged *before* the CI verdict: a branch behind its base fails
    the up-to-date check, so its red CI cannot be trusted until it updates. "Behind"
    surfaces three ways that can lag each other, so any one of them counts.

    ``behind_by`` is the branch comparison, and it is the only one of the three that works
    everywhere (#2101). ``merge_state == "BEHIND"`` requires branch protection that demands
    up-to-date branches, and ``uptodate_step`` requires a CI step; a repository with neither —
    this one, and most of the projects the release bits are switched on for — leaves both
    signals permanently silent. Passing ``None`` keeps exactly the former behaviour, so a
    caller that has not read the comparison is no worse off than before.
    """
    failed = list(failed_steps or [])
    if merge_state == "BEHIND" or uptodate_step in failed:
        return PrBlocker.BEHIND
    if behind_by is not None and behind_by > 0:
        return PrBlocker.BEHIND
    if merge_state == "DIRTY":
        return PrBlocker.DIRTY
    if check_state == "failing":
        if failed and all("lint" in step.lower() for step in failed):
            return PrBlocker.RED_LINT
        return PrBlocker.RED_CODE
    if check_state == "pending":
        return PrBlocker.PENDING
    return PrBlocker.READY


# Blockers the single merge-fix worker is asked to repair.
_REPAIRABLE = frozenset({PrBlocker.DIRTY, PrBlocker.RED_CODE, PrBlocker.RED_LINT})


def is_repairable(blocker: PrBlocker) -> bool:
    """Would ``plan_release`` spend a repair attempt on this blocker?

    Public because the merge watch asks the same question of a PR that went red after
    auto-merge was armed, and the two must never disagree about what a worker can fix.
    """
    return blocker in _REPAIRABLE


def plan_release(blockers: dict[str, PrBlocker]) -> ReleaseStep:
    """The next release step for a card with no repair in flight.

    Behind branches are updated first (their CI verdict is untrustworthy until then),
    then real blockers get the single repair attempt, then running checks are waited
    on. With every linked PR ready — or none open at all, everything already merged —
    the release arms native auto-merge and moves to watching.
    """
    values = list(blockers.values())
    if any(blocker is PrBlocker.BEHIND for blocker in values):
        return ReleaseStep.UPDATE_BEHIND
    if any(blocker in _REPAIRABLE for blocker in values):
        return ReleaseStep.REPAIR
    if any(blocker is PrBlocker.PENDING for blocker in values):
        return ReleaseStep.WAIT_CHECKS
    return ReleaseStep.ARM_MERGE


# --- the single repair attempt ---

def fix_keeps_merge(blockers: list[PrBlocker]) -> bool:
    """True when every blocker is lint-only, so a successful repair may keep merging."""
    return bool(blockers) and all(blocker is PrBlocker.RED_LINT for blocker in blockers)


def fix_may_hand_back(*, keep: bool) -> bool:
    """Could a successful repair on this release still need the approver's eyes?

    One predicate because two callers ask it: the dispatch comment, which tells the card
    up front what a code change will cost, and ``assess_fix``, which decides it once the
    worker has reported. A third exception must not be able to make the promise and the
    decision disagree. Only whether the worker changed the *diff* is left to ``assess_fix``,
    which is the one caller that can see it — so the dispatch comment promises "if this
    changes code", not "since", and a base-sync-only repair may still keep merging (#1436).
    """
    return not keep


def arm_fix(
    rec: dict,
    *,
    heads: dict[str, str | None],
    keep: bool,
    now: datetime,
    diffs: dict[str, str | None] | None = None,
) -> None:
    """Record the one repair attempt, with the content identities it started from.

    ``heads`` are the head SHAs and ``diffs`` the effective PR diffs. Both are snapshotted
    because they answer different questions afterwards: a moved head means the worker acted,
    an unchanged diff means it changed nothing the approver would read. Unknown diffs are
    dropped rather than stored as ``None``, so a ref with no snapshot falls back to the head
    fence instead of silently reading as "diff unchanged".
    """
    stamp = now.astimezone(UTC).isoformat()
    rec["fix"] = {"at": stamp, "keep": keep, "heads": dict(heads)}
    known = {ref: digest for ref, digest in (diffs or {}).items() if digest}
    if known:
        rec["fix"]["diffs"] = known
    rec["started_at"] = stamp


# --- the second attempt an armed merge that went red is entitled to ---

_POST_ARM_FIX = "post_arm_fix"


def post_arm_repair_available(rec: dict) -> bool:
    """Has this release already been rescued once after arming auto-merge?"""
    return not rec.get(_POST_ARM_FIX)


def arm_post_arm_repair(rec: dict, *, now: datetime) -> None:
    """Return an armed-then-broken release to assessment with a fresh repair attempt.

    Dropping ``awaiting_merge`` is what hands the card back to ``_assess_release``, which
    re-classifies from the live world and spends the attempt through the ordinary
    ``merge-fix`` spawn — so the budget gate, the durable delivery and the hand-back rules
    are the ones already in use, not a second copy of them. The pre-arm ``fix`` record goes
    with it: that attempt is spent, and this is the separate one arming earned. The flag
    stays on the record so this can happen only once per release, and ``started_at`` is
    reset so the fresh attempt gets the full wait rather than inheriting an expired one.
    """
    rec[_POST_ARM_FIX] = True
    rec.pop("fix", None)
    rec.pop("awaiting_merge", None)
    rec["started_at"] = now.astimezone(UTC).isoformat()


def park_head_moved(parked: object, heads: dict[str, str | None]) -> bool:
    """Has a pull request a release park was stamped with moved to a new head since (#2771)?

    A park is about the commit it parked on. A new push, a review of it and a green run on it
    are a different question, so the park's reason must not keep standing over them. Only a
    pull request known on both sides counts: a park with no heads (an older record, or a park
    with no open pull request) and a pull request whose head is unread say nothing.
    """
    was = parked.get("heads") if isinstance(parked, dict) else None
    if not isinstance(was, dict):
        return False
    return any(
        sha and heads.get(ref) and str(heads.get(ref)) != str(sha) for ref, sha in was.items()
    )


def clear_post_arm_repair(rec: dict) -> None:
    """A person starting a release again gets the post-arm attempt back with it."""
    rec.pop(_POST_ARM_FIX, None)


def _pushed(fix: dict, heads: dict[str, str | None], refs: list[str]) -> bool:
    original = fix.get("heads") or {}
    return any(heads.get(ref) != original.get(ref) for ref in refs)


def _changed_code(
    fix: dict, heads: dict[str, str | None], diffs: dict[str, str | None], refs: list[str]
) -> bool:
    """Did the repair change what the approver approved, rather than merely the head SHA?

    The PR's effective diff is the identity, the same one the ``/merge review`` freshness
    gate uses: a repair that only merges the current base moves the head without changing a
    line the PR contributes, and asking for a second review of an identical diff is exactly
    the round trip that gate exists to avoid (#1436). Heads remain the fence whenever the
    diff is unknown on either side, so an unreadable diff can only be stricter, never looser.
    """
    original = fix.get("heads") or {}
    before = fix.get("diffs")
    before = before if isinstance(before, dict) else {}
    for ref in refs:
        if heads.get(ref) == original.get(ref):
            continue
        approved, current = before.get(ref), diffs.get(ref)
        if approved and current and approved == current:
            continue
        return True
    return False


def assess_fix(
    fix: dict,
    *,
    blockers: dict[str, PrBlocker],
    heads: dict[str, str | None],
    diffs: dict[str, str | None] | None = None,
    worker_ended: bool = False,
) -> FixOutcome:
    """Decide what an in-flight repair attempt has achieved (see module docstring).

    ``blockers`` is this wake's classification for every open linked PR, ``heads`` their
    current head SHAs, so a push can be detected against the attempt's snapshot, and
    ``diffs`` their current effective diffs, so a push that changed no reviewable content
    can be told apart from one that did. ``worker_ended`` says the repair worker's turn is
    over: a worker that ended without pushing and left the PR red has finished too, and
    waiting out the time limit for a push that will never come only hides that (#2771).
    """
    behind = [ref for ref, blocker in blockers.items() if blocker is PrBlocker.BEHIND]
    if behind:
        return FixOutcome.UPDATE_BEHIND
    broken = [ref for ref, blocker in blockers.items() if blocker in _REPAIRABLE]
    if broken:
        # Pushed and still broken → the single attempt is spent; a human owns it now.
        if _pushed(fix, heads, broken):
            return FixOutcome.EXHAUSTED
        return FixOutcome.UNCHANGED if worker_ended else FixOutcome.WAITING
    if any(blocker is PrBlocker.PENDING for blocker in blockers.values()):
        return FixOutcome.WAITING
    if fix_may_hand_back(keep=bool(fix.get("keep"))) and _changed_code(
        fix, heads, diffs or {}, list(blockers)
    ):
        # Green again, but the worker actually changed code — that gets the approver's eyes.
        return FixOutcome.HANDBACK
    # Lint/update-only, a push that only synced the base, or nothing
    # pushed at all (transient red CI).
    return FixOutcome.KEEP_MERGING


# --- bounded waiting ---

def age_seconds(since: str | None, now: datetime) -> float | None:
    try:
        started = datetime.fromisoformat((since or "").replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (now.astimezone(UTC) - started).total_seconds()


def is_timed_out(
    since: str | None, now: datetime, *, limit_s: int = MERGE_TIMEOUT_SECONDS
) -> bool:
    """True when a wait has run past the limit. An unreadable timestamp never times out."""
    age = age_seconds(since, now)
    return age is not None and age > limit_s


# --- watching the armed merges and the production deploy ---

def begin_merge_watch(
    rec: dict, *, armed: list[str], premerged: dict[str, dict], now: datetime
) -> None:
    """Watch the PRs whose native auto-merge was armed; PRs merged earlier still deploy-gate."""
    rec["awaiting_merge"] = {
        "prs": list(armed),
        "premerged": dict(premerged),
        "since": now.astimezone(UTC).isoformat(),
    }


def begin_deploy_watch(
    rec: dict, *, builds: dict[str, dict], now: datetime, merged_at: str | None = None
) -> None:
    """Watch the production deploy, anchored at the merge rather than at this wake (#815).

    ``since`` is the instant a deploy of this merge could first be observed, and a release
    gate accepts evidence only from at/after it. Stamping it with ``now`` — the wake that
    *noticed* the merge — makes it up to one poll interval later than the merge itself, and
    a deploy that fires and finishes inside that gap leaves its only evidence in the past:
    the gate then waits out its whole grace window and reports a healthy deploy as failed.
    Two consecutive releases died that way. ``merged_at`` is GitHub's own merge timestamp,
    which is exactly the earliest moment a deploy of this merge can exist; it falls back to
    ``now`` when a port does not supply one, which is the behaviour this replaces.
    """
    rec.pop("awaiting_merge", None)
    rec["awaiting_deploy"] = {
        "builds": dict(builds),
        "since": _anchor(merged_at, now),
    }


def _anchor(merged_at: str | None, now: datetime) -> str:
    """``merged_at`` as an absolute instant, or ``now`` when it is absent or unreadable.

    Never later than ``now``: a clock skew that put the anchor in the future would widen
    exactly the gap this exists to close.
    """
    stamp = (merged_at or "").strip()
    if stamp:
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            if parsed <= now:
                return parsed.astimezone(UTC).isoformat()
    return now.astimezone(UTC).isoformat()


def gate_deploy(
    statuses: dict[str, str],
    *,
    since: str | None,
    now: datetime,
    limit_s: int = MERGE_TIMEOUT_SECONDS,
) -> DeployOutcome:
    """Gate the release on production deploys the deploy extension (#179) reports.

    ``statuses`` maps each watched repo to a normalized ``ok``/``bad``/``pending``
    state, so no CI vendor's vocabulary reaches the engine. Any bad deploy blocks
    immediately — the merge is already on the base branch, and a broken production
    deploy is a human's problem, not something to retry silently.
    """
    if any(status == "bad" for status in statuses.values()):
        return DeployOutcome.FAILED
    if all(status == "ok" for status in statuses.values()) and statuses:
        return DeployOutcome.DONE
    if is_timed_out(since, now, limit_s=limit_s):
        return DeployOutcome.TIMED_OUT
    return DeployOutcome.WAITING


# --- re-reading a park that the deploy itself has since undone (#2069) ---

# How long a release parked on a red production deploy stays worth re-reading. A deploy that
# recovers does so within hours, so this bound is generous; its only job is to stop the engine
# re-reading a card whose deploy is never coming back.
DEPLOY_RECHECK_LIMIT_S = 7 * 24 * 60 * 60


def begin_deploy_recheck(rec: dict, *, builds: dict[str, dict], now: datetime) -> None:
    """Keep a deploy-failed park re-readable, so a deploy that recovers is noticed (#2069).

    Parking is right, and this does not weaken it: a broken production deploy stops for a
    human, and the engine must never answer one by pushing more code at it. What was wrong is
    that the park was also *final*. The watch was dropped with everything else, so nothing ever
    looked again — and six cards whose merge went live and healthy within the hour sat open for
    days, because the only thing that had ever been wrong had fixed itself unobserved.

    Reading the same evidence a second time is not retrying. Nothing is merged, pushed or
    re-run; the card stays parked, visibly and with its reason, until the deploy gate says the
    deploy is good — which is the same sentence from the same gate that would have finished the
    release the first time round.

    ``builds`` carries the same commit identity the forward watch carries: repository to merge
    commit. There is deliberately no second, weaker way to ask. A re-read that could not name
    the commit would need the deploy gate to answer a different question — "did *something*
    deploy after this moment" — and a release that can only be proved live by that question is
    not proved live at all. The caller reconstructs the commit or does not start a re-read.
    """
    rec["deploy_recheck"] = {
        "builds": {drepo: dict(info) for drepo, info in builds.items()},
        "from": now.astimezone(UTC).isoformat(),
    }
    # One re-read per card, ever. The marker outlives the watch on purpose: without it, a
    # re-read that ran its course and cleared itself would look exactly like a card that had
    # never been offered one, and the next wake would start it over for good.
    rec["deploy_recheck_seeded"] = True


def deploy_recheck(rec: dict) -> dict | None:
    """The live deploy re-read on this record, if it still carries one."""
    watch = rec.get("deploy_recheck")
    return watch if isinstance(watch, dict) else None


def deploy_recheck_expired(
    rec: dict, now: datetime, *, limit_s: int = DEPLOY_RECHECK_LIMIT_S
) -> bool:
    """Has the re-read run long enough that this deploy is plainly not coming back?"""
    watch = deploy_recheck(rec) or {}
    return is_timed_out(watch.get("from"), now, limit_s=limit_s)


def end_deploy_recheck(rec: dict) -> None:
    """Stop re-reading: either the deploy recovered, or it never will."""
    rec.pop("deploy_recheck", None)


# --- durable terminal intents ---

# The steps each terminal transition performs, in order. Each is recorded as it lands so
# an interrupted wake resumes the transition instead of repeating a visible step.
DONE_STEPS = ("status", "comment", "closed")
BLOCKED_STEPS = ("reopened", "status", "comment")
# A release whose every pull request was a step of the card ends here instead (#2730): the
# issue stays open, the merged branch's worktrees go, and the worker is handed the card back.
CONTINUE_STEPS = ("comment", "worktrees", "status", "queued")


def arm_done(
    rec: dict,
    *,
    merged: list[str],
    deploy_links: list[str] | None = None,
    recovered: bool = False,
) -> dict:
    """Record the intent to finish before publishing it, so a dying wake resumes the transition.

    ``recovered`` marks the one finish that did not go straight through: a release that parked
    on a red production deploy and is finishing now because the deploy came good. The card's
    own history says the deploy failed, so the closing comment has to say what changed rather
    than reading as though nothing ever went wrong (#2069).

    Whether the finish closes the card or continues it is decided here, once, from the release
    set ``note_release_set`` recorded (#2730), and kept on the intent so a wake that resumes it
    cannot change its mind.
    """
    step = bool(merged) and bool(rec.get(_RELEASE_STEP))
    intent = rec.setdefault(
        "done_pending",
        {
            "merged": list(merged),
            "deploy_links": list(deploy_links or []),
            "recovered": bool(recovered),
            "step": step,
            "steps": dict.fromkeys(CONTINUE_STEPS if step else DONE_STEPS, False),
        },
    )
    return intent


# --- step pull requests: merge one part of a card and keep working (#2730) ---
#
# A card normally ships one pull request and its merge is the end of the card. Some work cannot
# be done that way: the next part needs the first one merged, or live, before it can even be
# written. Such a pull request says so in its body (``RELEASE_STEP_MARKER``) and refers to its
# card without a closing keyword. It is reviewed, verified and released exactly like any other,
# and only the end differs: the card is handed back to its worker instead of closed.

# The release set under way, when every pull request in it is a step; absent otherwise.
# Rewritten by each assessment, so it always describes the set the release is actually merging,
# including a step a person merged by hand before the release armed the rest.
_RELEASE_STEP = "release_step"
# The step pull requests an earlier release of this card already merged and continued past —
# and, on a card a person reopened, everything its closing release merged (#2914). Discovery
# drops them, so the next release neither waits on them, re-watches their deploys, nor reads
# them as "already merged, so finish" or as an unmarked merge that makes a step release final.
_STEPS_MERGED = "steps_merged"


def note_release_set(rec: dict, *, refs: list[str], steps: list[str]) -> None:
    """Record whether the release set ``refs`` consists of step pull requests only."""
    if refs and set(refs) <= set(steps):
        rec[_RELEASE_STEP] = sorted(set(refs))
    else:
        rec.pop(_RELEASE_STEP, None)


def is_step_finish(intent: dict) -> bool:
    """Does this finish continue the card rather than close it?"""
    return bool(intent.get("step"))


def steps_merged(rec: dict) -> list[str]:
    """Every step pull request this card has merged and continued past, oldest first."""
    value = rec.get(_STEPS_MERGED)
    return [str(ref) for ref in value] if isinstance(value, list) else []


def note_steps_merged(rec: dict, refs: list[str]) -> None:
    """Add ``refs`` to what this card has merged and moved past, in order, without repeats."""
    done = steps_merged(rec)
    added = [str(ref) for ref in dict.fromkeys(refs) if str(ref) not in done]
    if added:
        rec[_STEPS_MERGED] = done + added


def end_step_release(rec: dict, *, merged: list[str]) -> None:
    """Close the release a step finished, keeping the card and the record of what merged.

    Everything that belonged to this release goes — its approval, its review pass and green
    run (those were facts about a head that is now on the base branch), its watches and its
    published bits — because the next part of the card is new work and must earn all of them
    again. The card itself, its lease and its chat stay.
    """
    released = rec.get(_RELEASE_STEP)
    note_steps_merged(rec, [*merged, *(released if isinstance(released, list) else ())])
    # Resting in review, not in progress: the dispatcher passes over an in-progress card, and
    # the worker's next turn is what moves it on.
    handback(rec, rests_in=IN_REVIEW)
    for field in (
        "awaiting_merge", "awaiting_deploy", "merged", _RELEASE_STEP, "review_prs",
        "started_at", "merge_control", "release_blocked", _STEP_RELEASE_STARTED,
    ):
        rec.pop(field, None)
    end_deploy_recheck(rec)
    clear_review_pass(rec)
    clear_tests_pass(rec)
    clear_bits(rec)
    clear_post_arm_repair(rec)
    clear_finalization(rec, "done_pending")


# The merged step pull requests a hand-merge release was started for (#2730): one fact keyed by
# the pull requests themselves, so the same merges are never released twice.
_STEP_RELEASE_STARTED = "step_release_started"


def step_release_started(rec: dict) -> list[str]:
    """The merged set a hand-merge release was last started for, sorted; empty when none."""
    value = rec.get(_STEP_RELEASE_STARTED)
    return sorted(str(ref) for ref in value) if isinstance(value, list) else []


def note_step_release_started(rec: dict, merged: list[str]) -> None:
    """Record that a release was started for exactly these merged step pull requests."""
    rec[_STEP_RELEASE_STARTED] = sorted(merged)


def without_steps_merged(rec: dict | None, merged: list[str]) -> list[str]:
    """``merged`` less the step pull requests this card has already continued past."""
    done = set(steps_merged(rec or {}))
    return [ref for ref in merged if ref not in done]


def arm_blocked(rec: dict, why: str, *, merged: list[str] | None = None) -> dict:
    """Record the intent to block, keeping whatever was already merged visible in the reason."""
    intent = rec.setdefault(
        "blocked_pending",
        {
            "why": why,
            "merged": list(merged or []),
            "steps": dict.fromkeys(BLOCKED_STEPS, False),
        },
    )
    return intent


def needs_step(intent: dict, step: str) -> bool:
    """Has this step of the terminal transition still not landed?"""
    return not (intent.get("steps") or {}).get(step)


def mark_step(intent: dict, step: str) -> None:
    """Record that a step of the terminal transition landed and must not repeat."""
    intent.setdefault("steps", {})[step] = True


def pending_finalization(rec: dict) -> str | None:
    """Which durable transition an interrupted wake must resume first, if any."""
    for field in ("done_pending", "blocked_pending", "merge_start_pending"):
        if rec.get(field):
            return field
    return None


def clear_finalization(rec: dict, field: str) -> None:
    """Drop a resumed intent once its visible transition actually landed."""
    rec.pop(field, None)


# --- the three release bits, and the merge they add up to (#2101) ---
#
# A card with an open ready pull request is waiting on exactly three things, and until now
# only one of them was ever written down. The pull request has to sit on current master, a
# reviewer has to have passed the diff, and the suite has to have run green on the exact
# commit that would merge. Each was knowable, none was kept, so a card that had satisfied all
# three still waited for a human to type `/merge` or for the reviewer to come round again.
#
# Keeping them makes the wait states visible and lets two of them close themselves: behind
# master is merged up by the engine (a conflict arms a merge-and-rerun turn, #2687), and all
# three green arms the release. The bits are
# deliberately *not* a second release state machine. They decide only when to enter the one
# that already exists, through the same approval binding a human's `/merge review` writes, so
# every guard past that point — repair, hand-back, the armed-merge watch, the deploy gate and
# the durable terminal intents — is the code already in use rather than a parallel copy.
#
# Two of the three are derived fresh every wake and never stored: whether a pull request is
# behind is this wake's `classify_pr`, and whether the reviewed diff is still current is
# `heads_unchanged` over the stored pass. Only the facts a later wake cannot re-derive are
# persisted — the reviewed content, and which commits a suite has run green on.

# Where the reviewer's clean verdict is kept, with the content it covered. Not the approval
# (`review_heads`), which a release spends: this outlives the release attempt, because a pass
# is a fact about a diff and stays true for as long as that diff is what would merge.
_REVIEW_PASS = "release_review_pass"

# The commits a worker has reported a green suite run on. A set rather than a single SHA
# because a card may ship several pull requests, and one worker's run proves one tree.
_TESTS_PASS = "release_tests_pass"

# How many green commits to remember per card. Only the current heads are ever asked about,
# so this needs to cover a short run of rebases, not the card's whole history.
_TESTS_PASS_LIMIT = 20

# This wake's reading of the three bits, kept so a reader outside the engine can see what the
# merge decision currently sees (#2121). Deliberately not one of the persisted *facts* above:
# nothing here is an input to a later wake, which recomputes all three from the live world
# before it decides anything. It is a published reading, and stale by construction the moment
# a head moves — which is why it carries the heads it was computed against, and why it is
# absent rather than wrong on any card the bits did not get to read.
_BITS = "release_bits"


# What makes a pull request *not* current with its base. Conflicting belongs here beside
# behind, and the card says why: "a conflicted rebase is the same worker turn, just with
# conflict resolution in it". Leaving `DIRTY` out let a conflicted pull request read as
# rebase-current, so a card whose other two bits were green skipped the rebase-and-rerun turn
# entirely and went into the merge path carrying a tests bit that proved a tree nobody had
# rebased. The repair worker would then be asked to fix it without the rerun the bit needs.
_NOT_REBASE_CURRENT = frozenset({PrBlocker.BEHIND, PrBlocker.DIRTY})


class ReleaseBit(Enum):
    REBASE_CURRENT = "rebase-current"  # every open PR sits on current base, conflict-free
    REVIEW_PASSED = "review-passed"    # a clean verdict covers the diff that would merge
    TESTS_PASS = "tests-pass"          # a worker ran the suite green on the exact head


def record_review_pass(
    rec: dict,
    *,
    heads: dict[str, str | None],
    diffs: dict[str, str | None] | None = None,
    trees: dict[str, str | None] | None = None,
    now: datetime,
) -> bool:
    """Keep a clean verdict as a standing fact about the content it covered.

    Recorded whenever a verdict passes, including on the wake that releases: the release may
    still hand back, park on a red deploy, or be superseded, and each of those returns a card
    whose diff a reviewer genuinely did pass. Without this the pass died with the attempt and
    the next round paid for a second review of a diff nobody had changed.

    A head nobody could read is not recorded at all. Storing it as unknown would make the
    freshness check defer forever on a snapshot that can never be satisfied, where dropping
    the record simply asks for the review again — strictness in the safe direction.

    **Every reference must carry a content digest — a diff or a legacy tree — or nothing is
    recorded.** A snapshot of bare heads is not a weaker version of this fact, it is a
    different and wrong one: ``_content_unchanged`` would then fall back to ``current ==
    approved_head``, and treating an unmoved head as proof of unchanged content is exactly
    what this feature refuses everywhere else, because a pull request's effective diff moves
    when its base does. Digests can go missing for ordinary reasons — a failed ``gh pr diff``,
    a quiet wake handing back empty values — so this is a live path, not a theoretical one.
    Refusing costs one more review; recording would silently merge on head equality.
    """
    known = {ref: str(sha) for ref, sha in heads.items() if sha}
    if not known or len(known) != len(heads):
        return False
    kept_diffs = {ref: digest for ref, digest in (diffs or {}).items() if digest}
    kept_trees = {ref: tree for ref, tree in (trees or {}).items() if tree}
    if any(ref not in kept_diffs and ref not in kept_trees for ref in known):
        return False
    snapshot: dict = {"heads": known, "at": now.astimezone(UTC).isoformat()}
    if kept_diffs:
        snapshot["diffs"] = kept_diffs
    if kept_trees:
        snapshot["trees"] = kept_trees
    rec[_REVIEW_PASS] = snapshot
    return True


def record_reviewed_ref(
    rec: dict,
    *,
    ref: str,
    head: str,
    open_refs: set[str],
    diff: str | None = None,
    tree: str | None = None,
    by: str = "",
    now: datetime,
) -> bool:
    """Fold an outside reviewer's clean verdict on one pull request into the pass (#2108).

    The engine's own merge-review verdict covers every open pull request at once, so it writes
    the whole snapshot. An outside reviewer reads one pull request, so its verdict arrives one
    at a time and is folded in beside whatever else already stands. A card with two open pull
    requests therefore needs a verdict for each before the bit can go green, which is what
    ``review_pass_fresh`` already insists on and not a second rule.

    Refuses a verdict with nothing to key it to: no commit, or a pull request whose content
    GitHub would not give us. A pass recorded without the diff it covered could never be judged
    stale, so it would be a standing green bit nothing could ever turn red.

    ``open_refs`` prunes pull requests that have since closed. Without that a merged pull
    request's entry would sit in the snapshot forever and hold the bit red against its
    successor, because the freshness check wants the recorded set to be exactly the open one.
    """
    sha = str(head or "").strip()
    if not ref or not sha or not (diff or tree) or ref not in open_refs:
        return False
    previous = review_pass(rec) or {}
    stamp = now.astimezone(UTC).isoformat()

    def _kept(field: str) -> dict[str, str]:
        held = previous.get(field)
        if not isinstance(held, dict):
            return {}
        return {k: str(v) for k, v in held.items() if v and k in open_refs and k != ref}

    heads, diffs, trees = _kept("heads"), _kept("diffs"), _kept("trees")
    heads[ref] = sha
    if diff:
        diffs[ref] = str(diff)
    else:
        trees[ref] = str(tree)
    held_sources = previous.get("sources")
    sources = {
        k: dict(v)
        for k, v in (held_sources if isinstance(held_sources, dict) else {}).items()
        if isinstance(v, dict) and k in open_refs and k != ref
    }
    # Who said it and when, beside the content it covered. Nothing reads this to decide
    # anything — the digest does that — but a pass that turns out to be wrong is only
    # unarguable if the record says who claimed what.
    sources[ref] = {"by": str(by or ""), "head": sha, "at": stamp}
    snapshot: dict = {"heads": heads, "at": stamp, "sources": sources}
    if diffs:
        snapshot["diffs"] = diffs
    if trees:
        snapshot["trees"] = trees
    rec[_REVIEW_PASS] = snapshot
    return True


def clear_review_pass(rec: dict) -> None:
    """Forget the pass: a reviewer has since said this needs changes."""
    rec.pop(_REVIEW_PASS, None)


def review_pass(rec: dict) -> dict | None:
    """The standing clean verdict on this card, if it carries one."""
    snapshot = rec.get(_REVIEW_PASS)
    return snapshot if isinstance(snapshot, dict) else None


def review_pass_fresh(
    rec: dict,
    *,
    open_refs: set[str] | None,
    heads: dict[str, str | None],
    review_diffs: dict[str, str | None] | None = None,
    release_trees: dict[str, str | None] | None = None,
    rebase_moved: dict[str, str] | None = None,
) -> bool | None:
    """Does the standing clean verdict still cover what would merge right now?

    This is the stale-base carry-over the reviewer side shipped on 2026-09-21: a pass at a head
    master has since moved past stays a pass, as long as the diff the pull request contributes
    is unchanged. Rebasing changes the head and changes nothing anybody reviewed.

    What "unchanged" means is the port's to decide, not this function's, and it is not the
    bytes of ``gh pr diff``: those move on any rebase onto a base that touched the same file.
    ``GithubPort.review_diffs`` digests the contribution instead (#2122).
    """
    snapshot = review_pass(rec)
    if snapshot is None:
        return False
    return _content_unchanged(
        snapshot.get("heads"),
        expected_trees=snapshot.get("trees"),
        expected_diffs=snapshot.get("diffs"),
        open_refs=open_refs,
        heads=heads,
        review_diffs=review_diffs,
        release_trees=release_trees,
        rebase_moved=rebase_moved,
    )


def record_tests_pass(rec: dict, *, head: str, now: datetime) -> bool:
    """Remember that a worker ran the repository-selected suite green on this exact commit.

    Keyed by commit and nothing else. A suite run proves one tree, so a head that moves for any
    reason — a fix, a rebase, a force-push — simply is not in the set, and the bit goes red
    without anything having to notice *why* it moved. That is the property the issue asks for
    ("any head change without a green report clears the bit") and it costs no invalidation
    logic at all.
    """
    sha = str(head or "").strip()
    if not sha:
        return False
    kept = tests_pass(rec)
    kept[sha] = now.astimezone(UTC).isoformat()
    if len(kept) > _TESTS_PASS_LIMIT:
        oldest = sorted(kept.items(), key=lambda item: item[1])[: len(kept) - _TESTS_PASS_LIMIT]
        for stale, _ in oldest:
            kept.pop(stale, None)
    rec[_TESTS_PASS] = kept
    return True


def tests_pass(rec: dict) -> dict[str, str]:
    """The commits this card has a green suite run on, newest-agnostic, as {sha: when}."""
    kept = rec.get(_TESTS_PASS)
    if not isinstance(kept, dict):
        return {}
    return {str(sha): str(at) for sha, at in kept.items() if sha}


def tests_pass_current(rec: dict, *, heads: dict[str, str | None]) -> bool:
    """Does the green-suite bit still cover every one of these heads? (#2101)

    Asked twice on purpose: once when the bits decide to release, and again immediately
    before the merge effect. The head can move in between — a behind branch being brought up
    to date does exactly that — and the release announces to the card that the suite ran green
    on this very commit, so the claim has to be true at the moment it is acted on and not only
    at the moment it was made. An unreadable head is not current, like everywhere else here.
    """
    if not heads:
        return False
    green = set(tests_pass(rec))
    return all(bool(sha) and str(sha) in green for sha in heads.values())


def untested_heads(
    rec: dict,
    *,
    heads: dict[str, str | None],
    review_only: frozenset[str] | set[str] = frozenset(),
) -> dict[str, str]:
    """The open pull requests whose current head no green run covers yet, as {ref: sha} (#2718).

    The tests bit wants every head, and a verification job credits only the head of the
    repository its ``--cwd`` is in. On a card that ships pull requests in two repositories the
    bit can therefore stay red on a companion head nobody ran, while the card's own head is
    green. This names which, so a worker and a person are told rather than left to read the
    bit. An unreadable head is left out: it is no head to run a suite on. So is a pull request
    in a ``review_only`` repository (#2729): no suite exists for it, and its review pass is
    what credits it, so naming it would send a worker to run tests that are not there.
    """
    green = set(tests_pass(rec))
    return {
        str(ref): str(sha)
        for ref, sha in sorted(heads.items())
        if sha and str(sha) not in green and _ref_repo(str(ref)) not in review_only
    }


def _ref_repo(ref: str) -> str:
    """The repository id of a ``repo#number`` reference."""
    return ref.split("#", 1)[0]


def credit_reviewed_heads(
    rec: dict,
    *,
    heads: dict[str, str | None],
    review_only: frozenset[str] | set[str],
    now: datetime,
) -> list[str]:
    """Credit the tests bit on each ``review_only`` head the review pass covers (#2729).

    The caller has just read the review bit green on exactly these heads, so for a repository
    with no tests the pass is the whole proof. Crediting the head itself, rather than skipping
    it in the bit, keeps every later reader honest for free: the merge-time check and the
    deck read the same set of green commits, and a head that moves is simply not in it.
    """
    credited = []
    green = set(tests_pass(rec))
    for ref, sha in sorted(heads.items()):
        if not sha or _ref_repo(str(ref)) not in review_only or str(sha) in green:
            continue
        if record_tests_pass(rec, head=str(sha), now=now):
            green.add(str(sha))
            credited.append(str(ref))
    return credited


def revoke_tests_pass(rec: dict, *, head: str) -> bool:
    """Take one commit's green run back (#2364): CI that passed it now says otherwise.

    Only a CI reading ever calls this. A verification job proves a tree once and for all, but
    CI on one commit can be re-run, and a red or pending re-run is the current answer.
    """
    kept = tests_pass(rec)
    if kept.pop(str(head or "").strip(), None) is None:
        return False
    if kept:
        rec[_TESTS_PASS] = kept
    else:
        rec.pop(_TESTS_PASS, None)
    return True


def clear_tests_pass(rec: dict) -> None:
    """Forget every green run — a fresh round of work has to prove itself again."""
    rec.pop(_TESTS_PASS, None)


def release_bits(
    rec: dict,
    *,
    blockers: dict[str, PrBlocker] | None,
    open_refs: set[str] | None,
    heads: dict[str, str | None],
    review_diffs: dict[str, str | None] | None = None,
    release_trees: dict[str, str | None] | None = None,
) -> dict[ReleaseBit, bool | None]:
    """Read all three release bits for one card against this wake's live world.

    ``None`` on a bit means "could not be established this wake" and is never treated as
    green; it is the same unverifiable-defers rule the approval freshness check follows. A
    card with no open pull request has nothing to release, so every bit reads ``None`` rather
    than vacuously green — an empty set of pull requests must never satisfy a merge gate.
    """
    if not open_refs or blockers is None:
        return dict.fromkeys(ReleaseBit, None)
    missing = [ref for ref in open_refs if ref not in blockers or not heads.get(ref)]
    if missing:
        return dict.fromkeys(ReleaseBit, None)
    green_shas = set(tests_pass(rec))
    return {
        ReleaseBit.REBASE_CURRENT: not any(
            blockers[ref] in _NOT_REBASE_CURRENT for ref in open_refs
        ),
        ReleaseBit.REVIEW_PASSED: review_pass_fresh(
            rec,
            open_refs=open_refs,
            heads=heads,
            review_diffs=review_diffs,
            release_trees=release_trees,
            # The card's own marker is the provenance, and it names the exact heads the
            # engine produced: one of those may reuse the reviewed digest, nothing else may.
            rebase_moved=rebase_round(rec),
        ),
        ReleaseBit.TESTS_PASS: all(str(heads[ref]) in green_shas for ref in open_refs),
    }


def bits_all_green(bits: dict[ReleaseBit, bool | None]) -> bool:
    """True only when all three bits are positively green — unknown is never green."""
    return all(bits.get(bit) is True for bit in ReleaseBit)


def record_bits(
    rec: dict,
    *,
    bits: dict[ReleaseBit, bool | None],
    heads: dict[str, str | None],
    now: datetime,
    review_only: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Publish this wake's reading of the three bits, with the heads it read them against.

    The deck's row pill reads this (#2121). It cannot re-derive the review bit itself: a pass
    carries across a rebase by diff identity, and the digests that establishes come from
    GitHub reads the engine has already paid for and a row must never make. So the engine
    hands over what it worked out rather than making every reader work it out again.

    The heads travel with it because a reading is only about the commits it read. A reader
    that finds its own head missing here is looking at a reading from before that head
    existed, and must treat the bits as saying nothing rather than as saying yes.
    """
    reading: dict = {
        "bits": {bit.value: bits.get(bit) for bit in ReleaseBit},
        "heads": {ref: sha for ref, sha in heads.items() if sha},
        "at": now.astimezone(UTC).isoformat(),
    }
    if bits.get(ReleaseBit.TESTS_PASS) is False:
        # Which heads hold the tests bit red (#2718): the deck names them on the Work, so a
        # card waiting on a companion pull request's run says so rather than only "red".
        reading["untested"] = untested_heads(rec, heads=heads, review_only=review_only)
    rec[_BITS] = reading


def clear_bits(rec: dict) -> None:
    """Drop the published reading for a card the bits no longer decide anything about.

    Absent says "nobody is judging this" and a stale reading would say "still green", so
    every path that establishes the card is out of scope clears rather than leaves it.
    """
    rec.pop(_BITS, None)


def recorded_bits(rec: dict) -> dict | None:
    """The last published reading, or None when there is not one to read."""
    value = rec.get(_BITS)
    return value if isinstance(value, dict) else None


def bits_summary(bits: dict[ReleaseBit, bool | None]) -> str:
    """The three bits as one readable line, for the cycle log and for a card comment."""
    marks = {True: "green", False: "red", None: "unknown"}
    return ", ".join(f"{bit.value}={marks[bits.get(bit)]}" for bit in ReleaseBit)


def wants_auto_release(
    rec: dict,
    *,
    live_status: str,
    owned: bool,
    resting_statuses: tuple[str, ...] = (IN_REVIEW,),
) -> bool:
    """Is this card free for the bits to decide anything about it? (Everything free to check.)

    The linked-pull-request read and the check probe are the caller's to perform on the few
    cards that get this far. Idleness is the same
    narrow door every release uses: no worker, no queued instruction, no release already under
    way. A card whose bits are green while somebody is still working on it is a card whose
    work is not finished, and merging it would take the decision off them.
    """
    return bool(
        owned
        and live_status in resting_statuses
        and not rec.get("mode")
        and not is_busy(rec)
        and not rec.get("dispatch")
        and not rec.get("dispatch_failed")
        and not rec.get("stopping")
    )


def arm_auto_release(
    rec: dict,
    *,
    heads: dict[str, str | None],
    review_diffs: dict[str, str | None] | None = None,
    release_trees: dict[str, str | None] | None = None,
    now: datetime,
) -> None:
    """Enter the ordinary guarded release with the reviewed diff as the approval (#2101).

    The approval written here is the reviewer's own pass, bound to the exact content that pass
    covered — so this is a `/merge review` that came back clean, with the bits standing in for
    the person who would have typed it. Binding it to a diff is deliberate and is the whole
    safety margin: a repair that changes code during the release hands back for another
    review, because nothing here approved a process, only a diff.

    Nothing about the merge itself is new. The record enters ``mode="merge"`` and the next
    assessment classifies, repairs, arms native auto-merge, watches and gates the deploy
    exactly as it does for a typed command.
    """
    arm_review(
        rec,
        heads={ref: str(sha) for ref, sha in heads.items() if sha},
        review_diffs=review_diffs,
        release_trees=release_trees,
        control_id="",
        now=now,
    )
    rec.pop("merge_control", None)
    rec["auto_release"] = True
    begin_merge(rec, now=now)
    rec["merge_start_pending"] = True
