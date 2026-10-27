"""Generation-bound control operations exposed to a dispatched worker.

Workers do not write lifecycle markers or edit the board directly.  They present the opaque
generation from their dispatch to this interface, which validates the lease while holding the
Poller Instance transaction lock and sends every outward write through the instance's approved
``EnginePort``.  Only ``finish``, ``review``, ``reopen`` and ``start-implementation`` are
lifecycle (#2155, #2522, #2716): each
validates its own authority atomically, so a worker is never told to check the lease first — a
check that could go stale the moment it returned.  Ordinary comments use normal GitHub tooling
(#2152).
``ready-for-review`` is not lifecycle: it offers the pull request's head to the outside reviewer
while the worker keeps its turn and goes on to verify that head (#2350).

The lock deliberately spans validation and the external effect.  A wake cannot invalidate
the generation between those steps.  ``finish`` additionally re-reads the live card after
posting its terminal marker and before moving the board, so a label removal or close in that
narrow external window leaves the marker as durable evidence but gives the stale worker no
status write.

Nothing here stops a worker. A stopped or superseded generation is refused, and the persistent
Runtime ends the provider process itself (#2159): a worker never has to acknowledge its own
stop, and a dead one could not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from . import (
    blockers,
    chat_answers,
    cycle,
    deck_releases,
    leases,
    queue,
    ready_review,
    reconcile,
    release,
    rounds,
    stop,
)
from .config import FINISH_ROLES, Role

# The marker prefixes come from the adapter that parses them back out of a comment, so a worker's
# report and the engine's reader cannot drift into two different strings.
from .github import (
    REOPEN_REQUEST_MARKER,
    REVIEW_MARKER,
    TERMINAL_MARKER,
    newest_terminal_generation,
    reopen_request_ids,
)
from .ports import CardQuery, CommentBody, Effect, EffectKind, EnginePort
from .reconcile import CardSnapshot
from .state import StateStore

# The endings a worker may report, each one a token from the shared marker vocabulary
# (`config.FINISH_ROLES`). The vocabulary is shared because the adapter has to read a marker
# back as the same role this command wrote it as; the subset is local because "may be reported"
# and "may be recovered" are different questions — `todo` is only ever the rest a map's
# ``resolved`` becomes, never something a worker asks for.
# How long ``ready-for-review --head`` waits for GitHub to show a push it has not shown yet
# (#2698): GitHub moves a pull request's head seconds after the push, and a worker must not be
# told to come back "in a minute" — it cannot sleep, so it ends its turn with nothing offered.
_HEAD_RETRIES = 2
_HEAD_RETRY_S = 5.0

_FINISH_STATUSES = {
    "in_review": FINISH_ROLES["in_review"],
    "needs_more_info": FINISH_ROLES["needs_more_info"],
    "blocked": FINISH_ROLES["blocked"],
    # The one ending nothing automatic ever chooses (#1465). A worker reports it only when the
    # card or its requester explicitly asked for the work to wait on a person, and the column
    # exists to say *that* rather than the several other things Needs More Info already means.
    # On a board with no such column it falls back to Needs More Info below, so a project that
    # has not made the column loses nothing and no status write goes to an unbound option.
    "human_review_pending": FINISH_ROLES["human_review_pending"],
    # Done, with nothing to merge (#1410). Every other ending hands the card to the release
    # progression, which looks for a linked pull request and finds none when the work was an
    # issue edit, such as an answered question. Reporting *In review*
    # for that would rest the card in a column whose whole meaning is "waiting to be released",
    # so this says the honest thing instead: the work landed where it was going to land, and the
    # ordinary Done path releases the label and the worktree from here.
    "resolved": FINISH_ROLES["resolved"],
}

_STATUS_TOKENS = {display: token for token, display in _FINISH_STATUSES.items()}
_REVIEW_VERDICTS = frozenset({"pass", "changes-needed"})

# Why a generation could not be proved against the live card (#1651). One string per cause,
# because a worker that cannot tell them apart treats all of them as terminal — including the
# two that mean "ask again in a minute". The `disabled:` ones are permanent for this worker,
# `stale:` means another generation has the card, and `unavailable:` means nothing about the
# lease at all: the card simply could not be read this second.
_NO_RECORD = (
    "disabled: this poller instance has no record of that card; check you are addressing "
    "the instance that dispatched you"
)
# A worker hearing any of these just stops. There is nothing to acknowledge: the Runtime ends
# the provider process itself and proves it to the engine (#2159).
#
# Except that the "new worker" a revive hands to is usually this same chat: the revive is a
# continuation, delivered as the chat's next turn (#2529). By the time this refusal can be heard
# that turn has landed, so a worker reading it in a chat that has since been given a newer
# dispatch must carry on there rather than drop finished work.
_HANDED_OFF = (
    "disabled: this generation's work was handed to a new worker after a turn ended without a "
    "report. If this chat has since received a newer dispatch for this card, that worker is "
    "you: finish with that dispatch's control command. Otherwise stop and leave the work where "
    "it is"
)
_STOP_THIS_GENERATION = "disabled: stop requested for this generation; stop now"
_STOP_OTHER_GENERATION = (
    "disabled: a stop is in progress for a different generation; stop now"
)
_SUPERSEDED = "stale: this generation has been superseded; stop now"
_READ_DEFERRED = (
    "unavailable: the card read was deferred by the shared GitHub query reserve — "
    "the lease is not stale; wait and retry"
)
_READ_FAILED = (
    "unavailable: the card could not be read right now — the lease is not stale; "
    "wait and retry"
)
_LABEL_REMOVED = (
    "disabled: the card's ownership label was removed; stop and leave the work where it is"
)
_CARD_CLOSED = "disabled: the card is closed and no longer active"
# `reopen` on a card it cannot take back in place and cannot queue for either (#2522, #2845):
# another Run reported on it since, or this instance cannot leave a request. The deck no longer
# takes board commands from agents (#3025), so the person posts the `/claude` themselves.
_RELEASED = (
    "disabled: this card cannot be reopened from here — another Run has reported on it since "
    "your finish, or this Poller cannot take the request; ask the person to post `/claude "
    "<what they asked>` on the issue themselves, which reopens it and starts a worker"
)
# `reopen` of a card the engine already merged, closed or forgot (#2845): the request is queued
# exactly as a deck `/claude` would be, and the wake reopens the issue and dispatches it.
_REOPEN_QUEUED = (
    "queued: the Poller reopens the issue and hands the work back on its next wake, normally "
    "into this chat. This Run no longer holds the card, so end your turn now with no finish"
)
# A queued `reopen` is for a closed card (#2845 D2). An open one the Poller no longer holds was
# reopened by somebody, and its next step is theirs to start; the person posts the `/claude`.
_OPEN_UNHELD = (
    "disabled: this card is open but no longer yours to reopen; ask the person to post "
    "`/claude <what they asked>` on the issue themselves"
)
# A project whose Works the deck runs (#2895, ADR 0008) settles every command on a card no Poller
# card holds as cancelled, so a queued `reopen` there was dropped while the worker waited for it
# (#3316). Said at once instead, with the step that does work.
_DECK_RUNS_WORKS = (
    "refused: the deck runs this project's Works, so its Poller drops a queued reopen and never "
    "takes this card back. Reopen the issue yourself (`gh issue reopen <number> -R <owner/repo>`) "
    "with a comment saying what the person asked, and carry on in this chat"
)
_REOPEN_RETRY = "unavailable: {}; run the same reopen again — it is safe to repeat"
_START_RETRY = (
    "unavailable: {}; run the same start-implementation again — it is safe to repeat"
)
_HELD = "refused: this card already has its next step decided ({}); leave it to that"
# A card whose next step is already decided is not taken back: a dispatch or a queued instruction
# starts a worker anyway, and a stop or a release owns the card. Unlike a chat-answer resume,
# ``processing`` does not hold it — those are this Run's own commands, still carried until the
# wake that reads its finish, and they simply stay with the Run that carries on.
_REOPEN_HELD_BY = (
    "dispatch", "pending", "stopping", "mode", "release_pending", "forget_pending",
    "recovery_action",
)


@dataclass(frozen=True)
class ControlResult:
    """A worker operation's explicit, fail-closed result."""

    ok: bool
    reason: str


@dataclass(frozen=True)
class LeaseCheck:
    """A proved live card, or the specific reason this generation could not prove one.

    ``transient`` marks the answers that are not a verdict about the lease: the live card read
    was deferred or failed, so the generation may be perfectly good and the worker should wait
    and retry instead of abandoning its turn. The write is refused either way.
    """

    snapshot: CardSnapshot | None = None
    reason: str = ""
    transient: bool = False

    @property
    def ok(self) -> bool:
        return self.snapshot is not None


def _last_failure(port: object) -> tuple[str, bool]:
    """What GitHub answered the port's last failed call, and whether it looked transient.

    Read off the real port's `Gh` (#2714); a port without one reports nothing, so the caller
    keeps its plain message and never repeats a write it cannot judge.
    """
    api = getattr(port, "api", None)
    said = getattr(api, "last_failure", "")
    transient = getattr(api, "last_failure_transient", False)
    return (said if isinstance(said, str) else ""), transient is True


class WorkerControl:
    """Small provider-neutral worker API over one Poller Instance."""

    def __init__(
        self,
        store: StateStore,
        port: EnginePort,
        *,
        human_review_pending: str | None = None,
        spec_only_label: str = "",
        parallel_review: bool = False,
        review_only_repos: frozenset[str] = frozenset(),
        works_router: bool = False,
        clock=None,
        sleep=None,
    ) -> None:
        self.store = store
        self.port = port
        # Repositories with no tests (#2729): their heads are credited by the review, so the
        # worker is never told to run a verification job on one.
        self.review_only_repos = frozenset(review_only_repos)
        # Whether this project reviews a head while its worker verifies it (#2350): an outside
        # reviewer and the poller merging on its release bits. Without both, the review is
        # itself a worker turn, which cannot run while this worker holds the card.
        self.parallel_review = parallel_review
        # The deck runs this project's Works (#2895): its Poller acts on no released card.
        self.works_router = works_router
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        # The optional *Human review pending* column, but only when this board actually has
        # one (#1465). Resolved once from the activation binding and never guessed: a status
        # write to an unbound role fails, which would leave a worker holding a landed terminal
        # marker and a board that never moved.
        self.human_review_pending = human_review_pending
        self.spec_only_label = spec_only_label

    def _spec_only_refusal(self) -> str:
        label = self.spec_only_label or "spec-only"
        return (
            f"disabled: the `{label}` label permits a specification only, so no in_review, "
            "pull request or review verdict; once a person approves the spec, remove the "
            "label and finish needs_more_info --spec-approved, and the poller starts the "
            "implementation "
            "worker by itself — or, when they asked you in your chat to build it, run "
            "start-implementation and build it yourself"
        )

    @staticmethod
    def _record(state: dict, repo: str, number: int) -> dict | None:
        rec = (state.get("issues") or {}).get(f"{repo}#{int(number)}")
        return rec if isinstance(rec, dict) else None

    def _lease(
        self, state: dict, repo: str, number: int, generation: str, *, allow_closed: bool = False
    ) -> LeaseCheck:
        """Prove this generation against the live card, or say exactly why it could not.

        Every refusal here is fail-closed and always was.  What it now also carries is *which*
        of the several unrelated situations it is (#1651): a superseded lease, a stop, a card
        this instance has never heard of, a safe-stop label removal, and — the two that are not
        a verdict about the lease at all — a live card read that the shared GraphQL reserve
        deferred or that simply failed.  A worker told ``disabled or stale generation`` for a
        deferred read reads it as terminal and throws away a finished turn, which is what
        happened on #1646.

        ``allow_closed`` is ``reopen``'s alone: a closed, finished card is exactly the one it
        takes back, and it proves for itself that this lease is what closed it (#2522).
        """
        rec = self._record(state, repo, number)
        if rec is None:
            return LeaseCheck(reason=_NO_RECORD)
        if _handed_off(rec, generation):
            return LeaseCheck(reason=_HANDED_OFF)
        if rec.get("stopping"):
            if generation and generation == stop.stopped_generation(rec):
                return LeaseCheck(reason=_STOP_THIS_GENERATION)
            return LeaseCheck(reason=_STOP_OTHER_GENERATION)
        # The current lease, or the one an outstanding continuation successor has just taken
        # over from. The second case is not a hole in the fence: the successor was minted for
        # the worker already on this card and has not been delivered to anyone, so refusing
        # here would only kill a turn that is still doing the work (#1801).
        if not leases.holds_or_outgoing(rec, generation):
            return LeaseCheck(reason=_SUPERSEDED)
        # One card, but through the batched port every other reader uses: a live read here must
        # cost and defer exactly like a wake's, and a deferred GraphQL reserve returns no cards.
        key = f"{repo}#{int(number)}"
        batch = self.port.snapshot_cards(
            {
                key: CardQuery(
                    repo,
                    int(number),
                    generation=generation,
                    stop_generation=stop.stopped_generation(rec),
                    stop_requested_at=str(rec.get("stop_requested_at") or ""),
                )
            }
        )
        snapshot = batch.cards.get(key)
        if snapshot is None:
            # The generation matched the record; only the live confirmation is missing. The
            # port distinguishes the two ways that happens: a reserve that deferred the whole
            # batch before spending a point, and a chunk that was attempted and failed. Both
            # are transient and self-healing, so the worker waits and retries rather than
            # concluding its lease is dead.
            return LeaseCheck(
                reason=_READ_DEFERRED if batch.deferred else _READ_FAILED,
                transient=True,
            )
        # A card whose ownership label has *gone* proves no live lease. Closing alone is
        # different: GitHub auto-closes an issue when its linked PR merges, which can happen
        # while a worker is still finishing its turn. Reconciliation deliberately preserves that
        # active lease and worktree, so the worker must still be able to report against it.
        # Removing the ownership label remains the explicit safe-stop boundary.
        #
        # Gone, not merely absent (#2053): the engine no longer writes the label onto the issue
        # when it dispatches, so a card started by a `/claude` comment never wore one and its
        # worker must not be refused for that. `label_claim` is what reconciliation recorded
        # from the live snapshots — the label really was seen on this card — so the refusal
        # still fires the moment a person takes it off.
        if not snapshot.owned and rec.get("label_claim"):
            return LeaseCheck(reason=_LABEL_REMOVED)
        # A card parked in Needs More Info under this lease is waiting on its own chat: a person
        # answering there resumes it, and may well close the issue while they do. Refusing that
        # worker's report as "no longer active" left the card with nothing able to finish it
        # (#2822); the lease itself was proven above.
        active = rec.get("status") in {"In progress", Role.NEEDS_INFO.value} or bool(
            rec.get("processing") or rec.get("mode")
        )
        if not (snapshot.open or active or allow_closed):
            return LeaseCheck(reason=_CARD_CLOSED)
        return LeaseCheck(snapshot=snapshot)

    def enabled(self, repo: str, number: int, generation: str) -> ControlResult:
        """Diagnostic: whether this worker still holds the live card's exact generation.

        No brief requires it (#2155). It is not a fence — the answer can be stale the moment it
        returns — and every lifecycle operation proves the lease again for itself.
        """
        with self.store.transaction() as state:
            check = self._lease(state, repo, number, generation)
        return ControlResult(check.ok, "enabled" if check.ok else check.reason)

    def _marker_retry(
        self,
        state: dict,
        repo: str,
        number: int,
        generation: str,
        canonical: str,
        marker: Effect,
    ) -> str:
        """After a terminal marker write failed: ``""`` when it is on the issue now, else why not.

        Posted once more only when GitHub's answer looked like a hiccup — no answer, a 5xx, an
        empty body, a server-side GraphQL error — and only after a fresh read shows the marker
        is not there (#2714). A failed write is not proof nothing landed, so that read is what
        keeps the repeat from posting the report twice; a 4xx is GitHub's considered answer
        and is never repeated. Either way the reason carries what GitHub actually said.
        """
        said, transient = _last_failure(self.port)
        why = "terminal marker did not land" + (f" — {said}" if said else "")
        if not transient:
            return why
        check = self._lease(state, repo, number, generation)
        if check.snapshot is None:
            if check.transient:
                return f"{why}; run the same finish again in a while"
            return f"{why}; {check.reason}"
        if check.snapshot.terminal_marker == canonical:
            return ""
        if self.port.apply(marker):
            return ""
        said, _transient = _last_failure(self.port)
        return (
            "terminal marker did not land on a second attempt either"
            + (f" — {said}" if said else "")
            + "; run the same finish again in a while"
        )

    def finish(
        self,
        repo: str,
        number: int,
        generation: str,
        status: str,
        body: str,
        blocked_by: tuple[str, ...] | list[str] = (),
        spec_approved: bool = False,
    ) -> ControlResult:
        """Post a terminal marker first, then move the board if the lease remains live.

        ``blocked_by`` names the issues or pull requests that have to land before this card
        can go any further (#1230). It only means anything alongside ``blocked``, and it is
        refused rather than ignored elsewhere: a worker that reports ``in_review`` while
        naming a dependency has misread its own outcome, and silently dropping the refs would
        leave the card resting with nothing arranged to wake it.

        ``spec_approved`` is a spec worker's word that a person approved its spec (#2773). On a
        spec card only the spec worker's ``needs_more_info`` finish decides whether the build
        starts; see ``cycle.settle_spec_finish``.
        """
        canonical = _FINISH_STATUSES.get(status.strip().lower().replace(" ", "_"))
        if canonical is None:
            return ControlResult(
                False,
                "finish status must be in_review, needs_more_info, human_review_pending, "
                "blocked, or resolved",
            )
        fallback = (
            canonical == Role.HUMAN_REVIEW_PENDING.value and self.human_review_pending is None
        )
        if fallback:
            # No such column on this board: park it where a person is already looked for.
            canonical = Role.NEEDS_INFO.value
        supplied = [str(value).strip() for value in blocked_by or () if str(value).strip()]
        if supplied and canonical != Role.BLOCKED.value:
            return ControlResult(False, "--blocked-by is only meaningful with finish blocked")
        refs = blockers.parse_refs(
            supplied,
            # The organization is the adapter's, not the protocol's: only a real GitHub port
            # knows it, and a bare ``repo#12`` is normalized against it.
            org=str(getattr(self.port, "org", "") or ""),
            repo=repo,
            number=int(number),
        )
        if spec_approved and canonical != Role.NEEDS_INFO.value:
            return ControlResult(False, "--spec-approved is only meaningful with needs_more_info")
        if supplied and not refs:
            return ControlResult(
                False,
                "--blocked-by must name an issue or pull request, as #12, repo#12, "
                "owner/repo#12, or a GitHub URL",
            )
        with self.store.transaction() as state:
            check = self._lease(state, repo, number, generation)
            snapshot = check.snapshot
            if snapshot is None:
                return ControlResult(False, check.reason)
            # ``resolved`` builds nothing, so a spec-only card may still close when a person
            # asks for it (#2661); only an implementation result is refused.
            if snapshot.spec_only and canonical == Role.IN_REVIEW.value:
                return ControlResult(False, self._spec_only_refusal())
            # ``resolved`` is the one ending that asserts there is nothing left to merge, so no
            # pull request will ever carry a ``Closes`` keyword for this card and nothing else
            # closes the issue. It stays open, keeps its ownership label, and is eligible on the
            # next poll — one card was investigated three times before a person
            # closed it by hand (#1581). Closing it here makes the consequence structural instead
            # of a step each worker has to remember.
            closes_issue = canonical == Role.DONE.value
            token = _STATUS_TOKENS[canonical]
            rendered = _with_marker(body, f"{TERMINAL_MARKER}{generation} status={token} -->")
            rec = self._record(state, repo, number)
            if canonical == Role.NEEDS_INFO.value and rec is not None:
                refusal = cycle.settle_spec_finish(
                    rec, generation, approved=spec_approved, label_on=snapshot.spec_only
                )
                if refusal:
                    return ControlResult(False, refusal)
            if canonical == Role.IN_REVIEW.value and rec and release.admits_verdict(rec):
                return ControlResult(
                    False,
                    "active merge review requires review pass or changes-needed, "
                    "not finish in_review",
                )
            # Recorded before the marker, and again on the repeat path below, so a worker whose
            # first attempt landed the comment and lost the board write still leaves the hold
            # behind. A hold from an earlier round is dropped by any other ending: the card is
            # no longer waiting on it, and a stale one would wake the card for nothing.
            if rec is not None:
                blockers.record_resume(rec, refs)
                # What the Needs More Info park stands in for, so a reader can tell a review
                # waiting for a person from a question (#2235). The next dispatch drops it.
                if fallback:
                    rec["parked_as"] = "human_review_pending"
                else:
                    rec.pop("parked_as", None)
            # A person answered in this chat and the wake has not resumed the card yet (#2499).
            # Resume it here, so this report lands as a new one instead of reading as a repeat
            # of the Needs More Info marker the answer was replying to.
            answered = cycle.resume_for_finish(
                state,
                self.port,
                self.store,
                key=f"{repo}#{int(number)}",
                generation=generation,
                now=self._clock(),
            )
            if answered == "resumed":
                # The resume comment is now the newest marker for this lease: unreported.
                snapshot = replace(snapshot, status=Role.IN_PROGRESS.value, terminal_marker=None)
            if rec is not None:
                # After the answers above, which resume only a parked card (#2887).
                cycle.unpark_prompt_for_finish(rec, generation)
            if snapshot.status == canonical and snapshot.terminal_marker == canonical:
                if answered == "waiting":
                    # Not success: this body would be thrown away, and the card would resume
                    # under a turn that already looks finished.
                    return ControlResult(
                        False,
                        "unavailable: an answer typed into this chat is not on the issue yet, "
                        "so this report would be dropped as a repeat; run the same finish "
                        "again in a minute",
                    )
                # This generation's marker is on the issue, so the round happened — even if the
                # attempt that landed it never reached the counting below, and even if
                # reconciliation is what finished the card. Counting is keyed by generation, so
                # arriving here second is free (#1840).
                if rec is not None:
                    rounds.count_round(rec, generation)
                    _adopt_parked(rec, generation, canonical)
                # The repeat path closes too. An earlier attempt that landed the marker and the
                # board write but lost the close would otherwise report success forever while
                # leaving the card exactly as eligible as before.
                if closes_issue and rec is not None:
                    _stamp_resolved(rec, generation, self._clock())
                if closes_issue and snapshot.open and not self._close_issue(repo, number):
                    return ControlResult(False, "already finished; issue close deferred")
                return ControlResult(True, f"already finished -> {canonical}")
            if snapshot.terminal_marker != canonical:
                marker = Effect(
                    EffectKind.COMMENT,
                    repo,
                    int(number),
                    CommentBody.from_record(rendered, self._record(state, repo, number)),
                )
                if not self.port.apply(marker):
                    missing = self._marker_retry(state, repo, number, generation, canonical, marker)
                    if missing:
                        return ControlResult(False, missing)
                # The marker is the report. Everything below this line can fail without the
                # round having failed, so the count happens here rather than after the board
                # write it used to hang off (#1840).
                if rec is not None:
                    rounds.count_round(rec, generation)
                # The Poller Instance generation cannot move while this transaction holds its
                # lock, but the issue can be closed or its ownership label removed on GitHub.
                # Re-read it before granting the worker the board transition.
                recheck = self._lease(state, repo, number, generation)
                if not recheck.ok:
                    # The marker is durable evidence either way. When the re-read merely could
                    # not happen, say so and point at the idempotent repeat path, so the worker
                    # finishes the card on a later attempt instead of leaving it half-reported.
                    retry = (
                        " Run the same finish again in a while; the marker is already posted "
                        "and the repeat is idempotent."
                        if recheck.transient
                        else ""
                    )
                    return ControlResult(
                        False,
                        "terminal marker landed, board transition refused — "
                        f"{recheck.reason}.{retry}",
                    )
            # Reached with the marker already up when an earlier attempt landed it and lost the
            # board write. That attempt counted the round; this one is the free repeat.
            if rec is not None:
                rounds.count_round(rec, generation)
            if not self.port.apply(
                Effect(EffectKind.SET_STATUS, repo, int(number), canonical)
            ):
                return ControlResult(False, "terminal marker landed; board transition deferred")
            if rec is not None:
                _adopt_parked(rec, generation, canonical)
                if closes_issue:
                    _stamp_resolved(rec, generation, self._clock())
            # Last, so a refused close leaves a card that is finished everywhere a reader looks
            # and merely still open. The worker retries ``finish``, takes the repeat path above,
            # and closes it then — the same order the release progression uses.
            if closes_issue and snapshot.open and not self._close_issue(repo, number):
                return ControlResult(False, "terminal marker landed; issue close deferred")
        return ControlResult(True, f"finished -> {canonical}")

    def _close_issue(self, repo: str, number: int) -> bool:
        """Close a card the worker finished with nothing left to merge (#1581)."""
        return self.port.apply(Effect(EffectKind.CLOSE_ISSUE, repo, int(number)))

    def reopen(self, repo: str, number: int, generation: str, body: str) -> ControlResult:
        """Take back this lease's own ``resolved`` finish and carry on in the same chat (#2522).

        A ``resolved`` report closes the card, and the person reading it in the worker's chat
        often asks for more. Nothing let the worker put the card back: reopening the issue by
        hand left a Done card that the next wake released. This is the chat-answer resume
        (#2348) for that ending — the same lease, no new dispatch and no slot — so the worker's
        later ``finish`` is an ordinary report.

        In place only for the lease that wrote the ``resolved`` and within ``REOPEN_HOLD`` of it.
        Any other finish of this Run — an ``in_review`` the Poller merged, a card already released,
        a hold that ran out — is queued as the person's ``/claude`` instead (``_reopen_by_request``,
        #2845). Each step is checked against the live card first, so a repeat after a partial
        failure posts nothing twice. The order is the approved one (#2522 D3): the comment, carrying
        the lease-bound ``resumed`` marker that makes the finish marker no longer the newest; then
        the issue; then the board. A wake that lands between the last two finds an open Done card
        whose newest marker is that ``resumed``, and reconciliation holds it for the repeat rather
        than releasing it.
        """
        if not (body or "").strip():
            return ControlResult(
                False, "refused: say in the body what the person asked you to carry on with"
            )
        with self.store.transaction() as state:
            rec = self._record(state, repo, number)
            if rec is None:
                # Released: merged and closed, or past the hold. Nothing is left to take back
                # in place, but the person's ask still stands (#2845).
                return self._reopen_by_request(state, None, repo, number, generation, body)
            if not leases.holds(rec, generation):
                # Not this card's lease any more: `_lease` says why in its own words. One it
                # still lets act — the Run an undelivered continuation replaces — has its next
                # step decided by that continuation.
                check = self._lease(state, repo, number, generation)
                return ControlResult(False, check.reason or _HELD.format("dispatch"))
            held = [name for name in _REOPEN_HELD_BY if rec.get(name)]
            if held:
                return ControlResult(False, _HELD.format(", ".join(held)))
            stamp = rec.get("resolved_finish")
            if (
                not isinstance(stamp, dict)
                or stamp.get("generation") != generation
                or not reconcile.reopen_held(rec, now=self._clock())
            ):
                # Not a `resolved` of this Run inside its hold — an `in_review` the Poller then
                # merged, say (#2841). Any finish of this Run can be carried on (#2845 D3).
                return self._reopen_by_request(state, rec, repo, number, generation, body)
            check = self._lease(state, repo, number, generation, allow_closed=True)
            snapshot = check.snapshot
            if snapshot is None:
                return ControlResult(False, check.reason)
            if snapshot.terminal_marker is not None:
                rendered = _with_marker(body, chat_answers.resume_marker(generation))
                if not self.port.apply(
                    Effect(
                        EffectKind.COMMENT,
                        repo,
                        int(number),
                        CommentBody.from_record(rendered, rec),
                    )
                ):
                    return ControlResult(False, _REOPEN_RETRY.format("the comment did not land"))
            if not snapshot.open and not self.port.apply(
                Effect(EffectKind.REOPEN_ISSUE, repo, int(number))
            ):
                return ControlResult(False, _REOPEN_RETRY.format("the issue did not reopen"))
            if snapshot.status != Role.IN_PROGRESS.value and not self.port.apply(
                Effect(EffectKind.SET_STATUS, repo, int(number), Role.IN_PROGRESS.value)
            ):
                return ControlResult(False, _REOPEN_RETRY.format("the board did not move"))
            rec["status"] = Role.IN_PROGRESS.value
            if rec.get("reportless_resolved") == generation:
                # It would make this Run's own next ending read as already handled.
                rec.pop("reportless_resolved", None)
            rec.pop("parked_as", None)
            rec.pop("resolved_finish", None)
        return ControlResult(
            True,
            "reopened -> In progress. Carry on in this chat and end with finish as usual",
        )

    def _reopen_by_request(
        self,
        state: dict,
        rec: dict | None,
        repo: str,
        number: int,
        generation: str,
        body: str,
    ) -> ControlResult:
        """Queue the person's ask as a deck ``/claude`` for a card not taken back in place (#2845).

        ``reopen`` in place needs this Run's lease and a ``resolved`` inside its hold. A card the
        Poller merged, or one already released, has neither, and before this the worker had to ask
        the person a second time for a board command they had in effect just given. The request
        is a deck ``/claude`` request file, so the wake reopens the issue and dispatches it
        exactly as it does a person's comment (#2748).

        Allowed only for a closed issue (#2845 D2) while this Run wrote its newest terminal
        comment, read from the issue's whole comment history: that proves it was the last to
        report without the record a release deletes, and refuses once another Run or its stop
        has spoken. The request id is fixed per Run and card, so a repeat queues nothing twice.
        The body is posted first, with a marker carrying that id, so the ask is on the issue and a
        retry after a request that could not be written finds the note and only writes.
        """
        if self.works_router:
            return ControlResult(False, _DECK_RUNS_WORKS)
        paths = getattr(self.store, "paths", None)
        reader = getattr(self.port, "issue_comments", None)
        if paths is None or reader is None or not generation:
            return ControlResult(False, _RELEASED)
        key = f"{repo}#{int(number)}"
        request_id = f"reopen-{generation}-{int(number)}"
        if (
            deck_releases.ID_PREFIX + request_id in queue.controls(state)
            or deck_releases.request_path(paths.state, request_id).exists()
        ):
            return ControlResult(True, _REOPEN_QUEUED)
        batch = self.port.snapshot_cards(
            {key: CardQuery(repo, int(number), generation=generation)}
        )
        snapshot = batch.cards.get(key)
        if snapshot is None:
            return ControlResult(False, _READ_DEFERRED if batch.deferred else _READ_FAILED)
        if snapshot.open:
            return ControlResult(False, _OPEN_UNHELD)
        comments = reader(repo, int(number))
        if comments is None:
            return ControlResult(False, _READ_FAILED)
        if newest_terminal_generation(comments) != generation:
            return ControlResult(False, _RELEASED)
        # The instruction is the ask's first line (#2845 D1); the whole body goes on the issue
        # in the note below (D8), where the next worker reads it with the rest of the card.
        first_line = next(line.strip() for line in body.splitlines() if line.strip())
        command = f"/claude {first_line}"
        if not deck_releases.accepts(command):
            return ControlResult(
                False,
                "refused: the body cannot be sent as a `/claude` instruction; say in plain words "
                "what the person asked",
            )
        if request_id not in reopen_request_ids(comments):
            note = (
                "**Reopen asked in the worker's chat.** The Poller reopens this issue and hands "
                f"the work back on its next wake.\n\n{body.strip()}\n\n"
                f"{REOPEN_REQUEST_MARKER}{request_id} -->"
            )
            if not self.port.apply(
                Effect(EffectKind.COMMENT, repo, int(number), CommentBody.from_record(note, rec))
            ):
                return ControlResult(False, _REOPEN_RETRY.format("the comment did not land"))
        try:
            deck_releases.write(
                paths.state,
                request_id=request_id,
                key=key,
                command=command,
                by="worker-reopen",
                now=self._clock(),
            )
        except (deck_releases.RequestError, OSError) as exc:
            return ControlResult(
                False, _REOPEN_RETRY.format(f"the request could not be written ({exc})")
            )
        return ControlResult(True, _REOPEN_QUEUED)

    def start_implementation(
        self, repo: str, number: int, generation: str, body: str
    ) -> ControlResult:
        """Let a spec worker build the spec a person approved in its own chat (#2716).

        Removing the spec-only label after a spec makes the poller start a new worker once the
        card rests in Needs More Info (#2706). When the person approves *and* asks this worker
        to build, that new worker would only rediscover what this chat already holds. So this
        posts ``body`` — who approved and what they said — with the lease-bound ``resumed``
        marker, moves the card to In progress under this same generation, and drops the
        approval mark, so no wake starts a second worker for it. The worker's later ``finish``
        is an ordinary implementation report.

        The label must already be off, and only the Run that wrote the spec may take it over. A
        repeat after a partial failure posts nothing twice.
        """
        if not (body or "").strip():
            return ControlResult(
                False,
                "refused: say in the body who approved the spec and what they asked for",
            )
        key = f"{repo}#{int(number)}"
        with self.store.transaction() as state:
            check = self._lease(state, repo, number, generation)
            snapshot = check.snapshot
            if snapshot is None:
                return ControlResult(False, check.reason)
            rec = self._record(state, repo, number)
            if rec is None:
                return ControlResult(False, _NO_RECORD)
            label = self.spec_only_label or "spec-only"
            if snapshot.spec_only:
                return ControlResult(
                    False,
                    f"refused: remove the `{label}` label first, and only on a person's "
                    "explicit approval",
                )
            if not cycle.was_spec_card(rec, generation):
                return ControlResult(
                    False,
                    f"refused: this Run did not write a spec under the `{label}` label; "
                    "there is nothing to take over",
                )
            held = [name for name in _REOPEN_HELD_BY if rec.get(name)]
            if held:
                return ControlResult(False, _HELD.format(", ".join(held)))
            # The person's approval is an answer in this chat, which a wake may not have posted
            # yet; post it first, exactly as ``finish`` does, so it is not left behind.
            answered = cycle.resume_for_finish(
                state, self.port, self.store, key=key, generation=generation, now=self._clock()
            )
            if answered == "waiting":
                return ControlResult(
                    False,
                    _START_RETRY.format("an answer typed into this chat is not on the issue yet"),
                )
            # A resume here moved only the record: ``resume_for_finish`` leaves the board to its
            # caller, so the snapshot's live column still decides the write below (#2777).
            if rec.get(cycle._SPEC_BUILD_IN_CHAT) != generation:
                rendered = _with_marker(body, chat_answers.resume_marker(generation))
                if not self.port.apply(
                    Effect(
                        EffectKind.COMMENT,
                        repo,
                        int(number),
                        CommentBody.from_record(rendered, rec),
                    )
                ):
                    return ControlResult(
                        False, _START_RETRY.format("the approval comment did not land")
                    )
                cycle.take_spec_build(rec, generation)
            if snapshot.status != Role.IN_PROGRESS.value and not self.port.apply(
                Effect(EffectKind.SET_STATUS, repo, int(number), Role.IN_PROGRESS.value)
            ):
                return ControlResult(False, _START_RETRY.format("the board did not move"))
            rec["status"] = Role.IN_PROGRESS.value
            if rec.get("reportless_resolved") == generation:
                rec.pop("reportless_resolved", None)
            rec.pop("parked_as", None)
        return ControlResult(
            True,
            "started -> In progress. Build the approved spec in this chat and end with finish "
            "as usual; no other worker will be started for it",
        )


    def ready_for_review(
        self, repo: str, number: int, generation: str, head: str = ""
    ) -> ControlResult:
        """Offer this card's open pull request heads for review, and keep the turn (#2350).

        The worker goes on to verify the same head. If the review comes back not passing, the
        poller cancels that verification and sends the findings into this chat; the rejected
        heads can never be offered again, so the worker has to push a fix first. ``head`` is an
        optional assertion: the commit the worker believes it pushed. When no open pull request
        is at it the answer is ``unavailable``, not an offer, because GitHub moves a pull
        request's head a little after the push lands; offering then would offer the previous
        head, or answer "already rejected" about a fix the worker did push (#2623). It reads
        again twice, a few seconds apart, before saying so (#2698).
        """
        if not self.parallel_review:
            return ControlResult(
                False,
                "refused: this project reviews a head after its worker finishes, not beside "
                "it; verify as the brief says and report finish in_review",
            )
        asserted = head.strip().lower()
        for attempt in range(_HEAD_RETRIES + 1):
            if attempt:
                # Outside the state lock, so the wait never stalls the poller's own wake.
                self._sleep(_HEAD_RETRY_S)
            outcome = self._offer_once(repo, number, generation, asserted)
            if outcome is not None:
                break
        else:
            return ControlResult(
                False,
                f"unavailable: GitHub still shows the previous head of this card's pull "
                f"request, not your push of {asserted[:12]}, after about "
                f"{int(_HEAD_RETRIES * _HEAD_RETRY_S)} s; submit the verification job, then "
                "offer once more before you end your turn",
            )
        if isinstance(outcome, ControlResult):
            return outcome
        answer, heads, untested = outcome
        listed = ", ".join(f"{ref}@{sha[:12]}" for ref, sha in sorted(heads.items()))
        if answer == "refused":
            return ControlResult(
                False,
                f"refused: the review already rejected {listed}; do not verify it. Fix the "
                "findings, push a new head, and offer that",
            )
        if answer == "already":
            return ControlResult(
                True, f"already offered for review: {listed}{_still_untested(heads, untested)}"
            )
        if len(heads) == 1:
            return ControlResult(
                True,
                f"offered for review: {listed}. Now run the verification job on this head",
            )
        return ControlResult(
            True, f"offered for review: {listed}{_still_untested(heads, untested)}"
        )

    def _offer_once(
        self, repo: str, number: int, generation: str, asserted: str
    ) -> ControlResult | tuple[str, dict[str, str], dict[str, str]] | None:
        """One proved read-and-offer: a refusal, ``(answer, heads, untested)``, or ``None``
        when no open pull request is at the ``asserted`` head yet. Each attempt proves the lease
        again, so a wait between attempts can never offer on behalf of a lease that ended during
        it."""
        with self.store.transaction() as state:
            check = self._lease(state, repo, number, generation)
            snapshot = check.snapshot
            if snapshot is None:
                return ControlResult(False, check.reason)
            if snapshot.spec_only:
                return ControlResult(False, self._spec_only_refusal())
            rec = self._record(state, repo, number)
            if rec is None or rec.get("status") != "In progress" or rec.get("mode"):
                return ControlResult(
                    False,
                    "refused: ready-for-review is for a worker still doing its own work on the "
                    "card; this card is not in that state",
                )
            linked = self.port.linked_prs(repo, int(number))
            if linked is None:
                return ControlResult(
                    False,
                    "unavailable: this card's pull requests could not be read right now; "
                    "wait and retry",
                )
            heads = {ref: sha for ref, sha in linked.open.items() if sha}
            if not heads or len(heads) != len(linked.open):
                why = (
                    "an open pull request's head could not be read; wait and retry"
                    if linked.open
                    else "no ready pull request links this card — open it (not as a draft) "
                    "with a closing reference first"
                )
                prefix = "unavailable" if linked.open else "refused"
                return ControlResult(False, f"{prefix}: {why}")
            if asserted and not any(sha.lower().startswith(asserted) for sha in heads.values()):
                return None
            answer = ready_review.offer(
                rec, heads=heads, generation=generation, now=self._clock()
            )
            untested = release.untested_heads(
                rec, heads=dict(heads), review_only=self.review_only_repos
            )
        return answer, heads, untested

    def review(
        self,
        repo: str,
        number: int,
        generation: str,
        verdict: str,
        body: str,
    ) -> ControlResult:
        """Post a generation-bound merge-review verdict for the cycle to consume.

        Only the armed ``merge-review`` turn may report one.
        """
        verdict = verdict.strip().lower()
        if verdict not in _REVIEW_VERDICTS:
            return ControlResult(False, "review verdict must be pass or changes-needed")
        rendered = _with_marker(body, f"{REVIEW_MARKER}{generation} verdict={verdict} -->")
        with self.store.transaction() as state:
            rec = self._record(state, repo, number)
            # A lease that cannot be proved is reported as itself. Folding it into "no active
            # merge review" was the same mistake in miniature: a reviewer whose card read was
            # deferred was told its review had gone away (#1651).
            check = self._lease(state, repo, number, generation)
            if not check.ok:
                return ControlResult(False, check.reason)
            if check.snapshot and check.snapshot.spec_only:
                return ControlResult(False, self._spec_only_refusal())
            if rec is None:  # `_lease` proved this; kept explicit for the type shape.
                return ControlResult(False, _NO_RECORD)
            if not release.admits_verdict(rec):
                return ControlResult(False, "no active merge review for this generation")
            landed = self.port.apply(
                Effect(
                    EffectKind.COMMENT,
                    repo,
                    int(number),
                    CommentBody.from_record(rendered, rec),
                )
            )
        return ControlResult(landed, "review recorded" if landed else "review did not land")


def _still_untested(heads: dict[str, str], untested: dict[str, str]) -> str:
    """Which offered heads still need a green verification run, for a card shipping several (#2718).

    The tests bit wants every open head, and a verification job credits only the head of the
    repository its ``--cwd`` is in. A worker told "run the verification job on this head" about
    two pull requests ran one job from its own worktree, and the companion's head was never
    credited, so the card sat reviewed and green-in-CI and never released.
    """
    if len(heads) < 2:
        return ""
    if not untested:
        return ". A green verification run already covers every one of these heads"
    green = [ref for ref in sorted(heads) if ref not in untested]
    missing = ", ".join(f"{ref}@{sha[:12]}" for ref, sha in sorted(untested.items()))
    covered = (
        f" Already green: {', '.join(f'{ref}@{heads[ref][:12]}' for ref in green)}."
        if green
        else ""
    )
    return (
        f". Each pull request's head needs its own green verification job, and a job credits "
        f"only the head of the repository its --cwd is in. Still missing: {missing} — submit "
        f"one verification job per pull request, with --cwd in that pull request's checkout."
        f"{covered}"
    )


def _stamp_resolved(rec: dict, generation: str, now: datetime) -> None:
    """Remember that this lease closed its own card, and when, so it may ``reopen`` it (#2522).

    A repeat of the same finish keeps the first moment: the hold runs from the report.
    """
    stamp = rec.get("resolved_finish")
    if isinstance(stamp, dict) and stamp.get("generation") == generation:
        return
    rec["resolved_finish"] = {"generation": generation, "at": now.astimezone(UTC).isoformat()}


def _with_marker(body: str, marker: str) -> str:
    text = body.rstrip()
    return f"{text}\n\n{marker}" if text else marker


def _adopt_parked(rec: dict, generation: str, status: str) -> None:
    """Save the status a worker finished with after the engine parked its lease (#2300).

    Only that case. A card still *In progress* in the record is left alone: reconciliation
    reads the board moving as the worker ending and does the bookkeeping that goes with it
    (``WORKER_ENDED``). But a lease the reportless path parked in Needs More Info, whose worker
    was only waiting and reports later, has had that bookkeeping done already, and its record
    kept saying Needs More Info until a full reconcile adopted the board — up to a webhook-quiet
    wake's whole ceiling, while the Work page, which reads the record, said the worker stopped
    without a report. The finish is the newer truth, so the record takes it now.
    """
    if rec.get("reportless_resolved") == generation and rec.get("status") != "In progress":
        rec["status"] = status


def _handed_off(rec: dict | None, generation: str) -> bool:
    """Whether reportless recovery already replaced this exact worker generation.

    Not while the revive is still undelivered (#2529). A revive is a continuation, sent to the
    chat already on the card once that chat is idle, so until it lands there is no second worker
    to fence out — only this one, which a person has typed into after its silent ending. On #2528
    that worker answered, was revived 47 seconds later, and had its finished spec refused as
    "handed to a new worker" that was really its own next turn. ``leases.holds_or_outgoing``
    gives it the same grace #1801 gives any other continuation's predecessor; acknowledging the
    delivery ends it.
    """
    return bool(
        rec
        and generation
        and rec.get("reportless_resolved") == generation
        and not leases.holds_or_outgoing(rec, generation)
    )
