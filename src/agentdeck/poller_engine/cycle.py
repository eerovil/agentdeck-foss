"""The wake: one cycle composing every engine module in a safe order (#176 slice 9).

A Poller Instance has no resident process — it wakes every ~30s, does one cycle, and
exits (#116). ``run_cycle`` *is* that wake. The individual invariants live in their
own modules; this module owns the composition, which has invariants of its own:

* **Phase order is a safety property, not a style choice.** Intake runs first and is
  made durable before anything acts on it, so a crash can never lose a command it
  already reacted to. Reconciliation runs before new commands are processed, so a
  command is judged against a self-healed world rather than a stale record. Dispatch
  runs last, once queues, leases, and ownership are settled.
* **Deterministic work never depends on the worker token budget.** Reconciliation,
  cleanup, releases, and stop handling run every wake while GitHub live reads remain
  available. Automatic model work waits, while a Work-authorized person's label or
  command carries an explicit override through the exact dispatch they approved.
* **One card's failure never stalls the others.** A card whose live state cannot be
  read this wake is skipped and counted, not fatal.
* **A wake always reports.** Health is written even when the cycle refuses to run
  (drift), is paused, or a phase fails — the record is the only observability surface
  (#107), so an unreported wake would be indistinguishable from a dead host.

The engine performs no I/O: reads arrive through ``EnginePort``, outward changes leave
as ``Effect`` values applied through that port, and worker delivery leaves as a
directive whose send intent is durable first (``dispatch.py``).

The phases, in order: pause/drift gate, intake (with the authorization seam), ownership
adoption, reconciliation, new commands, the release progression, dispatch, health. Each
phase's own rules live in its module; what lives here is the order, the durability
checkpoints between phases, and the failure isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from urllib.parse import urlsplit

from . import (
    authority,
    bug_reports,
    chat_answers,
    config,
    deck_releases,
    dispatch,
    flaky,
    health,
    intake,
    ownership,
    queue,
    quiet,
    ready_review,
    reconcile,
    release,
    reviews,
    revive,
    rounds,
    scheduled_work,
    stop,
    verification,
)
from . import authorization as auth
from . import blockers as card_blockers
from . import extensions as ext
from . import titles as card_titles
from .health import CycleReport, HealthVerdict
from .ports import (
    ArmOutcome,
    CardQuery,
    CommentBody,
    Effect,
    EffectKind,
    EnginePort,
    LinkedPRs,
    WorktreeCleanup,
)
from .reconcile import CardSnapshot
from .refusal_policy import (
    TERMINAL_SAME_DELIVERY_REFUSAL as _TERMINAL_SAME_DELIVERY_REFUSAL,
)
from .refusal_policy import (
    EngineRetryDisposition,
    refusal_disposition,
    same_delivery_retry_reasons,
)
from .state import StateStore

DEFAULT_WORKER_CAP = 10

# Compatibility projection for callers/tests that still import these public names.
# The source of truth remains refusal_policy (#1909).
SAME_DELIVERY_RETRIES = same_delivery_retry_reasons()
TERMINAL_SAME_DELIVERY_REFUSAL = _TERMINAL_SAME_DELIVERY_REFUSAL

_TODO = "Todo"
_IN_PROGRESS = "In progress"
# Where a card goes when its worker ended and said nothing: a human decides what the work
# reached, because the engine has no statement of it and silence is not success (#279).
_NEEDS_INFO = config.Role.NEEDS_INFO.value
# The one observation strong enough to publish on. `gone` proves nothing is running, not that
# anything finished, and a runtime restart makes every in-flight card `gone` at once.
_TURN_ENDED = "ended"

# Command ledger statuses the engine sets (the ledger itself lives in ``queue.py``).
_REACTION_RECEIVED = "eyes"
_REACTION_WORKING = "rocket"
_REACTION_DONE = "+1"
_REACTION_REFUSED = "confused"

_NEW = "new"
_QUEUED = "queued"
_PROCESSING = "processing"
_REJECTED = "rejected"
_DISCOVERY_PENDING = "ownership_discovery_pending"
# The ownership label on this issue was added by an app, so it is not a hand-over (#2056).
_APP_LABEL = "ownership_label_by_app"
# ...unless that app is the engine's own account, whose label the operator asked for (#2390).
_OWN_LABEL = "ownership_label_by_engine_account"

# Reconcile decisions after which the card is going away or has just been handed back to the
# board. Nothing about a dependency it was once waiting on applies to it any more (#1230).
_CARD_ENDING_DECISIONS = frozenset(
    {
        reconcile.ReconcileDecision.CLEANUP,
        reconcile.ReconcileDecision.CLEANUP_RETRY,
        reconcile.ReconcileDecision.RELEASED_DONE,
        reconcile.ReconcileDecision.IDLE_RELEASE,
    }
)

# Reconcile decisions that end a card's Run and leave the card resting where the Run put it.
_RUN_ENDING_DECISIONS = frozenset(
    {
        reconcile.ReconcileDecision.WORKER_ENDED,
        reconcile.ReconcileDecision.STOP_ENDED,
        reconcile.ReconcileDecision.TERMINAL_RECOVERED,
    }
)
_RUN_ENDED_AT = "run_ended_at"

_TERMINAL_STATUSES = frozenset(
    {
        config.Role.IN_REVIEW.value,
        config.Role.READY_TO_MERGE.value,
        # A card parked for a person (#1465) is as idle as one in Needs More Info: nothing
        # automatic will move it, so re-reading it from GitHub every wake buys nothing.
        config.Role.HUMAN_REVIEW_PENDING.value,
        config.Role.DONE.value,
        config.Role.NEEDS_INFO.value,
        config.Role.BLOCKED.value,
    }
)

# The optional column a release attempt that did not merge comes to rest in (#1099). It is
# ``None`` for a project whose board has no such column, and every caller below then keeps
# the destination it used before — *In review* for a review handback, *Blocked* for a failed
# release. Passing a role no board option is bound to would make every status write fail and
# retry forever, so this is resolved once from the activation binding and never guessed.
_READY_TO_MERGE = config.Role.READY_TO_MERGE.value


def _resting_statuses(ready_to_merge: str | None) -> tuple[str, ...]:
    """The columns an idle, approved-but-unmerged card may legitimately be sitting in."""
    return (release.IN_REVIEW, ready_to_merge) if ready_to_merge else (release.IN_REVIEW,)


def _release_refusal_message(
    verdict: release.ReleaseAdmission,
    *,
    repo: str,
    number: int,
    review_first: bool,
    eligible_statuses: tuple[str, ...] = (release.IN_REVIEW,),
) -> str:
    """Explain a final release refusal and the action that can clear it (#987)."""
    command = "/merge review" if review_first else "/merge"
    # Name every column the command would have been taken from, so a project with the optional
    # Ready to merge column is not told to go somewhere a retry is no longer required (#1099).
    admissible = " or ".join(eligible_statuses)
    messages = {
        release.ReleaseAdmission.REJECT_CLOSED: (
            f"I did not start `{command}` because this card is closed. Reopen it and run "
            f"`{command}` again if it still needs a release."
        ),
        release.ReleaseAdmission.REJECT_UNOWNED: (
            f"I did not start `{command}` because the poller no longer owns this card. Restore "
            f"its ownership label, wait for it to return to In review, and run `{command}` again."
        ),
        release.ReleaseAdmission.REJECT_BUSY: (
            f"I did not start `{command}` because this card still has active or queued work. "
            f"Wait until it is idle in {admissible}, then run `{command}` again. Removing and "
            "re-adding the ownership label does not clear that work: it restarts it."
        ),
        release.ReleaseAdmission.REJECT_STATUS: (
            f"I did not start `{command}` because this card is not in {admissible}. Finish or "
            f"hand back the current work, then run `{command}` again."
        ),
        release.ReleaseAdmission.REJECT_NO_LINKED_PR: (
            # The qualified reference is the advice because it is the one form no repository
            # convention forbids (#1734). A closing keyword still links a same-repo pull
            # request, but recommending it is how this card manufactured a policy conflict for
            # repositories whose own guide bans one.
            "I could not find a pull request linked to this card. Add the fully qualified "
            f"`{repo}#{number}` reference to the PR body — that works in any repository and "
            "needs no closing keyword. For a PR in this same repository, an "
            f"`agent/issue-{number}` branch with a bare `#{number}` reference also works. "
            f"Then run `{command}` again."
        ),
        release.ReleaseAdmission.REJECT_NOTHING_TO_REVIEW: (
            "I could not find an open linked pull request to review. Open or link one and run "
            "`/merge review` again; if the linked work is already merged, use `/merge` instead."
        ),
    }
    return messages[verdict]


@dataclass
class CycleOutcome:
    """What one wake did: durable effects already applied, directives still to deliver."""

    report: CycleReport
    verdict: HealthVerdict | None = None
    directives: list[dict] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    held: bool = False           # over budget: deterministic work ran, spawns deferred
    halted: str | None = None    # the cycle refused to run (drift / paused / query reserve)
    # The wake ran, with its live GitHub reads declined because the webhook streams proved
    # there was nothing to read (#1673). Not `halted`: durable state still moved, and the
    # host still consumes this wake's directives.
    quiet: str = ""
    live_snapshots: dict[str, CardSnapshot] = field(default_factory=dict, repr=False)
    # This wake read the whole labelled-issue sweep. Only then does "the engine has no record
    # of this card" also mean "no ownership label" — an unreadable sweep seeds nothing, and a
    # quiet wake does not sweep at all (#2056).
    label_sweep_complete: bool = False
    # Cards whose labeler this wake could not authorize. The ownership pass defers them rather
    # than trusting them, and anything downstream that reads "is this card in play" has to defer
    # with it — for an already-known card there is no marker on the record to read (#2056).
    ownership_deferred: set[str] = field(default_factory=set)
    # The project switched its Works to the deck (``works_router``, #2895 step 5): this engine
    # takes up no new card, and the cards it already holds finish under it.
    works_router: bool = False
    # Cards handed back this wake by a label removal that came after every live instruction on
    # them, with that removal. An instruction that arrives in the same wake but was written
    # before the removal is cancelled with the rest, not restarted after it (#2417).
    handed_back: dict[str, ownership.LabelRemoval] = field(default_factory=dict)


def _cards(state: dict) -> dict:
    return state.setdefault("issues", {})


def _split(key: str) -> tuple[str, int]:
    repo, number = key.split("#", 1)
    return repo, int(number)


def _linked(port: EnginePort, rec: dict | None, repo: str, number: int) -> LinkedPRs | None:
    """This card's release set, honouring a target the asker named on the release command.

    A named set *replaces* discovery for as long as that release runs, rather than filtering it.
    Filtering would be no hatch at all: the reason to name a pull request is that discovery got
    it wrong, and a discovery that missed the right one would go on missing it. The pin lives on
    the release record, so every wake of the same release — its CI wait, its repair, its arming —
    aims at the same thing the person pointed at, and the next release command decides again.
    """
    only = [str(ref) for ref in (rec or {}).get("release_only") or []]
    if only:
        return port.resolve_prs(only)
    return _without_steps_merged(rec, port.linked_prs(repo, number))


def _without_steps_merged(rec: dict | None, linked: LinkedPRs | None) -> LinkedPRs | None:
    """Discovery less the step pull requests this card already merged and moved past (#2730).

    They are still linked to the card, and always will be. Left in, the next release would
    re-watch their deploys, and a card whose next part has no pull request yet would read
    "nothing open, something merged" as a finished release.
    """
    if linked is None or not release.steps_merged(rec or {}):
        return linked
    return replace(
        linked,
        merged=release.without_steps_merged(rec, list(linked.merged)),
        step=release.without_steps_merged(rec, list(linked.step)),
    )


def _ids(rec: dict, field_name: str) -> list[str]:
    return [str(cid) for cid in (rec.get(field_name) or [])]


def _claimed(rec: dict | None, snapshot: CardSnapshot | None) -> bool:
    """Is this card the engine's — by a person's ownership label, or by its own dispatch?

    Until #2053 these were the same question, because dispatch wrote the label onto the issue.
    It no longer does, so a card started by a ``/claude`` comment is held in state alone and
    wears nothing. Everything that read "does the issue carry the label" as "is this ours" —
    arming an automatic review, resuming a blocked card — asks here
    instead, or it would quietly stop serving comment-started cards.

    A *person's* label. A label an app added starts nothing (#2056), and the label is left on
    the issue where the app put it, so the live fact alone would still read as ownership here —
    and the blocked-card resume needs no dispatch and no `started` of its own to act on it.
    """
    if snapshot is not None and snapshot.owned and not _labelled_by_an_app(rec):
        return True
    return bool((rec or {}).get("engine_claim"))


def _labelled_by_an_app(rec: dict | None) -> bool:
    """Did an app put this card's ownership label there? (#2056)

    Two ways to know. The marker this engine writes when it refuses a bot's label event — and,
    for a card whose app label was already accepted and consumed by the code this replaces, the
    standing work authority it left behind, which names the labeler and says it came from a
    label. Without the second one an upgrade would trust exactly the cards this guards against,
    because a consumed event is never fresh again and the marker is only written for a fresh one.
    """
    if (rec or {}).get(_APP_LABEL):
        return True
    if (rec or {}).get(_OWN_LABEL):
        # The engine's own account may be an App's bot login too (#2390); its label counts.
        return False
    asked = authority.current(rec)
    return asked.get("source") == "label" and queue.is_bot_login(asked.get("login"))


def _earliest_merge(states: dict, refs: list[str]) -> str | None:
    """The earliest ``merged_at`` among these PRs, or None when no port supplied one (#815).

    Earliest, because it is the first instant a deploy of any of this release's merges could
    have run, and the deploy watch must not discard evidence that arrived before it started
    looking. A port that does not report merge times leaves this None and the watch keeps
    anchoring at the wake, exactly as before.
    """
    instants = []
    for ref in refs:
        stamp = str(getattr(states.get(ref), "merged_at", "") or "").strip()
        if not stamp:
            continue
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue  # an unreadable stamp is no evidence, not a reason to drop the rest
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        # Compared as instants, not as text: two stamps written in different offsets sort
        # by their strings in the wrong order, and the earliest is the one that matters.
        instants.append(parsed)
    return min(instants).astimezone(UTC).isoformat() if instants else None


def run_cycle(
    store: StateStore,
    port: EnginePort,
    *,
    now: datetime,
    cap: int = DEFAULT_WORKER_CAP,
    drift: str | None = None,
    auto_deploy_repos: frozenset[str] | set[str] | None = None,
    issue_repos: frozenset[str] | set[str] | None = None,
    enabled_extensions: list | None = None,
    mode: ext.Mode = ext.Mode.LIVE,
    policy: auth.Policy | None = None,
    placeholder_titles: tuple[str, ...] | list[str] = (),
    terminal_recheck_s: int = config.DEFAULT_TERMINAL_RECHECK_S,
    proposal_daily_cap: int | None = None,
    proposal_worker_cap: int | None = None,
    ready_to_merge: str | None = None,
    command_output: config.CommandOutputPolicy | None = None,
    webhook_skip_s: int = config.DEFAULT_WEBHOOK_SKIP_S,
    webhook_quiet: bool = False,
    ownership_label: str | tuple[str, ...] = "",
    auto_release: bool = False,
    ci_repos: frozenset[str] | set[str] | None = None,
    parallel_review: bool = False,
    verify_turn: bool = False,
    review_only_repos: frozenset[str] | set[str] | None = None,
    spec_only_label: str = "",
    works_router: bool = False,
) -> CycleOutcome:
    """Run one wake and return what it did (see module docstring).

    ``drift`` is the fail-closed signal from the config/engine fingerprint check
    (#108/#116): when set, the cycle records health and does nothing else.
    ``auto_deploy_repos`` names the repositories whose merges gate on a production
    deploy — Manifest policy the config layer (#177) will supply. ``enabled_extensions`` are
    the project's extensions (#179); one loaded refused stays in the list because it is
    still what this project enables — it simply answers nothing, at any hook (#2158).
    ``mode`` is live or shadow, and in shadow mode nothing an
    extension asks for is honored (#109's dry run). ``policy`` is the project's controller
    policy (#110); the default refuses everyone, because a project with no stated policy has
    named nobody who may steer it. ``placeholder_titles`` are the issue-template titles this
    project wants repaired on sight (#338); naming none leaves every title exactly as filed.
    ``proposal_daily_cap`` and ``proposal_worker_cap`` are the ceilings on extension-proposed
    work (#371); naming neither leaves proposals bounded by ``cap`` like any other card.
    ``issue_repos`` are the repositories this project holds issues in, which bounds where an
    extension may cause a card to be *opened* (#393): an extension asks, and where a real GitHub
    issue may appear is the project's decision rather than the asker's. Naming none permits none,
    because a project that has not said where issues live has not consented to any.
    ``ready_to_merge`` is the optional column a release attempt that did not merge rests in
    (#1099), and it is passed only when the board actually has that column bound. Naming none
    keeps the old destinations exactly, so a project that never makes the column sees no change.
    ``webhook_quiet`` is the host's claim that the webhook streams prove nothing new landed for
    this instance and the watcher writing them is alive (#1631). It is a *hint the engine may
    decline*: with the ``webhook_skip_s`` policy knob on and a full wake recent enough, the wake
    declines its live GitHub reads — and only those. The cycle still runs, behind
    ``quiet.QuietPort``, so queued commands, waiting deliveries and already-decided release
    finalizations keep moving at the ordinary 30-second cadence; what waits for the ceiling is
    the discovery the streams are actually evidence about (#1673). Every missing condition,
    including a knob set to zero, runs the ordinary full wake, so the failure mode is always
    more polling.
    ``ownership_label`` is only ever *quoted*: a refusal that tells a person how to start a card
    by hand has to name the label their board actually uses, and naming none simply says "the
    ownership label" instead. Nothing is decided from it here — what the label means is the
    port's question, and it is the port that finds the cards carrying it.
    ``spec_only_label`` is the project's spec-only label, which an ``open-work`` proposal may ask
    to be opened under (#2874). Naming none refuses such a proposal rather than opening it as a
    build nobody approved.
    ``auto_release`` is the project's ``capabilities.auto_release`` (#2101): with it on, a resting
    card whose three release bits are all green is merged by the poller itself, and one that has
    fallen behind its base is given a rebase-and-rerun turn. Off — the default — the bits are
    neither read nor acted on, and every release still waits for a person's ``/merge``, so a
    project that has not opted in sees no change at all.
    ``command_output`` is the project's ``[command_output]`` policy (#2154): when it is on, every
    work dispatch carries the bounded command-output settings its worker's hooks apply. Off — the
    default — leaves command output exactly as the provider shows it.
    ``ci_repos`` are the repositories whose GitHub checks are the verification (``[[repos]]
    verification = "github_checks"``, #2364): their checks on a pull request's exact head credit
    the same tests bit a verification job does, and only when every reported check passed.
    Reading them merges nothing and asks no worker for anything: ``auto_release`` stays the only
    way a card merges by itself.
    ``review_only_repos`` are the repositories with no tests, where the review pass alone proves
    a pull request (``[[repos]] verification = "review_only"``, #2729): the bits credit the
    tests bit on such a head once the review bit reads green on it, and a release merges their
    pull requests before the others, since the others may depend on them.
    ``parallel_review`` is whether this project reviews a head while its worker verifies it
    (#2350): an outside reviewer and ``auto_release`` together. With it on, a worker may offer
    its head with ``ready-for-review`` and keep its turn, and a review that does not pass that
    head cancels the Run's verification and tells the worker so.
    ``verify_turn`` is ``config.verify_turn`` (#2375): the bits are read, and a reviewed card
    gets its rebase and verify turns, on a project whose releases a person still starts. Only
    ``auto_release`` merges on them. The same flag, read the other way, is why an outside
    reviewer's ``/reviewed`` needs only Work authority where ``auto_release`` is off: there a
    pass can ask for a worker turn and nothing more.
    ``works_router`` is the project's ``capabilities.works_router`` (#2895 step 5, ADR 0008):
    the deck starts and runs its new Works, so neither a label nor a command makes a new card
    here, and a card already on the board is handed to the deck before any phase runs
    (`_hand_over_held`, ADR 0008 point 12).
    """
    report = CycleReport()
    outcome = CycleOutcome(report=report, works_router=works_router)
    output_policy = command_output or config.CommandOutputPolicy()

    with store.transaction() as state:
        if drift:
            report.drift = drift
            outcome.halted = "drift"
            outcome.log.append(f"cycle refused: {drift} drift — fail closed")
            _finish(state, outcome, now=now)
            return outcome

        if works_router:
            # Ownership transfer is not a Poller phase: pausing work must not retain cards that
            # this project has assigned to the deck (#2895, ADR 0008 point 12).
            _hand_over_held(state, port, outcome, now=now)

        if health.is_paused(state):
            outcome.halted = "paused"
            outcome.log.append("cycle paused by operator note")
            _finish(state, outcome, now=now)
            return outcome

        load = store.last_load
        report.state_fallback = load.used_backup
        for key in load.recovered:
            outcome.log.append(f"recovered from the command ledger: {key}")

        # Decided before the gate purely so the gate can be told (#2082). `_webhook_skip` reads
        # state and writes nothing, so asking it early changes no behaviour; what it buys is that
        # a quiet wake may answer the reserve gate from an older reading instead of paying for a
        # fresh probe, which was the single biggest spender on this host. The skip itself is still
        # applied below, after the gate, exactly where it was.
        ready_review.set_enabled(state, parallel_review)
        if auto_release or verify_turn:
            state[_VERIFY_TURN_ENABLED] = True
        else:
            state.pop(_VERIFY_TURN_ENABLED, None)
        quiet_skip = _webhook_skip(
            state, now=now, webhook_skip_s=webhook_skip_s, webhook_quiet=webhook_quiet
        )
        if quiet_skip and any(ready_review.needs_offer(rec) for rec in _cards(state).values()):
            # A head offered for review is local news no webhook announces, and only a full
            # wake shows it to the reviewer's extension (#2350). Waiting for the ceiling would
            # leave the worker's suite running with nobody reviewing beside it.
            quiet_skip = None
        if quiet_skip and any(
            release.release_recheck_wanted(rec) for rec in _cards(state).values()
        ):
            # A suite that went green is local news too: no webhook says a job finished, and a
            # card that is ready to merge must not wait out the ceiling for its read (#2449).
            quiet_skip = None
        if quiet_skip and scheduled_work.due(state, now):
            # A scheduled run falling due is local news too (#2517): nothing on GitHub changes
            # when a clock does, and filing the run needs a live read of the previous one.
            quiet_skip = None
        if quiet_skip and _command_waiting(state, store):
            # A release asked for and not yet gated (#2671), or any deck request not yet read
            # (#2764). Gating and starting a worker both read the card's pull requests live, and
            # the deck sends GitHub nothing, so on quiet wakes alone a deck Merge or a reviewer's
            # findings sat for up to the whole ceiling looking broken.
            quiet_skip = None
        budget_halt = port.live_queries_halt(stale_reading_ok=bool(quiet_skip))
        if budget_halt:
            halt_reason, _, reading_source = budget_halt.partition(": ")
            outcome.halted = halt_reason
            budget_name = "REST core" if halt_reason == "core-budget" else "GraphQL"
            source_note = f" ({reading_source})" if reading_source else ""
            outcome.log.append(
                f"{budget_name} reserve reached{source_note} — live wake deferred"
            )
            _finish(state, outcome, now=now)
            return outcome
        if quiet_skip:
            # The skip is of the *live GitHub queries*, not of the wake (#1673). The cycle
            # runs on behind a port that refuses every GitHub read with the answer that read
            # already uses for "not this wake", so a queued command, a delivery waiting to be
            # retried and a release finalization already decided all keep moving at 30-second
            # cadence. What waits for the ceiling is discovery, which is the only thing the
            # webhook streams are evidence about.
            outcome.quiet = "webhook-quiet"
            outcome.log.append(quiet_skip)
            port = quiet.QuietPort(port)
        else:
            # Any wake past this point runs live GitHub queries, and this stamp is what bounds
            # how long a quiet skip may trust the streams (#1631). Written inside the same
            # transaction the cycle commits, so a wake that dies mid-flight records nothing and
            # the next one is full.
            state["last_live_wake"] = now.astimezone(UTC).isoformat()

        installed = list(enabled_extensions or [])
        commands = ext.declared_commands(installed, mode=mode) if installed else {}
        # Provider usage remains visible, but it never holds model work (#1497).
        dispatch_ok = True

        # The three phases below are pure discovery: each exists only to *find* work by
        # asking GitHub, and each reports an unreadable sweep as a health failure — which is
        # the right answer when GitHub broke and the wrong one when the wake declined to ask
        # (#1673). A quiet wake therefore does not run them at all rather than running them
        # against a port that would refuse. Everything after them moves state the engine
        # already holds, so it runs on either kind of wake.
        if not outcome.quiet:
            _intake(
                state,
                port,
                outcome,
                now=now,
                extension_words=frozenset(commands),
                policy=policy or auth.Policy(),
                extension_tiers={
                    word: declared.tier for word, (_ext, declared) in commands.items()
                },
                reviewed_merges=auto_release,
            )
        # A command pressed on the deck (#2324, #2419) is local, so it is read on a quiet wake
        # too; it becomes the same control its comment would and is handled like one below.
        deck_requests = _intake_deck_releases(state, store, outcome)
        # A Work the deck saw stop on an AgentDeck bug (#2888): local too, filed on a full wake.
        reported = _intake_bug_reports(state, store, outcome, issue_repos)
        _observe(
            state, port, outcome, installed,
            now=now, mode=mode, store=store, daily_cap=proposal_daily_cap,
            issue_repos=frozenset(issue_repos or ()), spec_only_label=spec_only_label,
        )
        if not outcome.quiet:
            for rec in _cards(state).values():
                ready_review.note_offered(rec)
        # Durable intake before any side effect or dispatch: a crash after this point
        # can repeat an idempotent step but can never forget an accepted command.
        store.checkpoint(state)
        deck_releases.discard(deck_requests)
        bug_reports.discard(reported)

        if not outcome.quiet:
            _adopt_ownership(state, port, outcome, now=now, policy=policy or auth.Policy())
        store.checkpoint(state)

        # An answer typed into a parked card's worker chat (#2348) is local like a deck Merge, so
        # it is read on a quiet wake too — and before reconciliation, so the card it resumes is
        # reconciled as the running worker it now is.
        _chat_answers(state, port, outcome, store=store, now=now)
        # A worker stopped on a person's prompt (#2887) is a local Runtime read too, and it runs
        # after the answers, so a card a chat answer just resumed is not parked again for it.
        _prompt_parks(state, port, outcome)
        # Before anything reads a card's linked pull requests: a card reopened since its record
        # was forgotten gets back what it already merged (#2914).
        _restore_released(state)

        _reconcile(
            state,
            port,
            outcome,
            now=now,
            cap=cap,
            dispatch_ok=dispatch_ok,
            store=store,
            placeholder_titles=tuple(placeholder_titles or ()),
            terminal_recheck_s=terminal_recheck_s,
            ready_to_merge=ready_to_merge,
            bot=(policy or auth.Policy()).bot,
        )
        if not outcome.quiet:
            # Discovery plus the cards it files, so a full wake only (#2517). A run filed here is
            # labelled at creation and found by the next wake's labelled sweep like any card.
            scheduled_work.run(
                state, port, outcome, now=now, store=store, policy=policy or auth.Policy(),
            )
        _retire_standing_approvals(state, outcome)
        _process_controls(
            state, port, outcome, now=now, commands=commands, mode=mode,
            ready_to_merge=ready_to_merge, ownership_label=ownership_label,
        )
        # After this wake's verdicts, so a rejection taken just now is enforced on the same
        # wake; and on a quiet wake too, since the job queue is a local read (#2350).
        _hold_rejected_verification(state, port, outcome, now=now)
        # The release progression is poll-driven and mostly token-free, so it runs even
        # over budget; only its one repair spawn is gated.
        _process_releases(
            state,
            port,
            outcome,
            now=now,
            dispatch_ok=dispatch_ok,
            auto_deploy_repos=auto_deploy_repos or frozenset(),
            store=store,
            installed=installed,
            mode=mode,
            ready_to_merge=ready_to_merge,
            auto_release=auto_release,
            merge_first=frozenset(review_only_repos or ()),
        )
        # Straight after them, because a park that the deploy has since undone is a release
        # still under way — it just stopped being read (#2069).
        _recheck_deploys(
            state,
            port,
            outcome,
            now=now,
            installed=installed,
            mode=mode,
            ready_to_merge=ready_to_merge,
            auto_deploy_repos=auto_deploy_repos or frozenset(),
        )
        # After the releases already under way, and still before dispatch: an explicit command
        # is a person's decision and must win over the bits, while a rebase turn
        # the bits ask for is ordinary queued work this wake's dispatch can pick up (#2101).
        # Before the bits, so CI that went green this wake is already in the tests bit they read.
        ci = frozenset(ci_repos or ())
        if ci:
            _apply_ci_verification(
                state, port, outcome, now=now, ci_repos=ci, ready_to_merge=ready_to_merge,
            )
        bits = bool(auto_release or verify_turn)
        if bits:
            _apply_release_bits(
                state, port, outcome, now=now, ready_to_merge=ready_to_merge, ci_repos=ci,
                merges=auto_release, review_only=frozenset(review_only_repos or ()),
            )
        # The release bits read every resting card's pull requests and notice a step merged by
        # hand as they go; this covers the cards they do not read (#2730).
        _notice_hand_merged_steps(
            state, port, outcome, now=now, ready_to_merge=ready_to_merge,
            statuses=(config.Role.BLOCKED.value,) if bits else None,
        )

        _dispatch_ready(
            state,
            port,
            outcome,
            now=now,
            cap=cap,
            store=store,
            budget_ok=dispatch_ok,
            proposal_cap=proposal_worker_cap,
            command_output=output_policy,
        )

        _extension_health(outcome, installed, state, mode=mode)
        report.queue_depth = len(queue.pending(state, statuses={_NEW, _QUEUED}))
        report.active_workers = sum(
            1 for rec in _cards(state).values() if rec.get("status") == _IN_PROGRESS
        )
        _finish(state, outcome, now=now)
    return outcome


def _finish(state: dict, outcome: CycleOutcome, *, now: datetime) -> None:
    """Always publish health, whatever the cycle did or refused to do."""
    health.record_cycle(state, outcome.report, now=now)
    outcome.verdict = health.evaluate(state, now=now)


# How the host offers the quiet hint to the pinned engine artifact (#1631). An environment
# variable rather than a CLI flag, deliberately: the wake script comes from the checkout while
# the artifact is pinned, and an unknown flag would make an older engine refuse every wake,
# where an unknown variable is simply ignored — version skew in either direction degrades to
# full polling.
WEBHOOK_QUIET_ENV = "AGENTDECK_POLLER_WEBHOOK_QUIET"


def _webhook_skip(
    state: dict, *, now: datetime, webhook_skip_s: int, webhook_quiet: bool
) -> str | None:
    """Why this wake may skip its live GitHub queries, or None to run them (#1631).

    Three conditions, and every one that cannot be proved runs the full wake — the failure
    mode always points toward more polling. The host proved two of them before offering
    ``webhook_quiet`` (no new deliveries on this instance's streams, and a fresh heartbeat
    from the watcher writing them); the engine owns the third: a *full* wake recent enough
    that trusting an at-most-once delivery channel stays bounded by ``webhook_skip_s``. A
    missing, unreadable or future ``last_live_wake`` stamp is no evidence, not a skip.
    """
    if webhook_skip_s <= 0 or not webhook_quiet:
        return None
    stamp = str(state.get("last_live_wake") or "")
    if not stamp:
        return None
    try:
        last = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    age = (now - last).total_seconds()
    if not 0 <= age < webhook_skip_s:
        return None
    return (
        f"webhook streams quiet ({int(age)}s since the last full wake, "
        f"ceiling {webhook_skip_s}s) — live wake skipped"
    )


def _do(port: EnginePort, outcome: CycleOutcome, effect: Effect) -> bool:
    """Apply one outward change through the port, recording it for observability."""
    result = port.apply(effect)
    landed = result.landed if isinstance(result, WorktreeCleanup) else bool(result)
    outcome.effects.append(effect)
    if not landed:
        target = f"{effect.repo}#{effect.number}"
        outcome.report.failed(f"effect {effect.kind.value} did not land on {target}")
    return landed


def _comment_effect(
    state: dict,
    repo: str,
    number: int,
    body: object,
    *,
    include_session: bool = True,
    pull_request_ref: str = "",
) -> Effect:
    """A comment carrying links from this exact in-memory card generation (#957)."""
    rec = _cards(state).get(f"{repo}#{int(number)}")
    return Effect(
        EffectKind.COMMENT,
        repo,
        int(number),
        CommentBody.from_record(
            str(body),
            rec,
            include_session=include_session,
            pull_request_ref=pull_request_ref,
        ),
    )


def _cleanup_worktrees(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    repo: str,
    number: int,
    paths: list[str],
) -> list[str]:
    """Apply cleanup while retaining only the exact paths the host could not remove.

    A path the host kept on purpose — one still holding uncommitted work (#1718), or one no
    Work claims and so nothing here may delete (#2195) — is not among them. It is not a
    failed effect and must never become one: a path reported pending is retried on every
    wake for as long as the card is tracked, which dirties every report and wedges
    promotion (#1189). So the card is released as usual, the directory is left
    where it is, and the release says on the card which one it kept and why, because a
    worktree nothing tracks any more is invisible until somebody is told about it.
    """
    effect = Effect(EffectKind.REMOVE_WORKTREES, repo, number, list(paths))
    result = port.apply(effect)
    outcome.effects.append(effect)
    cleanup = (
        result
        if isinstance(result, WorktreeCleanup)
        else WorktreeCleanup(() if result else tuple(paths))
    )
    failed = list(cleanup.failed)
    if cleanup.retained:
        _report_retained_worktrees(state, port, outcome, repo=repo, number=number,
                                   retained=cleanup.retained)
    if failed:
        outcome.report.failed(
            f"effect {effect.kind.value} did not land on {repo}#{number}: "
            f"{len(failed)} path(s) pending"
        )
    return failed


def _report_retained_worktrees(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    repo: str,
    number: int,
    retained: tuple[tuple[str, str], ...],
) -> None:
    """Say on the card which worktree the release kept, and why nothing will remove it now.

    The reason is the path's own: uncommitted work (#1718) and a directory no Work claims
    (#2195) are different facts, so the heading claims neither and each line says which.
    """
    listing = "\n".join(f"- `{path}` — {reason}" for path, reason in retained)
    body = (
        "**Kept this card's worktree.** Releasing the card left it on the host:\n\n"
        f"{listing}\n\n"
        "Nothing tracks it from here. Whatever is in there is yours to save or discard; the "
        "operator worktree sweep (`reap-worktrees`) is what eventually clears it, and only "
        "once it is clean and merged."
    )
    _do(port, outcome, _comment_effect(state, repo, number, body))
    for path, reason in retained:
        outcome.log.append(f"reconcile: {repo}#{number} kept {path} ({reason})")


# --- phase 1: intake ---

def _intake(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    extension_words: frozenset[str] = frozenset(),
    policy: auth.Policy | None = None,
    extension_tiers: dict[str, str] | None = None,
    reviewed_merges: bool = True,
) -> None:
    """Scan comments into the durable ledger, exactly once each, with authorization applied.

    ``reviewed_merges`` is whether a ``/reviewed`` pass can end in a merge here
    (``auto_release``); it decides the tier the verdict needs (``auth.required_tier``).
    """
    report = outcome.report
    policy = policy or auth.Policy()
    floor = queue.scan_floor(state, now=now)
    result = port.scan_comments(since=floor)
    report.scans += 1
    if not result.complete:
        report.scan_failures += 1

    for comment in result.comments:
        # A bot is not a controller, and its comment is dropped here — before the grammar, the
        # ledger and the refusal — unless this project named it in its own controller allowlist
        # (#2033). Grant-only: an unnamed bot is skipped exactly as it always was, and a named
        # one still has to clear authorization for the tier its command needs.
        if queue.is_bot_comment(comment) and not policy.names(_login_of(comment)):
            continue
        if queue.is_seen(state, comment.get("id")):
            continue
        command = intake.parse_control(
            comment.get("body") or "", extension_words=extension_words
        )
        if command is None:
            continue
        # A release command's payload is the pull requests it named, if it named any; every
        # other kind's is its instruction text. Both ride the one ledger field, so recording a
        # named release target needs no new column and survives a restart with the command.
        kind = command.kind.value
        payload = list(command.targets) if command.targets else command.instruction
        if command.kind is intake.CommandKind.REVIEWED:
            # A verdict is several facts, not one string, and all of them have to survive a
            # restart with the command: what was decided, about the difference between which
            # two commits, and — when the card runs more than one pull request — about which
            # of them (#2108).
            payload = {
                "verdict": command.verdict,
                "head": command.head,
                "digest": command.digest,
                "targets": list(command.targets),
            }
        if command.is_valid:
            verdict = _authorize(
                port,
                comment,
                kind,
                repo=_repo_of(comment),
                policy=policy,
                extension_tiers=extension_tiers or {},
                extension_words=extension_words,
                reviewed_merges=reviewed_merges,
            )
            if verdict is auth.Verdict.UNCERTAIN:
                # Uncertainty is never permission, and never a silent drop: leave the
                # comment unseen so a later wake can decide, and surface it in health.
                report.failed(f"authorization uncertain for comment {comment.get('id')}")
                continue
            if verdict is auth.Verdict.DENIED:
                kind, payload = "unauthorized", command.kind.value
        # Invalid and unauthorized commands are still recorded (exactly once) and refused
        # in the processing phase, so the requester always sees an answer.
        status = _NEW
        record = queue.build_record(comment, kind, payload, repo=_repo_of(comment), status=status)
        if record is None:
            continue
        if queue.is_bot_comment(comment):
            # Only an app this project allowlisted reaches here — every other bot was dropped
            # above. Remember that it was one, because an app may steer a card a person put in
            # play but may not start a card nobody handed the engine (#2056). The fact is
            # recorded with the command rather than re-derived later, so a login that stops
            # looking like a bot cannot change what an already-accepted command may do.
            record["app"] = True
        if queue.enqueue(state, record):
            report.commands_ingested += 1

    if result.complete and result.scanned_to is not None:
        queue.record_scan(state, result.scanned_to, comments=result.comments)


def _command_waiting(state: dict, store: StateStore) -> bool:
    """Does a release wait for its gate in the ledger, or any deck request wait to be read?

    A release still ``new`` was deferred by an earlier wake, often a quiet one. A deck request
    file is read only after the skip is decided, so it is looked at here directly.
    """
    if any(
        control.get("kind") in deck_releases.RELEASE_KINDS
        for control in queue.pending(state, statuses={_NEW})
    ):
        return True
    paths = getattr(store, "paths", None)
    return paths is not None and deck_releases.request_waiting(
        deck_releases.directory(paths.state)
    )


def _intake_deck_releases(state: dict, store: StateStore, outcome: CycleOutcome) -> list:
    """Queue the command requests the deck left for this instance (#2324, #2419).

    Returns the request files to remove once the ledger holding their controls is checkpointed.
    """
    paths = getattr(store, "paths", None)
    if paths is None:
        return []
    done, log, queued = deck_releases.ingest(state, deck_releases.directory(paths.state))
    outcome.log.extend(log)
    outcome.report.commands_ingested += queued
    return done


def _intake_bug_reports(
    state: dict, store: StateStore, outcome: CycleOutcome, issue_repos
) -> list:
    """Fold the bug reports the deck left for this instance into their records (#2888).

    Returns the report files to remove once the state holding them is checkpointed.
    """
    paths = getattr(store, "paths", None)
    if paths is None:
        return []
    done, log, _added = bug_reports.ingest(
        state, bug_reports.directory(paths.state), repos=frozenset(issue_repos or ())
    )
    outcome.log.extend(log)
    return done


# A card with its own next step already decided is not resumed by a chat answer: a dispatch or
# a queued instruction will start a worker anyway, and a stop or a release owns the card.
_RESUME_HELD_BY = (
    "dispatch", "pending", "processing", "stopping", "mode",
    "release_pending", "forget_pending", "recovery_action",
)


def _chat_answers(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, store: StateStore, now: datetime
) -> None:
    """Post the answers typed into parked cards' worker chats, and resume those cards (#2348).

    See ``chat_answers``. A waiting file becomes a pending delivery on its card, checkpointed
    before the file is removed, and stays there until its comment is confirmed. A comment that
    was attempted and not confirmed is retried only after a live read of the issue shows its
    marker is not there, so a write that did land is never posted twice.
    """
    cards = _cards(state)
    _retry_resume_status(cards, port, outcome)
    _intake_chat_answers(state, cards, outcome, store=store)
    for key, rec in list(cards.items()):
        if isinstance(rec, dict) and rec.get("chat_answers_pending"):
            _deliver_chat_answers(state, port, outcome, store=store, key=key, rec=rec, now=now)
    _retry_resume_status(cards, port, outcome)


def _intake_chat_answers(
    state: dict, cards: dict, outcome: CycleOutcome, *, store: StateStore
) -> None:
    """Make every waiting answer file a pending delivery on its card, then remove the file."""
    paths = getattr(store, "paths", None)
    if paths is None:
        return
    done, answers, log = chat_answers.read(chat_answers.directory(paths.state))
    outcome.log.extend(log)
    if not done:
        return
    for answer in answers:
        rec = cards.get(answer.key)
        if not isinstance(rec, dict):
            outcome.log.append(f"chat answer {answer.id}: no card {answer.key}; discarded")
            continue
        if chat_answers.seen(rec, answer.id):
            continue
        if str(rec.get("generation") or "") != answer.generation:
            outcome.log.append(
                f"chat answer {answer.id}: {answer.key} has moved to another Run; discarded"
            )
            continue
        parked = rec.get("status") == _NEEDS_INFO
        if not chat_answers.answered_while_parked(rec, answer, parked=parked):
            outcome.log.append(
                f"chat answer {answer.id}: {answer.key} was no longer parked; discarded"
            )
            continue
        chat_answers.remember(rec, answer.id)
        chat_answers.queue(rec, answer)
    store.checkpoint(state)
    chat_answers.discard(done)


def _deliver_chat_answers(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    store: StateStore,
    key: str,
    rec: dict,
    now: datetime,
) -> None:
    """Post one card's pending answers in order; stop at the first that cannot be proved."""
    repo, number = _split(key)
    posted: frozenset[str] | None = None
    for entry in list(rec.get("chat_answers_pending") or ()):
        answer = chat_answers.from_entry(key, entry)
        if answer is None:
            chat_answers.drop(rec, entry)
            continue
        if entry.get("attempted"):
            # An earlier attempt may have landed. Only the issue itself can say.
            if posted is None:
                batch = port.snapshot_cards(
                    {key: CardQuery(repo, number, generation=str(rec.get("generation") or ""))}
                )
                snapshot = batch.cards.get(key)
                if snapshot is None:
                    outcome.log.append(
                        f"chat answer {answer.id}: {key} could not be read to check an earlier "
                        "attempt; waiting for the next wake"
                    )
                    return
                posted = snapshot.chat_answer_ids
            if answer.id in posted:
                chat_answers.drop(rec, entry)
                _settle_chat_answer(
                    port, rec, answer, outcome, now=now, resume=bool(entry["resume"])
                )
                continue
        held = [name for name in _RESUME_HELD_BY if rec.get(name)]
        resume = (
            rec.get("status") == _NEEDS_INFO
            and str(rec.get("generation") or "") == answer.generation
            and not held
        )
        entry["attempted"] = True
        entry["resume"] = resume
        # Durable before the write, so a wake that dies mid-write reads before it retries.
        store.checkpoint(state)
        body = chat_answers.comment_body(
            answer,
            marker=chat_answers.resume_marker(answer.generation) if resume else "",
        )
        if not _do(
            port, outcome, _comment_effect(state, repo, number, body, include_session=False)
        ):
            outcome.log.append(
                f"chat answer {answer.id}: comment on {key} not confirmed; the issue is read "
                "before it is tried again"
            )
            return
        chat_answers.drop(rec, entry)
        if not resume and held:
            outcome.log.append(
                f"chat answer {answer.id}: posted on {key} (left as it is: {', '.join(held)})"
            )
            continue
        _settle_chat_answer(port, rec, answer, outcome, now=now, resume=resume)


def _settle_chat_answer(
    port: EnginePort,
    rec: dict,
    answer: chat_answers.Answer,
    outcome: CycleOutcome,
    *,
    now: datetime,
    resume: bool,
) -> None:
    """What a confirmed answer comment does to its card: resume it when the comment said so."""
    key = answer.key
    if not resume:
        outcome.log.append(f"chat answer {answer.id}: posted on {key}")
        return
    if rec.get("status") != _NEEDS_INFO or str(rec.get("generation") or "") != answer.generation:
        outcome.log.append(
            f"chat answer {answer.id}: posted on {key}, which has moved on since; not resumed"
        )
        return
    # The same lease, now running again: the chat that took the answer is the worker.
    rec["status"] = _IN_PROGRESS
    # Whether this lease had already reported before it was parked. A reportless park stamps
    # it, and a quiet re-park (#2774) says the park it made still stands on the earlier report.
    reported = rec.get("reportless_resolved") != answer.generation or (
        rec.get("quiet_reparked") == answer.generation
    )
    rec.pop("quiet_reparked", None)
    repo, number = _split(answer.key)
    rec["chat_resumed"] = {
        "generation": answer.generation,
        "at": now.astimezone(UTC).isoformat(),
        "status_pending": True,
        "reported": reported,
        # What the work looked like when the answer arrived, so a turn that only answers in
        # the chat can be told from one that changed something (#2774).
        "produced": _produced(port, rec, repo=repo, number=number),
    }
    # A reportless park is stamped with the lease it resolved, which would make the resumed
    # turn's own ending read as already handled.
    if rec.get("reportless_resolved") == answer.generation:
        rec.pop("reportless_resolved", None)
    rec.pop("parked_as", None)
    outcome.log.append(f"chat answer {answer.id}: posted on {key} -> In progress in the same chat")


def resume_for_finish(
    state: dict, port: EnginePort, store: StateStore, *, key: str, generation: str, now: datetime
) -> str:
    """Do the chat-answer resume a wake would, for a worker finishing right now (#2499).

    The answer reaches the chat at once, but a wake resumes the card later. A worker that
    reports inside that window would hit its own old Needs More Info marker and its new report
    would be dropped as a repeat. So ``finish`` calls this under the same lock first. Answers
    ``"resumed"`` when this card is now running again under ``generation``, ``"waiting"`` when an
    answer for it is still not confirmed on the issue, and ``""`` when there was nothing to do.
    """
    cards = _cards(state)
    rec = cards.get(key)
    if not isinstance(rec, dict) or rec.get("status") != _NEEDS_INFO:
        return ""
    outcome = CycleOutcome(report=CycleReport())
    _intake_chat_answers(state, cards, outcome, store=store)

    def mine() -> list[dict]:
        return [
            entry
            for entry in rec.get("chat_answers_pending") or ()
            if isinstance(entry, dict) and entry.get("generation") == generation
        ]

    if not mine():
        return ""
    _deliver_chat_answers(state, port, outcome, store=store, key=key, rec=rec, now=now)
    resumed = rec.get("chat_resumed")
    if (
        rec.get("status") == _IN_PROGRESS
        and isinstance(resumed, dict)
        and resumed.get("generation") == generation
    ):
        # The finish that called this writes the board itself, straight after. Owing the
        # In progress write as well would let a later wake move the card back from the report.
        resumed.pop("status_pending", None)
        return "resumed"
    return "waiting" if mine() else ""


def _retry_resume_status(cards: dict, port: EnginePort, outcome: CycleOutcome) -> None:
    """Land the board write a chat-answer resume still owes; it is idempotent."""
    for key, rec in cards.items():
        resumed = rec.get("chat_resumed") if isinstance(rec, dict) else None
        if not isinstance(resumed, dict) or not resumed.get("status_pending"):
            continue
        if (
            rec.get("status") != _IN_PROGRESS
            or resumed.get("generation") != str(rec.get("generation") or "")
        ):
            # The card has moved on since; the write is no longer this resume's to make.
            resumed.pop("status_pending", None)
            continue
        repo, number = _split(key)
        if _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, _IN_PROGRESS)):
            resumed.pop("status_pending", None)


# What a Needs More Info park made for a worker's prompt says it stands in for (#2887).
PROMPT_PARK = "prompt"
# A card with its own next step already decided is not parked for a prompt: a dispatch, a stop
# or a release owns the card. Unlike a chat answer's resume, a running worker's own commands
# (``processing``) and the instructions queued behind it (``pending``) do not hold it.
_PROMPT_PARK_HELD_BY = (
    "dispatch", "stopping", "mode", "release_pending", "forget_pending", "recovery_action",
)


def _prompt_parks(state: dict, port: EnginePort, outcome: CycleOutcome) -> None:
    """Park a card whose worker waits on a person's prompt; put it back once it does not (#2887).

    A worker stopped on a question, a permission or approval request, or a blocked command
    needs its owner as much as one that finished ``needs_more_info``, but its card stayed In
    progress, so nothing told anyone. The Runtime already reads that wait as ``waiting_human``.

    The park keeps the lease, posts no comment and writes no terminal marker, so the worker's
    own later report is an ordinary one. The card goes back to In progress, under the same
    lease, as soon as the Runtime no longer reads the wait — answered, or gone with the turn —
    and this runs before reconciliation, so a turn that ended is then handled as it always was.
    A park the card has moved on from (a finish, a chat answer, a new Run) is only forgotten.
    """
    for key, rec in list(_cards(state).items()):
        if not isinstance(rec, dict):
            continue
        generation = str(rec.get("generation") or "")
        parked = str(rec.get("prompt_parked") or "")
        if parked:
            if parked != generation or rec.get("status") != _NEEDS_INFO:
                _drop_prompt_park(rec)
                continue
        elif (
            not generation
            or rec.get("status") != _IN_PROGRESS
            or any(rec.get(name) for name in _PROMPT_PARK_HELD_BY)
        ):
            continue
        repo, number = _split(key)
        waiting = port.waiting_on_person(repo, number, generation)
        if waiting is None or waiting == bool(parked):
            continue
        status = _NEEDS_INFO if waiting else _IN_PROGRESS
        if not _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, status)):
            continue
        rec["status"] = status
        if waiting:
            rec["prompt_parked"] = generation
            rec["parked_as"] = PROMPT_PARK
            outcome.log.append(f"prompt: {key} worker waits on a person's prompt -> {status}")
        else:
            _drop_prompt_park(rec)
            outcome.log.append(f"prompt: {key} worker no longer waits on a prompt -> {status}")


def _drop_prompt_park(rec: dict) -> None:
    rec.pop("prompt_parked", None)
    if rec.get("parked_as") == PROMPT_PARK:
        rec.pop("parked_as", None)


def unpark_prompt_for_finish(rec: dict, generation: str) -> None:
    """Undo this lease's prompt park in the record before its worker's own report (#2887).

    The finish writes the board itself, straight after. Left parked, the record would not read
    the board moving as the worker ending, and the ending's bookkeeping would never run.
    """
    if str(rec.get("prompt_parked") or "") == generation and rec.get("status") == _NEEDS_INFO:
        rec["status"] = _IN_PROGRESS
    _drop_prompt_park(rec)


def _authorize(
    port: EnginePort,
    comment: dict,
    kind: str,
    *,
    repo: str,
    policy: auth.Policy,
    extension_tiers: dict[str, str],
    extension_words: frozenset[str],
    reviewed_merges: bool = True,
) -> auth.Verdict:
    """Decide whether this commenter may issue this command, looking up only when needed.

    The tier comes from the command (#110's table), the decision from the engine's own policy,
    and GitHub is asked for a permission level only when neither the allowlist nor the cheap
    ``author_association`` pre-filter already settles it.
    """
    if kind == intake.CommandKind.EXTENSION.value:
        word = str(comment.get("body") or "").strip().split(maxsplit=1)
        kind = word[0].lower() if word else ""
    tier = auth.required_tier(
        kind, extension_tiers=extension_tiers, reviewed_merges=reviewed_merges
    )
    if tier is None:
        # Nothing commenter-initiated maps to this: engine-internal acts need no authority.
        return auth.Verdict.AUTHORIZED
    login = _login_of(comment)
    association = comment.get("author_association")
    if not auth.needs_lookup(policy, tier, login=login, association=association):
        return auth.decide(policy, tier, login=login, association=association)
    return auth.decide(
        policy,
        tier,
        login=login,
        association=association,
        permission=port.permission(repo, login),
    )


def _login_of(comment: dict) -> str:
    """The account that wrote a scanned comment ("" when GitHub named none)."""
    return (comment.get("user") or {}).get("login") or ""


def _repo_of(comment: dict) -> str:
    """The repository a scanned comment came from (the scan is per-repo)."""
    repo = comment.get("repo")
    if isinstance(repo, str) and repo:
        return repo
    url = (comment.get("issue_url") or "").rstrip("/")
    parts = url.split("/")
    return parts[-3] if len(parts) >= 3 else ""


# --- phase 2: ownership adoption ---

def _discover_labelled(
    state: dict, port: EnginePort, outcome: CycleOutcome
) -> tuple[dict[str, list[ownership.LabelEvent]], bool, list[str]]:
    """Seed cards and return their ownership histories plus sweep completeness.

    Without this the label is inert: `_adopt_ownership` reads events only for cards already in
    state, and only a command comment put them there, so labelling a fresh issue did nothing at
    all — silently, forever (#311). The old poller's entire intake was this query.

    Seeding is all that happens here. Whether a seeded card is a *start* is the ledger's
    decision, made from its label events like any other card's, so discovery cannot invent a
    start the provenance rules would not have reached on their own.
    """
    found = port.labelled_cards()
    if found is None:
        # An unreadable sweep is not an empty board. Reporting incomplete holds the ledger, so
        # a card that could not be listed is reconsidered next wake instead of being skipped.
        outcome.report.scan_failures += 1
        outcome.report.failed("the labelled-issue sweep could not be completed")
        outcome.log.append("discovery: the labelled-issue sweep could not be completed")
        return {}, False, []
    if outcome.works_router:
        # The deck starts this project's new Works (#2895 step 5): only a card already on the
        # board is in play here, so a labelled issue it does not hold is not seeded or started.
        found = {key: events for key, events in found.items() if key in _cards(state)}
    seeded = [key for key in found if key not in _cards(state)]
    for key in seeded:
        _cards(state)[key] = {"status": _TODO, _DISCOVERY_PENDING: True}
    for key, rec in _cards(state).items():
        if rec.get(_DISCOVERY_PENDING) and key not in found:
            rec.pop(_DISCOVERY_PENDING, None)
    first_seen = [
        key for key in found if _cards(state).get(key, {}).get(_DISCOVERY_PENDING)
    ]
    if seeded:
        outcome.log.append(f"discovery: {', '.join(sorted(seeded))} carry the label and were new")
    # Every labelled card is now in state, which is what lets a later phase read "no record" as
    # "no label" (#2056). Set only on the complete path, beside the seeding it describes.
    outcome.label_sweep_complete = True
    return found, True, first_seen


def _queue_engine_instruction(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    ref: str,
    cid: str,
    instruction: str,
    url: str,
    now: datetime,
    author: str,
) -> bool:
    """Queue an instruction the engine itself authored, exactly once, and report whether it was new.

    It goes through the same ``_queue_instruction`` a person's ``/claude`` comment does, so it
    lands in the same ledger, joins a live turn the same way, and is superseded by a release the
    same way. What it skips is authorization: a controller policy answers "may *this person*
    steer this project", and there is no person here — the engine is not one of its own
    controllers, and asking would either refuse every nudge or require naming the engine as an
    operator, which would then also let a comment forged in its name steer the board.

    Exact-once is the command ledger's own rule, keyed by ``cid``: the caller derives that id
    from the fact it is reporting, so no second record has to remember what was announced.
    """
    if queue.is_seen(state, cid):
        return False
    try:
        repo, number = _split(ref)
    except ValueError:
        return False
    control = {
        "id": cid,
        "repo": repo,
        "number": number,
        "kind": intake.CommandKind.CLAUDE.value,
        "payload": instruction,
        "body": "",
        "url": url or "",
        "author": author,
        "created_at": now.astimezone(UTC).isoformat(),
        "status": _NEW,
    }
    queue.enqueue(state, control)
    outcome.report.commands_ingested += 1
    _queue_instruction(state, port, outcome, control)
    return True


def _adopt_ownership(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    policy: auth.Policy | None = None,
) -> None:
    """Turn a hand-added ownership label into a start, using the provenance ledger.

    Re-adding the label to a card a worker is still on is a *restart*, not a second worker:
    the stop protocol runs first so the superseded worker is invalidated and given a safe
    exit, and the card starts again only once it has acknowledged.

    Adoption can only classify a card the engine already knows, so it is preceded by discovery:
    a labelled issue nobody has commented on has to be seeded before its label can mean anything
    (#311). The ledger's own `initialized` rule still protects a rebuilt state — on a fresh
    ledger nothing is a start, so state loss adopts the standing world rather than re-running it.
    """
    provenance = state.setdefault("ownership", ownership.new_ledger())
    events, complete, first_seen = _discover_labelled(state, port, outcome)
    events, deferred = _authorized_label_events(
        state, port, outcome, events, policy=policy or auth.Policy()
    )
    if deferred:
        # A card whose labeler could not be authorized yet is left out entirely, so the ledger
        # does not advance past an event the engine has not decided on — the start survives to
        # be reconsidered next wake instead of being silently consumed.
        complete = False
        # Carried for the rest of this wake: a card already in state gets no marker of its own
        # from discovery, so this is the only place the deferral is knowable later (#2056).
        outcome.ownership_deferred.update(deferred)
    starts = ownership.detect_starts(
        provenance, events, all_scanned_ok=complete, scanned_at=now
    )
    suppressed = sorted(
        key for key in first_seen if key in events and key not in starts
    )
    for key in suppressed:
        outcome.suppressed.append(key)
        outcome.log.append(
            f"discovery: {key} already carried the ownership label on first sight — "
            "not dispatched; post `/claude …`, or remove and re-add the label, to start it"
        )
    for key in set(first_seen).difference(deferred):
        _cards(state).get(key, {}).pop(_DISCOVERY_PENDING, None)
    for key in starts:
        rec = _cards(state).setdefault(key, {"status": _TODO})
        if rec.pop("adopt_ownership", False):
            # First sighting of a card that already carried the label. The ledger has advanced
            # past the event, which is all that is needed; classifying it would be treating the
            # engine's own adoption as somebody's instruction.
            outcome.log.append(f"ownership: {key} label adopted with the card — not a start")
            continue
        if rec.pop("engine_labeled", False):
            # The engine added this label itself; the ledger has already advanced past the
            # event, so absorbing it here is all that is needed. Exactly one is absorbed, so a
            # human re-adding the label afterwards still starts work.
            #
            # Nothing sets this any more (#2053 stopped the engine writing the label at all).
            # It is kept for the cards already in a live `state.json` when that shipped, whose
            # label event is still unabsorbed and would otherwise read as a fresh instruction.
            outcome.log.append(f"ownership: {key} label added by the engine itself — not a start")
            continue
        if rec.get("stopping"):
            rec["restart_after_stop"] = True
            rec["manual_restart_after_stop"] = True
            outcome.log.append(f"ownership: {key} re-added while stopping -> restart after stop")
            continue
        if stop.is_active(rec):
            stop.request_stop(rec, now=now)
            rec["restart_after_stop"] = True
            rec["manual_restart_after_stop"] = True
            abandoned = reconcile.abandon_dispatch(rec)
            if abandoned:
                _discard_receipts(port, outcome, key, dispatch=abandoned)
            outcome.log.append(f"ownership: {key} re-added during active work -> lease rolled")
            continue
        rec["manual_start"] = True
        outcome.log.append(f"ownership: {key} label added -> start")


def _authorized_label_events(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    events: dict[str, list[str | ownership.LabelEvent]],
    *,
    policy: auth.Policy,
) -> tuple[dict[str, list[str | ownership.LabelEvent]], list[str]]:
    """Keep only the label events whose actor may hand work to the engine (#110's Work tier).

    Adding the ownership label starts a worker, so it is a work-level act and not merely a label
    edit. An unauthorized labeler is ignored — the label stays, but it starts nothing — and an
    actor the engine could not check is deferred rather than trusted.
    """
    fresh = {
        key: ids
        for key, ids in events.items()
        if ids
        and ownership.event_id(ids[-1])
        != (state.get("ownership", {}).get("events", {}) or {}).get(key)
    }
    if not fresh:
        return events, []
    actors = port.ownership_actors({
        key: ownership.event_id(ids[-1]) for key, ids in fresh.items()
    })
    kept: dict[str, list[str | ownership.LabelEvent]] = dict(events)
    deferred: list[str] = []
    for key in fresh:
        actor = actors.get(key)
        if actor is None:
            deferred.append(key)
            kept.pop(key, None)
            continue
        if policy.bot and actor.lower() == policy.bot:
            # The engine's own account: an agent chat, or the AgentDeck App, adding the label
            # because the operator asked it to (#2390). The engine itself stopped writing the
            # label in #2053, so this is never its own echo. Only the label counts: this
            # account's comments still authorize nothing.
            card = _cards(state).get(key)
            if card is not None:
                card.pop(_APP_LABEL, None)
                card[_OWN_LABEL] = True
            authority.record(card, login=actor, source="label")
            continue
        if queue.is_bot_login(actor):
            # An app may steer a card, never start one (#2056) — and the label is a start. An
            # allowlisted app clears this very bar, so without this it could hand itself the
            # card by labelling the issue. Consumed like a denied label: left where the app put
            # it, judged once rather than on every wake, starting nothing.
            #
            # The label stays on the issue, though, and the live "is it labelled" fact is what
            # `_claimed` reads — which the blocked-card resume acts on without needing a
            # dispatch or a start of its own. So who put it there is remembered on the card.
            ledger = state.setdefault("ownership", ownership.new_ledger())
            ledger.setdefault("events", {})[key] = ownership.event_id(events[key][-1])
            kept.pop(key, None)
            card = _cards(state).get(key)
            if card is not None:
                card.pop(_OWN_LABEL, None)
                card[_APP_LABEL] = True
            outcome.log.append(f"ownership: {key} labeled by the app {actor} — not a start")
            continue
        repo, _number = _split(key)
        verdict = auth.decide(
            policy,
            auth.OWNERSHIP_TIER,
            login=actor,
            permission=port.permission(repo, actor) if actor else None,
        )
        if verdict is auth.Verdict.UNCERTAIN:
            deferred.append(key)
            kept.pop(key, None)
            outcome.report.failed(f"authorization uncertain for the labeler of {key}")
            continue
        if verdict is auth.Verdict.DENIED:
            # The label stays where the human put it; it simply does not start anything. The
            # event is recorded as seen directly, so it is not re-judged on every wake, but it
            # never reaches the start classifier.
            ledger = state.setdefault("ownership", ownership.new_ledger())
            ledger.setdefault("events", {})[key] = ownership.event_id(events[key][-1])
            kept.pop(key, None)
            outcome.log.append(f"ownership: {key} labeled by {actor or 'nobody'} — not authorized")
            continue
        # Authorized: handing the engine ownership by hand is how this card's work was asked
        # for, so it is this card's work authority just as a `/claude` comment would be (#1659).
        card = _cards(state).get(key)
        # A person's label supersedes an app's on the same card: the label on the issue is
        # theirs now, whoever added it first (#2056).
        if card is not None:
            card.pop(_APP_LABEL, None)
            card.pop(_OWN_LABEL, None)
        authority.record(card, login=actor, source="label")
    return kept, deferred


# --- phase 3: reconciliation ---

def _live_check_due(
    rec: dict, *, now: datetime, terminal_recheck_s: int, queued_control: bool = False
) -> bool:
    """Active state is always fresh; only a truly idle terminal card may cool down."""
    always_due = (
        queued_control
        or rec.get("status") == _IN_PROGRESS
        or bool(rec.get("stopping"))
        or bool(rec.get("dispatch"))
        or bool(rec.get("pending"))
        or bool(rec.get("processing"))
        or bool(rec.get("recovery_action"))
        # A label re-add is a request to start, and it can land on a card resting in any
        # terminal column. Cooling that card down meant the wake that armed a worker for it
        # never re-read the board, so the reconcile that releases a Done card was skipped and
        # the dispatch below armed on a stale column (#1581).
        or bool(rec.get("manual_start"))
        or bool(rec.get("forget_pending"))
        or bool(rec.get("release_pending"))
        or bool(rec.get("release_control"))
        or release.release_recheck_wanted(rec)
        or rec.get("mode") in {"merge", "merge-review"}
        or _awaiting_blocker(rec)
    )
    if always_due or rec.get("status") not in _TERMINAL_STATUSES:
        return True
    try:
        checked = datetime.fromisoformat(str(rec.get("last_live_check") or ""))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
    except ValueError:
        return True
    return (now.astimezone(UTC) - checked.astimezone(UTC)).total_seconds() >= terminal_recheck_s


def _awaiting_blocker(rec: dict) -> bool:
    """Is this card resting in Blocked only until something it names closes? (#2241)

    Such a card is not idle: ``_reconsider_blocked`` picks it back up on the first wake that
    reads it after the last blocker closes, and the card's own notice promises exactly that
    wake. Cooling it down with the other terminal columns let it sit for up to a whole
    ``terminal_recheck_s`` after the blocker merged — long enough that a person watching the
    board nudged it by hand. The read is bounded anyway: a quiet wake reads no card at all, and
    a blocker closing is a webhook delivery that makes the next wake a full one.

    Both holds count: the refs a worker named (``blocked_until``) and the dependencies the last
    read still showed open (``blocked_by``), which is what the #1588 fallback resumes from. A
    card parked in Blocked with neither is somebody's decision and keeps cooling down.

    A green verification that landed while the card rested there counts too (#2306): it is a
    resume waiting for its first full read.
    """
    if rec.get("status") != config.Role.BLOCKED.value:
        return False
    return bool(
        card_blockers.resuming(rec)
        or rec.get("blocked_by")
        or card_blockers.verified_while_blocked(rec)
    )


_NEVER = datetime.min.replace(tzinfo=UTC)


def _instant(stamp: object) -> datetime | None:
    """A GitHub timestamp as an aware instant, or None when it cannot be read."""
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _person_instruction(control: dict) -> bool:
    """Is this a ``/claude`` a person typed on the issue? (#2417)

    Not an app's, and not one the engine made up itself — a release-bits rebase, a verify turn,
    a handoff, an extension's proposal — which carry ``prefix:``-shaped ids rather than a
    comment's.
    """
    cid = str(control.get("id") or "")
    return (
        control.get("kind") == "claude"
        and bool(cid)
        and ":" not in cid
        and not _uncommented(control)
        and not _app_authored(control)
    )


def _deck_instruction(control: dict) -> bool:
    """Is this a ``/claude`` sent through the deck for a person, not by the reviewer? (#2748)

    It reopens a closed issue like the person's own comment would: a worker whose card was
    released after its merge can ask, with the person's leave, for the card back.
    """
    return (
        control.get("kind") == "claude"
        and deck_releases.is_deck_control(control)
        and not _app_authored(control)
    )


def _written_after(control: dict, removal: ownership.LabelRemoval) -> bool:
    """Was this instruction written after the label removal? (#2417)

    The issue's timeline decides when it lists the comment, because it orders two things GitHub
    stamped in the same second. Only a comment outside the page it read falls back to the
    stamps, where a tie goes to the removal: stopping is the safe reading of a dead heat.
    """
    cid = str(control.get("id") or "")
    if cid in removal.comments_after:
        return True
    if cid in removal.comments_before:
        return False
    removed_at = _instant(removal.created_at)
    if removed_at is None:
        return True  # no removal on record: nothing to be older than
    written_at = _instant(control.get("created_at"))
    return written_at is not None and written_at > removed_at


def _closed_before(snapshot: CardSnapshot, asked_at: object) -> bool:
    """Was this issue closed before the instruction asking for work on it was written? (#2417)

    Strictly before: GitHub's "Close with comment" posts the comment and closes the issue in the
    same second, and that is a person closing the issue, not asking for it to be reopened. An
    unknown time on either side reopens nothing, which is what the engine did before.
    """
    closed_at = _instant(snapshot.closed_at)
    asked = _instant(asked_at)
    return closed_at is not None and asked is not None and closed_at < asked


def _live_person_instructions(state: dict, key: str) -> list[dict]:
    """A person's instructions on this card that are still new, queued or being worked."""
    repo, number = _split(key)
    return [
        control
        for control in queue.controls(state).values()
        if control.get("repo") == repo
        and control.get("number") == number
        and control.get("status") in {_NEW, _QUEUED, _PROCESSING}
        and _person_instruction(control)
    ]


def _weigh_hand_back(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    snapshot: CardSnapshot,
    bot: str,
) -> CardSnapshot:
    """Decide a missing label against the instructions still live on the card (#2417).

    A card seen wearing the label and now without it is a person's hand-back — unless a person
    also asked for work on it. Whichever of the two happened last wins: an instruction written
    after the removal keeps the card, and a removal made after every instruction stops it as
    before. Only this conflict costs a read; a card with no live instruction is handed back
    without one, exactly as it always was.

    A removal made by the engine's own account after the instructions is not a hand-back
    anybody asked for. The engine never removes the label itself (#2497), so this is somebody
    else acting as that account, and the hand-back is made loud: the card parks in Needs More
    Info with a comment saying what was cancelled, instead of resting quietly in Todo.
    """
    if (
        snapshot.owned
        or not rec.get("label_claim")
        or rec.get("stopping")
        or not reconcile.hands_back_on_missing_label(rec)
    ):
        # A stopping card was decided on an earlier wake; weighing it again would re-read the
        # removal every wake of the stop and forget that its notice was already posted. A card
        # still waiting for its first start is kept whatever the label says (#1663).
        return snapshot
    instructions = _live_person_instructions(state, key)
    if not instructions:
        return snapshot
    repo, number = _split(key)
    removal = port.ownership_removal(repo, number)
    if removal is None:
        # Uncertainty never cancels a person's instruction: read the card as still labelled
        # for this wake, and decide on one that can see the removal.
        outcome.log.append(
            f"ownership: {key} label gone but its removal could not be read — "
            "hand-back held for a wake that can"
        )
        return replace(snapshot, owned=True)
    newer = [control for control in instructions if _written_after(control, removal)]
    if newer:
        # No removal on record, or an instruction written after it: the newest ask is work.
        rec.pop("label_claim", None)
        outcome.log.append(
            f"ownership: {key} label removed, but instruction {newer[-1].get('id')} is newer "
            "-> kept"
        )
        return snapshot
    outcome.handed_back[key] = removal
    actor = removal.actor.strip().lower()
    if bot and actor == bot:
        rec["loud_hand_back"] = {
            "actor": removal.actor,
            "removed_at": removal.created_at,
            "controls": [str(control.get("id")) for control in instructions],
        }
        outcome.log.append(
            f"ownership: {key} label removed by the engine's own account after the last "
            "instruction -> stopping, and saying so"
        )
    return snapshot


def _reopen_on_sight(
    port: EnginePort, outcome: CycleOutcome, *, key: str, rec: dict, snapshot: CardSnapshot
) -> CardSnapshot | None:
    """Reopen a closed issue a person's instruction was queued on (#2417).

    Queuing the instruction leaves the marker. A dispatch that can see the issue settles it
    itself; otherwise — a card seeded in the same wake, or one held by the worker cap — the
    first live read does, before a closed card would be cleaned up. The marker is spent on that
    first read either way, so an issue closed later — a linked pull request merging under a
    live worker — is not reopened by it. None means the reopen was refused and the card must
    wait for the next wake rather than be cleaned up as closed.
    """
    asked_at = rec.pop("reopen_if_closed", None)
    if not asked_at or snapshot.open or not _closed_before(snapshot, asked_at):
        return snapshot
    repo, number = _split(key)
    if not _do(port, outcome, Effect(EffectKind.REOPEN_ISSUE, repo, number)):
        rec["reopen_if_closed"] = asked_at
        outcome.log.append(f"reopen pending: {key} (a person's instruction is running on it)")
        return None
    reopened = replace(snapshot, open=True)
    outcome.live_snapshots[key] = reopened
    outcome.log.append(f"reopened: {key} for a person's instruction")
    return reopened


def _reconcile(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    cap: int,
    dispatch_ok: bool,
    store: StateStore | None = None,
    placeholder_titles: tuple[str, ...] = (),
    terminal_recheck_s: int = config.DEFAULT_TERMINAL_RECHECK_S,
    ready_to_merge: str | None = None,
    bot: str = "",
) -> None:
    """Self-heal every managed card from its live snapshot."""
    _credit_verification(state, port, outcome, now=now)
    decisions: dict[str, reconcile.ReconcileDecision] = {}
    queued_cards = {
        queue.card_key(str(control.get("repo") or ""), int(control.get("number") or 0))
        for control in queue.controls(state).values()
        if control.get("status") in {_NEW, _QUEUED, _PROCESSING}
        and isinstance(control.get("number"), int)
    }
    due = {
        key: CardQuery(
            *_split(key),
            generation=str(rec.get("generation") or ""),
            stop_generation=stop.stopped_generation(rec),
            stop_requested_at=str(rec.get("stop_requested_at") or ""),
        )
        for key, rec in list(_cards(state).items())
        if _live_check_due(
            rec,
            now=now,
            terminal_recheck_s=terminal_recheck_s,
            queued_control=key in queued_cards,
        )
    }
    batch = port.snapshot_cards(due)
    if batch.deferred:
        outcome.log.append(f"{batch.reason} — live card reconciliation deferred")
        return
    outcome.live_snapshots.update(batch.cards)
    _file_bug_issues(state, port, outcome, now=now, store=store)

    for key in due:
        rec = _cards(state).get(key)
        if rec is None:
            continue
        repo, number = _split(key)
        snapshot = batch.cards.get(key)
        if snapshot is None:
            # One unreadable card never stalls the wake.
            outcome.report.scan_failures += 1
            outcome.log.append(f"reconcile skipped: {key} (not readable this wake)")
            continue
        rec["last_live_check"] = now.astimezone(UTC).isoformat()
        reopened = _reopen_on_sight(port, outcome, key=key, rec=rec, snapshot=snapshot)
        if reopened is None:
            decisions[key] = reconcile.ReconcileDecision.ACTIVE
            continue
        snapshot = reopened
        if snapshot.spec_only:
            rec["spec_only"] = True
            rec.pop(_SPEC_APPROVED, None)
        elif rec.get("spec_only") and _spec_worker_active(rec, snapshot):
            # The label came off while the spec worker is mid-turn, or a chat answer is about to
            # resume it. The person may have said anything in that chat, so only that worker's
            # own finish decides whether the build starts (#2773); the record stays a spec card
            # until then.
            pass
        elif (
            rec.pop("spec_only", None)
            and rec.get("started")
            and rec.get(_SPEC_BUILD_IN_CHAT) != rec.get("generation")
        ):
            # The label came off a resting card a spec worker already ran on: a person approved
            # the spec. ``_start_approved_spec`` starts the build once the card rests (#2706),
            # unless the spec worker already took it over in its chat (#2716).
            rec[_SPEC_APPROVED] = True
        if snapshot.paused:
            rec["paused"] = True
        else:
            # Resumed: a later pause is a new one, and says so again.
            rec.pop("paused", None)
            rec.pop("paused_notice", None)
        if snapshot.open:
            _repair_title(
                port, outcome, key=key, rec=rec, snapshot=snapshot, names=placeholder_titles
            )
            # Reconciliation is the one phase that reads every managed card, so this is where
            # a dependency is noticed — and it is noticed for every card, not only the ones
            # about to start, so the deck can show a card that is waiting on another (#640).
            card_blockers.record(rec, snapshot.blockers)
            _record_park_note(rec, snapshot)
        # A card that is going away is cleaned up first: an outstanding spawn must never
        # keep a closed card alive waiting for an acknowledgement that cannot come.
        closing = not snapshot.open or bool(rec.get("forget_pending"))
        if not closing and _reconcile_dispatch(
            state, port, outcome, key=key, rec=rec, now=now, dispatch_ok=dispatch_ok,
            store=store, ready_to_merge=ready_to_merge,
        ):
            # An unacknowledged spawn owns this card until it is acknowledged or given up on.
            decisions[key] = reconcile.ReconcileDecision.ACTIVE
            continue
        if key in queued_cards:
            # A person's command on the card is theirs to run, not this Run's to take back, and a
            # card held for ``reopen`` rests in Done, where nothing may start (#2522).
            rec.pop("resolved_finish", None)
        was_in_progress = rec.get("status") == _IN_PROGRESS
        snapshot = _weigh_hand_back(
            state, port, outcome, key=key, rec=rec, snapshot=snapshot, bot=bot
        )
        decision = reconcile.reconcile_card(rec, snapshot, now=now)
        decisions[key] = decision
        _note_await_terminal(
            outcome,
            key=key,
            rec=rec,
            decision=decision,
            snapshot=snapshot,
            was_in_progress=was_in_progress,
            now=now,
        )
        _note_reportless_end(
            state, port, outcome, key=key, rec=rec, decision=decision, snapshot=snapshot,
            now=now, dispatch_ok=dispatch_ok, store=store,
        )
        _act_on(state, port, outcome, key=key, rec=rec, decision=decision, snapshot=snapshot)
        _reconsider_blocked(
            state, port, outcome, key=key, rec=rec, decision=decision, snapshot=snapshot
        )
        _start_approved_spec(
            state, port, outcome, key=key, rec=rec, decision=decision, snapshot=snapshot
        )


def _credit_verification(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, now: datetime
) -> None:
    """Attach each finished verification job to its Run as release evidence (#2154).

    A Run proves its work with an explicit verification job; ``verification.credited_head``
    decides whether one proves a commit. Crediting is ``release.record_tests_pass`` and
    nothing more: evidence stored by commit, which a later head move makes stale on its own.

    Nothing waits here and nothing parks. A job still running, one that failed, or one that
    ran over an unclean tree simply credits nothing, and the bit stays red until a green run
    on the current head does. Only local host reads, so a quiet wake credits too.
    """
    cards = {
        key: rec for key, rec in _cards(state).items() if str(rec.get("generation") or "")
    }
    if not cards:
        return
    jobs = port.verification_jobs()
    if not jobs:
        return
    owner = getattr(port, "org", "")
    for key, rec in cards.items():
        run_id = str(rec.get("generation") or "")
        repo, number = _split(key)
        work = (
            dispatch.work_id(owner, repo, number)
            if isinstance(owner, str) and owner.strip()
            else ""
        )
        credited = release.tests_pass(rec)
        for job in jobs:
            head = verification.credited_head(job, run_id=run_id, work_id=work, jobs=jobs)
            own_run = bool(head)
            if not head:
                # The suite outlived the worker that submitted it: an earlier generation of this
                # same card (#2306). The commit it proves is still proven.
                head = verification.credited_head_for_work(job, work_id=work, jobs=jobs)
            if not head:
                continue
            # If this round was the engine's own rebase, the commit its suite ran on *is* the
            # rebase's output, and this is the first moment that head exists to be recorded.
            # Binding here rather than at dispatch is what stops a push landing in between
            # from inheriting the carry-over (#2122). Only this Run's own job may bind it: an
            # earlier Run's suite ran on whatever that Run left, not on this round's rebase.
            # Bound before the already-credited check, so an earlier Run's job that credited
            # the same head first — whatever order the queue lists them in — cannot take the
            # binding away from the evidence that owns it (review of #2307).
            if own_run:
                rebased = release.take_pending_rebase_round(
                    rec, round_id=_round_id(_ids(rec, "processing"))
                )
                if rebased:
                    _bind_rebase_round(rec, rebased, head=head, now=now)
            # The usual case is later: the rebase round queued its suite and ended, so the job
            # names a round that is over. Its Run kept the references for exactly this job, and
            # the head it binds is still the one it tested (#2449).
            rebased = release.take_rebase_round_for_run(rec, run_id=str(job.get("run_id") or ""))
            if rebased:
                _bind_rebase_round(rec, rebased, head=head, now=now)
            if head in credited:
                continue
            release.record_tests_pass(rec, head=head, now=now)
            credited = release.tests_pass(rec)
            if state.get(_VERIFY_TURN_ENABLED):
                # A new green run is the one thing the release bits were waiting on, so the
                # card is read now rather than when its idle re-check comes round (#2449).
                release.want_release_recheck(rec)
            # Kept whichever column the card is in when the result lands (review of #2307).
            # Green can be credited while the worker is still running and its Blocked finish
            # land a wake later; the mark then waits until the card rests in Blocked, and only
            # the next dispatch start forgets it. ``_resume_verified`` spends it (#2306).
            card_blockers.note_verified(rec, head=head, job=str(job.get("id") or ""))
            forgiven = [dict(item) for item in verification.forgiven_tests(job, jobs)]
            outcome.log.append(
                f"release: {key} tests green on {head[:12]} "
                f"(verification job {job.get('id') or 'unknown'}"
                f"{'' if own_run else ', from an earlier Run of this Work'}, tested "
                f"{verification.describe(verification.snapshots_tested(job))}"
                f"{f', forgiven {len(forgiven)} failing test(s)' if forgiven else ''})"
            )


_BUG_MARKER = "<!-- agentdeck-needs-retry-bug: {ident} -->"


def _file_bug_issues(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    store: StateStore | None,
) -> None:
    """File, or add to, the one labelled issue per Needs retry bug cause (#2888)."""
    _file_marked_issues(
        state, port, outcome, now=now, store=store, slot=bug_reports.RECORDS, what="bug report",
        marker=lambda ident, _record: _BUG_MARKER.format(ident=ident),
        title=lambda record, _pending: bug_reports.title(str(record.get("cause") or "")),
        body=lambda record, pending, marker: bug_reports.body(
            str(record.get("cause") or ""), pending, marker
        ),
        update=lambda record, pending: bug_reports.update(str(record.get("cause") or ""), pending),
        name=lambda record: str(record.get("cause") or ""),
    )


def _file_marked_issues(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    store: StateStore | None,
    slot: str,
    what: str,
    marker,
    title,
    body,
    update,
    name,
) -> None:
    """File, or add to, one labelled issue per record in ``state[slot]`` (#2888).

    Opened through ``open_issue``, which labels it with the ownership label in the same write,
    so a worker picks it up like any other card. Creating an issue is not idempotent, so this
    follows ``_open_proposal``: the intent is durable before the write, and a wake that finds
    an intent looks for the marked issue before it creates one. An issue already filed and
    still open gets a comment with the new evidence instead; a closed one means the fix did
    not hold, so a new issue is filed.
    """
    records = state.get(slot)
    if not isinstance(records, dict):
        return
    waiting = {
        ident: record
        for ident, record in records.items()
        if isinstance(record, dict) and record.get("pending")
    }
    if not waiting:
        return
    filed_refs = [
        f"{record['repo']}#{record['number']}"
        for record in waiting.values()
        if isinstance(record.get("number"), int)
    ]
    states = port.ref_states(filed_refs) if filed_refs else {}
    for ident, record in waiting.items():
        repo = str(record.get("repo") or "")
        pending = list(record.get("pending") or ())
        number = record.get("number")
        if isinstance(number, int):
            seen = states.get(f"{repo}#{number}")
            if seen is None:
                continue  # unreadable this wake: neither a comment nor a second issue
            if seen == "OPEN":
                if _do(
                    port, outcome,
                    _comment_effect(
                        state, repo, number, update(record, pending), include_session=False,
                    ),
                ):
                    record["pending"] = []
                continue
            record.pop("number", None)
        mark = marker(ident, record)
        found: int | None = None
        if record.get("opening_at"):
            found, conclusive = port.find_marked_issue(
                repo, mark, since=str(record["opening_at"])
            )
            if not conclusive:
                outcome.log.append(f"{what}: cannot yet confirm whether {ident} was filed")
                continue
        else:
            record["opening_at"] = now.astimezone(UTC).isoformat()
            if store is not None:
                store.checkpoint(state)
        if found is None:
            found = port.open_issue(repo, title(record, pending), body(record, pending, mark))
        if found is None:
            outcome.log.append(f"{what}: could not open an issue for {ident}")
            continue
        record.pop("opening_at", None)
        record["number"] = int(found)
        record["pending"] = []
        outcome.log.append(f"{what}: {repo}#{found} filed for {name(record)}")


def _note_await_terminal(
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    decision: reconcile.ReconcileDecision,
    snapshot: CardSnapshot,
    was_in_progress: bool,
    now: datetime,
) -> None:
    """Say in the cycle log that a card is holding a worker slot, and when it stops (#1091).

    Nothing reported this before: a card held at ``await-terminal`` spends a worker slot
    while the wake prints ``no directives this wake``, so the engine reads as idle when it
    is in fact one worker short. Both the hold and the stale release get a line naming the
    card, because either one is a thing an operator would otherwise have to read state.json
    to discover.

    ``release-gated`` gets the same treatment for the same reason (#1406). A card in a release
    mode still counts as a working card while its record says *In progress*, and a review that
    ends the wrong way can hold that slot with nothing in flight — for twenty-one days, in the
    case that asked for this. The line waits for the stale window so a review actually being
    worked on stays quiet, and then repeats every wake, because an operator who has to notice
    an absence never does.

    A stop that is still waiting gets it too, and needed it most (#2073). A stop has no
    deadline: it is released only when the Runtime proves the old Run stopped (#2159), because
    a timeout is not proof a provider process stopped writing a shared worktree. The instance
    behind #2073 sat silent for two and a half hours holding a worker slot. The wait is
    correct; the silence was not.
    """
    quiet = int(snapshot.quiet_seconds)
    window = int(reconcile.STALE_SECONDS)
    if decision is reconcile.ReconcileDecision.STOP_WAITING:
        # Measured from the stop itself, not from ``quiet_seconds``: that is the *smaller* of
        # issue-quiet and since-requested, so a stopping card whose issue is being commented
        # on would report seconds of waiting after hours of it.
        waited = int(stop.seconds_since_request(rec, now) or 0)
        outcome.log.append(
            f"reconcile: {key} has been stopping {waited}s and is still holding a worker "
            f"slot (waiting for the Runtime to stop Run {stop.stopped_generation(rec)})"
        )
        # Nothing is counted here. Past the window this stop degrades the instance's health,
        # but that is derived from the card's own record in ``stop.stalled`` — a wake that did
        # not read this card must not be able to say anything about it either way.
        return
    if decision is reconcile.ReconcileDecision.AWAIT_TERMINAL:
        outcome.log.append(
            f"reconcile: {key} board moved to {snapshot.status or 'unknown'} with no worker "
            f"report -> holding a worker slot ({quiet}s quiet of {window}s)"
        )
        return
    if (
        decision is reconcile.ReconcileDecision.RELEASE_GATED
        and rec.get("status") == _IN_PROGRESS
        and not rec.get("dispatch")
        and quiet > window
    ):
        outcome.log.append(
            f"reconcile: {key} {rec.get('mode')} has been quiet {quiet}s (over {window}s) "
            f"and is still holding a worker slot"
        )
        return
    escaped = {
        reconcile.ReconcileDecision.WORKER_ENDED,
        reconcile.ReconcileDecision.CLEANUP,
    }
    if (
        decision in escaped
        and was_in_progress
        and not rec.get("dispatch")
        and not snapshot.terminal_marker
        and snapshot.status
        and snapshot.status != _IN_PROGRESS
        and snapshot.run_ended
    ):
        outcome.log.append(
            f"reconcile: {key} never reported and the Runtime says its Run ended "
            f"-> releasing its slot"
        )



def _record_park_note(rec: dict, snapshot: CardSnapshot) -> None:
    """Keep what the current lease's worker last told a person, bound to that lease (#2332).

    The deck shows a parked card's question or block reason from it: the worker's chat may be
    long out of the deck's scan window, and this wake already read the comment. Bound to the
    generation, so a later Run's card never shows an older worker's words.
    """
    generation = str(rec.get("generation") or "")
    note = snapshot.terminal_note
    if not generation or not note:
        return
    entry = {"generation": generation, "status": snapshot.terminal_marker or "", "text": note}
    if rec.get("park_note") != entry:
        rec["park_note"] = entry


def _repair_title(
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    snapshot: CardSnapshot,
    names: tuple[str, ...],
) -> None:
    """Retitle a card that names no work: a template placeholder (#338) or a paste (#595).

    Reconciliation is where this belongs: it is the one phase that reads every managed card
    every wake, so such a title is repaired whether the card was just labelled, is being
    worked, or has been sitting in review — and the repair costs no query of its own.

    ``names`` may be empty. The placeholder branch is then simply never taken, while a title
    that is a screenshot's ``<img …`` tag or a bare link is still repaired: recognising it
    needs nothing a project has to declare.

    **The engine gets one attempt at naming a card, and it is recorded.** What it writes always
    names the work, so the match alone would usually not repeat — but "title starts with the
    placeholder" is a guess, and a human whose title genuinely opens with those words would be
    arguing with a poller that rewrote it on every wake. One recorded attempt makes that a
    single event somebody can see and undo, rather than a fight.

    A closed card is left alone by the caller. Renaming an issue nobody will open again is
    noise on a finished thread, and the reason to do it — telling live cards apart — is gone.
    """
    if rec.get("title_repaired"):
        return
    better = card_titles.repaired_title(snapshot.title, snapshot.body, names)
    if not better:
        return
    repo, number = _split(key)
    if _do(port, outcome, Effect(EffectKind.SET_TITLE, repo, number, better)):
        # Recorded only for a write that landed: a refused edit is retried next wake, like
        # every other effect the engine could not get GitHub to accept.
        rec["title_repaired"] = True
        outcome.log.append(f"title: {key} renamed to {better!r}")


_SAME_DELIVERY_REASONS = {
    "at_capacity": "host capacity",
    "active_turn": "an active card worker",
    "context_operation_pending": "a pending context operation",
    "host_shutting_down": "a host shutdown",
    "prior_run_executing": "an older Run still stopping",
    "retryable_same_delivery": "the same context delivery",
    "worker_changed": "a changed worker version",
}


def _reconcile_dispatch(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    now: datetime,
    dispatch_ok: bool,
    store: StateStore | None = None,
    ready_to_merge: str | None = None,
) -> bool:
    """Settle an outstanding dispatch; True while it still owns this card's reconciliation.

    A dispatch sent inside the acknowledgement window is *awaited*, never re-sent, so an
    uncertain delivery can never spawn a second worker. A retry keeps the lease and the
    delivery id, so the runtime replays a spawn that did land instead of refusing a new Run
    (#2824), and a spawn that never lands after the bounded attempts hands the work back
    with the instructions still queued.
    """
    outstanding = rec.get("dispatch")
    if not outstanding:
        return False
    repo, number = _split(key)
    if port.dispatch_acknowledged(repo, number, outstanding):
        # Record any worktree the host reported *before* the receipt is discarded — after that
        # the only account of what was created on disk is gone, and a card released later
        # would leave it behind forever.
        for path in port.dispatch_worktrees(repo, number, outstanding):
            reconcile.register_worktree(rec, path)
        # Likewise the chat that took it (#2155), which the next dispatch continues (#2127).
        dispatch.record_run_session(
            rec,
            port.dispatch_session_url(repo, number, outstanding),
            str(outstanding.get("generation") or ""),
        )
        if outstanding.get("steer"):
            _accept_steer(state, port, outcome, rec, outstanding)
        _discard_receipts(
            port, outcome, key, dispatch=outstanding,
            dispatch_id_only=_steer_like(outstanding),
        )
        rec.pop("dispatch", None)
        # Something landed, so the park this card may have come back from is spent history:
        # a later spawn that never lands earns its own free retry rather than inheriting a
        # used one (#1491).
        dispatch.clear_rearm(rec)
        outcome.log.append(f"dispatch acknowledged: {key}")
        return False

    deferred = port.dispatch_deferred(repo, number, outstanding)
    if deferred and outstanding.get("steer"):
        _discard_receipts(
            port, outcome, key, dispatch=outstanding, dispatch_id_only=True
        )
        rec.pop("dispatch", None)
        rec["steer_refused_generation"] = outstanding.get("generation")
        outcome.log.append(f"steer refused: {key} ({deferred}); instructions kept queued")
        return False
    if refusal_disposition(deferred).engine is EngineRetryDisposition.TERMINAL:
        # Every retry keeps this dispatch's id, and the runtime's delivery id is built from it,
        # so the refusal the ledger just gave is the answer to every later attempt too. Park
        # now rather than spend the cap discovering that: `agentdeck#1840` spent nineteen hours
        # and 797 attempts on this exact refusal, reading `In progress` to the board throughout.
        #
        # Not clean, despite being a refusal decided before any write. Nothing was written by
        # *this* attempt, but the conflicting row's outcome is precisely what is unknown, so a
        # free re-arm under a fresh id is the duplicate at-most-once exists to prevent.
        dispatch.observe_attempt(outstanding, clean=False, refusal=deferred)
        outcome.log.append(f"dispatch refused for good: {key} ({deferred})")
        _give_up_dispatch(
            state, port, outcome, key=key, rec=rec, now=now,
            ready_to_merge=ready_to_merge, permanent=True, final_refusal=deferred,
        )
        return True
    if refusal_disposition(deferred).engine is EngineRetryDisposition.SAME_DELIVERY:
        # Reuse this exact lease and delivery id. Capacity/active/pending refusals wrote
        # nothing; retryable_same_delivery is distinct because compact or successor state may
        # exist, but the runtime explicitly fenced the original message as unstarted.
        # Falls back to the raw reason: a refusal added to the policy without a phrase here
        # must not crash the whole wake, which once stalled every card of two instances.
        reason = _SAME_DELIVERY_REASONS.get(deferred, deferred)
        outstanding["sent_at"] = None
        _emit(
            state,
            port,
            outcome,
            key=key,
            rec=rec,
            attempt=outstanding,
            now=now,
            store=store,
        )
        outcome.log.append(f"dispatch deferred by {reason}: {key}")
        return True

    decision = dispatch.reconcile(
        outstanding,
        acknowledged=False,
        now=now,
        budget_ok=dispatch_ok or bool(outstanding.get("allow_over_budget")),
    )
    if decision is dispatch.DispatchDecision.AWAITING:
        outcome.log.append(f"dispatch awaiting acknowledgement: {key}")
        return True
    # This attempt is over, one way or the other. A deferral proves the host refused it before
    # writing anything; nothing at all means the send's outcome is unknown, and one unknown
    # attempt makes the whole dispatch ambiguous for good (#1491). Recorded here, at the only
    # points that end an attempt; awaiting ends nothing and returned above.
    dispatch.observe_attempt(outstanding, clean=bool(deferred), refusal=deferred)
    if decision is dispatch.DispatchDecision.EXHAUSTED:
        if _steer_like(outstanding) and not dispatch.steer_exhausted(outstanding):
            # The keyed runtime deduplicates this stable delivery id. Keep establishing the
            # outcome under the same lease; a fresh lease would invalidate the worker whose
            # active turn this message may already have joined.
            #
            # Bounded by `steer_exhausted` (#1886). Without it this branch is the whole of the
            # steer's future: a delivery the ledger holds as `uncertain` is refused on every
            # attempt by design, so the outcome this is establishing can never be established,
            # and the card asks once a window for the rest of time while reading `In progress`
            # to anybody looking. Past the bound it falls through to the ordinary park below.
            dispatch.retry_steer(outstanding)
            _emit(
                state, port, outcome, key=key, rec=rec, attempt=outstanding,
                now=now, store=store,
            )
            outcome.log.append(
                f"steer retry: {key} attempt {outstanding['attempts']} under the same lease"
            )
            return True
        _give_up_dispatch(
            state, port, outcome, key=key, rec=rec, now=now, ready_to_merge=ready_to_merge,
            final_refusal=deferred,
        )
        return True
    _discard_receipts(
        port, outcome, key, dispatch=outstanding, dispatch_id_only=_steer_like(outstanding),
    )
    if _steer_like(outstanding):
        if dispatch.steer_exhausted(outstanding):
            # Same bound as the exhausted branch above (#1886). `retry_steer` clears `sent_at`,
            # which is what `reconcile` reads to decide EXHAUSTED at all — so a steer that has
            # run out arrives here as an ordinary RETRY on the wake after it, and capping only
            # the branch above would leave the loop running through this one.
            _give_up_dispatch(
                state, port, outcome, key=key, rec=rec, now=now, ready_to_merge=ready_to_merge,
                final_refusal=deferred,
            )
            return True
        dispatch.retry_steer(outstanding)
    else:
        dispatch.next_attempt(rec, outstanding)
    _emit(state, port, outcome, key=key, rec=rec, attempt=outstanding, now=now, store=store)
    outcome.log.append(f"dispatch retry: {key} attempt {outstanding['attempts']}")
    return True


def _steer_like(outstanding: dict) -> bool:
    """Whether this dispatch is a message into a turn that may still be running."""
    return bool(outstanding.get("steer"))


def _give_up_dispatch(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, key: str, rec: dict,
    now: datetime, ready_to_merge: str | None = None, permanent: bool = False,
    final_refusal: str | None = None,
) -> None:
    """A spawn that never landed: keep the instructions, invalidate the lease, hand back.

    ``final_refusal`` is the host's refusal of the attempt that ended the dispatch, or None
    when that attempt's outcome is unknown. It only tells the park's reader whether the last
    positive refusal was the final attempt's or an earlier one's (#2141).

    A reviewer that never started leaves approved work that did not merge, so it rests
    wherever the other release exits rest (#1099). An ordinary work spawn that never landed
    is not a release attempt at all and still parks in *Blocked*.

    The park is announced on the card, and gated on that comment landing (#1491): a card
    resting in *Blocked* for a delivery failure, with nothing in its own thread saying so,
    reads exactly like a card blocked on real work. If the comment does not land nothing is
    consumed and the next wake gives up again, which is the same failure with a trace. A
    review handed back to the release columns is announced too but not gated on it: that
    hand-back is a release exit (#1099) and holding it on a comment would leave approved
    work in a state no release path is watching.
    """
    repo, number = _split(key)
    review = rec.get("mode") == "merge-review"
    status = (ready_to_merge or release.IN_REVIEW) if review else "Blocked"
    if not _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, status)):
        return
    outstanding = rec.get("dispatch") or {}
    attempts = int(outstanding.get("attempts") or 0)
    # A steer is a message *into* a worker that may still be running, not a spawn that never
    # started, so the notice describes a different thing and the receipts
    # discarded below are this delivery's alone (#1886). Taking the generation with them would
    # blind the engine to that worker's turn ending, which is the one signal still coming.
    boundary = _steer_like(outstanding)
    # The free retry belongs to a work spawn that parked, whose every attempt was positively
    # refused. A review handed back to In Review is picked up by the ordinary release path and
    # needs no re-arm; an ambiguous park must not have one, and its notice says why.
    clean = dispatch.provably_unwritten(outstanding)
    # A boundary never earns the free retry, however cleanly its attempts were refused. What
    # the re-arm does is start the card again under a fresh delivery id, and this delivery was
    # aimed at a worker whose existence the engine could not establish — which is the same
    # duplicate risk the ambiguous park is fenced for, arriving by a different road (#1886).
    rearming = not review and not boundary and clean and not rec.get("dispatch_rearmed")
    said = _do(
        port, outcome,
        _comment_effect(
            state, repo, number,
            dispatch.gave_up_notice(
                attempts, status=status, rearming=rearming, clean=clean,
                refused=dispatch.last_refusal(outstanding), permanent=permanent,
                boundary=boundary,
            ),
        ),
    )
    if not said and not review:
        outcome.log.append(f"dispatch given up: {key} but the notice did not land")
        return
    if review:
        rec.pop("mode", None)
        rec.pop("review_heads", None)
        rec.pop("review_diffs", None)
        rec.pop("review_trees", None)
    else:
        processing = _ids(rec, "processing")
        rec["pending"] = processing + [
            cid for cid in _ids(rec, "pending") if cid not in processing
        ]
        rec["processing"] = []
        for cid in processing:
            control = queue.controls(state).get(cid)
            if control:
                control["status"] = _QUEUED
        dispatch.mark_given_up(
            rec, now, clean=clean and not boundary, refusal=dispatch.last_refusal(outstanding),
            final_refused=bool(final_refusal),
        )
    # Say how much was tried and that nothing ever acknowledged (#482 follow-up). A bare
    # "given up" reads like a decision; #470 sat `Blocked` for four hours because a card
    # parked for a transport failure is indistinguishable from one blocked on real work.
    tried = f" after {attempts} attempt(s), never acknowledged" if attempts else ""
    _discard_receipts(
        port, outcome, key, dispatch=rec.get("dispatch"), dispatch_id_only=boundary
    )
    rec.pop("dispatch", None)
    rec["status"] = status
    outcome.report.deliveries_failed += 1
    outcome.log.append(
        f"dispatch given up: {key} -> {status}{tried} (instructions kept)"
    )


def _act_on(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    decision: reconcile.ReconcileDecision,
    snapshot: CardSnapshot,
) -> None:
    """Perform the outward part of one reconcile decision; state was already updated."""
    repo, number = _split(key)
    kinds = reconcile.ReconcileDecision
    if decision in _RUN_ENDING_DECISIONS:
        # When the Run itself said it was over — its terminal comment's own stamp, never the
        # wake that read it, or a stop posted in between would count as aimed at the Run. A
        # `/claude stop` written before it was answered by that ending (#2448). No readable
        # stamp records nothing, and such a stop hands the idle card back as it always has.
        ended = (
            snapshot.stopped_terminal_at
            if decision is kinds.STOP_ENDED
            else snapshot.terminal_at
        )
        if _instant(ended) is not None:
            rec[_RUN_ENDED_AT] = ended
        else:
            rec.pop(_RUN_ENDED_AT, None)
    if decision in {kinds.CLEANUP, kinds.CLEANUP_RETRY, kinds.RELEASED_DONE}:
        if decision is not kinds.CLEANUP_RETRY:
            _finish_processing(state, port, outcome, rec, cancelled=decision is kinds.CLEANUP)
            _cancel_pending(state, port, outcome, rec)
            if rec.get("mode"):
                _resolve_release_control(state, port, outcome, rec, status="cancelled")
        _release_card(state, port, outcome, key=key, rec=rec)
    elif decision is kinds.IDLE_RELEASE:
        _release_idle(state, port, outcome, key=key, rec=rec)
    elif decision is kinds.TERMINAL_RECOVERED:
        marker = snapshot.terminal_marker
        if marker and _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, marker)):
            rec["status"] = marker
            # The marker proves the worker delivered a round, and this path exists precisely
            # because the worker's own board write did not happen. Counting here is what keeps
            # a round that came back through recovery visible to a head-watching reviewer; the
            # generation key makes the worker's later retry free rather than a second round
            # (#1840).
            rounds.count_round(rec, str(rec.get("generation") or ""))
            # The card ended here exactly as it does under WORKER_ENDED — only the way it
            # was proven differs — so the commands it was carrying end with it. Left in
            # ``processing`` they keep the card busy to a later release, and a later state
            # load reattaches them (ledger.recover) as work to run all over again. A refused
            # status write above resolves nothing, so the next wake retries the whole ending.
            _finish_processing(state, port, outcome, rec)
            outcome.log.append(f"reconcile: {key} recovered terminal status {marker}")
    elif decision is kinds.STOP_ENDED:
        # The worker finished the turn the stop was interrupting, and said what it did. The
        # stop is satisfied by that, and the finished work keeps its own answer: publishing
        # Todo here would discard a completed PR's status and invite a second worker onto
        # it. So this lands like WORKER_ENDED — only the stop bookkeeping is extra (#299).
        marker = snapshot.stopped_terminal_marker
        if marker and _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, marker)):
            rec["status"] = marker
            _finish_processing(state, port, outcome, rec)
            stop_control = rec.get("stop_control")
            if stop_control:
                _mark_controls(
                    state, port, outcome, [str(stop_control)],
                    status="done", reaction=_REACTION_DONE,
                )
            if rec.get("mode"):
                _resolve_release_control(state, port, outcome, rec, status="cancelled")
            rec.pop("restart_after_stop", None)
            rec.pop("manual_restart_after_stop", None)
            rec.pop("loud_hand_back", None)  # the turn ended on its own; nothing was cancelled
            stop.finalize_stop(rec)
            outcome.log.append(f"reconcile: {key} stop met by a finished turn -> {marker}")
    elif decision is kinds.STOP_RELEASED:
        unawaitable = not stop.stopped_generation(rec)
        _finalize_stop(state, port, outcome, key=key, rec=rec)
        if unawaitable:
            outcome.log.append(
                f"reconcile: {key} stop released — it awaited no generation, so no Run "
                "ever started on this card and there was nothing to stop (#2111)"
            )
        else:
            outcome.log.append(
                f"reconcile: {key} stop confirmed — the Runtime stopped the old Run"
            )
    elif decision is kinds.WORKER_ENDED:
        _finish_processing(state, port, outcome, rec)
        outcome.log.append(f"reconcile: {key} worker ended -> {rec.get('status')}")


def _note_reportless_end(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    decision: reconcile.ReconcileDecision,
    snapshot: CardSnapshot,
    now: datetime,
    dispatch_ok: bool,
    store: StateStore | None = None,
    release_review: bool = False,
) -> None:
    """Resolve a card whose worker ended without reporting (#279, step 3; #802; #835).

    A card only leaves *In progress* on a worker's own lease-bound marker. When the worker
    never posts one, the card looks exactly like healthy work: the wake logs `no directives`,
    the queued follow-up sits `pending`, and nothing degrades until the two-hour stale window
    cold-reloads a second worker onto finished work.

    The Runtime can see the turn end and says so in the Run's outcome. What the engine does
    with that depends on *which* observation it is:

    * ``died``, or ``ended`` with any subtype — the turn is conclusively over and no report
      exists for it. The card is **revived**: re-dispatched under a fresh lease exactly as the
      stale window's cold reload would, keeping its queued instructions and its worktrees, and
      a comment says what was observed and what the new worker is being asked to do. A worker
      that was killed is simply run again; one that reached its own end quietly leaves work a
      second worker can go and look at, which beats a human doing the looking (#835).
      ``revive.py`` owns that policy, including the cap that hands a card that keeps ending
      silently to a human anyway.
    * ``gone`` — the deck has no worker on this card at all, which is what a runtime restart
      or a reboot produces for *every* in-flight card at once. That proves nothing finished,
      only that nothing is running, so it is reported and the card is left alone. Publishing
      on it would move a whole board's live work to Needs More Info on one restart.

    Past the cap the card is published to *Needs More Info* with the count and what the engine
    can see of the leftovers, and the commands it was carrying are resolved so a queued
    follow-up can finally dispatch. Needs More Info rather than In review because silence is
    still not success: nothing here reads a branch, a dirty file, or an open PR as a verdict.

    The observation is per lease, so an older Run's ending cannot speak for the generation now
    running, and a resolved lease is remembered so it cannot be resolved twice — a revive
    stamps it under the *dead* generation, since the spawn it performs mints a new one.
    """
    reportless_candidate = decision is reconcile.ReconcileDecision.ACTIVE or (
        release_review
        and decision is reconcile.ReconcileDecision.RELEASE_GATED
        and rec.get("status") == _IN_PROGRESS
    )
    if not reportless_candidate or snapshot.terminal_marker:
        return
    repo, number = _split(key)
    if any(
        control.get("kind") == intake.CommandKind.STOP.value
        for control in queue.pending(
            state, repo=repo, number=number, statuses={_NEW}
        )
    ):
        # Intake is durable before reconciliation, so an explicit stop already waiting in
        # this wake is authoritative. Do not announce or account for an automatic revive
        # that command processing would immediately cancel before host delivery.
        return
    if rec.get("stopping") or rec.get("dispatch"):
        # A stop has its own protocol, and an unacknowledged dispatch is not a turn yet.
        return
    generation = str(rec.get("generation") or "")
    if rec.get("reportless_resolved") == generation:
        # Already published for this lease. The Runtime keeps reporting the same ending and
        # `ACTIVE` is decided from the *board's* status, so a board that has not caught up
        # with the write yet would otherwise resolve the same ending again — and post a
        # second comment, every wake, forever. Keyed by generation, so a fresh lease is a
        # fresh question.
        return
    # The queue is read before the ending (#2300): the Runtime delivers a job's completion
    # before it marks the job notified, so a job read as notified already has the next turn
    # under way, and the ending read after it is that turn's own.
    jobs = port.verification_jobs()
    observation = port.turn_ended(repo, number, generation)
    if not isinstance(observation, dict):
        return
    observed = str(observation.get("observed") or "ended")
    subtype = str(observation.get("subtype") or "")
    detail = f" ({subtype})" if subtype else ""
    # The Runtime reports this when the account the delivery went to was provably unable to
    # run a turn at all. The turn was refused rather than attempted, which is a different thing
    # to tell the card and a different budget to spend (#1519).
    refused = str(observation.get("refused") or "")
    refused_account = str(observation.get("account") or "") if refused else ""
    # Only a turn that reached its own end can be waiting: a death, a stop or a refusal is over
    # whatever the queue holds.
    awaited, purpose = (
        _awaited_job(
            port, jobs, repo=repo, number=number, generation=generation,
            session_key=str(observation.get("session_key") or ""),
        )
        if observed == revive.ENDED and not refused
        else ("", "")
    )
    if awaited:
        # The worker queued its suite and ended its turn, as it is told to; the job's completion
        # wakes it (#2300). Nothing is spent and nothing is said: once the chat has been told,
        # the woken turn's own ending is the one that must carry a report. Any job of the Run
        # holds it, not only a verification job (#2622).
        kind = "verification job" if purpose == verification.PURPOSE else "job"
        outcome.log.append(
            f"reconcile: {key} worker turn {observed}{detail} waiting on its {kind} "
            f"{awaited} -> left in progress"
        )
        return
    background = revive.background_wait(observation, now=now.timestamp())
    if background:
        # The chat ended its turn while a background command of its own still runs; that
        # command's completion wakes it (#2890). The same hold as a queued job: nothing is
        # spent or said, and the woken turn's own ending is judged when it comes.
        outcome.log.append(
            f"reconcile: {key} worker turn {observed}{detail} waiting on background "
            f"task(s) {', '.join(background)} -> left in progress"
        )
        return
    if (
        observed == revive.ENDED
        and not refused
        and not revive.involuntary(observed=observed, subtype=subtype)
        and _quiet_chat_answer(port, rec, generation=generation, repo=repo, number=number)
    ):
        _repark_quiet_answer(state, port, outcome, key=key, rec=rec, generation=generation)
        return
    decided = revive.decide(
        rec, observed=observed, dispatch_ok=dispatch_ok, refused=bool(refused)
    )

    if decided is revive.ReviveDecision.REVIVE and rec.get("paused"):
        # A paused card starts nothing, a restart included (#2548). Nothing is recorded, so
        # the wake after the pause is lifted reads the same ending and restarts it then.
        outcome.log.append(
            f"reconcile: {key} worker turn {observed}{detail} -> not revived, paused"
        )
        return
    if decided is revive.ReviveDecision.REVIVE:
        _revive_card(
            state, port, outcome, key=key, rec=rec, now=now, store=store,
            generation=generation, observed=observed, subtype=subtype,
            detail=detail, refused=refused, refused_account=refused_account,
        )
        return

    # From here the card is not being restarted, so the ending is a lease nothing will
    # resolve on its own — which is exactly what the health counter means.
    outcome.report.reportless_ends += 1

    if decided is revive.ReviveDecision.REPORT:
        outcome.log.append(
            f"reconcile: {key} worker turn {observed} with no report{detail}"
            " — nothing is running, which is not proof anything finished; the card is left as it is"
        )
        return

    # Status first: it is the part an operator sees, and a wake that dies after it leaves the
    # card visibly needing attention rather than silently mid-resolution.
    if not _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, _NEEDS_INFO)):
        return
    rec["status"] = _NEEDS_INFO
    rec["reportless_resolved"] = generation
    if rec.get("mode") == "merge-review":
        # The review is no longer in flight once all bounded revives are spent. Leaving its
        # mode behind would keep the parked card in RELEASE_GATED forever and would leave an
        # explicit /merge review control processing despite the human handoff.
        _resolve_release_control(state, port, outcome, rec, status="cancelled")
        rec.pop("mode", None)
    _finish_processing(state, port, outcome, rec)
    tried = revive.attempts(rec)
    gave_up = (
        f"\n\nThe engine already restarted this card's work {tried} time(s) and every attempt "
        "ended without a report as well, so it stops trying and asks."
        if tried
        else ""
    )
    if observed == revive.DIED:
        saw = "The deck observed the worker's process disappear without ever finishing this turn"
    elif observed == revive.ABANDONED:
        saw = (
            "The Runtime observed the worker stop doing anything at all, with its turn never "
            "finishing, and stopped it"
        )
    elif observed == revive.STOPPED:
        saw = "The Runtime stopped the worker's turn before it finished"
    else:
        saw = f"The deck observed the turn finish{detail}"
    _do(port, outcome, _comment_effect(
        state, repo, number,
        f"**This card's worker ended without reporting.** {saw}, but no `claude-terminal` "
        "comment was posted for it, so the engine has no statement of what the work reached "
        "— and silence is not success.\n\n"
        "Moved to **Needs More Info** rather than guessing. Check whether the work was "
        f"finished; `/claude <instruction>` restarts it if not.{gave_up}"
        f"{_reportless_evidence(port, rec, repo=repo, number=number)}",
    ))
    outcome.log.append(
        f"reconcile: {key} worker turn {observed} with no report{detail} -> Needs More Info"
    )


def _produced(port: EnginePort, rec: dict, *, repo: str, number: int) -> dict | None:
    """What the card's work has produced so far: its pull requests and worktree commits.

    ``None`` when any part cannot be read, which proves nothing either way (#2774).
    """
    linked = port.linked_prs(repo, number)
    if linked is None or any(head is None for head in linked.open.values()):
        return None
    paths = reconcile.worktrees(rec)
    changes = port.worktree_changed_paths(paths) if paths else {}
    trees: dict[str, dict] = {}
    for path in paths:
        found = changes.get(path)
        if found is None:
            return None
        trees[path] = {
            "commits": [commit for commit, _paths in found.commits],
            "local": sorted(found.local),
        }
    return {
        "open": {ref: linked.open[ref] for ref in sorted(linked.open)},
        "merged": sorted(linked.merged),
        "withheld": sorted(linked.withheld),
        "closed_unmerged": sorted(linked.closed_unmerged),
        "worktrees": trees,
    }


def _quiet_chat_answer(
    port: EnginePort, rec: dict, *, generation: str, repo: str, number: int
) -> bool:
    """Whether this reportless ending is a chat-answer turn that only answered (#2774).

    A parked worker that a person answers in its chat is resumed under the same lease. When
    it replies in the chat and ends its turn without a new report, that is a conversation,
    not a crash: the lease already reported, and nothing about the work moved. Restarting it
    spends one of the card's restarts on a new worker that can only answer again.

    Every part must be proven: the resume belongs to this lease, the lease reported before it
    was parked, and the pull requests and worktree commits read now match what they were
    when the answer arrived. Anything unreadable is a change, so the ending is handled as it
    always was. A chat that took a spec's build over with ``start-implementation`` is building,
    never only answering (#2890).
    """
    if rec.get(_SPEC_BUILD_IN_CHAT) == generation:
        # The chat took the approved spec's build over (#2890): its turns build, and a research
        # or doc build changes no pull request or commit until it reports, so "nothing moved"
        # proves nothing here.
        return False
    resumed = rec.get("chat_resumed")
    if not isinstance(resumed, dict) or resumed.get("generation") != generation:
        return False
    if resumed.get("reported") is not True:
        return False
    before = resumed.get("produced")
    if not isinstance(before, dict):
        return False
    return _produced(port, rec, repo=repo, number=number) == before


def _repark_quiet_answer(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, key: str, rec: dict, generation: str
) -> None:
    """Put a card whose chat-answer turn only answered back in Needs More Info (#2774)."""
    repo, number = _split(key)
    if not _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, _NEEDS_INFO)):
        return
    rec["status"] = _NEEDS_INFO
    rec["reportless_resolved"] = generation
    # The park still stands on the lease's earlier report, so a later answer resumes it as one.
    rec["quiet_reparked"] = generation
    _finish_processing(state, port, outcome, rec)
    _do(port, outcome, _comment_effect(
        state, repo, number,
        "Back to **Needs More Info**: the worker answered in its chat and ended its turn "
        "without a new report, and its pull requests and commits did not change.",
    ))
    outcome.log.append(
        f"reconcile: {key} chat-answer turn ended with no report and no change -> Needs More Info"
    )


def _awaited_job(
    port: EnginePort,
    jobs: list[dict] | None,
    *,
    repo: str,
    number: int,
    generation: str,
    session_key: str = "",
) -> tuple[str, str]:
    """The job this lease's worker is still waiting on and its purpose, or ``("", "")``.

    Any job of the lease's Run, verification or not (#2300, #2622), and any job naming no Run
    that the Run's own chat (``session_key``) queued (#2902).

    An unreadable queue is ``("", "")``: it proves no wait, so the ending is handled as it always
    was rather than held for as long as the queue stays unreadable.
    """
    owner = getattr(port, "org", "")
    work = (
        dispatch.work_id(owner, repo, number) if isinstance(owner, str) and owner.strip() else ""
    )
    return verification.awaited_run_job(
        jobs, run_id=generation, work_id=work, session_key=session_key
    )


def _revive_card(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    now: datetime,
    store: StateStore | None,
    generation: str,
    observed: str,
    subtype: str,
    detail: str,
    refused: str = "",
    refused_account: str = "",
) -> None:
    """Restart a card whose worker ended without reporting, instead of parking it (#802, #835).

    This is the stale window's cold reload, run now on proof rather than in two hours on a
    timer: the same ``reload_action`` under a fresh lease, with the card's queued commands
    left in ``processing`` so the new worker is handed exactly the instructions the dead one
    was carrying. Nothing here re-sends a delivery — the dead lease is over, and the spawn is
    a new dispatch with its own id, so at-most-once is untouched.

    The dispatch is marked a *continuation*, which is the difference between running the work
    again and finishing it. The host turns that flag into a paragraph telling the new worker
    that the previous one left without a report, that its worktree and any pull request are
    still there, and that its own job is to check what is already done before repeating any of
    it — and to report either way. Without that, a revived worker's most likely first act on a
    card whose work was actually finished is to open a second pull request for it.

    The commands are deliberately *not* finished: that is what a park does, and this card is
    not finishing.

    A revived card holds the worker slot it already held — it was ``ACTIVE`` a moment ago —
    so this admits nothing past the concurrency cap and needs no admission of its own.
    """
    repo, number = _split(key)
    if _refresh_before_worker(port, outcome, key=key, rec=rec):
        return
    # A refused turn never ran, so it is not an attempt and spends nothing (#1519, #2159).
    attempt = revive.attempts(rec) if refused else revive.record(rec)
    reconcile.begin_cold_reload(rec, now=now)
    # Stamp the dead lease before `_spawn`: the new dispatch and this fence are checkpointed
    # together before any board write. If that write is refused, the dispatch is still durable
    # for the next wake and the old generation is still refused precisely (#1510).
    rec["reportless_resolved"] = generation
    if not _spawn(
        state, port, outcome, key=key, rec=rec, action=reconcile.reload_action(rec),
        now=now, store=store, reviving=True,
        # A refused turn asks not to be sent back where it was refused. The host walks the
        # same Overlay order without that account, which is what makes `fallback_accounts`
        # do something again.
        avoid_account=refused_account,
    ):
        # The board write was refused. A dispatch is installed regardless, so the next wake
        # settles it; nothing is retried from here and nothing is said on the card yet.
        return
    outcome.report.revives += 1
    if refused:
        _do(port, outcome, _comment_effect(
            state, repo, number,
            f"**This card's turn was refused, not started**: "
            f"{refused_account or 'the account it was delivered to'} could not run a turn at "
            f"all ({refused}), so nothing ran and there is nothing to look at.\n\n"
            "Handing the card out again on the next account rather than leaving it *In "
            "progress* with an empty chat. Its queued instructions and any worktree it "
            "registered are kept.",
        ))
        outcome.log.append(
            f"reconcile: {key} turn refused by {refused_account or 'its account'} "
            f"({refused}) -> re-dispatch (not counted as an attempt)"
        )
        return
    if observed == revive.DIED:
        why = "its process disappeared mid-turn"
    elif observed == revive.ABANDONED:
        why = "it went silent mid-turn and did nothing for hours, so the Runtime stopped it"
    elif observed == revive.STOPPED:
        why = "the Runtime stopped its turn"
    elif subtype == revive.USAGE_LIMIT:
        why = "its account hit its usage limit mid-turn"
    elif revive.involuntary(observed=observed, subtype=subtype):
        why = f"it was killed mid-turn{detail}"
    else:
        why = f"its turn reached its own end{detail} but it never reported"
    _do(port, outcome, _comment_effect(
        state, repo, number,
        f"**Restarting this card's worker** ({attempt} of {revive.MAX_REVIVES}): {why}, "
        "so there is no report of what it reached — but a fresh worker can go and look at what "
        "is there, which is better than the card waiting for a person to.\n\n"
        "Its queued instructions and any worktree it registered are kept, and the new worker is "
        "asked to check what is already done before repeating it, then report. If this many "
        "attempts all end without a report the card goes to **Needs More Info** for a human.",
    ))
    outcome.log.append(
        f"reconcile: {key} worker turn {observed} with no report"
        f"{detail} -> revive {attempt}/{revive.MAX_REVIVES}"
    )


def _reportless_evidence(port: EnginePort, rec: dict, *, repo: str, number: int) -> str:
    """What the engine can cheaply see of the work a silent worker left behind (#787).

    The park still lands on *Needs More Info* — a human decides — but the human deciding
    deserves more than the silence: the PR the worker may already have opened, and whether
    its registered worktrees hold uncommitted progress. Every read here degrades to a line
    saying it could not be read; nothing in it may block or reorder the park itself, which
    is why it runs after the status and the resolution fence are already durable.
    """
    lines: list[str] = []
    linked = port.linked_prs(repo, number)
    if linked is None:
        lines.append("- Linked pull requests could not be read this wake.")
    else:
        states = port.pr_states(sorted(linked.open)) if linked.open else {}
        for ref in sorted(linked.open):
            state = (states or {}).get(ref)
            if state is None:
                head = linked.open.get(ref)
                head_text = f" at `{head[:9]}`" if head else ""
                lines.append(f"- {ref} is open{head_text}; its state could not be read.")
            else:
                head_text = f" at `{state.head[:9]}`" if state.head else ""
                steps = f" ({', '.join(state.failed_steps)})" if state.failed_steps else ""
                lines.append(
                    f"- {ref} is open{head_text}, checks {state.check_state}{steps}."
                )
        for ref in sorted(linked.merged):
            lines.append(f"- {ref} is already merged.")
    paths = reconcile.worktrees(rec)
    counts = port.worktree_dirty_counts(paths) if paths else {}
    for path in paths:
        count = counts.get(path)
        if count is None:
            lines.append(f"- `{path}` — state could not be read.")
        elif count:
            lines.append(f"- `{path}` — {count} uncommitted file(s); the progress may be here.")
        else:
            lines.append(f"- `{path}` — clean, nothing uncommitted.")
    if not lines:
        return ""
    return "\n\nWhat the engine can see of the work it left:\n" + "\n".join(lines)


def _finalize_stop(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
) -> None:
    """Land a stop: hand the card back to Todo, then either restart it or release it.

    Each outward step may be refused, and the card stays ``stopping`` if one is, so the
    next wake retries rather than half-finishing a stop.
    """
    repo, number = _split(key)
    if rec.get("restart_after_stop"):
        rec.pop("loud_hand_back", None)  # the card is starting again; nothing was lost
    if not _announce_hand_back(state, port, outcome, key=key, rec=rec):
        return
    status = _hand_back_status(rec)
    if not _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, status)):
        return
    rec["status"] = status
    _finish_processing(state, port, outcome, rec, cancelled=True)
    stop_control = rec.get("stop_control")
    if stop_control:
        _mark_controls(
            state, port, outcome, [str(stop_control)], status="done", reaction=_REACTION_DONE
        )
    if rec.get("mode"):
        _resolve_release_control(state, port, outcome, rec, status="cancelled")
    manual_restart = bool(rec.pop("manual_restart_after_stop", False))
    restart = bool(rec.pop("restart_after_stop", False)) and (
        bool(rec.get("pending")) or manual_restart
    )
    _discard_receipts(port, outcome, key)
    stop.finalize_stop(rec)
    if restart:
        # Let the ordinary dispatch path re-own and start the card. It durably installs the
        # dispatch before attempting the label write, so a refusal remains a retryable spawn
        # instead of a half-finalized stop that can forget pending work and its worktree.
        rec["manual_start"] = manual_restart
        return
    _forget(state, key)
    outcome.report.releases += 1


def _hand_back_status(rec: dict) -> str:
    """Where a handed-back card rests: Todo, or Needs More Info when nobody asked (#2417)."""
    return _NEEDS_INFO if rec.get("loud_hand_back") else _TODO


def _announce_hand_back(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, key: str, rec: dict
) -> bool:
    """Say on the issue why a person's instruction was cancelled, once (#2417).

    Only for a hand-back ``_weigh_hand_back`` found nobody asked for. A person who removed
    the label got what they asked for, and that stop stays as quiet as it always was. Posted
    before the status moves, so a comment that did not land is retried with the rest of the
    hand-back; the ``announced`` mark keeps a retried hand-back from posting it twice.
    """
    loud = rec.get("loud_hand_back")
    if not isinstance(loud, dict) or loud.get("announced"):
        return True
    repo, number = _split(key)
    controls = queue.controls(state)
    asked = [
        str((controls.get(cid) or {}).get("url") or "") or f"comment {cid}"
        for cid in loud.get("controls") or ()
    ]
    body = (
        "I stopped the work on this issue and cancelled "
        + (", ".join(asked) if asked else "the instruction it was carrying")
        + ". The ownership label was removed"
        + (f" at {loud.get('removed_at')}" if loud.get("removed_at") else "")
        + f" by `{loud.get('actor') or 'this poller'}` — the account this poller runs as, "
        "although the poller never removes it itself — after the last instruction, and "
        "nobody asked for that. Post `/claude …` again to restart the work."
    )
    if not _do(port, outcome, _comment_effect(state, repo, number, body)):
        outcome.log.append(f"hand-back notice pending: {key}")
        return False
    loud["announced"] = True
    return True


def _release_card(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
) -> None:
    """Give the card back: release host worktrees, then forget it.

    The ownership label stays on the issue: the engine never removes it (#2497). Every
    worktree removal is checked and the record survives a refused one so the next wake
    retries it, because a forgotten card with a live worktree leaks the checkout.
    """
    repo, number = _split(key)
    paths = reconcile.begin_cleanup(rec)
    _discard_receipts(port, outcome, key)
    failed = _cleanup_worktrees(
        state, port, outcome, repo=repo, number=number, paths=list(paths)
    ) if paths else []
    if reconcile.record_cleanup(rec, failed=failed):
        _forget(state, key)
        outcome.report.releases += 1
        outcome.log.append(f"reconcile: {key} released")
    else:
        outcome.log.append(f"reconcile: {key} cleanup retry pending ({len(failed)} worktree(s))")


def _discard_receipts(
    port: EnginePort,
    outcome: CycleOutcome,
    key: str,
    *,
    dispatch: dict | None = None,
    generation: str | None = None,
    dispatch_id_only: bool = False,
) -> None:
    """Ask the host to drop acknowledgement receipts an abandoned lease left behind.

    Without this, a later lease — or a later card at the same number — could read a stale
    acknowledgement as its own and skip a delivery or a stop it never actually got.
    """
    repo, number = _split(key)
    value = None
    if dispatch is not None or generation is not None:
        value = {
            "dispatch_id": (dispatch or {}).get("id"),
            "generation": None if dispatch_id_only else (
                generation or (dispatch or {}).get("generation")
            ),
        }
    _do(port, outcome, Effect(EffectKind.DISCARD_RECEIPTS, repo, number, value))


def _forget(state: dict, key: str, *, released: list[str] | tuple[str, ...] = ()) -> None:
    """Drop the card's record, keeping the label event the ownership ledger last saw.

    The label stays on the issue after a release (#2497), so the next sweep still lists an open
    card. Keeping its last event id means that sweep reads the same label as already accounted
    for — no actor lookup and no start — while a removal and re-add is a new event and starts
    the card as it always did.

    What the card already merged outlives the record (#2914): its finished steps, plus
    ``released``, the pull requests a closing release just merged. A person who reopens the card
    gets a fresh record, and without this its next release would count those old merges as its
    own — re-watching their deploys, and reading an unmarked one as making a step release final.
    """
    rec = _cards(state).pop(key, None)
    ledger = state.setdefault(_RELEASED_KEY, {})
    remembered = [str(ref) for ref in ledger.pop(key, None) or []]
    refs = [*remembered, *release.steps_merged(rec or {}), *released]
    if refs:
        ledger[key] = list(dict.fromkeys(str(ref) for ref in refs))
        while len(ledger) > _RELEASED_CAP:
            ledger.pop(next(iter(ledger)))
    elif not ledger:
        state.pop(_RELEASED_KEY, None)


#: Per card handed to the deck (#2895), when and from which worker session, for the deck to
#: greet the Work's chat or start one. Oldest go first past the cap.
HANDED_OVER = "handed_over"
_HANDED_OVER_CAP = 500


def _hand_over_held(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, now: datetime
) -> None:
    """Let go of every card a switched project still holds: the deck runs it from here (#2895).

    The owner's call: nothing stays Poller-owned, in-flight cards included. The record is
    forgotten as a release forgets it (`_forget`, label kept, merges remembered). Its command
    controls are cancelled first so the independent ledger cannot recover the forgotten card;
    otherwise nothing outward happens here: the column is left where it is, and the worker's
    worktree stays with its chat, which carries on as the Work's chat. A worker mid-turn finds
    its control command refused as stale; the deck tells it what changed. A Done card is not held.
    """
    handed = state.setdefault(HANDED_OVER, {})
    for key, rec in list(_cards(state).items()):
        if str(rec.get("status") or "") == "Done":
            continue
        handed[key] = {
            "at": now.astimezone(UTC).isoformat(),
            "session": _run_chat_key(rec.get("run_session")),
            "status": str(rec.get("status") or ""),
        }
        _finish_processing(state, port, outcome, rec, cancelled=True)
        _cancel_pending(state, port, outcome, rec)
        _resolve_release_control(state, port, outcome, rec, status="cancelled")
        stop_control = rec.pop("stop_control", None)
        if stop_control:
            _mark_controls(
                state, port, outcome, [str(stop_control)],
                status="cancelled", reaction=_REACTION_REFUSED,
            )
        _forget(state, key)
        outcome.log.append(f"handed to the deck: {key}")
    while len(handed) > _HANDED_OVER_CAP:
        handed.pop(next(iter(handed)))
    if not handed:
        state.pop(HANDED_OVER, None)


def _run_chat_key(run_session: object) -> str:
    """The chat key in the card's recorded Run link (`dispatch.record_run_session`), or "".

    The link is ``…/chats/<chat id>`` or ``…/sessions/<session key>``; the deck resolves either
    to a chat it knows, and asks the Work's own owner when it does not.
    """
    url = run_session.get("session_url") if isinstance(run_session, dict) else None
    path = urlsplit(str(url or "").strip()).path
    for prefix in ("/chats/", "/sessions/"):
        if path.startswith(prefix):
            return path[len(prefix):].partition("/")[0]
    return ""


#: Per forgotten card, the pull requests it had merged and moved past (#2914).
_RELEASED_KEY = "released_prs"
#: Oldest entries go first; a card reopened after this many later ones were forgotten is rare,
#: and costs no more than the behaviour before the ledger existed.
_RELEASED_CAP = 2000


def _restore_released(state: dict) -> None:
    """Hand a card that has a record again the merges its forgotten record had moved past."""
    ledger = state.get(_RELEASED_KEY)
    if not isinstance(ledger, dict):
        return
    cards = _cards(state)
    for key in [key for key in ledger if key in cards]:
        release.note_steps_merged(cards[key], [str(ref) for ref in ledger.pop(key) or []])
    if not ledger:
        state.pop(_RELEASED_KEY, None)


# --- phase 4: new commands ---

def _no_card_here(
    state: dict, port: EnginePort, outcome: CycleOutcome, control: dict
) -> bool:
    """Settle a command on an issue no card holds, in a project whose Works the deck runs.

    #2895 step 5: every kind — `/claude`, `/reviewed`, `/merge`, a stop, an extension's — is
    settled as cancelled with a "seen" reaction and makes no card. The Work's own chat hears
    the comment from the router, and the deck takes its verdicts and merges, so a command left
    new here would only be re-read and logged on every wake.
    """
    if not outcome.works_router:
        return False
    key = queue.card_key(control["repo"], control["number"])
    if key in _cards(state):
        return False
    _mark_controls(
        state, port, outcome, [str(control["id"])],
        status="cancelled", reaction=_REACTION_RECEIVED,
    )
    outcome.log.append(f"no card: {key} — the deck runs this project's Works")
    return True


def _process_controls(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    commands: dict | None = None,
    mode: ext.Mode = ext.Mode.LIVE,
    ready_to_merge: str | None = None,
    ownership_label: str | tuple[str, ...] = "",
) -> None:
    """Apply this wake's fresh commands in FIFO order; release commands gate last."""
    fresh = queue.pending(state, statuses={_NEW})
    releases = []
    for control in fresh:
        if _no_card_here(state, port, outcome, control):
            continue
        kind = control.get("kind")
        if kind == intake.CommandKind.CLAUDE.value:
            # Before the findings are queued, so the note saying what they cost the Run's
            # verification reaches the worker ahead of them (#2350).
            _reject_on_findings(state, port, outcome, control, now=now)
            _queue_instruction(state, port, outcome, control, ownership_label=ownership_label)
        elif kind == intake.CommandKind.STOP.value:
            _request_stop(state, port, outcome, control, now=now)
        elif kind == intake.CommandKind.EXTENSION.value:
            _run_extension_command(
                state, port, outcome, control, commands=commands or {}, now=now, mode=mode
            )
        elif kind == intake.CommandKind.REVIEWED.value:
            _accept_reviewed(
                state, port, outcome, control, now=now, ready_to_merge=ready_to_merge
            )
        elif kind in {intake.CommandKind.MERGE.value, intake.CommandKind.MERGE_REVIEW.value}:
            releases.append(control)
        elif kind == "unauthorized":
            # Say who was refused and for what: a reaction alone left a reviewer's refused
            # `/reviewed pass` invisible everywhere but the comment itself (#2581).
            key = queue.card_key(control["repo"], control["number"])
            command = control.get("payload")
            _reject(port, outcome, control, f"unauthorized:{command}")
            outcome.log.append(
                f"refused: {key} /{command} from {control.get('author') or '?'} (not authorized)"
            )
        else:
            _reject(port, outcome, control)
    # Every instruction and stop from this scan is reflected in the queues before any
    # irreversible release gating reads them.
    for control in releases:
        _gate_release(state, port, outcome, control, now=now, ready_to_merge=ready_to_merge)


def _acknowledge(
    port: EnginePort, outcome: CycleOutcome, control: dict, reaction: str
) -> None:
    """Show a command's progress on the comment that asked for it.

    An extension's proposal has no such comment: its id is the synthesized ``ext:<name>:<key>``
    from ``_queue_proposal``, not a GitHub comment id, and reacting to it asks GitHub for a
    comment that does not exist. That is not a degraded acknowledgement but a failed effect, and
    a failed effect is what a first activation's canary refuses — so an instance whose canary
    wake happened to honor a proposal could never come up (#370). Nobody is waiting to be
    acknowledged here either: the requester is the extension, and it learns the outcome from the
    ledger. So skip the reaction rather than fail it.
    """
    if _uncommented(control):
        return
    _do(
        port, outcome,
        Effect(
            EffectKind.REACT, control.get("repo") or "", int(control.get("number") or 0),
            {"comment": control.get("id"), "reaction": reaction},
        ),
    )


def _proposed(control: dict) -> bool:
    """Did this instruction come from an extension rather than a person's comment?"""
    return str(control.get("author") or "").startswith("extension:")


def _uncommented(control: dict) -> bool:
    """Is there no real GitHub comment behind this instruction to react to?

    Three authors reach here without one: an extension's proposal, the engine's own
    instruction, and a command pressed on the deck (#2324, #2419). They are kept apart because
    only the reaction is common to them. A proposal additionally may not join an active turn —
    so ``_proposed`` stays the narrower question.
    """
    return (
        _proposed(control)
        or _engine_authored(control)
        or deck_releases.is_deck_control(control)
    )


def _engine_authored(control: dict) -> bool:
    """Was this queued instruction synthesized by the engine rather than typed by a person?"""
    return str((control or {}).get("author") or "").startswith("engine:")


def _plain_followup_ids(state: dict, pending: list[str]) -> list[str]:
    """The ordered plain `/claude` prefix that may join an active turn."""
    steerable: list[str] = []
    for cid in pending:
        control = queue.controls(state).get(cid)
        if (
            control is None
            or control.get("kind") != intake.CommandKind.CLAUDE.value
            or _proposed(control)
        ):
            break
        steerable.append(cid)
    return steerable


def _reject(port: EnginePort, outcome: CycleOutcome, control: dict, reason: str = "") -> None:
    """Refuse a command visibly: the reaction is how the requester learns it did not take."""
    control["status"] = _REJECTED
    if reason:
        control["rejection"] = reason
    _acknowledge(port, outcome, control, _REACTION_REFUSED)


def _mark_controls(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    ids: list[str],
    *,
    status: str,
    reaction: str,
) -> None:
    """Move command records to a new status and show it on their comments."""
    for cid in ids:
        control = queue.controls(state).get(str(cid))
        if control is None:
            continue
        control["status"] = status
        _acknowledge(port, outcome, control, reaction)


def _finish_processing(
    state: dict, port: EnginePort, outcome: CycleOutcome, rec: dict, *, cancelled: bool = False
) -> None:
    """Resolve the commands a worker was carrying once its turn ended."""
    ids = _ids(rec, "processing")
    if ids:
        _mark_controls(
            state, port, outcome, ids,
            status="cancelled" if cancelled else "done",
            reaction=_REACTION_REFUSED if cancelled else _REACTION_DONE,
        )
    rec["processing"] = []
    # The round is over however it ended — done, cancelled, parked. A rebase round that never
    # reported a head produced no engine-owned provenance, and must not leave a permission
    # behind for the next round's head to claim (#2122). Its suite usually outlives it, so the
    # round's references are kept under its Run for that one job to bind (#2449). Not for a
    # cancelled round: its lease may already have been superseded, and it vouches for nothing.
    if not cancelled:
        release.keep_rebase_round_for_run(rec, run_id=str(rec.get("generation") or ""))
    release.retire_pending_rebase_round(rec)
    # A verify round, by contrast, leaves a marker precisely because it may have pushed a fix
    # without rerunning anything: the next release-bits reading decides what it changed (#2253).
    release.end_verify_round(rec)


def _accept_steer(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    rec: dict,
    outstanding: dict,
) -> None:
    """Join an acknowledged active-turn follow-up to that turn's command lifecycle."""
    accepted = [str(cid) for cid in outstanding.get("control_ids") or []]
    pending = _ids(rec, "pending")
    landed = [cid for cid in accepted if cid in pending]
    if not landed:
        return
    landed_set = set(landed)
    rec["pending"] = [cid for cid in pending if cid not in landed_set]
    processing = _ids(rec, "processing")
    rec["processing"] = processing + [cid for cid in landed if cid not in processing]
    _mark_controls(
        state, port, outcome, landed, status=_PROCESSING, reaction=_REACTION_WORKING
    )


def _cancel_pending(
    state: dict, port: EnginePort, outcome: CycleOutcome, rec: dict
) -> int:
    """Drop the queue a stop or cleanup supersedes, telling each requester it did not run."""
    ids = _ids(rec, "pending")
    if ids:
        _mark_controls(
            state, port, outcome, ids, status="cancelled", reaction=_REACTION_REFUSED
        )
    rec["pending"] = []
    return len(ids)


def _resolve_release_control(
    state: dict, port: EnginePort, outcome: CycleOutcome, rec: dict, *, status: str
) -> None:
    """Resolve the command that asked for a release, and drop its approval snapshot."""
    cid = rec.pop("merge_control", None)
    rec.pop("review_heads", None)
    rec.pop("review_diffs", None)
    rec.pop("review_trees", None)
    if cid:
        _mark_controls(
            state, port, outcome, [str(cid)],
            status=status,
            reaction=_REACTION_DONE if status == "done" else _REACTION_REFUSED,
        )


def _release_running(rec: dict) -> bool:
    """Is this card's release past the point where a newer instruction may cancel it?"""
    return any(
        rec.get(field)
        for field in ("awaiting_merge", "awaiting_deploy", "done_pending", "blocked_pending",
                     "fix", "merged")
    )


def _app_start_refusal(login: str, ownership_label: str | tuple[str, ...]) -> str:
    """What the engine says when an app's instruction would have started a card (#2056)."""
    labels = (ownership_label,) if isinstance(ownership_label, str) else ownership_label
    named = [f"`{value}`" for value in labels if value]
    if len(named) > 1:
        label = f"one of the {' or '.join(named)} labels"
    elif named:
        label = f"the {named[0]} label"
    else:
        label = "the ownership label"
    return (
        f"**Not starting this card.** `{login}` is an app, not a person. Naming it a work "
        "controller lets it ask for more work on a card somebody already handed the engine; it "
        "does not let it hand over a card by itself.\n\n"
        "Nobody with accepted authority has handed this one over, so the instruction above would "
        f"have started work nobody asked for. To start it, add {label} or comment `/claude` "
        "yourself — the app's instructions are carried as usual from then on."
    )


def _app_authored(control: dict) -> bool:
    """Was this instruction written by an app? (#2056)

    Intake stamps the bit, but only onto records it builds — a comment already in the ledger is
    skipped before that. So a bot `/claude` checkpointed as ``new`` by the code this replaced
    survives the upgrade with no bit at all, and reading a missing bit as "not an app" would let
    exactly the instruction this guards against through, once, on the wake after a deploy. The
    author's login answers the same question for those. The engine's own synthesized
    instructions — a proposal, an engine round — never had a bot comment
    behind them and are not app commands whatever their author string looks like.
    """
    if "app" in control:
        return bool(control["app"])
    if _uncommented(control):
        return False
    return queue.is_bot_login(control.get("author"))


def _handed_to_the_engine(rec: dict | None) -> bool:
    """Did somebody with accepted authority put this card in play? (#2056)

    A record's mere existence does not answer that. ``_discover_labelled`` seeds one for every
    currently labelled issue *before* the labeler's authority is decided, so a denied labeler
    leaves a bare record behind and an unresolved one is deferred with its record still there —
    while the ownership rules are explicit that a denied label starts nothing and an uncertain
    one is deferred rather than trusted. What proves a hand-over is the acceptance itself: a
    label start the ledger classified, an instruction the engine recorded authority for, a card
    it has already taken, or work already under way on it.
    """
    if not rec:
        return False
    # A label's authority is recorded as soon as its *labeler* is authorized, which is before
    # `ownership.detect_starts` says whether the event was a start at all — a rebuilt ledger
    # starts nothing, and a standing label seen for the first time is adopted rather than acted
    # on. So the label's own acceptance is `manual_start`, set only for a classified start, and
    # a `label`-sourced authority proves nothing here on its own.
    asked = authority.current(rec)
    # A label an app added is not a hand-over either. The label's actor clears the same Work
    # policy a command does, and an allowlisted app clears it — so without this an app could add
    # the label itself, collect the classified start, and let its own `/claude` through the gate.
    started_by_hand = bool(rec.get("manual_start")) and not _labelled_by_an_app(rec)
    return bool(
        started_by_hand
        or rec.get("engine_claim")
        or rec.get("started")
        or rec.get("dispatch")
        or _ids(rec, "pending")
        or _ids(rec, "processing")
        or (asked and asked.get("source") != "label")
    )


def _queue_instruction(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    control: dict,
    *,
    ownership_label: str | tuple[str, ...] = "",
) -> None:
    """Queue an instruction, superseding a release that has not passed its point of no return."""
    if _no_card_here(state, port, outcome, control):
        return
    key = queue.card_key(control["repo"], control["number"])
    repo, number = _split(key)
    rec = _cards(state).get(key)
    removal = outcome.handed_back.get(key)
    if (
        removal is not None
        and _person_instruction(control)
        and not _written_after(control, removal)
    ):
        # Written before the label came off, and read in the wake that handed the card back:
        # the removal is the newer word, so this instruction goes with the rest instead of
        # restarting the card the person just stopped (#2417).
        _mark_controls(
            state, port, outcome, [str(control["id"])],
            status="cancelled", reaction=_REACTION_REFUSED,
        )
        outcome.log.append(
            f"instruction cancelled by a later hand-back: {key} comment {control['id']}"
        )
        return
    if _app_authored(control) and not _handed_to_the_engine(rec):
        # An app cannot start a card (#2056). It may only join one somebody with accepted
        # authority already put in play — an app that reviews pull requests kept opening work on
        # issues nobody had handed over, and since #2053 nothing labels them, so they did not
        # even look claimed.
        #
        # Two kinds of uncertainty are not refusals. A sweep this wake could not read, or
        # declined to run, proves nothing about the label; and a card whose labeler is still
        # being authorized is exactly the deferral the ownership rules ask for. Both leave the
        # command new for a wake that can decide, which is what uncertainty gets everywhere
        # else here: never permission, never a silent drop.
        if (
            not outcome.label_sweep_complete
            or key in outcome.ownership_deferred
            or (rec or {}).get(_DISCOVERY_PENDING)
        ):
            outcome.log.append(f"app start undecided: {key} — ownership is not settled yet")
            return
        #
        # The refusal is a comment, not just a reaction: the person who has to decide is not
        # the one being answered. Posted first, exactly like a refused release, so a comment
        # that did not land leaves the command new for the next wake instead of silently
        # swallowed.
        body = _app_start_refusal(str(control.get("author") or "an app"), ownership_label)
        if not _do(port, outcome, _comment_effect(state, repo, number, body)):
            outcome.log.append(f"app start refusal comment pending: {key}")
            return
        _reject(port, outcome, control, "app-cannot-start")
        outcome.log.append(f"app start refused: {key} comment {control.get('id')}")
        return
    behind_release = False
    if rec is not None and rec.get("mode") == "merge":
        if _release_running(rec):
            # Merging or deploying is under way and cannot be taken back — but the instruction
            # can wait for it instead of being thrown away (#1440). Dispatch passes over a card
            # in ``merge`` mode, so the queued command simply sits there: a handback clears the
            # mode and the next wake starts it as an ordinary follow-up, while a release that
            # completes cancels it along with the card it was addressed to.
            behind_release = True
        else:
            # A review-derived release that has not started yet: the newer instruction wins.
            if not _do(
                port, outcome, Effect(EffectKind.SET_STATUS, repo, number, release.IN_REVIEW)
            ):
                return  # retry next wake; the command stays new
            _resolve_release_control(state, port, outcome, rec, status="cancelled")
            rec.pop("mode", None)
            rec["status"] = release.IN_REVIEW
            outcome.log.append(f"release cancelled by a newer instruction: {key}")
    elif (
        rec is not None
        and rec.get("mode") == "merge-review"
        and (
            rec.get("stopping")
            or (rec.get("status") != _IN_PROGRESS and not rec.get("dispatch"))
        )
    ):
        # A queued review has not started, or its worker is already stopping; either way the
        # change request supersedes the release intent before the card is restarted.
        _resolve_release_control(state, port, outcome, rec, status="cancelled")
        rec.pop("mode", None)
        outcome.log.append(f"queued review cancelled by a newer instruction: {key}")
    if rec is None:
        known = key in _cards(state)
        rec = _cards(state).setdefault(key, {"status": _TODO})
        if not known:
            # The card is new to this engine. Whatever ownership label it already carries is
            # part of the world being adopted, not an instruction — the same rule the ledger
            # applies when it first initializes, applied per card, because a card can join long
            # after that. Without this, a card labelled before its first command is read as a
            # human re-adding the label mid-work and stops the lease it was just given.
            rec["adopt_ownership"] = True
    if control.get("spec_only") and not rec.get("started"):
        # The card was opened under the spec-only label this wake (#2874), and this wake's
        # dispatch runs before any reconciliation reads that label back.
        rec["spec_only"] = True
    pending = rec.setdefault("pending", [])
    cid = str(control["id"])
    if cid not in pending and cid not in _ids(rec, "processing"):
        pending.append(cid)
    control["status"] = _QUEUED
    # Everything that reaches here was authorized at intake: an unauthorized comment is
    # rewritten to the ``unauthorized`` kind and rejected instead. Remember who it was (#2056).
    authority.record(
        rec,
        login=str(control.get("author") or ""),
        source=f"control:{cid}",
        at=str(control.get("created_at") or ""),
    )
    rec["manual_start"] = False
    # A new instruction re-arms a card whose spawn was given up, and gives it its automatic
    # retry back: a person asking again is not the engine retrying itself.
    dispatch.clear_rearm(rec)
    if rec.get("stopping"):
        # The stop must complete first; the queued instruction restarts the card after.
        rec["restart_after_stop"] = True
    if _person_instruction(control) or _deck_instruction(control):
        # Asking for work on a closed issue reopens it (#2417): the dispatch does it when it can
        # see the issue, and otherwise the first live read does, before a closed card is cleaned
        # up — which would otherwise cancel this very instruction. The mark carries when the
        # ask was written, so only an issue closed before it is reopened.
        asked = str(control.get("created_at") or "")
        if asked and (_instant(asked) or _NEVER) >= (
            _instant(rec.get("reopen_if_closed")) or _NEVER
        ):
            rec["reopen_if_closed"] = asked
    _acknowledge(port, outcome, control, _REACTION_RECEIVED)
    if behind_release:
        outcome.log.append(f"control queued behind a release in flight: {key} comment {cid}")
    else:
        outcome.log.append(f"control queued: {key} comment {cid}")


def _request_stop(
    state: dict, port: EnginePort, outcome: CycleOutcome, control: dict, *, now: datetime
) -> None:
    """Stop a card: cancel its queue, and either await a safe worker exit or release it now."""
    key = queue.card_key(control["repo"], control["number"])
    rec = _cards(state).get(key)
    if rec is None:
        # Nothing is running: there is nothing to stop.
        _reject(port, outcome, control, "not-running")
        return
    if rec.get("mode") == "merge" and _release_running(rec):
        _reject(port, outcome, control, "release-in-flight")
        return
    if rec.get("stopping"):
        if not rec.get("restart_after_stop"):
            _reject(port, outcome, control, "already-stopping")
            return
        # This stop is only on its way to a restart — a re-added ownership label, or an
        # instruction queued behind it. A person asking to stop outranks that restart, so the
        # restart is what is cancelled, not this command (#2448).
        rec.pop("restart_after_stop", None)
        rec.pop("manual_restart_after_stop", None)
        rec["manual_start"] = False
        cancelled = _cancel_pending(state, port, outcome, rec)
        if rec.get("stop_control"):
            _mark_controls(
                state, port, outcome, [str(control["id"])],
                status="done", reaction=_REACTION_DONE,
            )
        else:
            rec["stop_control"] = str(control["id"])
            control["status"] = _PROCESSING
        outcome.log.append(
            f"stop: {key} cancelled the restart the running stop was heading for; "
            f"cancelled {cancelled} queued"
        )
        return
    if _stop_already_answered(rec, control):
        # The Run this stop was written against has ended since, and the card rests where
        # that Run put it — In review with a pull request, say. Releasing it now would throw
        # that away and leave `/merge` nothing to act on, for a stop whose target is gone.
        cancelled = _cancel_pending(state, port, outcome, rec)
        _mark_controls(
            state, port, outcome, [str(control["id"])], status="done", reaction=_REACTION_DONE
        )
        outcome.log.append(
            f"stop: {key} — the Run it was written against had already ended; left in "
            f"{rec.get('status')}; cancelled {cancelled} queued"
        )
        return
    cancelled = _cancel_pending(state, port, outcome, rec)
    if stop.request_stop(rec, now=now):
        rec["stop_control"] = str(control["id"])
        control["status"] = _PROCESSING
        abandoned = reconcile.abandon_dispatch(rec)
        if abandoned:
            _discard_receipts(port, outcome, key, dispatch=abandoned)
            outcome.log.append(f"stop: {key} dropped an outstanding dispatch")
        outcome.log.append(f"stop requested: {key}; cancelled {cancelled} queued")
        return
    # Idle: there is no worker to wait for, so hand the card straight back.
    rec.update(release_pending=True, release_control=str(control["id"]))
    control["status"] = _PROCESSING
    _release_idle(state, port, outcome, key=key, rec=rec)
    outcome.log.append(f"stop on an idle card: {key} released; cancelled {cancelled} queued")


def _stop_already_answered(rec: dict, control: dict) -> bool:
    """Was this stop written while a Run that has ended since was still running? (#2448)

    Only for an idle card. A stop written on a card that was already idle still hands it back,
    as it always has; this is the stop that raced the Run's own ending and lost.
    """
    if stop.is_active(rec):
        return False
    ended = _instant(rec.get(_RUN_ENDED_AT))
    written = _instant(control.get("created_at"))
    return ended is not None and written is not None and written < ended


def _release_idle(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, key: str, rec: dict
) -> None:
    """Hand an idle card back: return it to Todo and forget it, leaving its label on (#2497).

    Every step may be refused, and the armed ``release_pending`` survives, so the next
    wake retries instead of leaving the card half-released.
    """
    repo, number = _split(key)
    if not _announce_hand_back(state, port, outcome, key=key, rec=rec):
        return
    _cancel_pending(state, port, outcome, rec)
    if not _do(
        port, outcome, Effect(EffectKind.SET_STATUS, repo, number, _hand_back_status(rec))
    ):
        return
    control_id = rec.get("release_control")
    if control_id:
        _mark_controls(
            state, port, outcome, [str(control_id)], status="done", reaction=_REACTION_DONE
        )
    _forget(state, key)
    outcome.report.releases += 1
    outcome.log.append(f"reconcile: {key} idle release -> {_hand_back_status(rec)}")


def _named_targets(control: dict) -> list[str]:
    """The pull requests a release command named, or an empty list when it named none."""
    payload = control.get("payload")
    return [str(ref) for ref in payload] if isinstance(payload, list) else []


def _pin_targets(rec: dict, named: list[str]) -> None:
    """Bind this release to the pull requests its command named, or back to discovery.

    Set unconditionally, so a bare ``/merge`` after a named one goes back to the full linked
    set. A pin that outlived the release that asked for it would quietly narrow every later
    release on the card, which is the same class of bug as the wrong mapping it fixes.
    """
    if named:
        rec["release_only"] = list(named)
    else:
        rec.pop("release_only", None)


def _named_are_releasable(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    control: dict,
    *,
    repo: str,
    number: int,
    named: list[str],
    linked: LinkedPRs,
) -> bool:
    """Refuse a named release target that cannot be released, saying which and why.

    Naming a pull request is someone correcting the engine, so a name it cannot act on has to
    come back as an answer. Releasing the rest of the set instead would ship a narrower release
    than was asked for while reporting success, and deferring silently would leave the person
    waiting on a wake that will never do anything.
    """
    known = set(linked.open) | set(linked.merged) | set(linked.withheld)
    problems = [f"`{ref}` is not an open pull request" for ref in named if ref not in known]
    problems += [
        f"`{ref}` is a draft, or opts out of release, so it can never be merged"
        for ref in named if ref in set(linked.withheld)
    ]
    if not problems:
        return True
    body = (
        "I cannot release what that `/merge` named:\n\n"
        + "\n".join(f"- {problem}" for problem in problems)
        + "\n\nNothing was merged. Name an open pull request, or use `/merge` on its own to "
        "release the ones this card links."
    )
    if not _do(port, outcome, _comment_effect(state, repo, number, body)):
        outcome.log.append(f"release refusal comment pending: {repo}#{number} (named target)")
        return False
    _reject(port, outcome, control, "unreleasable-target")
    outcome.log.append(f"release refused: {repo}#{number} (named target unreleasable)")
    return False


def _gate_release(
    state: dict, port: EnginePort, outcome: CycleOutcome, control: dict, *, now: datetime,
    ready_to_merge: str | None = None,
) -> None:
    """Admit or refuse a release command; admission itself is the guard (see release.py)."""
    key = queue.card_key(control["repo"], control["number"])
    repo, number = _split(key)
    rec = _cards(state).get(key)
    snapshot = outcome.live_snapshots.get(key)
    if snapshot is None:
        batch = port.snapshot_cards({key: CardQuery(repo, number)})
        if batch.deferred:
            outcome.log.append(f"release deferred: {key} ({batch.reason})")
            return
        snapshot = batch.cards.get(key)
    if snapshot is None:
        return  # unreadable this wake: leave the command new and retry next wake
    named = _named_targets(control)
    linked = (
        port.resolve_prs(named)
        if named
        else _without_steps_merged(rec, port.linked_prs(repo, number))
    )
    if linked is None:
        return  # incomplete discovery must never look like "nothing to release"
    if named and not _named_are_releasable(
        state, port, outcome, control, repo=repo, number=number, named=named, linked=linked
    ):
        return
    review_first = control.get("kind") == intake.CommandKind.MERGE_REVIEW.value
    verdict = release.admit(
        rec,
        review_first=review_first,
        issue_open=snapshot.open,
        owned=_claimed(rec, snapshot),
        live_status=snapshot.status,
        linked_open=len(linked.open),
        linked_merged=len(linked.merged),
        eligible_statuses=_resting_statuses(ready_to_merge),
    )
    if verdict is release.ReleaseAdmission.DEFER_NO_RECORD:
        # The card is owned but the engine has no record of it — the labelled-issue sweep
        # has not seeded it yet, or a release earlier this wake forgot it. Either way the
        # record arrives on a later wake, so the command stays new instead of being
        # refused for something that was never true (#294).
        outcome.log.append(f"release deferred: {key} (no record of this card yet)")
        return
    if verdict is release.ReleaseAdmission.ADMIT_REVIEW:
        if any(head is None for head in linked.open.values()):
            # An unreadable head cannot be approved against; retry next wake.
            outcome.log.append(f"release: {key} review deferred (no stable PR heads)")
            return
        _pin_targets(rec, named)
        refs = list(linked.open)
        review_diffs = port.review_diffs(refs)
        tree_fallbacks = [ref for ref in refs if not review_diffs.get(ref)]
        release.arm_review(
            rec,
            heads={ref: str(head) for ref, head in linked.open.items()},
            review_diffs=review_diffs,
            release_trees=port.release_trees(tree_fallbacks),
            control_id=str(control["id"]),
            now=now,
        )
        control["status"] = _QUEUED
        outcome.log.append(f"release: {key} review queued")
    elif verdict is release.ReleaseAdmission.ADMIT_MERGE:
        # A person asking again is a new release, so it is owed the post-arm repair attempt
        # a previous one may have spent — otherwise the second `/merge` the engine's own
        # advice asks for silently gets less than the first.
        release.clear_post_arm_repair(rec)
        _pin_targets(rec, named)
        rec.update(
            mode="merge",
            status=_IN_PROGRESS,
            started=True,
            started_at=now.astimezone(UTC).isoformat(),
            merge_control=str(control["id"]),
            merge_start_pending=True,
        )
        control["status"] = _PROCESSING
        if _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, _IN_PROGRESS)):
            release.clear_finalization(rec, "merge_start_pending")
        outcome.log.append(f"release: {key} merge registered")
    else:
        refusal = _release_refusal_message(
            verdict,
            repo=repo,
            number=number,
            review_first=review_first,
            eligible_statuses=_resting_statuses(ready_to_merge),
        )
        if not _do(port, outcome, _comment_effect(state, repo, number, refusal)):
            outcome.log.append(f"release refusal comment pending: {key} ({verdict.value})")
            return
        _reject(port, outcome, control, verdict.value)
        outcome.log.append(f"release refused: {key} ({verdict.value})")


# --- phase 4a: an outside reviewer's verdict (#2108) ---

#: How long a verdict waits for an unreadable diff before it is refused (#2772).
_REVIEWED_DIFF_UNREADABLE_GRACE = timedelta(hours=1)


def _accept_reviewed(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    control: dict,
    *,
    now: datetime,
    ready_to_merge: str | None = None,
) -> None:
    """Take an outside reviewer's verdict and keep it as a standing fact about a diff.

    On a repository whose reviews come from the ChatGPT reviewer rather than from the engine's
    own merge-review turn, this is the only thing that writes the review bit. It is deliberately
    the same intake every other command uses — scanned once into the ledger, authorized at
    Release tier because it can end in a merge with nobody in the loop — so it inherits the
    refusals, the exactly-once and the restart behaviour rather than getting its own.

    What it writes is only the fact, never the merge: the bits are read further down the wake
    and a release still needs the other two green and the capability switched on.

    The care here is all about *what* the verdict is allowed to bind to. The reviewer states
    the hash of the diff it read, and this only confirms that the pull request still holds
    exactly that — it never samples the live diff and files it as reviewed. That distinction is
    the whole safety of the thing: a head can sit still while the diff under it changes, so a
    verdict rebound to a fresh read would write "a reviewer passed this" over content nobody
    read. When the two do not match, the content moved since the review and the verdict is
    refused, not adjusted to fit.
    """
    key = queue.card_key(control["repo"], control["number"])
    repo, number = _split(key)
    rec = _cards(state).get(key)
    if rec is None:
        # The labelled-issue sweep has not seeded this card yet. Leave the command new so a
        # later wake decides it, exactly as a release command defers for the same reason.
        outcome.log.append(f"reviewed deferred: {key} (no record of this card yet)")
        return
    payload = control.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    verdict = str(payload.get("verdict") or "")
    head = str(payload.get("head") or "").lower()
    digest = str(payload.get("digest") or "").lower()
    named = [str(ref) for ref in (payload.get("targets") or [])]

    if verdict == "changes-needed":
        # Cleared without asking what it was about. Refusing a changes-needed for a ref we
        # could not resolve would leave a green bit standing on a diff a reviewer has just
        # objected to, and every refusal here has to fall on the side of not merging.
        release.clear_review_pass(rec)
        # A worker still verifying the head this names stops verifying it (#2350). Repeated on
        # every wake that re-reads an unsettled objection, which is free: both halves are
        # exactly-once on their own.
        _reject_offer(
            state, port, outcome, key=key, rec=rec, head=head, by=str(control.get("id") or ""),
            url=str(control.get("url") or ""), now=now,
        )
        if not _settle_objection(
            state, port, outcome, key=key, rec=rec, now=now, ready_to_merge=ready_to_merge,
        ):
            # The objection is not settled yet — it is written on the card and the release
            # stands still until it is. Leave the command new so the ledger hands it back next
            # wake: acknowledging it here would spend the reviewer's one objection on a release
            # it did not stop. Clearing the pass above already holds the bits red meanwhile,
            # and re-clearing it is free.
            return
        control["status"] = "done"
        _acknowledge(port, outcome, control, _REACTION_DONE)
        outcome.log.append(f"reviewed: {key} pass cleared (changes needed)")
        return

    linked = port.linked_prs(repo, number)
    if linked is None:
        return  # incomplete discovery is not "no such pull request": retry next wake
    ref = _reviewed_ref(linked, head=head, named=named)
    rebased_to = ""
    late = False
    if ref is None:
        ref, rebased_to = _rebased_reviewed_ref(
            state, rec, key=key, linked=linked, head=head, named=named
        )
        late = ref is not None
    if ref is None and _names_moved_head(linked, head=head, named=named):
        # The reviewer read a commit the pull request has since moved off, and the move was not
        # the engine's own rebase (#2453). Its chat already says the head moved and the new head
        # gets a review of its own, so nothing here needs a person: the refusal reaction on the
        # verdict is the whole answer, and a comment would only be noise on the card.
        _reject(port, outcome, control, "reviewed-head-moved")
        outcome.log.append(f"reviewed refused quietly: {key} verdict for moved head {head[:12]}")
        return
    untracked = _untracked_targets(port, named) if ref is None else []
    if untracked:
        _refuse_reviewed(
            state, port, outcome, control, repo=repo, number=number,
            reason=_untracked_reason(port, untracked),
        )
        return
    if ref is None:
        _refuse_reviewed(
            state, port, outcome, control, repo=repo, number=number,
            reason=(
                "**That verdict names nothing I can check.** No open pull request on this card "
                f"is at `{head[:12]}`"
                + (f" under {named[0]}" if named else "")
                + ". A verdict is only usable while it names the commit that would merge, so "
                "this one was not recorded and the review bit is unchanged. Post it again "
                "against the current head."
            ),
        )
        return
    current = port.review_diffs([ref]).get(ref)
    if not current:
        # GitHub did not hand the diff over, which says nothing about whether it moved (#2772):
        # refusing here once lost a verdict whose digest still matched three hours later. Leave
        # the command new so a later wake checks it again, and refuse only once the diff has
        # stayed unreadable long enough that it is not a passing failure.
        since = _instant(control.get("created_at"))
        if since is not None and now - since < _REVIEWED_DIFF_UNREADABLE_GRACE:
            outcome.log.append(f"reviewed deferred: {key} diff of {ref} unreadable")
            return
        _refuse_reviewed(
            state, port, outcome, control, repo=repo, number=number,
            reason=(
                f"**I could not read the diff of {ref}.** The verdict covers "
                f"`{digest[:12]}`, and GitHub has not handed the pull request's diff over "
                "since it arrived, so there is nothing to check it against. Nothing was "
                "recorded and the review bit is unchanged; post it again once the diff "
                "can be read."
            ),
        )
        return
    if current != digest:
        if late:
            # The engine's rebase did change what this pull request contributes, so the
            # verdict on the pre-rebase head covers nothing that would merge. Quiet for the
            # same reason as a moved head above: the rebased head gets its own review.
            _reject(port, outcome, control, "reviewed-head-moved")
            outcome.log.append(
                f"reviewed refused quietly: {key} rebase from {head[:12]} changed the diff"
            )
            return
        _refuse_reviewed(
            state, port, outcome, control, repo=repo, number=number,
            reason=(
                f"**That is not the diff {ref} has now.** The verdict covers "
                f"`{digest[:12]}`, and hashing the pull request's effective diff today gives "
                "something else. A head "
                "can sit perfectly still while the diff under it changes, which is why the "
                "verdict says what it read rather than only where. Nothing was recorded and "
                "the review bit is unchanged; review what is there now and post it again."
            ),
        )
        return
    if not release.record_reviewed_ref(
        rec, ref=ref, head=head, open_refs=set(linked.open), diff=digest,
        by=str(control.get("author") or ""), now=now,
    ):
        _refuse_reviewed(
            state, port, outcome, control, repo=repo, number=number,
            reason=(
                f"**I could not record that verdict against {ref}.** The pull request it names "
                "is no longer one this card has open, so there is nothing for the pass to "
                "cover. The review bit is unchanged."
            ),
        )
        return
    if rebased_to:
        # Recorded at the head the reviewer read; the ordinary carry-over moves it to the
        # rebased head on the next release-bits reading, by the same digest comparison every
        # engine rebase gets. Re-noting the marker is what lets it, because the marker the
        # rebase's own suite bound may already have been spent before this verdict arrived.
        # A rebase whose suite has not bound its output yet re-notes nothing: the pass waits
        # at the reviewed head until that suite gives the new head its provenance.
        release.note_rebase_round(rec, {ref: rebased_to})
    control["status"] = "done"
    _acknowledge(port, outcome, control, _REACTION_DONE)
    outcome.log.append(
        f"reviewed: {key} external pass recorded for {ref} at {head[:12]}"
        + (f" (poller rebased to {rebased_to[:12]})" if rebased_to else "")
        + (" (poller rebase not bound yet)" if late and not rebased_to else "")
    )


#: How the engine's own notes about a rejected offer announce themselves (#2350). The fact is in
#: the id — this card, these heads, or this job — so a retried wake queues each note once.
_READY_REJECTED_CID_PREFIX = "engine:ready-review:rejected:"
_READY_CANCELLED_CID_PREFIX = "engine:ready-review:cancelled:"


def _reject_on_findings(
    state: dict, port: EnginePort, outcome: CycleOutcome, control: dict, *, now: datetime
) -> None:
    """Read the outside reviewer's findings handoff as the review not passing (#2350).

    Only the reviewer's own handoff counts: an app-authored ``/claude`` carrying its marker or
    its heading. A person's ``/claude`` on the same card is an ordinary steer and rejects
    nothing. A handoff naming a head the worker did not offer is about older code, and leaves
    the current offer standing.
    """
    if not ready_review.enabled(state):
        return
    payload = control.get("payload")
    if not (_app_authored(control) and ready_review.is_findings(payload)):
        return
    key = queue.card_key(control["repo"], control["number"])
    rec = _cards(state).get(key)
    if rec is None:
        return
    _reject_offer(
        state, port, outcome, key=key, rec=rec, head=ready_review.findings_head(payload),
        by=str(control.get("id") or ""), url=str(control.get("url") or ""), now=now,
        findings=True,
    )


def _reject_offer(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    head: str,
    by: str,
    url: str,
    now: datetime,
    findings: bool = False,
) -> None:
    """Take the live worker's offered head as rejected, and stop verifying it (#2350).

    Nothing happens unless a worker still holds the card and offered heads in this generation:
    a resting card's review goes the ordinary way. The rejection is recorded before anything is
    cancelled, so a job the worker submits after this wake is still refused by the sweep that
    follows. The note to the worker says what became of its verification — cancelled, never
    started, or not cancellable yet — and rides ahead of the findings into the same chat.
    """
    generation = str(rec.get("generation") or "")
    if rec.get("status") != _IN_PROGRESS or not generation:
        return
    ready = ready_review.current(rec, generation=generation)
    if ready is None:
        return
    if head and not ready_review.names_head(ready, head):
        outcome.log.append(
            f"ready-review: {key} review of {head[:12]} is not about the offered head; kept"
        )
        return
    offered = dict(ready["heads"])
    if not ready_review.reject(rec, by=by, now=now):
        return
    cancelled, uncancelled, unreadable = _cancel_run_verification(port, key=key, rec=rec)
    for job_id in cancelled:
        ready_review.note_cancelled(rec, job_id)
        ready_review.note_told(rec, job_id)
    listed = ", ".join(f"`{ref}@{sha[:12]}`" for ref, sha in sorted(offered.items()))
    if cancelled:
        jobs = ", ".join(f"`{job}`" for job in cancelled)
        cost = f"Your verification job {jobs} was cancelled."
    elif uncancelled or unreadable:
        cost = (
            "Your verification of it could not be cancelled yet; the poller keeps trying, and "
            "its result will not be used."
        )
    else:
        cost = "No verification job had started, so there was nothing to cancel."
    where = (
        "The findings follow."
        if findings
        else "The reviewer's verdict and its reasons are on the card and the pull request."
    )
    _queue_engine_instruction(
        state, port, outcome,
        ref=key,
        cid=f"{_READY_REJECTED_CID_PREFIX}{key}:{ready_review.fingerprint(offered)}",
        instruction=(
            f"**The review of {listed} did not pass.** {cost} {where}\n\n"
            "Do not verify this head. Fix the findings, push a new head, run "
            "`ready-for-review` again, and only then start the verification job for the new "
            "head. `ready-for-review` refuses the rejected head."
        ),
        url=url,
        now=now,
        author="engine:ready-review",
    )
    outcome.log.append(
        f"ready-review: {key} review rejected {listed.replace('`', '')}"
        + (f"; cancelled {', '.join(cancelled)}" if cancelled else "")
    )


def _cancel_run_verification(
    port: EnginePort, *, key: str, rec: dict
) -> tuple[list[str], list[str], bool]:
    """Cancel this Run's unfinished verification jobs not already cancelled.

    Returns what was cancelled, what could not be, and whether the queue was unreadable.
    """
    generation = str(rec.get("generation") or "")
    jobs = port.verification_jobs()
    if jobs is None:
        return [], [], True
    repo, number = _split(key)
    owner = getattr(port, "org", "")
    work = dispatch.work_id(owner, repo, number) if isinstance(owner, str) and owner.strip() else ""
    ready = ready_review.current(rec) or {}
    done = {str(item) for item in ready.get("cancelled") or ()}
    cancelled: list[str] = []
    uncancelled: list[str] = []
    for job_id in verification.unfinished_jobs(jobs, run_id=generation, work_id=work):
        if job_id in done:
            continue
        (cancelled if port.cancel_job(job_id) else uncancelled).append(job_id)
    return cancelled, uncancelled, False


# What the retired automerge label (#2502) wrote onto a card record.
_STANDING_APPROVAL_KEYS = (
    "automerge", "automerge_spent", "automerge_rounds", "auto_review", "auto_review_generation",
)


def _retire_standing_approvals(state: dict, outcome: CycleOutcome) -> None:
    """Drop what the retired automerge label left on a record armed before #2502 shipped.

    A review that label armed approved no diff (``review_heads``), so its verdict could only
    ever defer, and without ``merge_control`` nothing would dispatch its reviewer either: the
    card would sit in review mode for good. It goes back to rest instead, unreleased. A
    release already merging keeps going under the ordinary diff-bound rules.
    """
    for key, rec in _cards(state).items():
        if not isinstance(rec, dict) or not any(name in rec for name in _STANDING_APPROVAL_KEYS):
            continue
        armed = bool(rec.get("auto_review"))
        for name in _STANDING_APPROVAL_KEYS:
            rec.pop(name, None)
        if (
            armed
            and rec.get("mode") == "merge-review"
            and not rec.get("merge_control")
            and not rec.get("review_heads")
        ):
            rec.pop("mode", None)
            outcome.log.append(f"automerge retired: {key} review it armed is dropped, not released")


def _hold_rejected_verification(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, now: datetime
) -> None:
    """No verification of a rejected head runs, whenever it was submitted (#2350).

    The rejection itself cancels what was running at that moment. This is the rest of the
    rule: a job the worker submits afterwards anyway — it had not read the note yet — and a
    cancel that did not land the first time. Each job cancelled here gets one note of its own,
    so the worker learns why its suite stopped instead of reading it as a failure.
    """
    if not ready_review.enabled(state):
        return
    for key, rec in _cards(state).items():
        generation = str(rec.get("generation") or "")
        if (
            rec.get("status") != _IN_PROGRESS
            or not generation
            or not ready_review.rejected(rec, generation=generation)
        ):
            continue
        if (ready_review.current(rec) or {}).get("rejected_at") == now.astimezone(UTC).isoformat():
            continue  # rejected this very wake, which already tried every cancel it could
        cancelled, uncancelled, unreadable = _cancel_run_verification(port, key=key, rec=rec)
        if unreadable:
            return  # the queue is one read for every card; try them all again next wake
        for job_id in uncancelled:
            outcome.log.append(f"ready-review: {key} could not cancel {job_id}; retrying")
        for job_id in cancelled:
            ready_review.note_cancelled(rec, job_id)
            outcome.log.append(f"ready-review: {key} cancelled {job_id} (head was rejected)")
        ready = ready_review.current(rec) or {}
        told = {str(item) for item in ready.get("told") or ()}
        for job_id in [str(item) for item in ready.get("cancelled") or ()]:
            if job_id in told:
                continue
            if _queue_engine_instruction(
                state, port, outcome,
                ref=key,
                cid=f"{_READY_CANCELLED_CID_PREFIX}{key}:{job_id}",
                instruction=(
                    f"**Verification job `{job_id}` was cancelled.** It ran on a head the "
                    "review rejected, and no run of that head can release. Fix the findings, "
                    "push a new head, run `ready-for-review` on it, and only then start the "
                    "verification job again."
                ),
                url="",
                now=now,
                author="engine:ready-review",
            ) or queue.is_seen(state, f"{_READY_CANCELLED_CID_PREFIX}{key}:{job_id}"):
                ready_review.note_told(rec, job_id)


def _settle_objection(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    now: datetime,
    ready_to_merge: str | None,
) -> bool:
    """Carry one review objection to its end, and be the only place that does (#2108).

    An accepted ``changes-needed`` against a release the bits started is not answered by one
    edit. Clearing the standing pass is not enough by itself: all three bits green copies that
    pass into the *active* approval and enters ``mode="merge"``, and the release progression
    then reads the approval rather than the bit — so an objection that only deleted the bit
    would be acknowledged while the code it objected to carried on merging. Depending on how
    far the release has got, what is owed is either taking it back or saying out loud that it
    was too late, and either can fail on the wake that tries it.

    So the objection is written down as a record with a state, and this function is the whole
    of its life: it decides which state the card is in, does the one thing that state owes, and
    removes the record when that lands. Everything else only reads it. ``_accept_reviewed``
    starts it and acknowledges the command when it is gone; the release progression stands
    still while it exists. Nothing else cancels the merge, hands the card back, or decides that
    the reviewer has been answered — which is what keeps a failed effect from being read as a
    finished objection, and a finished one from being retried forever.

    Only a release the bits armed is taken back. A person's typed ``/merge`` is their decision
    and is not undone by a reviewer's comment; the pass being cleared is what stops the *next*
    one from arming itself.

    Returns whether the objection is settled. False means it is not, and then the caller must
    leave the command unanswered so a later wake tries again.
    """
    if not rec.get("auto_release") or not rec.get("mode"):
        release.settle_objection(rec)
        return True
    repo, number = _split(key)
    aim = release.OBJECTION_NOTIFY if release.beyond_recall(rec) else release.OBJECTION_RECALL
    release.aim_objection(rec, state=aim, now=now)
    if aim == release.OBJECTION_NOTIFY:
        # A release past recall — watching a merge, watching a deploy, or merged — may genuinely
        # have landed, so it is not quietly abandoned. Saying so is the whole of what is owed
        # here, which is why a comment that did not post leaves the objection unsettled: the
        # warning is the only thing that sends a person to look at what shipped.
        if not _do(
            port, outcome,
            _comment_effect(
                state, repo, number,
                "**This came in too late to stop the release.** A reviewer has asked for "
                "changes, and the pass is cleared so nothing else will arm from it — but the "
                "merge was already under way and may well have landed. Somebody needs to look "
                "at what shipped rather than assume this held it.",
            ),
        ):
            outcome.log.append(
                f"reviewed: {key} too-late warning did not land, retrying next wake"
            )
            return False
        release.settle_objection(rec)
        outcome.log.append(f"reviewed: {key} changes needed arrived after the release left recall")
        return True
    if _handback(
        state, port, outcome, key=key, rec=rec, ready_to_merge=ready_to_merge,
        why=(
            "**Held back.** A reviewer has since asked for changes on this, so the release the "
            "release bits started is off and the card is back in review. Nothing merged. The "
            "pass is gone too, so the bits will not start another release until a fresh clean "
            "verdict covers whatever is there next."
        ),
    ):
        release.settle_objection(rec)
        outcome.log.append(f"reviewed: {key} bits-driven release revoked (changes needed)")
        return True
    outcome.log.append(f"reviewed: {key} release recall incomplete, retrying next wake")
    return False


def _tracked_repos(port: EnginePort) -> tuple[str, ...]:
    """The repositories this board reads pull requests in, or ``()`` when the port cannot say."""
    repos = getattr(port, "code_repos", ())
    if not isinstance(repos, (tuple, list)):
        return ()
    return tuple(str(name) for name in repos if str(name or "").strip())


def _untracked_targets(port: EnginePort, named: list[str]) -> list[str]:
    """The named pull requests in a repository this board never reads (#2718).

    Pull request discovery lists only the board's code repositories, so a companion pull
    request anywhere else is never part of the card, whatever its description says. A verdict
    on one was refused with "no open pull request is at this head", which read as a wrong
    commit; the reviewer and the owner both believed it would ship with the card.
    """
    tracked = {name.casefold() for name in _tracked_repos(port)}
    if not tracked:
        return []
    org = str(getattr(port, "org", "") or "").casefold()
    found = []
    for ref in named:
        repo_part = str(ref).rpartition("#")[0].strip()
        owner, _, name = repo_part.rpartition("/")
        if not name:
            continue
        if (owner and org and owner.casefold() != org) or name.casefold() not in tracked:
            found.append(str(ref))
    return found


def _untracked_reason(port: EnginePort, untracked: list[str]) -> str:
    """Why a verdict on a pull request outside the board's repositories was not counted."""
    tracked = ", ".join(f"`{name}`" for name in sorted(_tracked_repos(port)))
    return (
        f"**{', '.join(untracked)} is not a pull request this board tracks.** This board reads "
        f"pull requests only in {tracked}, so a pull request in any other repository is never "
        "part of this card: it is not reviewed, verified or released with it, whatever its "
        "description says. Nothing was recorded and the review bit is unchanged. If it should "
        "ship with the card, add its repository to the poller manifest's code repositories and "
        "give the AgentDeck GitHub App access to it; otherwise merge it by hand."
    )


def _reviewed_ref(linked: LinkedPRs, *, head: str, named: list[str]) -> str | None:
    """Which open pull request a verdict is about, or None when it does not say clearly.

    The commit has to be the pull request's *current* head, not merely one it once had. What
    the pass is bound to is the reviewer's own digest, checked separately; this only settles
    *which* pull request is being talked about, and a verdict on a head that has moved on is
    talking about work somebody has since replaced.

    Prefix matching lets a reviewer quote the short commit it was shown, and an abbreviation
    that fits two open pull requests resolves to neither rather than to the first.

    A named pull request is matched without regard to the case of the repository name, because
    GitHub itself treats ``AgentDeck#12`` and ``agentdeck#12`` as the same thing and a reviewer
    quoting the wrong case means the same pull request rather than no pull request.
    """
    if not head:
        return None
    wanted = {ref.lower() for ref in named}
    candidates = {
        ref: str(sha).lower()
        for ref, sha in linked.open.items()
        if sha and (not wanted or ref.lower() in wanted)
    }
    matched = [ref for ref, sha in candidates.items() if sha.startswith(head)]
    return matched[0] if len(matched) == 1 else None


def _rebased_reviewed_ref(
    state: dict, rec: dict, *, key: str, linked: LinkedPRs, head: str, named: list[str]
) -> tuple[str | None, str]:
    """The open pull request a verdict on a pre-rebase head is about, and where it sits now.

    A verdict names the commit the reviewer read, and the engine may have rebased the pull
    request off it while the review ran (#2453). That verdict is still about this pull request
    only when every one of these holds: the head is a full SHA, not an abbreviation; the engine
    itself asked for a rebase of that reference from exactly that head; and either the engine's
    own rebases lead from it to the head the pull request has now, so no push landed in between,
    or a rebase round from that head is still waiting for its suite to bind what it produced.
    Whether the diff is still the one reviewed is the caller's ordinary digest check.

    Answers the reference and the head the pass may be carried to now; that head is ``""`` while
    the round's output is unbound, so the pass is kept at the reviewed head and the round's own
    suite decides later, exactly as for a verdict that arrived before the rebase. Answers
    ``(None, "")`` unless exactly one open pull request qualifies.
    """
    if not _FULL_SHA.fullmatch(head or ""):
        return None, ""
    wanted = {ref.lower() for ref in named}
    found: list[tuple[str, str]] = []
    for ref, sha in linked.open.items():
        current = str(sha or "").lower()
        if not current or (wanted and ref.lower() not in wanted):
            continue
        if not _rebase_asked_from(state, key=key, ref=ref, head=head):
            continue
        if release.lineage_reaches(rec, ref=ref, start=head, end=current):
            found.append((ref, current))
        elif head in release.rebase_round_sources(rec, ref):
            found.append((ref, ""))
    if len(found) != 1:
        return None, ""
    return found[0]


_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def _rebase_asked_from(state: dict, *, key: str, ref: str, head: str) -> bool:
    """Did the engine queue a rebase of ``ref`` on this card from exactly ``head``? (#2453)"""
    prefix = f"{_REBASE_CID_PREFIX}{key}:"
    for cid in queue.controls(state):
        cid = str(cid)
        if not cid.startswith(prefix):
            continue
        for named_ref, named_head in _fingerprint(cid[len(_REBASE_CID_PREFIX):]).items():
            if named_ref.lower() == ref.lower() and named_head == head:
                return True
    return False


def _names_moved_head(linked: LinkedPRs, *, head: str, named: list[str]) -> bool:
    """Does a verdict name an open pull request of this card, at a head it is no longer on?"""
    if not head or not named:
        return False
    wanted = {ref.lower() for ref in named}
    return any(
        ref.lower() in wanted and sha and not str(sha).lower().startswith(head)
        for ref, sha in linked.open.items()
    )


def _refuse_reviewed(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    control: dict,
    *,
    repo: str,
    number: int,
    reason: str,
) -> None:
    """Say out loud why a verdict did not take, and only then mark it refused."""
    if not _do(port, outcome, _comment_effect(state, repo, number, reason)):
        outcome.log.append(f"reviewed refusal comment pending: {repo}#{number}")
        return
    _reject(port, outcome, control, "reviewed-unusable")
    outcome.log.append(f"reviewed refused: {repo}#{number}")


# --- phase 5: dispatch ---

# --- phase 4c: the three release bits (#2101) ---

# The instruction a card that fell behind its base gets. Deliberately names the rerun as well
# as the base update: this repository has no CI, so the suite run on the updated tree is the
# only time the exact code that deploys is tested, and an update without it would leave the
# tests bit red and the card looping straight back here. It asks for a merge, not a rebase
# (#2675): the engine's own branch updates are merges, and a worker that rebased over one had
# to force-push, costing the head, its CI run and its review.
#: How a queued control announces itself as the rebase-and-rerun turn rather than ordinary
#: work. Dispatch reads it to mark the round as a rebase (#2122), so the prefix is a fact two
#: places depend on, not a string built twice.
_REBASE_CID_PREFIX = "engine:release-bits:rebase:"

_REBASE_RULES = (
    "resolving any conflicts, and push normally: never rewrite or force-push commits already "
    "on the remote, including a merge commit the poller added. Then rerun the "
    "repository-selected suite on the updated tree and report the result. Do not change "
    "anything else: a base update that changes no line a pull request contributes keeps its "
    "review, and any other edit costs it a fresh one."
)


def _rebase_instruction(
    stale: dict[str, tuple[release.PrBlocker, str | None]], current: list[str]
) -> str:
    """The rebase turn's text, naming which pull requests need it and onto what.

    A card can hold pull requests in several repositories, each with its own base branch.
    A generic "this pull request … onto `origin/master`" sent workers on such a card through
    every one of them, rebasing ones that were already current (#2552), so the instruction
    names the stale ones, why, and their own base, and says which to leave alone.
    """

    def why(blocker: release.PrBlocker, base: str | None) -> str:
        onto = f"`{base}`" if base else "its base branch"
        if blocker is release.PrBlocker.DIRTY:
            return f"conflicts with {onto}"
        return f"behind {onto}"

    listed = ", ".join(f"`{ref}` ({why(*stale[ref])})" for ref in sorted(stale))
    text = (
        f"Pull requests that need updating onto their own base branch: {listed}. Into "
        "each, in its own repository, merge the current `origin` head of its base branch, "
        + _REBASE_RULES
    )
    if current:
        others = ", ".join(f"`{ref}`" for ref in sorted(current))
        text += f" Leave {others} alone: already current with its base."
    return text

#: The same kind of fact for the final verification turn (#2253): a queued control with this
#: prefix is the engine asking for the one selected-suite run a reviewed head still lacks.
#: Dispatch treats it like a rebase round — it is not fresh work — and additionally notes a
#: verify round, so its suite never binds a rebase round's provenance.
_VERIFY_CID_PREFIX = "engine:release-bits:verify:"

#: What a worker writes about its Work (#2328). Every work dispatch carries it in its Verification
#: evidence section, not only the verify turn (#2355). It is tied to reporting ``in_review``, not
#: to submitting a verification job (#2375): a repository whose CI is the verification (#2364)
#: never submits one, and a project whose worker turn ends at ``in_review`` would otherwise
#: finish its Work with no resolution at all.
WORK_RESOLUTION_ASK = (
    "Write this Work's resolution before you report `in_review`, and again whenever you submit "
    "that verification job, while it runs rather than waiting idle: "
    "`PUT $AGENTDECK_URL/api/sessions/<session-key>/work-resolution` "
    "with JSON `problem` (the original problem and its root cause), `solution` (what changed "
    "and how behaviour changed), `rationale` (the realistic alternatives, why each was not "
    "chosen, the tradeoffs accepted, and why this approach fits best) and `correctness` (the "
    "invariants and acceptance criteria it meets, and the evidence so far). Describe the whole "
    "Work and its final decision, not this turn. Keep each part short and easy to scan: "
    "start it with one plain sentence that gives the gist, then a blank line, then any detail "
    "as short paragraphs or `- ` bullet lines, and keep each part under about 120 words. "
    "Never say the suite passed before its result "
    "exists; once the result is in, `PUT` the whole record again with the outcome in "
    "`correctness`. Each `PUT` replaces the Work's one record, so a retry never duplicates it. "
    "If a fix changes the solution, `PUT` it again to describe the new final solution before "
    "you report.\n\n"
)

#: What a worker does when its verification fails on tests (#2382). Shared by the engine's own
#: verify turn and every dispatch's Verification evidence section. It replaces the promise of
#: a baseline policy (#1737) that had no caller after #2154, which left workers waiting on a
#: rule that never ran and parking cards behind unrelated tests.
FAILING_TESTS_GUIDE = (
    "A verification that fails on tests your change did not break does not park the card, "
    "and you do not have to prove that yourself. When it fails on a few tests, the job queue "
    "reruns just those tests on the same commit (up to twice), and runs whatever still fails "
    "once on this branch's merge base; a verification that ended with no test result at all "
    "(the cluster had no room, the run was killed, the connection to the pod dropped) is "
    "resubmitted once. If that resubmission ends with no test result too, it is still not "
    "this branch's failure: change nothing, do not report `blocked`, and report `in_review` "
    "saying that nothing ran. Each of those jobs "
    "reports to this chat when it finishes. A test a same-commit rerun passes (flaky), or "
    "that also fails on the merge base (broken on master), is forgiven, and once every "
    "failure is forgiven the verification counts as green and a completion message says so. "
    "Do not submit those reruns yourself; wait for them the way you wait for the suite.\n\n"
    "When a verification counted as green that way, name each forgiven test and why in your "
    "terminal comment and in the Work's resolution. The job queue quarantines each flaky "
    "one and files its labelled fix issue itself: do not file your own, and do not block on "
    "it.\n\n"
    "A failure the reruns do not clear and the merge base does not share is this branch's: "
    "fix it. If a failure is still unexplained after the queue's follow-ups have finished, "
    "report `blocked` with the evidence.\n\n"
)

# The wake's own reading of whether this project's engine asks for the final suite itself
# (#2360), so a dispatch's brief can say who starts it.
_VERIFY_TURN_ENABLED = "verify_turn_enabled"

_VERIFY_INSTRUCTION = (
    "A review passed this pull request, and no green run of the repository-selected suite "
    "covers its current head. Run that suite once on exactly this head, as this card's "
    "verification job, and wait for its result.\n\n"
    "While it runs, write this Work's resolution as the Verification evidence section of this "
    "dispatch says, rather than waiting idle.\n\n"
    "If it is green, report it and change nothing in the repository.\n\n"
    + FAILING_TESTS_GUIDE
    + "If fixing it needs a code change, make the fix, check it with focused tests only, push, "
    "and report `in_review`. Do **not** run the selected or full suite on the changed head: the "
    "changed code goes back to review first, and the poller asks for the final run again once "
    "a review passes it."
)

def _release_hand_merged_step(
    outcome: CycleOutcome, *, key: str, rec: dict, linked: LinkedPRs, now: datetime
) -> bool:
    """Enter the guarded release for a card whose only new merges are steps (#2730).

    A person merged the card's step pull request by hand. The step marker, not who pressed
    merge, decides what a merge means, so the card enters the same guarded release a `/merge`
    of already-merged work does. Its next assessment finds nothing to arm, watches the
    production deploy where the project has one, and continues the card only once that deploy
    is green — the next part may need it live.

    Whether these merges already had that release is one fact, kept under the pull requests
    themselves (``step_release_started``): the same set is never released twice, so a release
    that parked on a red deploy waits for the deploy recheck or a person's `/merge`, while a
    card whose own release parked *before* its merge, and that a person then merged, is new.
    """
    if linked.open or not linked.merged or not set(linked.merged) <= set(linked.step):
        return False
    merged = sorted(linked.merged)
    if release.step_release_started(rec) == merged:
        return False
    release.clear_bits(rec)
    release.note_step_release_started(rec, merged)
    # A park this release supersedes; its own end parks it again if it has to (#2235).
    rec.pop("release_blocked", None)
    # Pinned to exactly what was seen merged: nothing approved this release, so a pull request
    # that opens before its next assessment must not become something it arms unreviewed.
    _pin_targets(rec, merged)
    release.begin_merge(rec, now=now)
    rec["merge_start_pending"] = True
    outcome.log.append(f"release: {key} step merged by hand -> releasing")
    return True


def _end_moved_park(outcome: CycleOutcome, *, key: str, rec: dict, heads: dict) -> None:
    """End a release park once a pull request it parked on has a new head (#2771).

    The park's reason is about the commit it parked on; a push from the chat, a review of the
    new head and a green run on it are all newer than that reason, so it must not stand over them.
    """
    if release.park_head_moved(rec.get("release_blocked"), heads):
        rec.pop("release_blocked", None)
        outcome.log.append(f"release: {key} park ended, a parked pull request has a new head")


def _notice_hand_merged_steps(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    ready_to_merge: str | None = None,
    statuses: tuple[str, ...] | None = None,
) -> None:
    """Look for a step merged by hand on idle cards the release bits do not read (#2730).

    ``statuses`` are the columns to read: by default every resting column and *Blocked*, for a
    project without release bits; only *Blocked* where the bits already read the resting ones,
    so no card's pull requests are read twice in one wake. A release that parked before its
    merge rests in Blocked, and a person merging its step by hand is exactly what moves it on.
    """
    columns = statuses if statuses is not None else (
        *_resting_statuses(ready_to_merge), config.Role.BLOCKED.value,
    )
    for key, rec in list(_cards(state).items()):
        snapshot = outcome.live_snapshots.get(key)
        if snapshot is None or not snapshot.open:
            continue
        if not release.wants_auto_release(
            rec,
            live_status=snapshot.status or "",
            owned=_claimed(rec, snapshot),
            resting_statuses=columns,
        ):
            continue
        repo, number = _split(key)
        linked = _without_steps_merged(rec, port.linked_prs(repo, number))
        if linked is not None:
            _end_moved_park(outcome, key=key, rec=rec, heads=dict(linked.open))
            _release_hand_merged_step(outcome, key=key, rec=rec, linked=linked, now=now)


#: Every prefix whose round is the engine's own release-bit turn rather than fresh work.
_ENGINE_ROUND_PREFIXES = (_REBASE_CID_PREFIX, _VERIFY_CID_PREFIX)


def _apply_release_bits(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    ready_to_merge: str | None = None,
    ci_repos: frozenset[str] = frozenset(),
    merges: bool = True,
    review_only: frozenset[str] = frozenset(),
) -> None:
    """Read the three release bits on every resting card, and close what they leave open.

    Two of the three wait states close themselves here. A pull request behind its base has the
    base merged in by the engine, and one that conflicts gets one worker turn to merge and
    rerun (#2687); a card whose three bits are all green enters the guarded
    release with the reviewer's own pass as the approval. Everything else is logged and left,
    because an unknown bit is not a red one and a red one is somebody else's turn.

    What always blocks, regardless of the bits, blocks *before* they are read: a draft pull
    request and one carrying the release opt-out marker are withheld by discovery and never
    reach ``linked.open``, and *Human review pending* is not a resting status, so a card parked
    there is not free for this to decide anything about. None of those are re-implemented here
    — they are the same three refusals every other release path already inherits.

    Only cards this wake actually read are considered, which is also what bounds the cost: an
    In-review card's live read is throttled by ``terminal_recheck_s``, so the extra pull-request
    reads below happen at that cadence rather than every thirty seconds.

    ``merges`` is ``auto_release``. Without it (``verify_turn``, #2375) the rebase and verify
    turns are asked for exactly as they are with it, and three green bits are only logged: the
    release waits for a person's ``/merge``, as it always did.
    """
    resting = _resting_statuses(ready_to_merge)
    for key, rec in list(_cards(state).items()):
        snapshot = outcome.live_snapshots.get(key)
        if snapshot is None:
            continue  # not read this wake; the last published reading stands
        if not snapshot.open:
            release.clear_bits(rec)
            release.settle_release_recheck(rec)
            continue
        if not release.wants_auto_release(
            rec,
            live_status=snapshot.status or "",
            owned=_claimed(rec, snapshot),
            resting_statuses=resting,
        ):
            release.clear_bits(rec)
            if not rec.get("processing") and not rec.get("dispatch"):
                # A worker still on the card keeps the wanted read for when it comes to rest;
                # a card at rest out of the bits' scope has nothing left to be read for.
                release.settle_release_recheck(rec)
            continue
        repo, number = _split(key)
        linked = _without_steps_merged(rec, port.linked_prs(repo, number))
        if linked is None:
            outcome.log.append(f"release bits: {key} deferred (linked PR discovery incomplete)")
            continue
        _end_moved_park(outcome, key=key, rec=rec, heads=dict(linked.open))
        if _release_hand_merged_step(outcome, key=key, rec=rec, linked=linked, now=now):
            continue
        if not linked.open:
            # Nothing releasable: no pull request, or every one of them withheld. Not a
            # deferral — this is a positive answer, so any earlier reading is retired.
            release.clear_bits(rec)
            release.settle_release_recheck(rec)
            continue
        states = port.pr_states(list(linked.open))
        if states is None:
            outcome.log.append(f"release bits: {key} deferred (pull request state unreadable)")
            continue
        open_refs = [ref for ref in linked.open if ref in states]
        heads = {ref: states[ref].head for ref in open_refs}
        blockers = {
            ref: release.classify_pr(
                check_state=states[ref].check_state,
                merge_state=states[ref].merge_state,
                failed_steps=states[ref].failed_steps,
                # The branch comparison, and only here (#2101). The other three callers keep
                # the former signals: this is the one path the capability gates, so widening
                # what counts as "behind" cannot reach a project that did not opt in.
                behind_by=states[ref].behind_by,
            )
            for ref in open_refs
        }
        for line in _confirm_branch_syncs(port, rec, heads=heads, now=now):
            outcome.log.append(f"release bits: {key} {line}")
        diffs, trees = _release_pass_content(port, rec, heads)
        bits = release.release_bits(
            rec,
            blockers=blockers,
            open_refs=set(linked.open),
            heads=heads,
            review_diffs=diffs,
            release_trees=trees,
        )
        if review_only and bits.get(release.ReleaseBit.REVIEW_PASSED) is True:
            # A repository with no tests is proved by its review alone (#2729), and the review
            # bit just read green on these exact heads.
            credited = release.credit_reviewed_heads(
                rec, heads=heads, review_only=review_only, now=now
            )
            if credited:
                bits[release.ReleaseBit.TESTS_PASS] = release.tests_pass_current(
                    rec, heads=heads
                )
                outcome.log.append(
                    f"verify: {key} review-only {', '.join(credited)} credited by the review"
                )
        # Published before the branches below, not inside them: all three of them are a
        # reading worth showing, and the two that act on it return early.
        release.record_bits(rec, bits=bits, heads=heads, now=now, review_only=review_only)
        if all(value is not None for value in bits.values()):
            # An unknown bit is a deferred reading, so the wanted read waits for a real one.
            release.settle_release_recheck(rec)
        # Spent only once the review bit is *determinate*, and decided before either early
        # return below. An unreadable `gh pr diff` leaves the bit unknown; popping the marker
        # there would throw away the rebase's one chance to carry its review over. A card that
        # is still behind must not swallow it either, which is why this sits above both returns.
        review_bit = bits.get(release.ReleaseBit.REVIEW_PASSED)
        spent = release.take_rebase_round(rec) if review_bit is not None else {}
        if spent:
            if review_bit is False:
                outcome.log.append(
                    f"release bits: {key} review not carried over the engine's merge: "
                    + _carry_refusal(spent, heads=heads, diffs=diffs, rec=rec)
                )
            if review_bit is True:
                # The rebase kept the review, so the pass now covers the rebased head. Without
                # re-recording it here the marker is gone by the next wake and the same moved
                # head would read as a push nobody reviewed.
                release.record_review_pass(
                    rec, heads=heads, diffs=diffs, trees=trees, now=now
                )
        if release.bits_all_green(bits):
            if not merges:
                outcome.log.append(f"release bits: {key} all green; waiting for a person's /merge")
                continue
            _arm_bits_release(
                state, port, outcome, key=key, rec=rec, linked=linked,
                heads=heads, diffs=diffs, trees=trees, now=now, review_only=review_only,
            )
            continue
        if bits.get(release.ReleaseBit.REBASE_CURRENT) is False:
            stale = {
                ref: (blockers[ref], states[ref].base)
                for ref in open_refs
                if blockers[ref] in release._NOT_REBASE_CURRENT
            }
            # One owner for a base update (#2687): the engine merges a branch that is only
            # behind, and a worker is asked only for what that merge could not do. The rebase
            # turn used to be queued for this as well, and the pre-dispatch branch refresh then
            # merged the base itself, so the worker was sent to merge what was already merged.
            # The suite rerun the turn also carried is the verify turn's job now (#2253).
            updated = _merge_base_in(
                port, outcome, key=key, rec=rec, stale=stale, heads=heads, now=now
            )
            if updated and len(updated) == len(stale):
                continue
            for ref in updated:
                del stale[ref]
            _arm_rebase_turn(
                state, port, outcome, key=key, rec=rec, snapshot=snapshot,
                heads=heads, now=now, stale=stale,
                current=[ref for ref in open_refs if ref not in stale],
            )
            continue
        if (
            review_bit is True
            and bits.get(release.ReleaseBit.REBASE_CURRENT) is True
            and bits.get(release.ReleaseBit.TESTS_PASS) is False
            # A pull request whose CI is the verification waits for its CI, not a worker, and
            # one whose review is the verification has nothing for a worker to run (#2729).
            and any(_split(ref)[0] not in ci_repos | review_only for ref in heads)
        ):
            running = _suite_running(port, key=key, heads=heads)
            if running:
                # The rebase turn already queued this head's suite. Asking a second worker to
                # run it again only buys a turn that finds it running and waits (#2449); if it
                # ends without a green run, the next reading asks for the verify turn.
                outcome.log.append(
                    f"release bits: {key} suite already running (job {running}) -> waiting"
                )
                continue
            _arm_verify_turn(
                state, port, outcome, key=key, rec=rec, snapshot=snapshot, heads=heads,
                now=now,
            )
            continue
        untested = (
            release.untested_heads(rec, heads=heads, review_only=review_only)
            if bits.get(release.ReleaseBit.TESTS_PASS) is False
            else {}
        )
        outcome.log.append(
            f"release bits: {key} {release.bits_summary(bits)}"
            + (
                " (no green run on "
                + ", ".join(f"{ref}@{sha[:12]}" for ref, sha in untested.items())
                + ")"
                if untested
                else ""
            )
        )


def _apply_ci_verification(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    ci_repos: frozenset[str],
    ready_to_merge: str | None = None,
) -> None:
    """Read CI on the exact heads of every ``github_checks`` pull request read this wake (#2364).

    For a pull request in such a repository, GitHub's checks on its exact current head are the
    verification. Green credits the tests bit (``record_tests_pass``) for that commit, which
    ``tests_pass_current`` then holds to the head exactly as it holds a verification job's;
    pending, failing and no reported checks credit nothing. The head is read again on every
    such wake, green or not, because CI on one commit can be re-run: a later pending or red
    answer takes the credit back (``revoke_tests_pass``). Nobody waits for it.

    Only resting, idle cards read this wake — the one place a verification decides anything
    (a Work is Ready for merge only there) — which is what bounds the cost: an In-review card's
    live read is throttled by ``terminal_recheck_s``, and each one costs a pull request
    discovery. Nothing here merges, rebases or asks a worker.
    """
    resting = _resting_statuses(ready_to_merge)
    for key, rec in list(_cards(state).items()):
        snapshot = outcome.live_snapshots.get(key)
        if snapshot is None or not snapshot.open:
            continue
        if not release.wants_auto_release(
            rec,
            live_status=snapshot.status or "",
            owned=_claimed(rec, snapshot),
            resting_statuses=resting,
        ):
            continue
        repo, number = _split(key)
        linked = port.linked_prs(repo, number)
        if linked is None:
            continue  # incomplete discovery: the last readings stand
        heads = {ref: str(sha or "") for ref, sha in linked.open.items()}
        _end_moved_park(outcome, key=key, rec=rec, heads=heads)
        _credit_ci(port, outcome, key=key, rec=rec, heads=heads, ci_repos=ci_repos, now=now)


#: The deck's view of each pull request's CI on the head it was read at (#2364).
_CI_CHECKS = "ci_checks"


def _credit_ci(
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    heads: dict[str, str],
    ci_repos: frozenset[str],
    now: datetime,
) -> None:
    """Read CI for each open ``github_checks`` pull request's exact head: the answer now is it.

    Green credits the head; pending, failing or no checks revoke any credit it had, since a
    re-run on the same commit is the current answer. Unreadable changes nothing.
    """
    kept = rec.get(_CI_CHECKS) if isinstance(rec.get(_CI_CHECKS), dict) else {}
    readings: dict[str, dict] = {}
    green = set(release.tests_pass(rec))
    for ref, head in sorted(heads.items()):
        if not head or _split(ref)[0] not in ci_repos:
            continue
        before = kept.get(ref) if isinstance(kept.get(ref), dict) else {}
        verdict = port.commit_checks(_split(ref)[0], head)
        if verdict is None:
            if before.get("head") == head:
                readings[ref] = before  # unreadable is not a change of verdict
            continue
        readings[ref] = {"head": head, "state": verdict, "at": now.astimezone(UTC).isoformat()}
        if verdict == "green" and head not in green:
            release.record_tests_pass(rec, head=head, now=now)
            green.add(head)
            outcome.log.append(f"verify: {key} CI green on {ref}@{head[:12]} -> credited")
        elif verdict != "green" and head in green:
            release.revoke_tests_pass(rec, head=head)
            green.discard(head)
            outcome.log.append(f"verify: {key} CI {verdict} on {ref}@{head[:12]} -> revoked")
    if readings:
        rec[_CI_CHECKS] = readings
    else:
        rec.pop(_CI_CHECKS, None)


def _release_pass_content(
    port: EnginePort, rec: dict, heads: dict[str, str | None]
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """Read what it takes to judge the standing review pass, and not a byte more.

    An unmoved head is deliberately *not* treated as proof of unchanged content. A pull
    request's effective diff is measured against its base, so a base branch that moved changes
    the diff while the head sits exactly where the reviewer left it — which is precisely the
    case this bit exists to catch. So the diff is read for every reference the pass recorded
    one for, and the read is skipped only for a card with no recorded pass at all, where there
    is nothing to judge. What bounds the cost is the live-read throttle above, not this.
    """
    snapshot = release.review_pass(rec)
    if snapshot is None:
        return {}, {}
    reviewed = snapshot.get("diffs")
    refs = [ref for ref in heads if isinstance(reviewed, dict) and reviewed.get(ref)]
    diffs = port.review_diffs(refs) if refs else {}
    trees = snapshot.get("trees")
    fallbacks = [
        ref for ref in heads
        if ref not in refs and isinstance(trees, dict) and trees.get(ref)
    ]
    return diffs, (port.release_trees(fallbacks) if fallbacks else {})


def _arm_bits_release(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    linked: LinkedPRs,
    heads: dict[str, str | None],
    diffs: dict[str, str | None],
    trees: dict[str, str | None],
    now: datetime,
    review_only: frozenset[str] = frozenset(),
) -> None:
    """Enter the ordinary guarded release because all three bits are green.

    At-most-once falls out of the record rather than needing its own ledger: ``mode="merge"``
    is what ``wants_auto_release`` refuses on, so the next wake cannot arm a second release for
    the same card, and the visible column change is the durable ``merge_start_pending`` intent
    every other release start already uses.
    """
    repo, number = _split(key)
    release.arm_auto_release(
        rec, heads=heads, review_diffs=diffs, release_trees=trees, now=now
    )
    if _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, _IN_PROGRESS)):
        release.clear_finalization(rec, "merge_start_pending")
    forgiven = _forgiven_on(port, rec, key=key, heads=heads)
    note = (
        "\n\nThe suite counted as green after forgiving these failures, none of them this "
        "commit's doing; each has its own fix issue:\n" + flaky.describe(forgiven)
        if forgiven
        else ""
    )
    untested = sorted(ref for ref in heads if _split(ref)[0] in review_only)
    proof = (
        "the suite ran green on this exact commit"
        if not untested
        else (
            "the suite ran green on each tested commit"
            if len(untested) < len(heads)
            else "nothing else proves it"
        )
        # A repository with no tests is proved by its review alone (#2729); the comment must
        # not claim a suite ran on it.
        + "; " + ", ".join(f"`{ref}`" for ref in untested)
        + " has no tests, so the review is its only check"
    )
    _do(
        port, outcome,
        _comment_effect(
            state, repo, number,
            "**Releasing.** This pull request sits on current master, a review passed the diff "
            f"that would merge, and {proof} — so the poller is "
            "merging it. Nothing here needs a person; a failed check still stops it, and a "
            "repair that changes code goes back for another review." + note,
            include_session=False,
            pull_request_ref=sorted(linked.open)[0],
        ),
    )
    outcome.log.append(f"release bits: {key} all green -> release")


def _forgiven_on(
    port: EnginePort, rec: dict, *, key: str, heads: dict[str, str | None]
) -> list[dict]:
    """The tests forgiven on the verification that made any of these heads green (#2382).

    Read from the job rows, where the queue recorded them, rather than copied onto the card.
    """
    owner = getattr(port, "org", "")
    repo, number = _split(key)
    work = dispatch.work_id(owner, repo, number) if isinstance(owner, str) and owner else ""
    wanted = {str(sha) for sha in heads.values() if sha}
    found: dict[str, dict] = {}
    jobs = port.verification_jobs() or []
    for job in jobs:
        head = verification.credited_head(
            job, run_id=str(rec.get("generation") or ""), jobs=jobs
        ) or verification.credited_head_for_work(job, work_id=work, jobs=jobs)
        if head in wanted:
            for item in verification.forgiven_tests(job, jobs):
                found.setdefault(str(item.get("test") or ""), dict(item))
    return list(found.values())


def _arm_rebase_turn(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    snapshot: object,
    heads: dict[str, str | None],
    now: datetime,
    stale: dict[str, tuple[release.PrBlocker, str | None]],
    current: list[str],
) -> None:
    """Ask a worker to rebase and rerun, exactly once per head the card fell behind at.

    The id is derived from the fact — this card, at these head commits — so a card that is
    still behind on the next wake re-reads the same id and queues nothing. A worker that
    actually rebases moves the heads, which is what makes a *fresh* instruction available if
    the base runs away again; and a worker that does nothing leaves the id spent, so the card
    stops rather than being nagged every wake.
    """
    fingerprint = ",".join(f"{ref}@{heads.get(ref) or '?'}" for ref in sorted(heads))
    cid = f"{_REBASE_CID_PREFIX}{key}:{fingerprint}"
    url = getattr(snapshot, "url", "") or ""
    if _queue_engine_instruction(
        state, port, outcome, ref=key, cid=cid, instruction=_rebase_instruction(stale, current),
        url=url, now=now, author="engine:release-bits",
    ):
        outcome.log.append(
            f"release bits: {key} behind or conflicting with base "
            f"({', '.join(sorted(stale))}) -> rebase turn queued"
        )


def _carry_refusal(
    spent: dict[str, str], *, heads: dict, diffs: dict, rec: dict
) -> str:
    """Why an engine move did not keep the review, said per pull request (#2793)."""
    reviewed = release.review_pass(rec) or {}
    reviewed_diffs = reviewed.get("diffs") if isinstance(reviewed.get("diffs"), dict) else {}
    reasons = []
    for ref, produced in sorted(spent.items()):
        live = str(heads.get(ref) or "")
        if live != produced:
            reasons.append(f"{ref} is at {live[:12]}, not the merged {produced[:12]}")
        elif reviewed_diffs.get(ref) and diffs.get(ref) != reviewed_diffs.get(ref):
            reasons.append(f"{ref} contributes a different diff than was reviewed")
        else:
            reasons.append(f"{ref} has no reviewed diff to compare")
    return "; ".join(reasons)


def _merge_base_in(
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    stale: dict[str, tuple[release.PrBlocker, str | None]],
    heads: dict[str, str | None],
    now: datetime,
) -> list[str]:
    """Merge the base into every pull request that is only behind, and answer which moved.

    A conflicted one is left for the worker, and so is one GitHub refused to update. The head
    each merge produced is recorded as the engine's own move, so a merge that changes no
    contributed line keeps its review (#2122) — read back at once where GitHub already shows
    it, and otherwise confirmed from the merge commit once it lands (#2793). The card is read
    again next wake, where the moved head asks for its suite.
    """
    updated: list[str] = []
    for ref in sorted(stale):
        if stale[ref][0] is not release.PrBlocker.BEHIND:
            continue
        if not _do(port, outcome, Effect(EffectKind.UPDATE_BRANCH, *_split(ref))):
            outcome.log.append(f"release bits: {key} could not update {ref}; worker will merge")
            continue
        note = _note_branch_sync(port, rec=rec, ref=ref, before=heads.get(ref), now=now)
        updated.append(ref)
        outcome.log.append(f"release bits: {key} behind -> merged base into {ref}{note}")
    if updated:
        release.want_release_recheck(rec)
    return updated


def _note_branch_sync(
    port: EnginePort, *, rec: dict, ref: str, before: str | None, now: datetime
) -> str:
    """Record the engine's base merge into ``ref``, answering a note for its log line (#2793).

    The head read straight after the update counts only once it is proven to be GitHub's
    merge commit on top of ``before`` — the same check a later reading makes — because it can
    still be ``before`` (GitHub lands the merge late) or an author's push that raced it.
    """
    release.note_branch_sync(rec, ref=ref, before=before, now=now)
    if not before:
        return " (resulting head unknown; it carries no review)"
    moved = port.pr_states([ref])
    after = getattr(moved.get(ref) if isinstance(moved, dict) else None, "head", "") or ""
    lines = _confirm_branch_syncs(port, rec, heads={ref: after}, now=now, only=ref)
    if lines:
        return f" ({lines[0].split(': ', 1)[-1]})"
    return f" (new head not confirmed yet; confirming the merge of {str(before)[:12]} later)"


def _confirm_branch_syncs(
    port: EnginePort,
    rec: dict,
    *,
    heads: dict[str, str | None],
    now: datetime,
    only: str | None = None,
) -> list[str]:
    """Bind each engine base merge still waiting to the head it produced, answering why (#2793).

    The pull request's head counts as the engine's move only when it is GitHub's own merge
    commit whose first parent is the head the engine updated: a push after the merge sits on
    top of that commit, and anything else is not GitHub's branch update at all. Only then may
    the release bits judge it by its contributed diff. A head that has not moved yet waits,
    and an unreadable commit is asked again on the next reading.
    """
    lines: list[str] = []
    for ref, entry in release.branch_syncs_pending(rec).items():
        if only is not None and ref != only:
            continue
        source = entry["from"]
        live = str(heads.get(ref) or "").strip()
        if release.branch_sync_expired(entry, now=now):
            release.settle_branch_sync(rec, ref)
            lines.append(
                f"{ref}: the engine's merge from {source[:12]} was never confirmed; dropped"
            )
            continue
        if not live or live.lower() == source:
            continue
        parent = port.sync_merge_parent(ref, live)
        if parent is None:
            continue
        release.settle_branch_sync(rec, ref)
        if parent == source:
            release.note_rebase_round(rec, {ref: live})
            lines.append(f"{ref}: {live[:12]} is the engine's merge into {source[:12]}")
            continue
        why = (
            "it is not GitHub's merge commit"
            if not parent
            else f"it merged into {parent[:12]}, not {source[:12]}"
        )
        lines.append(
            f"{ref}: head {live[:12]} is not the engine's merge into {source[:12]} ({why}); "
            "the review does not carry over"
        )
    return lines


def _suite_running(port: EnginePort, *, key: str, heads: dict[str, str | None]) -> str:
    """The ids of queued or running verifications covering *every* open head, or ``""``.

    Every head, not any: on a card with two pull requests, one suite running for the first
    must not hold back the verify turn the second, untested head still needs.
    """
    owner = getattr(port, "org", "")
    if not isinstance(owner, str) or not owner.strip() or not heads:
        return ""
    repo, number = _split(key)
    jobs = port.verification_jobs()
    work = dispatch.work_id(owner, repo, number)
    covering: list[str] = []
    for sha in heads.values():
        if not sha:
            return ""
        job = verification.unfinished_for_heads(jobs, work_id=work, heads={str(sha)})
        if not job:
            return ""
        covering.append(job)
    return ", ".join(sorted(set(covering)))


def _untested_run(port: EnginePort, *, key: str, heads: dict[str, str | None]) -> str:
    """The latest verification of these heads, when it ended with no test result, or ``""``."""
    owner = getattr(port, "org", "")
    shas = {str(sha) for sha in heads.values() if sha}
    if not isinstance(owner, str) or not owner.strip() or not shas:
        return ""
    repo, number = _split(key)
    return verification.ended_untested(
        port.verification_jobs(), work_id=dispatch.work_id(owner, repo, number), heads=shas
    )


def _arm_verify_turn(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    snapshot: object,
    heads: dict[str, str | None],
    now: datetime,
) -> None:
    """Ask a worker for the final selected-suite run, once per reviewed head (#2253).

    The suite is the last gate before release, not the ticket into review, so a card reaches
    here reviewed and current with no green run. The id is the fact again — this card at these
    heads — so the same head is asked once, and only a new head (a fix that went back through
    review) can be asked again. The one exception is a head whose latest run tested nothing,
    which may be asked once more under the id suffixed ``:again`` (#2475). A card behind its
    base never reaches here: its rebase turn already reruns the suite, and two turns for one
    head would race each other.
    """
    fingerprint = ",".join(f"{ref}@{heads.get(ref) or '?'}" for ref in sorted(heads))
    cid = f"{_VERIFY_CID_PREFIX}{key}:{fingerprint}"
    # One more ask for the same heads, and only one, when the last run on them tested nothing:
    # the cluster cut it off, the queue's own resubmission too, and without this the card
    # waited for a person to notice (#2475). A run that tested and failed never earns it, and
    # nor does a head never asked yet: the first ask stays the plain id, so a later real failure
    # cannot buy a second one with it.
    untested = _untested_run(port, key=key, heads=heads) if queue.is_seen(state, cid) else ""
    if untested:
        cid = f"{cid}:again"
    url = getattr(snapshot, "url", "") or ""
    if _queue_engine_instruction(
        state, port, outcome, ref=key, cid=cid,
        instruction=_VERIFY_INSTRUCTION + _verify_heads_note(rec, heads),
        url=url, now=now, author="engine:release-bits",
    ):
        release.note_verify_asked(rec, heads)
        outcome.log.append(
            f"release bits: {key} reviewed, job {untested} tested nothing -> verify turn "
            "queued again"
            if untested
            else f"release bits: {key} reviewed, no green run -> verify turn queued"
        )


def _verify_heads_note(rec: dict, heads: dict[str, str | None]) -> str:
    """Name each head a verify turn has to cover, on a card shipping several pull requests (#2718).

    The instruction says "this head", and a worker on a two-repository card read it as one: it
    ran one job from its own worktree, which credits only that repository's head, and the
    companion's head stayed red with nothing saying so.
    """
    untested = release.untested_heads(rec, heads=heads)
    if len(heads) < 2 or not untested:
        return ""
    listed = "\n".join(f"- `{ref}` at `{sha}`" for ref, sha in untested.items())
    return (
        "\n\nThis card ships more than one pull request, and these heads have no green run "
        f"yet:\n\n{listed}\n\nA verification job credits only the head of the repository its "
        "`--cwd` is in, so submit one verification job for each of them, with `--cwd` in that "
        "pull request's checkout."
    )


def _round_id(controls) -> str:
    """One processing round's identity: the control ids the worker is carrying.

    Enough to tell this round from the next one, which is all the rebase provenance needs —
    a marker armed by one round must never be spendable by another (#2122).
    """
    return ",".join(sorted(str(cid) for cid in (controls or ()) if cid))


def _rebase_round_refs(pending) -> set[str]:
    """Which pull requests this round of work is the engine's own rebase or final verification
    of — none if any other instruction rides along.

    ``all`` over a non-empty batch, so a rebase control queued *alongside* a person's
    instruction is ordinary work and arms normally: the other instruction is real work, and
    work the reviewer has not seen still owes a review.

    The references come out of the control id, which already records exactly which pull
    requests were behind and at which heads — ``<prefix><card>:<ref>@<head>,...``. Reading
    them back is what keeps the provenance per reference: a card can ship several pull
    requests, and rebasing one must not vouch for a push to another (#2122).
    """
    ids = [str(cid) for cid in (pending or ())]
    if not ids or not all(cid.startswith(_ENGINE_ROUND_PREFIXES) for cid in ids):
        return set()
    refs: set[str] = set()
    for cid in ids:
        prefix = next(p for p in _ENGINE_ROUND_PREFIXES if cid.startswith(p))
        _, _, fingerprint = cid[len(prefix):].partition(":")
        for part in fingerprint.split(","):
            ref = part.rsplit("@", 1)[0].strip()
            if ref:
                refs.add(ref)
    return refs


def _rebase_round_sources(pending) -> dict[str, str]:
    """The head each reference was rebased *from*, read from the rebase control's own id (#2453).

    Only a pure rebase round answers. A verify round may push a code fix, and a head that fix
    produced must never be written down as a rebase's output, so a round carrying a verify
    control records no source and its head gets no lineage — exactly as before this existed.
    """
    ids = [str(cid) for cid in (pending or ())]
    if not ids or not all(cid.startswith(_REBASE_CID_PREFIX) for cid in ids):
        return {}
    sources: dict[str, str] = {}
    for cid in ids:
        for ref, head in _fingerprint(cid[len(_REBASE_CID_PREFIX):]).items():
            sources.setdefault(ref, head)
    return sources


def _fingerprint(rest: str) -> dict[str, str]:
    """``<card>:<ref>@<head>,...`` read back as ``{ref: head}``; unknown heads are left out."""
    _, _, fingerprint = rest.partition(":")
    found: dict[str, str] = {}
    for part in fingerprint.split(","):
        ref, _, head = part.strip().rpartition("@")
        head = head.strip().lower()
        if ref.strip() and head and head != "?":
            found[ref.strip()] = head
    return found


def _bind_rebase_round(rec: dict, rebased: dict[str, str], *, head: str, now: datetime) -> None:
    """The rebase round's suite ran on ``head``: that head is the engine's own output (#2122).

    Written twice: as the carry-over marker the next release-bits reading spends, and as the
    longer-lived lineage an outside verdict on the pre-rebase head is traced through (#2453).
    """
    release.note_rebase_round(rec, dict.fromkeys(rebased, head))
    for ref, source in rebased.items():
        release.note_rebase_lineage(rec, ref=ref, source=source, head=head, now=now)


def _ready_action(rec: dict) -> str:
    if rec.get("mode") == "merge-review":
        return "merge-review"
    recovery = rec.get("recovery_action")
    if recovery:
        return str(recovery)
    if rec.get("kind") == "pr" and not rec.get("started"):
        return "pr"
    links = rec.get("comment_links")
    prior_work = bool(
        rec.get("kind") == "pr"
        or rec.get("worktrees")
        or rec.get("branch")
        or rec.get("branches")
        or (isinstance(links, dict) and links.get("pull_request_url"))
    )
    has_instructions = bool(_ids(rec, "pending"))
    return "followup" if rec.get("started") and (has_instructions or prior_work) else "new"


def _dispatch_ready(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    cap: int,
    store: StateStore,
    budget_ok: bool = True,
    proposal_cap: int | None = None,
    command_output: config.CommandOutputPolicy | None = None,
) -> None:
    """One worker per ready card, all queued instructions riding in a single ordered batch.

    ``proposal_cap`` additionally bounds how many of those workers may be running purely
    extension-proposed work (#371), so a flood of autofix proposals cannot occupy every slot
    the project has and starve the people using it.
    """
    active = [
        rec
        for rec in _cards(state).values()
        if rec.get("status") == _IN_PROGRESS and rec.get("mode") != "merge"
    ]
    working = len(active)
    proposal_working = sum(
        1 for rec in active if _is_proposal_batch(state, _ids(rec, "processing"))
    )
    unchecked = _read_new_card_dependencies(state, port, outcome)
    for key, rec in list(_cards(state).items()):
        pending = _ids(rec, "pending")
        steerable = _plain_followup_ids(state, pending)
        if (
            rec.get("status") == _IN_PROGRESS
            and not rec.get("mode")
            and not rec.get("stopping")
            and not rec.get("dispatch")
            and steerable
            and rec.get("generation")
            and rec.get("steer_refused_generation") != rec.get("generation")
            and not rec.get("paused")
        ):
            _steer(
                state, port, outcome, key=key, rec=rec, control_ids=steerable,
                now=now, store=store,
            )
            continue
        if rec.get("dispatch_failed") and not rec.get("dispatch") and not rec.get("paused"):
            _rearm_given_up_dispatch(state, port, outcome, key=key, rec=rec, now=now)
        if (
            rec.get("mode") == "merge"
            or rec.get("stopping")
            or rec.get("dispatch")
            or rec.get("dispatch_failed")
            or rec.get("status") == _IN_PROGRESS
        ):
            continue
        pending = _ids(rec, "pending")
        review_ready = release.review_ready(rec)
        if (
            not pending
            and not rec.get("manual_start")
            and not rec.get("recovery_action")
            and not review_ready
        ):
            continue
        if rec.get("status") == config.Role.DONE.value:
            # The board says this card is finished, so nothing here may put a worker on it.
            # Reconciliation runs before this loop and releases a Done card, so arriving here
            # means that release did not happen this wake — an unreadable snapshot, a deferred
            # read. Dispatching anyway is how one answered ticket was investigated three times
            # (#1581). Nothing is consumed: the queued instructions and the start both stay, so
            # the wake that does reconcile the card still sees exactly what this one saw.
            outcome.log.append(f"held: {key} rests in Done")
            continue
        if rec.get("paused"):
            # Nothing is consumed: the instructions, the start and the column stay, so the wake
            # after a person removes the label starts it exactly as this one would have (#2548).
            _hold_paused(state, port, outcome, key=key, rec=rec)
            continue
        if key in unchecked:
            # A card the engine has never seen on GitHub, on a wake that declined to look.
            # Nothing is consumed, and the full wake the ceiling guarantees starts it with its
            # dependencies actually read (#1673).
            outcome.log.append(f"held: {key} (dependencies unread this wake)")
            continue
        held = card_blockers.holding(rec)
        if held:
            # Nothing is consumed: the label, the queued instructions and the column all stay
            # exactly as they are, so the wake after the blocker closes starts this normally.
            _hold_blocked(state, port, outcome, key=key, rec=rec, held=held)
            continue
        # Two cards writing the same files are both dispatched (#2081). The gate that held the
        # second one bought no clean merge — both branches are cut from the same base either
        # way — and serialized nearly every pair in a repository with shared source files.
        proposed = _is_proposal_batch(state, pending)
        recovering = bool(rec.get("recovery_action"))
        personal_start = not recovering and bool(
            rec.get("manual_start")
            or review_ready
            or (pending and not proposed)
        )
        if working >= cap:
            outcome.log.append(f"queued: {key} (worker cap {cap} reached)")
            continue
        if proposed and proposal_cap is not None and proposal_working >= proposal_cap:
            outcome.log.append(f"queued: {key} (proposal worker cap {proposal_cap} reached)")
            continue
        action = _ready_action(rec)  # before the record is marked started
        if action in {"followup", "resume", "merge-review"} and (
            _refresh_before_worker(port, outcome, key=key, rec=rec)
        ):
            continue
        rec.update(
            status=_IN_PROGRESS,
            started=True,
            manual_start=False,
            started_at=now.astimezone(UTC).isoformat(),
            processing=pending,
            pending=[],
        )
        rec.pop("recovery_action", None)
        # A new generation starts with nothing offered: an offer belongs to the worker that
        # made it, and this one has made none yet (#2350).
        ready_review.clear(rec)
        # Whatever this card was waiting on, a worker is on it now. Leaving the hold would let
        # a later blocker close and arm a resume on top of finished work (#1230).
        card_blockers.record_resume(rec, ())
        card_blockers.clear_verified(rec)
        rebase_refs = _rebase_round_refs(pending)
        if not rebase_refs:
            # Ordinary work over the top of an unfinished rebase round: that round is not going
            # to report a head now, so its provenance goes with it (#2122).
            release.retire_pending_rebase_round(rec)
            release.retire_rebase_round_runs(rec)
            # Nor may an older rebase bridge a late verdict across this work's pushes (#2453).
            release.retire_rebase_lineage(rec)
            # Nor a verify round it supersedes (#2253).
            release.retire_verify_round(rec)
        if rebase_refs:
            # Not a fresh round of work: the engine asked for a rebase and a rerun, and told
            # the worker in as many words that a rebase changing no contributed line keeps its
            # review (#2122). The marker lets the release-bits pass carry that review over.
            release.arm_rebase_round(
                rec, rebase_refs, round_id=_round_id(pending),
                sources=_rebase_round_sources(pending),
            )
            if any(str(cid).startswith(_VERIFY_CID_PREFIX) for cid in pending):
                release.arm_verify_round(rec, round_id=_round_id(pending))
        _mark_controls(
            state, port, outcome, pending, status=_PROCESSING, reaction=_REACTION_WORKING
        )
        if review_ready:
            _mark_controls(
                state, port, outcome, [str(rec.get("merge_control"))],
                status=_PROCESSING, reaction=_REACTION_WORKING,
            )
        if _spawn(
            state,
            port,
            outcome,
            key=key,
            rec=rec,
            action=action,
            now=now,
            store=store,
            allow_over_budget=personal_start,
            command_output=command_output,
        ):
            working += 1
            proposal_working += proposed


def _refresh_before_worker(
    port: EnginePort, outcome: CycleOutcome, *, key: str, rec: dict
) -> bool:
    """Update known-behind PR branches before a continuation or review worker starts.

    A successful remote update changes the head and restarts strict CI, so the worker waits for
    the next wake's fresh snapshot instead of receiving an identity the engine already knows is
    stale. A refused update is the conflict case: that is real work, so it does not consume the
    worker's turn in advance or leave the card waiting forever.

    This runs immediately before every review dispatch and has just read the card's whole open-PR
    set, so it also records that set for the brief (#1338). A reviewer told only the issue reviews
    the one pull request on its own branch, while a ``pass`` releases every one of them — on a card
    with pull requests in three repositories that shipped two nobody had read.
    """
    repo, number = _split(key)
    linked = _linked(port, rec, repo, number)
    if linked is None:
        # A stale list is worse than none: the worker would be told to review a set the engine
        # can no longer confirm. The brief falls back to naming no pull request at all.
        rec.pop("review_prs", None)
        outcome.log.append(f"branch refresh: {key} linked PRs unreadable; deferring worker")
        return True
    refs = sorted(linked.open)
    rec["review_prs"] = refs
    if not refs:
        return False
    states = port.pr_states(refs)
    if states is None or any(ref not in states for ref in refs):
        outcome.log.append(f"branch refresh: {key} PR state unreadable; deferring worker")
        return True
    refreshed = False
    unknown = False
    for ref in refs:
        pr = states[ref]
        blocker = release.classify_pr(
            check_state=pr.check_state,
            merge_state=pr.merge_state,
            failed_steps=pr.failed_steps,
        )
        if blocker is not release.PrBlocker.BEHIND:
            if pr.merge_state in {"", "UNKNOWN"}:
                unknown = True
            continue
        if _do(port, outcome, Effect(EffectKind.UPDATE_BRANCH, *_split(ref))):
            refreshed = True
            # Deliberately *not* marked as an engine head move (#2122). A worker is about to
            # run and push, so a marker left here would still be standing when that push lands
            # and would vouch for a head the worker moved. The engine's own rebase round marks
            # itself at dispatch, which is the case this would otherwise have covered.
            outcome.log.append(f"branch refresh: {key} updated {ref} before worker")
        else:
            outcome.log.append(
                f"branch refresh: {key} could not update {ref}; worker will resolve it"
            )
    if refreshed:
        outcome.log.append(f"branch refresh: {key} waiting for the refreshed PR snapshot")
    elif unknown:
        outcome.log.append(f"branch refresh: {key} mergeability unknown; deferring worker")
    return refreshed or unknown


def _read_new_card_dependencies(
    state: dict, port: EnginePort, outcome: CycleOutcome
) -> set[str]:
    """Give a card this wake introduced its dependency facts before it can be started (#640).

    Reconciliation reads every card the engine already knows, which covers every card the board
    hands over: a labelled card is discovered *before* that phase and snapshotted by it. A card
    a **comment** introduced is created after it, so on its first wake nothing on the record
    could gate it — and somebody commenting is not a reason to ignore a dependency the issue's
    author wrote down. One bounded batch for exactly those cards keeps the rule true on every
    start path without making an ordinary command wait a wake.

    A card that could not be read starts anyway, and says so. That is the one place this feature
    fails open, deliberately: the alternative is a GitHub blip silently swallowing commands on
    cards nobody has ever declared a dependency for.

    A batch the wake *declined* to read is a different fact, and the returned keys are how the
    caller tells them apart (#1673). A webhook-quiet wake defers the whole batch on purpose and
    a full wake follows within ``webhook_skip_s``, so those cards wait that bounded moment
    rather than starting on a dependency check nobody attempted.
    """
    unseen = {
        key: CardQuery(*_split(key))
        for key, rec in _cards(state).items()
        if not rec.get("last_live_check")
        and not rec.get("started")
        and (_ids(rec, "pending") or rec.get("manual_start"))
    }
    if not unseen:
        return set()
    batch = port.snapshot_cards(unseen)
    if batch.deferred:
        return set(unseen)
    for key in unseen:
        rec = _cards(state).get(key)
        if rec is None:
            continue
        snapshot = batch.cards.get(key)
        if snapshot is None:
            outcome.log.append(f"dependencies unread: {key} (starting without a check)")
            continue
        card_blockers.record(rec, snapshot.blockers)
    return set()


def _hold_blocked(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    held: tuple[str, ...],
) -> None:
    """Keep a card that has not started until its declared blockers close, and say so once (#640).

    Said once per hold, not once per wake: at a thirty-second wake a repeated explanation would
    bury the card's actual thread within an hour. The fingerprint is the set of refs, so a
    dependency that changes is explained again, and one that closes leaves nothing to undo.
    """
    outcome.log.append(f"queued: {key} (blocked by {', '.join(held)})")
    if card_blockers.announced(rec, held):
        return
    repo, number = _split(key)
    if _do(
        port, outcome,
        _comment_effect(state, repo, number, card_blockers.notice(held)),
    ):
        card_blockers.mark_announced(rec, held)


PAUSED_NOTICE = (
    "**Paused.** This card carries the paused label, so the Poller starts nothing on it: no"
    " worker, no restart, no review and no merge. A turn that was already running finishes as"
    " usual. Queued instructions wait and run when the label is removed."
)


def _hold_paused(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, key: str, rec: dict
) -> None:
    """Keep a paused card from starting, and say so on the card once per pause (#2548)."""
    outcome.log.append(f"held: {key} (paused)")
    if rec.get("paused_notice"):
        return
    repo, number = _split(key)
    if _do(port, outcome, _comment_effect(state, repo, number, PAUSED_NOTICE)):
        rec["paused_notice"] = True


def _rearm_given_up_dispatch(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    now: datetime,
) -> None:
    """Give a card parked by a spawn that never landed its one automatic retry (#1491).

    A card gets here because delivery failed, not because the work is wrong, and the class
    of thing that causes it — host capacity above all — is gone minutes later. Before this,
    ``dispatch_failed`` was cleared in exactly one place, a brand-new instruction comment, so
    the card waited for a person who had no way of knowing it was waiting (#490).

    It arms once and it says so. ``dispatch.rearmable`` spends the retry rather than
    repeating it, and the comment gates the arm for the same reason ``_reconsider_blocked``'s
    does: a card must never come back to life with no trace in its own thread of why.
    """
    if not dispatch.rearmable(rec, now):
        return
    repo, number = _split(key)
    if not _do(
        port, outcome, _comment_effect(state, repo, number, dispatch.rearming_notice())
    ):
        outcome.log.append(f"dispatch rearm: {key} held, the notice did not land")
        return
    dispatch.mark_rearmed(rec)
    # A card parked by a label-driven spawn holds no queued instruction, so without an
    # explicit action the start path would pass straight over it. It takes the action it
    # would take now — the same one a person's `/claude` re-arm produces — and is marked as
    # engine recovery rather than somebody's start, so it still waits for the token budget.
    rec["recovery_action"] = _ready_action(rec)
    outcome.log.append(f"dispatch rearm: {key} parked spawn retried once automatically")


def _note_unowned_blockers(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    states: dict[str, str],
) -> None:
    """Say once when a parked card waits on an open issue nobody is working on (#2382).

    A worker that files a blocker issue without the ownership label parks its card behind
    something the poller will never pick up: #2331 waited almost eight hours on #2365 that
    way. The engine does not label someone else's issue -- whether it should be worked is a
    person's call -- but it says so on the card, once per set of such issues.
    """
    repo, number = _split(key)
    # Only this card's own repository: another repository may be held by another poller
    # instance, which this one cannot see.
    unowned = sorted(
        ref
        for ref in card_blockers.resuming(rec)
        if ref.startswith(f"{repo}#") and states.get(ref) == "OPEN" and ref not in _cards(state)
    )
    mark = card_blockers.fingerprint(unowned)
    if not unowned or rec.get("unowned_blockers_noted") == mark:
        return
    listed = ", ".join(f"`{ref}`" for ref in unowned)
    if _do(
        port, outcome,
        _comment_effect(
            state, repo, number,
            f"**Nothing is working on {listed}.** This card is waiting for it, but it is not "
            "on the board — it has no poller label — so nobody will pick it up unless a person "
            "does. Label it to have it worked, or answer here if this card should go on "
            "without it.",
            include_session=False,
        ),
    ):
        rec["unowned_blockers_noted"] = mark


def _reconsider_blocked(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    decision: reconcile.ReconcileDecision,
    snapshot: CardSnapshot,
) -> None:
    """Pick a card back up once the thing its worker was blocked on has landed (#1230).

    A worker that correctly stops because a prerequisite pull request is still in flight used
    to end the card's life: it reported ``blocked``, the board moved, and nothing on either
    side was watching the prerequisite. The card sat with the ownership label on it and the
    only way out was a person typing ``/claude the blocker merged, continue``.

    So a blocked worker names what it is waiting for (``finish blocked --blocked-by``), and
    reconciliation — the one phase that already reads every managed card — asks GitHub what
    became of those refs. Every one closed or merged, and the card is armed as an ordinary
    ``resume``: the same dispatch a cold reload performs, from the current base branch, with
    the card's worktrees, branch and pull request still attached.

    Three properties are load-bearing:

    * **It fails closed.** A ref that could not be read this wake is still holding the card
      (``blockers.unresolved``), exactly as an unreadable dependency holds an unstarted one.
      Waiting one more wake costs nothing; resuming into a prerequisite that never landed
      costs the worker's whole turn and produces a diff against the wrong base.
    * **It arms once.** The hold is dropped in the same transaction that sets the resume, and
      only a card actually resting in *Blocked* with no dispatch, mode or live worker is
      considered — so a card a person already resumed by hand is passed over rather than
      given a second concurrent session.
    * **The comment gates the arm.** If the explanation does not land, nothing is armed and
      the next wake tries the whole thing again. A card must never come back to life with no
      trace in its own thread of why.

    Only a worker's report leaves that hold, though, so for a long time this rescued exactly
    one way of reaching *Blocked* and no other (#1588). A card held from the start by a GitHub
    native dependency — the mechanism this project's docs tell every flow to use, and the one
    the board renders — had nothing recorded to clear, so it was never looked at again and sat
    in the column forever after its blocker closed. Five of the six blocked cards on this host
    were in that state.

    So an empty hold falls back to what the card *declares*: ``snapshot.dependencies`` is the
    whole list and ``snapshot.blockers`` the part still holding it, both read out of the query
    the wake already made. All declared and none holding means the card is provably free, and
    it is armed exactly as a recorded hold would arm it. A card declaring nothing at all is
    passed over, because *Blocked* with no dependency is somebody's deliberate parking and
    nothing here can tell it apart from a mistake.

    That fallback needs a fence the recorded hold does not: the hold is consumed when it arms,
    but the dependencies stay closed forever, so a card whose next worker reports *Blocked*
    again for an unnameable reason would be resumed on every wake. ``resume_armed`` gives one
    resume per set of cleared dependencies.
    """
    if decision in _CARD_ENDING_DECISIONS or rec.get("forget_pending"):
        return
    held = card_blockers.resuming(rec)
    recorded = bool(held)
    if not held:
        held = card_blockers.cleared_dependencies(snapshot.dependencies, snapshot.blockers)
        if not held or card_blockers.resume_armed(rec, held):
            _resume_verified(state, port, outcome, key=key, rec=rec, snapshot=snapshot)
            return
    if not (snapshot.open and _claimed(rec, snapshot)):
        return
    if (
        rec.get("status") != config.Role.BLOCKED.value
        or rec.get("dispatch")
        or rec.get("stopping")
        or rec.get("mode")
        or rec.get("recovery_action")
        or _ids(rec, "processing")
        or _ids(rec, "pending")
    ):
        return
    if recorded:
        # The refs a worker named are re-read: nothing else has proven what became of them,
        # and one that cannot be read still holds the card.
        states = port.ref_states(list(held))
        waiting = card_blockers.unresolved(held, states)
        if waiting:
            if waiting != held:
                # Something cleared but not everything. Narrowing the hold means the resume
                # comment can name what actually landed rather than repeating the whole list.
                card_blockers.record_resume(rec, waiting)
            outcome.log.append(f"blocked: {key} still waiting on {', '.join(waiting)}")
            _note_unowned_blockers(state, port, outcome, key=key, rec=rec, states=states)
            return
    started = bool(rec.get("started"))
    repo, number = _split(key)
    if not _do(
        port, outcome,
        _comment_effect(
            state, repo, number, card_blockers.resumed_notice(held, started=started)
        ),
    ):
        outcome.log.append(f"blocked: {key} blockers cleared but the notice did not land")
        return
    card_blockers.record_resume(rec, ())
    card_blockers.clear_verified(rec)
    if not recorded:
        card_blockers.mark_resume_armed(rec, held)
    # A card that never ran has no branch or worktree to resume into, so it takes the action a
    # start would take now rather than the recorded hold's ``resume``.
    rec["recovery_action"] = "resume" if started else _ready_action(rec)
    outcome.log.append(f"blocked: {key} {', '.join(held)} landed -> resuming")


# A card whose spec worker ran under the spec-only label, and whose label has since come off.
_SPEC_APPROVED = "spec_approved"
# The next work dispatch builds that approved spec, and its brief says so.
_SPEC_BUILD = "spec_build"
# The spec worker's own generation, once it took the build over in its chat (#2716).
_SPEC_BUILD_IN_CHAT = "spec_build_in_chat"


def was_spec_card(rec: dict, generation: str) -> bool:
    """Whether ``generation`` wrote a spec here, or already took its build over (#2716).

    ``spec_only`` stays on the record until a wake sees the label gone; that wake swaps it for
    ``spec_approved``. Either means the Run holding the lease is the spec Run.
    """
    return bool(
        rec.get("spec_only")
        or rec.get(_SPEC_APPROVED)
        or rec.get(_SPEC_BUILD_IN_CHAT) == generation
    )


def _spec_worker_active(rec: dict, snapshot: CardSnapshot) -> bool:
    """Whether a spec worker is mid-turn, or an answer typed into its chat will resume it.

    The record counts as well as the live card: a chat answer posted earlier this wake resumes
    the record before the board read shows it.
    """
    return (
        snapshot.status == _IN_PROGRESS
        or rec.get("status") == _IN_PROGRESS
        or bool(rec.get("chat_answers_pending"))
    )


def settle_spec_finish(rec: dict, generation: str, *, approved: bool, label_on: bool) -> str:
    """Let a spec worker's ``finish needs_more_info`` decide whether the build starts (#2773).

    A label removed mid-turn is not an approval on its own: the person may have told the worker
    in its chat to do something else entirely. ``approved`` (``--spec-approved``) marks the card
    so ``_start_approved_spec`` builds it once it rests; a plain finish with the label already
    off drops the spec record, so a later wake does not read the removal as an approval either.
    A plain finish with the label still on changes nothing: removing it later, while the card
    rests, is still a person's approval. Returns a refusal, or ``""``.
    """
    spec = was_spec_card(rec, generation)
    if not approved:
        if spec and not label_on:
            rec.pop("spec_only", None)
            rec.pop(_SPEC_APPROVED, None)
        return ""
    if not spec:
        return "refused: --spec-approved is only for the Run that wrote a spec on this card"
    if label_on:
        return (
            "refused: remove the spec-only label first, and only on a person's explicit "
            "approval, then finish again"
        )
    if rec.get(_SPEC_BUILD_IN_CHAT) == generation:
        return "refused: this Run already took the build over with start-implementation"
    rec.pop("spec_only", None)
    rec[_SPEC_APPROVED] = True
    return ""


def take_spec_build(rec: dict, generation: str) -> None:
    """The spec worker builds the approved spec itself, so the poller starts no worker for it."""
    rec.pop("spec_only", None)
    rec.pop(_SPEC_APPROVED, None)
    rec.pop(_SPEC_BUILD, None)
    rec[_SPEC_BUILD_IN_CHAT] = generation


def _start_approved_spec(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    decision: reconcile.ReconcileDecision,
    snapshot: CardSnapshot,
) -> None:
    """Start the build of an approved spec once its card rests in Needs More Info (#2706).

    A spec-only worker parks the card in Needs More Info. Removing the label afterwards is a
    person's approval — by hand on GitHub, or by that worker when the person approves in its
    chat. Either way nothing used to start the build: the spec worker's brief has no build
    steps, and only a person's `/claude` comment started a new worker.

    So the removal marks the card — a removal while the spec worker is mid-turn only through
    that worker's ``finish needs_more_info --spec-approved`` (#2773) — and once it rests in
    Needs More Info with nothing else
    queued or running, one comment records the approval and arms an ordinary ``resume``. The
    comment gates the arm, as a blocked card's resume does. A card that moved on by any other
    route (a person's `/claude`, a column change, a closed issue) drops the mark instead.
    """
    if not rec.get(_SPEC_APPROVED):
        return
    if decision in _CARD_ENDING_DECISIONS or rec.get("forget_pending"):
        rec.pop(_SPEC_APPROVED, None)
        return
    status = rec.get("status")
    if status == _IN_PROGRESS:
        # The spec worker may still be finishing in its chat; it parks the card next.
        return
    if status != _NEEDS_INFO:
        rec.pop(_SPEC_APPROVED, None)
        return
    if not (snapshot.open and _claimed(rec, snapshot)):
        return
    if (
        # An answer typed into the spec worker's chat that a wake could not post yet (#2716):
        # once it lands the card resumes In progress, and that worker may build it itself.
        rec.get("chat_answers_pending")
        or rec.get("dispatch")
        or rec.get("stopping")
        or rec.get("mode")
        or rec.get("recovery_action")
        or rec.get("paused")
        or _ids(rec, "processing")
        or _ids(rec, "pending")
    ):
        return
    repo, number = _split(key)
    if not _do(port, outcome, _comment_effect(state, repo, number, _SPEC_APPROVED_NOTICE)):
        outcome.log.append(f"spec approved: {key} the notice did not land")
        return
    rec.pop(_SPEC_APPROVED, None)
    rec[_SPEC_BUILD] = True
    rec["recovery_action"] = "resume"
    outcome.log.append(f"spec approved: {key} spec-only label removed -> building")


_SPEC_APPROVED_NOTICE = (
    "**Spec approved — starting the build.** The `spec-only` label was removed after the "
    "specification was posted, which is a person's approval, so a worker now implements the "
    "approved spec. Post `/claude stop` to hold it."
)


def _resume_verified(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    snapshot: CardSnapshot,
) -> None:
    """Pick a Blocked card back up once a green verification lands on its current head (#2306).

    A worker waiting on its own suite may stop in *Blocked* and say, in prose, that the suite is
    the only thing left. The suite then finishes green — often after the worker that submitted
    it was replaced by a newer generation, whose own ``finish`` is correctly refused — and
    nothing was left to act on the result: the tests bit went green on a card nobody would read
    again, and a person had to move it by hand.

    ``_credit_verification`` marks the card whenever that result is credited — before or after
    the worker reports *Blocked*, since either can land first — and this spends the mark, once
    the card rests in *Blocked*, as one ordinary ``resume`` — the same dispatch a cleared
    dependency arms — so the current generation, not a stale one, finishes the report. It is
    deliberately not a status write: whether the card is really done is the worker's call, and a
    resumed worker that is still blocked says so again. Every other hold still wins:

    * an open dependency, or a recorded ``--blocked-by`` ref, keeps the card where it is;
    * the verified commit must be the current head of every open pull request, and each of
      those heads must be green — evidence for an old head proves nothing about the new one;
    * a red, running or unclean job never marks anything, because it credits nothing.

    Unreadable pull requests defer with the mark kept, so a later wake tries again; a mark that
    fails the head check is dropped, since that result can never become true for this card.
    """
    verified = card_blockers.verified_while_blocked(rec)
    if not verified:
        return
    if rec.get("status") != config.Role.BLOCKED.value:
        # Not resting in Blocked (yet): the worker may still be running and about to report
        # Blocked, so the mark waits. The next dispatch start is what forgets it.
        return
    if not (snapshot.open and _claimed(rec, snapshot)):
        return
    if (
        rec.get("dispatch")
        or rec.get("stopping")
        or rec.get("mode")
        or rec.get("recovery_action")
        or _ids(rec, "processing")
        or _ids(rec, "pending")
    ):
        return
    if snapshot.blockers:
        outcome.log.append(
            f"blocked: {key} verified green but still waiting on {', '.join(snapshot.blockers)}"
        )
        return
    head = str(verified.get("head") or "")
    repo, number = _split(key)
    linked = port.linked_prs(repo, number)
    if linked is None:
        outcome.log.append(f"blocked: {key} verified green; linked PRs unreadable, deferring")
        return
    # A draft is withheld from release but is still the branch the suite had to prove, so its
    # head counts here exactly like an open one's.
    refs = list(dict.fromkeys((*linked.open, *linked.withheld)))
    heads: dict[str, str | None] = {}
    if refs:
        states = port.pr_states(refs)
        if states is None or any(ref not in states or not states[ref].head for ref in refs):
            outcome.log.append(f"blocked: {key} verified green; PR heads unreadable, deferring")
            return
        heads = {ref: states[ref].head for ref in refs}
        # Every linked pull request must sit on the verified commit itself. Separate green
        # evidence for a second head is not this result, and this result is what arrived.
        if any(str(sha or "") != head for sha in heads.values()) or not (
            release.tests_pass_current(rec, heads=heads)
        ):
            card_blockers.clear_verified(rec)
            outcome.log.append(
                f"blocked: {key} verified green on {head[:12]}, which is not the head of every "
                "open pull request -> stays Blocked"
            )
            return
    if not _do(
        port, outcome,
        _comment_effect(
            state, repo, number,
            card_blockers.verified_notice(
                head, job=str(verified.get("job") or ""), refs=tuple(sorted(heads))
            ),
        ),
    ):
        outcome.log.append(f"blocked: {key} verified green but the notice did not land")
        return
    card_blockers.clear_verified(rec)
    rec["recovery_action"] = "resume" if rec.get("started") else _ready_action(rec)
    outcome.log.append(f"blocked: {key} verified green on {head[:12]} -> resuming")


def _messages(state: dict, rec: dict, ids: list[str] | None = None) -> list[dict]:
    """The batched instructions a worker receives: the command records, not bare text.

    A worker answers the requester, so it needs who asked and where, not only what.
    """
    out = []
    for cid in ids if ids is not None else _ids(rec, "processing"):
        control = queue.controls(state).get(cid)
        if control is None:
            continue
        out.append({key: control.get(key) for key in ("id", "url", "author", "payload")})
    return out


def _spawn(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    action: str,
    now: datetime,
    store: StateStore | None = None,
    allow_over_budget: bool = False,
    avoid_account: str = "",
    reviving: bool = False,
    command_output: config.CommandOutputPolicy | None = None,
) -> bool:
    """Mint a leased dispatch for this card and emit its first attempt."""
    if not reviving:
        # Any dispatch that is not itself a revive is new work on this card, so the revive
        # budget starts over. Without this a card that was revived twice months ago would
        # reach a human on its first bad night instead of its third (#802).
        revive.reset(rec)
    # A dispatch ends whatever park the card rested in (#2235).
    rec.pop("release_blocked", None)
    rec.pop("parked_as", None)
    # A new Run is not the one whose ``resolved`` the hold kept for ``reopen`` (#2522).
    rec.pop("resolved_finish", None)
    spec_build = False
    if not reviving:
        # New work consumes an approved spec's start, whoever asked for it (#2706).
        rec.pop(_SPEC_APPROVED, None)
        rec.pop(_SPEC_BUILD_IN_CHAT, None)
        spec_build = bool(rec.pop(_SPEC_BUILD, None))
    attempt = dispatch.new_dispatch(
        rec,
        action,
        allow_over_budget=allow_over_budget,
        avoid_account=avoid_account,
        continuation=reviving,
    )
    if spec_build:
        attempt["spec_approved"] = True
    policy = command_output or config.CommandOutputPolicy()
    if policy.enabled and rec.get("mode") != "merge-review":
        # Bounded command output is a property of the worker's tools, not of any stage of the
        # work: the same settings ride every work dispatch of a project that turned it on.
        attempt["command_artifacts"] = policy.wire(card=key, generation=attempt["generation"])
    return _emit(state, port, outcome, key=key, rec=rec, attempt=attempt, now=now, store=store)


def _steer(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    control_ids: list[str],
    now: datetime,
    store: StateStore | None = None,
) -> bool:
    """Emit one generation-fenced active-turn follow-up without consuming a worker slot."""
    attempt = dispatch.new_steer(rec, control_ids)
    return _emit(
        state, port, outcome, key=key, rec=rec, attempt=attempt,
        now=now, store=store,
    )


def _emit(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    attempt: dict,
    now: datetime,
    store: StateStore | None = None,
) -> bool:
    """Publish the board transition, make the send intent durable, then queue the directive.

    The intent is written *before* the directive is handed out, so a crash in between
    leaves a dispatch the next wake awaits rather than a delivery nobody recorded. A
    refused board transition aborts the attempt with no directive at all.
    """
    repo, number = _split(key)
    steering = bool(attempt.get("steer"))
    # The Work is part of the durable send intent (#2142). The port is what knows the
    # project's GitHub owner; a port that names none (a test fake) leaves it uncorrelated.
    owner = getattr(port, "org", "")
    dispatch.stamp_work(
        attempt, owner=owner if isinstance(owner, str) else "", repo=repo, number=number
    )
    if store is not None:
        # The lease and the spawn intent are durable *before* the visible board transition:
        # a crash in between must leave a dispatch the next wake settles, never a status
        # change with no record of why.
        store.checkpoint(state)
    # Take the card, in the engine's own state rather than on the issue (#2053). The claim used
    # to be the ownership label, written back at dispatch, which meant a card started by a
    # `/claude` comment ended up wearing a label nobody asked for — and, because the poller edits
    # as its own token's owner, looking like the human had handed it over. The label is now
    # purely an inbound signal, and `engine_claim` is what tells the next reconcile the card is
    # ours. The label the *new* dispatch inherits is whatever this wake actually saw on the
    # issue: a card held by a blocker or the worker cap can have been handed back long before it
    # starts (#1663), and carrying that stale claim in would stop the worker on its next wake.
    if not steering:
        rec["engine_claim"] = True
        rec.pop("loud_hand_back", None)
        seen = outcome.live_snapshots.get(key)
        if seen is not None:
            rec["label_claim"] = bool(seen.owned)
        asked_at = rec.get("reopen_if_closed")
        if asked_at and seen is not None:
            # A person asked for work on this issue, so it should be open while it is done
            # (#2417). With no read this wake the marker waits for the first one.
            if not seen.open and _closed_before(seen, asked_at):
                if not _do(port, outcome, Effect(EffectKind.REOPEN_ISSUE, repo, number)):
                    return False
                outcome.live_snapshots[key] = replace(seen, open=True)
                outcome.log.append(f"reopened: {key} for a person's instruction")
            rec.pop("reopen_if_closed", None)
        if not _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, _IN_PROGRESS)):
            return False
        rec["status"] = _IN_PROGRESS
    dispatch.mark_sent(attempt, now)
    if store is not None:
        store.checkpoint(state)
    messages = _messages(
        state,
        rec,
        [str(cid) for cid in attempt.get("control_ids") or []] if steering else None,
    )
    directive = dispatch.build_directive(
        rec, attempt, repo=repo, number=number, ref=key, messages=messages
    )
    if directive.get("action") != "merge-review":
        flags = {}
        if ready_review.enabled(state):
            # The brief tells this worker to offer its head and verify beside the review (#2350).
            flags["ready_for_review"] = True
        elif state.get(_VERIFY_TURN_ENABLED):
            # The brief tells this worker the engine asks for its final suite after review
            # (#2360).
            flags["verify_turn"] = True
        if not directive.get("spec_only"):
            directive.update(flags)
        elif flags:
            # A spec worker writes no code, but may be asked in its chat to build the spec it
            # wrote (#2716); the build brief the host hands it then needs these.
            directive["build_flags"] = flags
    outcome.directives.append(directive)
    outcome.report.dispatch_attempts += 1
    verb = "steer" if steering else "dispatch"
    outcome.log.append(f"{verb}({attempt['action']}): {key} attempt {attempt['attempts']}")
    return True


# --- phase 5: the release progression ---

def _process_releases(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    dispatch_ok: bool,
    auto_deploy_repos: frozenset[str] | set[str],
    store: StateStore | None = None,
    installed: list | None = None,
    mode: ext.Mode = ext.Mode.LIVE,
    ready_to_merge: str | None = None,
    auto_release: bool = False,
    merge_first: frozenset[str] = frozenset(),
) -> None:
    """Drive every card in a release mode one step further, or to a terminal outcome.

    A release is mostly worker-free — discover, arm native auto-merge, observe, gate the
    production deploy, finish — so it runs every wake regardless of the token budget;
    only the single repair spawn is gated. One card's unreadable PR never stalls another.
    """
    for key in list(_cards(state)):
        rec = _cards(state).get(key)
        if rec is None or rec.get("mode") not in {"merge", "merge-review"} or rec.get("stopping"):
            continue
        if release.objection(rec):
            # A reviewer's objection is written on this card and not yet discharged (#2108), so
            # the release stands still until `_settle_objection` removes it. Driving it one step
            # further would arm the merge the objection is trying to stop, or finish and forget
            # the very card the too-late warning still has to be posted on — and the next wake's
            # retry would then have lost by its own rule. Stuck is the safe direction here.
            outcome.log.append(f"release: {key} held while a reviewer's objection is settled")
            continue
        repo, number = _split(key)
        if rec.get("mode") == "merge-review" and any(
            rec.get(field) for field in ("awaiting_merge", "awaiting_deploy", "merged")
        ):
            # A release watch is newer and authoritative proof that review already passed.
            # An older review recovery must never consume a late verdict and hand it back.
            rec.pop("dispatch", None)
            rec["mode"] = "merge"
            outcome.log.append(f"release: {key} ignored stale review over active release")
        finalization = release.pending_finalization(rec)
        if finalization == "done_pending":
            _finish_release(state, port, outcome, key=key, rec=rec, now=now)
            continue
        if finalization == "blocked_pending":
            _block_release(state, port, outcome, key=key, rec=rec, ready_to_merge=ready_to_merge)
            continue
        if finalization == "merge_start_pending":
            if not _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, _IN_PROGRESS)):
                continue
            release.clear_finalization(rec, "merge_start_pending")
            outcome.log.append(f"release: {key} recovered the start transition")
        if rec.get("paused") and not any(
            rec.get(field) for field in ("awaiting_merge", "awaiting_deploy", "merged")
        ):
            # A paused card's release waits (#2548). One that already armed or made its merge
            # goes on: stopping half way would leave a merge nobody watches.
            outcome.log.append(f"release: {key} held, paused")
            continue
        if rec.get("mode") == "merge-review":
            _progress_review(
                state,
                port,
                outcome,
                key=key,
                rec=rec,
                now=now,
                dispatch_ok=dispatch_ok,
                store=store,
                ready_to_merge=ready_to_merge,
                auto_release=auto_release,
            )
            continue
        if rec.get("awaiting_deploy"):
            _gate_deploy(
                state, port, outcome, key=key, rec=rec, now=now,
                installed=installed or [], mode=mode, ready_to_merge=ready_to_merge,
            )
        elif rec.get("awaiting_merge"):
            _gate_merge(
                state, port, outcome, key=key, rec=rec, now=now,
                auto_deploy_repos=auto_deploy_repos, ready_to_merge=ready_to_merge,
            )
        else:
            _assess_release(
                state, port, outcome, key=key, rec=rec, now=now,
                dispatch_ok=dispatch_ok, auto_deploy_repos=auto_deploy_repos, store=store,
                ready_to_merge=ready_to_merge, merge_first=merge_first,
            )


@dataclass(frozen=True)
class Freshness:
    """The freshness verdict together with the exact content it was decided from (#2101).

    Returning the snapshot rather than only the verdict is what closes the gap between
    checking and using it. A caller that wants to *keep* what a review covered must record
    these very values: reading GitHub a second time would pair heads validated at one instant
    with a diff digest read at another, and a push or base movement in between would make the
    pair describe content no review ever saw.
    """

    state: bool | None          # True current, False stale, None unverifiable
    heads: dict                 # the open pull requests' head SHAs, as validated
    diffs: dict                 # the effective diff digests that were compared
    trees: dict                 # the legacy prospective trees that were compared


def _freshness(port: EnginePort, rec: dict, repo: str, number: int) -> Freshness:
    """Is the approved diff still current? Returns the verdict *and* what proved it."""
    linked = _linked(port, rec, repo, number)
    if linked is None:
        return Freshness(None, {}, {}, {})
    heads = dict(linked.open)
    diffs, trees = _review_content(port, rec, heads)
    return Freshness(
        release.heads_unchanged(
            rec,
            open_refs=set(linked.open),
            heads=heads,
            review_diffs=diffs,
            release_trees=trees,
            # The same exact-transition provenance the release bit uses (#2122): a changed head
            # may reuse the contributed digest only where the engine recorded producing it.
            rebase_moved=release.rebase_round(rec),
        ),
        heads,
        diffs,
        trees,
    )


def _review_content(
    port: EnginePort, rec: dict, heads: dict[str, str | None]
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """Read current diff identities, plus legacy trees only for moved reviewed heads."""
    expected = rec.get("review_heads")
    reviewed_diffs = rec.get("review_diffs")
    reviewed_trees = rec.get("review_trees")
    if not isinstance(expected, dict):
        return {}, {}
    diff_refs = list(reviewed_diffs) if isinstance(reviewed_diffs, dict) else []
    changed = [
        ref for ref, head in heads.items()
        if (
            head
            and expected.get(ref) != head
            and isinstance(reviewed_trees, dict)
            and reviewed_trees.get(ref)
            and ref not in diff_refs
        )
    ]
    return (
        port.review_diffs(diff_refs) if diff_refs else {},
        port.release_trees(changed) if changed else {},
    )


def _handback(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, key: str, rec: dict, why: str,
    ready_to_merge: str | None = None,
) -> bool:
    """Return the card to rest with an explanation; nothing was released.

    Where it rests is *Ready to merge* for a project that made that column, and *In review*
    otherwise (#1099). The card is approved work whose release did not land, which is the
    same thing a failed merge leaves behind — putting both in one column is what lets a
    person retry with ``/merge`` instead of first working out which of two columns this
    particular failure happened to choose.
    """
    repo, number = _split(key)
    if release.beyond_recall(rec):
        # A watch proves the release is newer than this review path. Keep it authoritative.
        rec.pop("dispatch", None)
        rec["mode"] = "merge"
        outcome.log.append(f"release: {key} ignored stale review handback during release")
        return False
    if not _cancel_armed_auto_merges(port, outcome, key=key, rec=rec, discover=True):
        return False
    rests_in = ready_to_merge or release.IN_REVIEW
    if not _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, rests_in)):
        return False
    # The review request has reached a terminal answer. Leaving it queued lets the
    # independent command ledger recover it on the next wake as an unfinished review,
    # which can overwrite a later plain /merge and hand the card back again.
    _resolve_release_control(state, port, outcome, rec, status="rejected")
    rec.pop("awaiting_merge", None)  # every remote request above is now proven absent
    release.handback(rec, rests_in=rests_in)
    _do(port, outcome, _comment_effect(state, repo, number, why))
    outcome.log.append(f"release: {key} handed back for review")
    return True


def _cancel_armed_auto_merges(
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    discover: bool = False,
) -> bool:
    """Cancel every remote merge request before abandoning its local watch.

    Successful refs are removed one by one so a partial failure retries only what remains.
    The port operation is idempotent because GitHub may accept the write just before a crash.
    """
    watch = rec.get("awaiting_merge")
    watched = watch.get("prs") if isinstance(watch, dict) else None
    refs = watched if isinstance(watched, list) else None
    if refs is None and discover:
        repo, number = _split(key)
        linked = _linked(port, rec, repo, number)
        if linked is None:
            outcome.log.append(f"release: {key} could not discover auto-merge requests to cancel")
            return False
        refs = list(linked.open)
    if refs is None:
        return True
    for ref in list(refs):
        if not port.cancel_auto_merge(str(ref)):
            detail = f"release: {key} could not cancel auto-merge on {ref}"
            outcome.report.failed(detail)
            outcome.log.append(detail)
            return False
        if isinstance(watched, list) and ref in watched:
            watched.remove(ref)
        outcome.log.append(f"release: {key} cancelled auto-merge on {ref}")
    return True


def _review_reported_status(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    snapshot: CardSnapshot,
) -> bool:
    """End a review whose worker reported an ordinary terminal status, not a verdict (#1406).

    A reviewer is supposed to answer with a lease-bound verdict. One that instead posts the
    terminal marker an ordinary worker posts has still *reported*: the marker is bound to this
    same lease and names the column it wants. Until now nothing consumed it under a review
    lease and the card rested forever — ``reconcile_card`` answers ``RELEASE_GATED`` for a
    release mode long before the ``TERMINAL_RECOVERED`` branch that publishes a marker, and
    ``_note_reportless_end`` declines on sight of a marker because a marker means a report
    exists. Both are right on their own, and between them a card held a worker slot for three
    weeks with its board already showing the status nobody had published.

    So publish it, exactly as ``TERMINAL_RECOVERED`` would, and end the review. What is *not*
    published is a verdict: nothing here read a diff, so nothing is recorded against a commit
    (#1122), nothing merges, and the comment says so in as many words — a card arriving in
    *In review* by this route must not read as "the review passed".
    """
    marker = snapshot.terminal_marker
    if not marker or rec.get("dispatch") or rec.get("stopping"):
        # No report, an unacknowledged respawn (whose marker would be the previous lease's
        # answer to a question already asked again), or a stop running its own protocol.
        return False
    repo, number = _split(key)
    if not _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, marker)):
        return True  # the ending stands; the next wake retries the write
    rec["status"] = marker
    _resolve_release_control(state, port, outcome, rec, status="cancelled")
    rec.pop("mode", None)
    rec.pop("dispatch", None)
    _finish_processing(state, port, outcome, rec)
    _do(
        port,
        outcome,
        _comment_effect(
            state,
            repo,
            number,
            "This card's reviewer ended by asking for this status instead of reporting a "
            "review verdict, so the engine has published it and closed the review. **No "
            "verdict was recorded and nothing was merged** — this status is the reviewer's "
            "own report, not a passed review. Ask for the review again with `/merge review` "
            "when the change is ready to ship.",
        ),
    )
    outcome.log.append(
        f"release: {key} reviewer reported terminal status {marker} instead of a verdict "
        "-> published, nothing merged"
    )
    return True


def _progress_review(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    now: datetime,
    dispatch_ok: bool,
    store: StateStore | None,
    ready_to_merge: str | None = None,
    auto_release: bool = False,
) -> None:
    """Consume a lease-bound review verdict: release the work, or hand it back."""
    repo, number = _split(key)
    if rec.get("status") != _IN_PROGRESS:
        # A review command is armed after this wake's live snapshot was read, while the
        # reviewer is spawned later in the cycle. Until that spawn marks the card active,
        # the snapshot can still carry the previous review lease's verdict. Never let that
        # stale marker answer a newly queued review (#973).
        return
    snapshot = outcome.live_snapshots.get(key)
    if snapshot is None:
        return
    verdict = snapshot.review_verdict
    if not verdict:
        if _review_reported_status(
            state, port, outcome, key=key, rec=rec, snapshot=snapshot
        ):
            return
        # Release modes normally bypass worker reconciliation: their progress comes from
        # the guarded release state machine, not the board column. A merge review is the
        # one release mode that is still waiting on a worker, though, and its required
        # report is the verdict checked in the same readable snapshot above. If the host
        # proves that turn ended without one, feed the same bounded revive path as ordinary
        # work instead of leaving RELEASE_GATED to hold a worker slot forever (#932).
        _note_reportless_end(
            state,
            port,
            outcome,
            key=key,
            rec=rec,
            decision=reconcile.ReconcileDecision.RELEASE_GATED,
            snapshot=snapshot,
            now=now,
            dispatch_ok=dispatch_ok,
            store=store,
            release_review=True,
        )
        return  # the reviewer has not reported under this lease yet
    fresh = _freshness(port, rec, repo, number)
    heads_state, heads = fresh.state, fresh.heads
    decision = release.classify_verdict(
        verdict, heads_state=heads_state, has_pending=bool(rec.get("pending"))
    )
    if decision is release.ReviewOutcome.DEFER:
        outcome.log.append(f"release: {key} review approval could not be verified; deferring")
        return
    _record_review_verdict(
        outcome,
        store,
        key=key,
        rec=rec,
        heads=heads,
        verdict=verdict,
        decision=decision,
        now=now,
    )
    if verdict != "pass":
        release.clear_review_pass(rec)
    elif auto_release and heads_state is True:
        # Keep the pass as a standing fact about the content it covered (#2101). The release
        # below spends the *approval*; this outlives it, so a release that hands back, parks
        # on a red deploy or is superseded does not cost a second review of an unchanged diff.
        #
        # Both conditions are load-bearing, and dropping either launders an unreviewed push
        # into a reviewed one. ``heads``, here, is the *live* open-pull-request set, not the
        # set the review lease covered: if the reviewer passed head A while somebody pushed
        # head B, `classify_verdict` correctly hands back — but writing the snapshot anyway
        # would store B's freshly-read diff as "reviewed", and once B also had a green suite
        # the poller would release code no reviewer ever saw. `heads_state is True` is exactly
        # the statement that live content and reviewed content are the same thing.
        #
        # ``auto_release`` gates it because the capability's contract says a project that did
        # not opt in neither reads nor acts on the bits, and writing durable state onto its
        # cards would make that false. Nothing else would notice — the bit is only ever read
        # behind the same flag — but a contract that is only true where somebody happens to
        # look is not one, and this state outlives the wake that wrote it.
        _record_release_review_pass(rec, key=key, fresh=fresh, now=now, outcome=outcome)
    if decision is release.ReviewOutcome.RELEASE:
        rec.pop("dispatch", None)
        release.begin_merge(rec, now=now)
        outcome.log.append(f"release: {key} review passed -> release")
        return
    rec.pop("dispatch", None)
    if verdict != "pass":
        why = (
            "Review did not clear the release. Nothing was merged — look at the diff and "
            "run `/merge review` again when it is ready."
        )
    else:
        why = (
            "Linked PRs or their head commits changed after `/merge review`. Nothing was "
            "merged; review the new diff and approve again."
        )
    _handback(
        state, port, outcome, key=key, rec=rec, why=why, ready_to_merge=ready_to_merge,
    )


def _record_release_review_pass(
    rec: dict,
    *,
    key: str,
    fresh: Freshness | None,
    now: datetime,
    outcome: CycleOutcome,
) -> None:
    """Snapshot the content a clean verdict covered, so a later rebase can carry it (#2101).

    Takes no port, deliberately, and that is the whole point of the signature. An earlier
    version read the diffs here instead, which looked harmless — the freshness check had just
    proved the content current, so re-reading it should return the same answer. It need not.
    The check validated one instant and this read a later one, so a push or a base movement in
    between would store the *old* heads beside a *newer* diff digest, and a later wake's
    diff-based freshness would then accept content this review never covered. That is the
    round-1 bug in a second dress, not a new edge case, so the fix is the same shape: record
    the exact snapshot that was validated, rather than guarding a second read.

    Recording only what was validated is also why a reference whose diff was never read falls
    back to its head. That is stricter, not looser — the pass then survives only while the
    commit is untouched, instead of claiming a carry-over nothing established.
    """
    if fresh is None or fresh.state is not True:
        return
    heads = {ref: sha for ref, sha in fresh.heads.items() if sha}
    if not heads or len(heads) != len(fresh.heads):
        return
    if release.record_review_pass(
        rec, heads=heads, diffs=fresh.diffs, trees=fresh.trees, now=now
    ):
        outcome.log.append(
            f"release: {key} review pass recorded for {len(heads)} pull request(s)"
        )


def _record_review_verdict(
    outcome: CycleOutcome,
    store: StateStore | None,
    *,
    key: str,
    rec: dict,
    heads: dict[str, str | None],
    verdict: str,
    decision: release.ReviewOutcome,
    now: datetime,
) -> None:
    """Keep what this verdict decided about each reviewed commit (#1122).

    The card is about to forget it: a release spends the approval and a hand-back drops it,
    so this is the last moment the reviewed commit and the verdict exist together. One record
    per open pull request, keyed by that pull request's own repository and head commit —
    which is not necessarily the card's repository, since a card may ship several.

    Writing is best effort in the strongest sense: no failure here changes what the release
    does, because a history record is worth less than the release it would otherwise block.
    """
    if store is None:
        return
    issue_repo, issue_number = _split(key)
    generation = str(rec.get("generation") or "")
    written = 0
    for ref, sha in sorted(heads.items()):
        if not sha:
            continue  # an unreadable head is not a commit anybody reviewed
        try:
            pr_repo, _ = _split(str(ref))
        except (TypeError, ValueError):
            continue
        if reviews.record(
            store.paths.review_verdict_dir,
            repo=pr_repo,
            sha=str(sha),
            pull_request=str(ref),
            issue_repo=issue_repo,
            issue_number=issue_number,
            verdict=verdict,
            result=decision.value,
            generation=generation,
            now=now,
        ):
            written += 1
    if written:
        outcome.log.append(
            f"release: {key} recorded {written} review verdict(s) as {decision.value}"
        )


def _gate_merge(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    now: datetime,
    auto_deploy_repos: frozenset[str] | set[str],
    ready_to_merge: str | None = None,
) -> None:
    """Observe the armed PRs until every one has merged, or the release must stop."""
    repo, number = _split(key)
    watch = rec["awaiting_merge"]
    refs = list(watch.get("prs") or [])
    states = port.pr_states(refs)
    if states is None:
        return  # transient: re-check next wake
    builds = dict(watch.get("premerged") or {})
    # What a park here parked on, so a later push ends it (#2771).
    heads = {
        ref: pr.head for ref, pr in states.items() if ref in refs and pr.state == "OPEN"
    }
    for ref in refs:
        pr = states.get(ref)
        if pr is None:
            return
        if pr.state == "OPEN":
            # The usual stall: the branch fell behind after arming, and protection requires
            # an up-to-date branch. Keep it current; auto-merge stays armed and fires later.
            if pr.merge_state == "BEHIND" and _do(
                port, outcome, Effect(EffectKind.UPDATE_BRANCH, *_split(ref))
            ):
                # The head this update produced, not a push.
                _note_branch_sync(port, rec=rec, ref=ref, before=pr.head, now=now)
                watch["since"] = now.astimezone(UTC).isoformat()
                outcome.log.append(f"release: {key} armed {ref} fell behind -> updated")
                return  # its CI verdict is untrustworthy until the update lands
            blocker = release.classify_pr(
                check_state=pr.check_state,
                merge_state=pr.merge_state,
                failed_steps=pr.failed_steps,
            )
            if release.is_repairable(blocker):
                _repair_after_arming(
                    state, port, outcome, key=key, rec=rec, ref=ref, blocker=blocker,
                    failed_steps=list(pr.failed_steps), now=now,
                    ready_to_merge=ready_to_merge, heads=heads,
                )
                return
            if not pr.auto_merge:
                # GitHub is no longer holding the request this release armed, so nothing is
                # going to fire however long the watch waits. Rest now rather than at the
                # timeout, and say so — the card is approved work that simply did not land.
                release.arm_blocked(
                    rec,
                    f"Auto-merge on `{ref}` is no longer armed on GitHub and its checks are "
                    "not failing, so nothing will merge it. `/merge` again to re-arm.",
                    merged=rec.get("merged"),
                )
                _block_release(
                    state, port, outcome, key=key, rec=rec, ready_to_merge=ready_to_merge,
                    heads=heads,
                )
                return
            if release.is_timed_out(watch.get("since"), now):
                release.arm_blocked(
                    rec,
                    f"Auto-merge on `{ref}` did not fire within the time limit — "
                    "check GitHub before closing.",
                    merged=rec.get("merged"),
                )
                _block_release(
                    state, port, outcome, key=key, rec=rec, ready_to_merge=ready_to_merge,
                    heads=heads,
                )
            else:
                outcome.log.append(f"release: {key} waiting for auto-merge on {ref}")
            return
        if pr.state == "CLOSED":
            release.arm_blocked(
                rec, f"`{ref}` was closed without merging — stopping.", merged=rec.get("merged")
            )
            _block_release(
                state, port, outcome, key=key, rec=rec, ready_to_merge=ready_to_merge,
                heads=heads,
            )
            return
        pr_repo, _ = _split(ref)
        if pr_repo in auto_deploy_repos and pr.merge_commit:
            builds[pr_repo] = {"sha": pr.merge_commit, "status": "pending"}

    rec.pop("awaiting_merge", None)
    rec["merged"] = refs
    if builds:
        release.begin_deploy_watch(
            rec, builds=builds, now=now, merged_at=_earliest_merge(states, refs)
        )
        _do(
            port, outcome,
            _comment_effect(
                state, repo, number,
                "**Merged** " + ", ".join(f"`{ref}`" for ref in refs)
                + ". Watching the production deploy(s) — Done once green, Blocked if one fails.",
            ),
        )
        outcome.log.append(f"release: {key} merged -> awaiting deploy {sorted(builds)}")
        return
    release.arm_done(rec, merged=refs)
    _finish_release(state, port, outcome, key=key, rec=rec, now=now)


def _repair_after_arming(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    ref: str,
    blocker: release.PrBlocker,
    failed_steps: list[str],
    now: datetime,
    ready_to_merge: str | None = None,
    heads: dict[str, str | None] | None = None,
) -> None:
    """An armed PR went red or conflicted: spend the one post-arm repair, or come to rest.

    Nothing is spawned here. Dropping the watch is enough, because the next wake's
    ``_assess_release`` re-reads the live world and drives the ordinary repair — which is
    what keeps the token budget gate, the leased dispatch and the hand-back rules single
    copies rather than a second implementation that can drift from the first.
    """
    repo, number = _split(key)
    named = (
        " (" + ", ".join(f"`{step}`" for step in sorted(failed_steps)) + ")"
        if failed_steps
        else ""
    )
    reason = (
        f"`{ref}` now conflicts with its base branch"
        if blocker is release.PrBlocker.DIRTY
        else f"CI on `{ref}` went red after auto-merge was armed{named}"
    )
    if not release.post_arm_repair_available(rec):
        release.arm_blocked(
            rec,
            f"{reason}, and this release has already spent its repair attempt — this needs "
            "a human. Fix it and `/merge` again.",
            merged=rec.get("merged"),
        )
        _block_release(
            state, port, outcome, key=key, rec=rec, ready_to_merge=ready_to_merge, heads=heads
        )
        return
    if not _cancel_armed_auto_merges(port, outcome, key=key, rec=rec):
        return  # never abandon a watch while GitHub may still merge behind it
    release.arm_post_arm_repair(rec, now=now)
    _do(
        port, outcome,
        _comment_effect(
            state, repo, number,
            f"{reason}, so it will never fire. I have cancelled the auto-merge request and "
            "will spend one repair attempt on it; the release resumes from there.",
            pull_request_ref=ref,
        ),
    )
    outcome.log.append(f"release: {key} armed {ref} broke ({blocker.value}) -> repairing")


def _read_deploy_states(
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    watch: dict,
    installed: list | None = None,
    mode: ext.Mode = ext.Mode.LIVE,
) -> dict:
    """Refresh one deploy watch's builds in place and hand them back.

    A release-gate extension is the source of deploy truth when the project enables one; a
    project without one keeps using the host adapter. Either way an unknown deploy stays
    pending, so the caller waits rather than deciding on missing evidence.
    """
    builds = watch.get("builds") or {}
    outstanding = {
        drepo: info["sha"]
        for drepo, info in builds.items()
        if info.get("status") != "ok" and info.get("sha")
    }
    gates = [item for item in (installed or []) if item.implements(ext.Hook.RELEASE_GATE)]
    if gates:
        reported, degraded = ext.release_states(
            gates,
            context={"ref": key, "builds": outstanding, "since": watch.get("since")},
            mode=mode,
        )
        for name in degraded:
            outcome.report.extension_failures += 1
            outcome.report.extension_health[name] = {
                "state": "degraded",
                "reason": "release-gate could not be read",
            }
        for drepo, reported_state in reported.items():
            info = builds.get(drepo)
            if info is None:
                continue
            info["status"] = reported_state.get("status") or "pending"
            if reported_state.get("link"):
                info["link"] = reported_state["link"]
    else:
        for drepo, deploy in (port.deploy_states(outstanding) or {}).items():
            info = builds.get(drepo)
            if info is None:
                continue
            info["status"] = deploy.status
            if deploy.link:
                info["link"] = deploy.link
    return builds


def _gate_deploy(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    now: datetime,
    installed: list | None = None,
    mode: ext.Mode = ext.Mode.LIVE,
    ready_to_merge: str | None = None,
) -> None:
    """Gate the release on the production deploys, as reported by an extension or the host.

    A release-gate extension is the source of deploy truth when the project enables one; a
    project without one keeps using the host adapter. Either way an unknown deploy stays
    pending, so the release waits rather than finishing on missing evidence.
    """
    watch = rec["awaiting_deploy"]
    builds = _read_deploy_states(
        port, outcome, key=key, watch=watch, installed=installed, mode=mode
    )
    statuses = {drepo: info.get("status") or "pending" for drepo, info in builds.items()}
    decision = release.gate_deploy(statuses, since=watch.get("since"), now=now)
    if decision is release.DeployOutcome.WAITING:
        outcome.report.deploys += 1
        return
    if decision is release.DeployOutcome.DONE:
        links = [
            f"[{drepo}]({info['link']})" for drepo, info in builds.items() if info.get("link")
        ]
        rec.pop("awaiting_deploy", None)
        release.arm_done(rec, merged=rec.get("merged") or [], deploy_links=links)
        _finish_release(state, port, outcome, key=key, rec=rec, now=now)
        return
    # Retrying here would cost another merge to the base branch of a live product, answering
    # "production is broken" by pushing more code at production. A broken deploy stops for a
    # human.
    why = (
        "**Production deploy failed.** The merge is on the base branch but the deploy did "
        "not go green; a broken production deploy needs a human."
        if decision is release.DeployOutcome.FAILED
        else "Production deploy did not reach a terminal status in time — check the deploy "
        "before closing."
    )
    release.arm_blocked(rec, why, merged=rec.get("merged"))
    # Park it, but keep looking. Stopping for a human is not the same as never reading the
    # deploy again, and the difference was costing whole cards: the deploy that failed here
    # often goes green on its own within the hour, and nothing noticed (#2069).
    release.begin_deploy_recheck(rec, builds=builds, now=now)
    _block_release(state, port, outcome, key=key, rec=rec, ready_to_merge=ready_to_merge)


def _assess_release(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    now: datetime,
    dispatch_ok: bool,
    auto_deploy_repos: frozenset[str] | set[str],
    store: StateStore | None = None,
    ready_to_merge: str | None = None,
    merge_first: frozenset[str] = frozenset(),
) -> None:
    """Drive the linked PRs towards a merge: update, repair, wait, or arm auto-merge."""
    repo, number = _split(key)
    linked = _linked(port, rec, repo, number)
    if linked is None:
        return
    open_refs = list(linked.open)
    release.note_release_set(
        rec, refs=open_refs + list(linked.merged), steps=list(linked.step)
    )
    states = port.pr_states(open_refs + list(linked.merged))
    if states is None:
        return
    if not open_refs and not linked.merged:
        release.arm_blocked(rec, _nothing_to_release(number, linked))
        _block_release(state, port, outcome, key=key, rec=rec, ready_to_merge=ready_to_merge)
        return

    heads = {ref: states[ref].head for ref in open_refs if ref in states}
    blockers = {
        ref: release.classify_pr(
            check_state=states[ref].check_state,
            merge_state=states[ref].merge_state,
            failed_steps=states[ref].failed_steps,
        )
        for ref in open_refs
        if ref in states
    }
    failed = {ref: list(states[ref].failed_steps or ()) for ref in open_refs if ref in states}

    if rec.get("review_heads"):
        for line in _confirm_branch_syncs(port, rec, heads=heads, now=now):
            outcome.log.append(f"release: {key} {line}")
        diffs, trees = _review_content(port, rec, heads)
        fresh = release.heads_unchanged(
            rec,
            open_refs=set(open_refs),
            heads=heads,
            review_diffs=diffs,
            release_trees=trees,
            rebase_moved=release.rebase_round(rec),
        )
        if fresh is None:
            outcome.log.append(f"release: {key} approval could not be verified; deferring")
            return
        if fresh is False:
            _handback(
                state, port, outcome, key=key, rec=rec,
                why="Linked PRs or their head commits changed after `/merge review`. Nothing "
                "was merged; review the new diff and approve again.",
                ready_to_merge=ready_to_merge,
            )
            return

    fix = rec.get("fix")
    if fix:
        settled = _assess_repair(
            state, port, outcome, key=key, rec=rec, fix=fix, blockers=blockers,
            heads=heads, now=now, ready_to_merge=ready_to_merge, failed=failed,
        )
        if not settled:
            return

    step = release.plan_release(blockers)
    if step is release.ReleaseStep.UPDATE_BEHIND:
        _update_behind(
            port, outcome, key=key, rec=rec, blockers=blockers, heads=heads, now=now
        )
        return
    if step is release.ReleaseStep.REPAIR:
        _spawn_repair(
            state, port, outcome, key=key, rec=rec, blockers=blockers, heads=heads,
            now=now, dispatch_ok=dispatch_ok, store=store, failed=failed,
        )
        return
    if step is release.ReleaseStep.WAIT_CHECKS:
        _wait_or_block(
            state, port, outcome, key=key, rec=rec, since=rec.get("started_at"), now=now,
            what="linked PR checks did not finish", ready_to_merge=ready_to_merge,
            heads=heads,
        )
        return
    if rec.get("auto_release") and not release.tests_pass_current(rec, heads=heads):
        # A bits-driven release said, in a comment on the card, that the suite ran green on
        # this exact commit. Between arming and here the head can move — `UPDATE_BEHIND` above
        # does it on purpose — and nothing else in the progression looks at the tests bit, so
        # without this the release would merge a tree nobody tested while its own announcement
        # claimed otherwise. Handing back is not a dead end: the card rests, the bits are
        # re-read next wake, and a red tests bit is exactly what arms the rebase-and-rerun turn
        # that makes the claim true again (#2101).
        _handback(
            state, port, outcome, key=key, rec=rec,
            why="The head moved after this release was armed, so the green suite run no "
            "longer covers what would merge. Nothing was merged — the rebase-and-rerun turn "
            "runs next, and the release resumes once the suite is green on the new head.",
            ready_to_merge=ready_to_merge,
        )
        outcome.log.append(f"release: {key} tests bit stale at merge time -> handback")
        return
    _arm_auto_merge(
        state, port, outcome, key=key, rec=rec,
        # A repository with no tests holds what the others are built on — the reviewer's own
        # instructions, say — so it merges first (#2729). Arming merges a pull request with no
        # required checks at once, so the arming order is the merge order.
        open_refs=sorted(open_refs, key=lambda ref: _split(ref)[0] not in merge_first),
        linked=linked, states=states, now=now, auto_deploy_repos=auto_deploy_repos,
        ready_to_merge=ready_to_merge, merge_first=merge_first,
    )


#: How many changed paths the review-only merge note lists before it summarises the rest.
_FILES_NOTE_LIMIT = 30


def _review_only_files_note(port: EnginePort, refs: list[str]) -> str:
    """Name the files each review-only pull request changes, for the merge comment (#2729).

    Such a repository has no tests and, for the review-chat repository, holds the reviewer's own
    instructions, which go live on its next run with no deploy in between. So the card says
    exactly which files went in, and a person reading it can see a change to the reviewer's
    instructions without opening the pull request.
    """
    if not refs:
        return ""
    lines = []
    for ref in refs:
        files = port.pr_files(ref)
        if files is None:
            lines.append(f"- `{ref}`: its file list could not be read; see the pull request.")
            continue
        shown = ", ".join(f"`{path}`" for path in files[:_FILES_NOTE_LIMIT]) or "no files"
        more = len(files) - _FILES_NOTE_LIMIT
        lines.append(f"- `{ref}`: {shown}" + (f", and {more} more" if more > 0 else ""))
    return (
        "\n\nThese have no tests, so their review is their only check, and they go live as "
        "soon as they merge. Files changed:\n" + "\n".join(lines)
    )


def _nothing_to_release(number: int, linked: LinkedPRs) -> str:
    """Why this card has no release target, told apart from having none at all (#1445).

    "No linked PR found" sends whoever reads it looking for a pull request the worker never
    opened. When the pull request is right there but its author marked it draft or opted it out
    of release, saying so is the difference between a five-minute fix and a hunt.
    """
    if not linked.withheld:
        return f"No linked PR found to release for #{number}."
    held = ", ".join(f"`{ref}`" for ref in linked.withheld)
    return (
        f"Nothing to release for #{number}: {held} is linked but is a draft, or opts out of "
        "release. Mark it ready for review, or remove the opt-out marker from its body."
    )


def _wait_or_block(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    since: str | None,
    now: datetime,
    what: str,
    ready_to_merge: str | None = None,
    heads: dict[str, str | None] | None = None,
) -> None:
    """Wait for the next wake unless the wait has run past the limit, which blocks."""
    if release.is_timed_out(since, now):
        release.arm_blocked(
            rec, f"{what} within the time limit — check CI before closing.",
            merged=rec.get("merged"),
        )
        _block_release(
            state, port, outcome, key=key, rec=rec, ready_to_merge=ready_to_merge, heads=heads
        )
        return
    outcome.log.append(f"release: {key} waiting ({what})")


def _update_behind(
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    blockers: dict,
    heads: dict[str, str | None],
    now: datetime,
) -> None:
    """Bring behind branches up to date; their CI verdict is untrustworthy until then."""
    for ref, blocker in blockers.items():
        if blocker is not release.PrBlocker.BEHIND:
            continue
        if not _do(port, outcome, Effect(EffectKind.UPDATE_BRANCH, *_split(ref))):
            # An update that fails is a conflict: the repair worker owns it next wake.
            blockers[ref] = release.PrBlocker.DIRTY
            continue
        # The provenance names a commit rather than a reference: a later push moves the head
        # again and stops matching (#2122), and a merge not visible yet is confirmed later.
        note = _note_branch_sync(port, rec=rec, ref=ref, before=heads.get(ref), now=now)
        outcome.log.append(f"release: {key} updated behind branch {ref}{note}")
    rec["started_at"] = now.astimezone(UTC).isoformat()


def _spawn_repair(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    blockers: dict,
    heads: dict,
    now: datetime,
    dispatch_ok: bool,
    store: StateStore | None,
    failed: dict[str, list[str]] | None = None,
) -> None:
    """Spend the one repair attempt on the failing PRs, under the same durable delivery."""
    del dispatch_ok  # retained in the internal call shape; usage does not gate repairs (#1497)
    keep = release.fix_keeps_merge(list(blockers.values()))
    hands_back = release.fix_may_hand_back(keep=keep)
    # Snapshot the effective diffs too, so a repair that only merges the base can be told
    # apart from one that changed code — read only when a hand-back is actually possible.
    diffs = port.review_diffs(sorted(blockers)) if hands_back and blockers else {}
    release.arm_fix(rec, heads=heads, keep=keep, now=now, diffs=diffs)
    # The repair spawn uses the same leased at-most-once dispatch as any other worker
    # (the reference engine's merge-fix was the one spawn without a receipt — #106 gap 3).
    _spawn(state, port, outcome, key=key, rec=rec, action="merge-fix", now=now, store=store)
    # The lease the repair runs under, so its own ending can be told apart from a later one's.
    if isinstance(rec.get("fix"), dict):
        rec["fix"]["generation"] = str(rec.get("generation") or "")
    _do(
        port, outcome,
        _comment_effect(
            state, *_split(key),
            f"A linked PR needs fixing before the release: {_failing_checks(blockers, failed)} "
            "failing. Spawning **one** worker to fix and push; I will re-check when it reports. "
            "If nothing in the code can fix it, the worker should push nothing and say so."
            + (
                " If the repair changes code, it comes back for your review once green."
                if hands_back
                else ""
            ),
            pull_request_ref=sorted(blockers)[0],
        ),
    )
    outcome.log.append(f"release: {key} repair dispatched (keep={keep})")


def _fix_diffs(
    port: EnginePort, fix: dict, heads: dict[str, str | None]
) -> dict[str, str | None]:
    """Current effective diffs for the PRs whose head moved during the repair.

    Only moved heads are read: an unmoved head is not a push, so its diff cannot have
    changed and asking GitHub for it would spend a request per waiting wake for nothing.
    A ref with no armed snapshot is skipped for the same reason — it falls back to the
    head fence either way.
    """
    snapshot = fix.get("diffs")
    if not isinstance(snapshot, dict):
        return {}
    original = fix.get("heads") or {}
    moved = [
        ref for ref, digest in snapshot.items()
        if digest and heads.get(ref) != original.get(ref)
    ]
    return port.review_diffs(moved) if moved else {}


def _repair_ended(port: EnginePort, *, key: str, rec: dict, fix: dict) -> bool:
    """Has the repair worker's own turn ended, by the Runtime's account (#2771)?

    Nothing else in a release says so: a merge-mode card is release-gated, so the ordinary
    reportless-ending path never looks at it, and the dispatch record goes at delivery, not at
    the end. Only the lease the repair was spawned under counts, so an older fix record (no
    generation) or a later Run never speaks for it. ``gone`` proves only that nothing runs (a
    Runtime restart), and a turn waiting on its own queued job is not over, so neither ends it.
    """
    generation = str(rec.get("generation") or "")
    if not generation or str(fix.get("generation") or "") != generation:
        return False
    if rec.get("dispatch") or rec.get("stopping"):
        return False
    repo, number = _split(key)
    jobs = port.verification_jobs()
    observation = port.turn_ended(repo, number, generation)
    if not isinstance(observation, dict):
        return False
    observed = str(observation.get("observed") or revive.ENDED)
    if observed == revive.GONE:
        return False
    if observed == revive.ENDED and not observation.get("refused"):
        awaited, _ = _awaited_job(port, jobs, repo=repo, number=number, generation=generation)
        if awaited:
            return False
    return True


def _failing_checks(blockers: dict, failed: dict[str, list[str]] | None) -> str:
    """The red checks by name, for a comment, or what is red when no name was read."""
    named = [
        f"`{step}` on `{ref}`"
        for ref, blocker in sorted(blockers.items())
        if release.is_repairable(blocker)
        for step in (failed or {}).get(ref) or ()
    ]
    if named:
        return ", ".join(named) + (" is" if len(named) == 1 else " are")
    broken = [
        f"`{ref}`" for ref, blocker in sorted(blockers.items()) if release.is_repairable(blocker)
    ]
    return "checks or conflicts on " + ", ".join(broken) + " are"


def _assess_repair(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    fix: dict,
    blockers: dict,
    heads: dict,
    now: datetime,
    ready_to_merge: str | None = None,
    failed: dict[str, list[str]] | None = None,
) -> bool:
    """Judge the in-flight repair. True when it is settled and the release may continue."""
    decision = release.assess_fix(
        fix,
        blockers=blockers,
        heads=heads,
        diffs=_fix_diffs(port, fix, heads),
        worker_ended=_repair_ended(port, key=key, rec=rec, fix=fix),
    )
    if decision is release.FixOutcome.UPDATE_BEHIND:
        _update_behind(
            port, outcome, key=key, rec=rec, blockers=blockers, heads=heads, now=now
        )
        _wait_or_block(
            state, port, outcome, key=key, rec=rec, since=fix.get("at"), now=now,
            what="behind branch(es) did not update after the repair",
            ready_to_merge=ready_to_merge, heads=heads,
        )
        return False
    if decision is release.FixOutcome.WAITING:
        _wait_or_block(
            state, port, outcome, key=key, rec=rec, since=fix.get("at"), now=now,
            what="the repair worker did not finish", ready_to_merge=ready_to_merge,
            heads=heads,
        )
        return False
    if decision is release.FixOutcome.UNCHANGED:
        release.arm_blocked(
            rec,
            "The repair worker finished without pushing a change, and "
            f"{_failing_checks(blockers, failed)} still failing. A check no code change can "
            "fix — a deploy service that wants outside authorization, say — needs a person. "
            "Fix it and `/merge` again.",
            merged=rec.get("merged"),
        )
        _block_release(
            state, port, outcome, key=key, rec=rec, ready_to_merge=ready_to_merge, heads=heads
        )
        outcome.log.append(f"release: {key} repair ended with nothing pushed -> blocked")
        return False
    if decision is release.FixOutcome.EXHAUSTED:
        release.arm_blocked(
            rec,
            "The repair worker ran but CI or conflicts are still unresolved — this needs a "
            "human. Fix it and `/merge` again.",
            merged=rec.get("merged"),
        )
        _block_release(
            state, port, outcome, key=key, rec=rec, ready_to_merge=ready_to_merge, heads=heads
        )
        return False
    if decision is release.FixOutcome.HANDBACK:
        _handback(
            state, port, outcome, key=key, rec=rec,
            why="CI is green now — but the repair **changed code**, so it comes back for your "
            "review. Look it over and `/merge` again to ship it.",
            ready_to_merge=ready_to_merge,
        )
        return False
    rec.pop("fix", None)
    outcome.log.append(f"release: {key} repair cleared -> keeping the release")
    return True


def _arm_auto_merge(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    open_refs: list[str],
    linked: object,
    states: dict,
    now: datetime,
    auto_deploy_repos: frozenset[str] | set[str],
    ready_to_merge: str | None = None,
    merge_first: frozenset[str] = frozenset(),
) -> None:
    """Every linked PR is ready: arm native gated auto-merge and move to watching."""
    armed: list[str] = []
    for ref in open_refs:
        result = port.arm_auto_merge(ref)
        if result in {ArmOutcome.ARMED, ArmOutcome.ALREADY_MERGED}:
            outcome.log.append(f"release: {key} armed auto-merge on {ref}")
            if result is ArmOutcome.ARMED:
                armed.append(ref)
            continue
        # Both failures name the pull request, because the commonest cause of an arm that
        # keeps failing is the release aiming at the wrong one — and a message that only says
        # "could not arm" leaves nowhere to go but repeating the command that just failed.
        aim = (
            f" If `{ref}` is not what this card should release, name the right one: "
            "`/merge <repo>#<number>`."
        )
        why = (
            f"Native auto-merge is disabled for `{_split(ref)[0]}` — enable it in the "
            f"repository settings, or merge `{ref}` manually."
            if result is ArmOutcome.UNAVAILABLE
            else f"Could not arm auto-merge on `{ref}`.{aim}"
        )
        if armed:
            # Make every accepted request visible before abandoning the release, so the block
            # path can disable them even when a later PR in the same batch failed to arm.
            release.begin_merge_watch(rec, armed=armed, premerged={}, now=now)
        release.arm_blocked(rec, why, merged=rec.get("merged"))
        _block_release(
            state, port, outcome, key=key, rec=rec, ready_to_merge=ready_to_merge,
            heads={ref: states[ref].head for ref in open_refs if ref in states},
        )
        return
    # The approval stays durable through every check above and is spent only once every
    # auto-merge request has been accepted.
    release.consume_approval(rec)

    merged_refs = list(getattr(linked, "merged", []) or [])
    premerged = {}
    for ref in merged_refs:
        pr_repo, _ = _split(ref)
        pr = states.get(ref)
        if pr_repo in auto_deploy_repos and pr is not None and pr.merge_commit:
            premerged[pr_repo] = {"sha": pr.merge_commit, "status": "pending"}
    if open_refs:
        release.begin_merge_watch(rec, armed=open_refs, premerged=premerged, now=now)
        _do(
            port, outcome,
            _comment_effect(
                state, *_split(key),
                "**Auto-merge armed** on " + ", ".join(f"`{ref}`" for ref in open_refs)
                + ". GitHub merges each once its required checks pass."
                + _review_only_files_note(
                    port, [ref for ref in open_refs if _split(ref)[0] in merge_first]
                ),
                pull_request_ref=sorted(open_refs)[0],
            ),
        )
        return
    if premerged:
        # What the finish names, and what tells it whether this was a step (#2730): the gate
        # finishes from the record, and nothing else would have written these merges to it.
        rec["merged"] = merged_refs
        release.begin_deploy_watch(
            rec, builds=premerged, now=now, merged_at=_earliest_merge(states, merged_refs)
        )
        outcome.log.append(f"release: {key} already merged -> awaiting deploy")
        return
    release.arm_done(rec, merged=merged_refs)
    _finish_release(state, port, outcome, key=key, rec=rec, now=now)


def _finish_release(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, key: str, rec: dict,
    now: datetime,
) -> None:
    """Publish the finished release step by step, then release the card's resources."""
    repo, number = _split(key)
    intent = release.arm_done(rec, merged=rec.get("merged") or [])
    if release.is_step_finish(intent):
        _continue_after_step(state, port, outcome, key=key, rec=rec, intent=intent, now=now)
        return
    if release.needs_step(intent, "status"):
        if not _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, "Done")):
            return
        release.mark_step(intent, "status")
    if release.needs_step(intent, "comment"):
        body = "**Done.** Merged " + ", ".join(f"`{m}`" for m in intent.get("merged") or [])
        links = intent.get("deploy_links") or []
        if links:
            body += ". Production deploy(s): " + ", ".join(links)
        body += "."
        if intent.get("recovered"):
            # This card's own history says the deploy failed. Closing it silently would read
            # as though that had never happened, and leave the failure as the last word anyone
            # finds on the issue (#2069).
            body += (
                " The production deploy this card was parked on has since gone green, so the"
                " release finished on its own; no new merge was made."
            )
        _do(port, outcome, _comment_effect(state, repo, number, body))
        release.mark_step(intent, "comment")
    if release.needs_step(intent, "closed"):
        if not _do(port, outcome, Effect(EffectKind.CLOSE_ISSUE, repo, number)):
            return
        release.mark_step(intent, "closed")
    _resolve_release_control(state, port, outcome, rec, status="done")
    _finish_processing(state, port, outcome, rec)
    _cancel_pending(state, port, outcome, rec)
    _discard_receipts(port, outcome, key)
    paths = reconcile.begin_cleanup(rec)
    failed = _cleanup_worktrees(
        state, port, outcome, repo=repo, number=number, paths=list(paths)
    ) if paths else []
    if not reconcile.record_cleanup(rec, failed=failed):
        outcome.log.append(f"release: {key} done, cleanup retry pending")
        return
    _forget(state, key, released=[str(ref) for ref in intent.get("merged") or []])
    outcome.report.releases += 1
    outcome.log.append(f"release: {key} done")


_STEP_CID_PREFIX = "engine:release-step:"


def _continue_after_step(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, key: str, rec: dict, intent: dict,
    now: datetime,
) -> None:
    """A release of step pull requests landed: keep the card open and hand it back (#2730).

    The same durable-intent shape as the Done path, so a wake that dies half way resumes rather
    than repeats: say so on the card, let go of the merged branch's worktrees so the next part
    starts from the current base, and queue the worker's next turn. The card is not forgotten
    and the issue is not touched — the record of what merged is what keeps the next release
    from mistaking this one's pull requests for its own.
    """
    repo, number = _split(key)
    merged = [str(ref) for ref in intent.get("merged") or []]
    if release.needs_step(intent, "comment"):
        body = "**Step merged.** Merged " + ", ".join(f"`{m}`" for m in merged)
        links = intent.get("deploy_links") or []
        if links:
            body += ". Production deploy(s): " + ", ".join(links)
        body += (
            ". It was marked as one step of this card, so the issue stays open and the card goes "
            "back to its worker for the next part."
        )
        if not _do(port, outcome, _comment_effect(state, repo, number, body)):
            return  # the next wake posts it; nothing past this step has happened yet
        release.mark_step(intent, "comment")
    if release.needs_step(intent, "worktrees"):
        paths = reconcile.worktrees(rec)
        failed = _cleanup_worktrees(
            state, port, outcome, repo=repo, number=number, paths=list(paths)
        ) if paths else []
        if failed:
            outcome.log.append(f"release: {key} step merged, worktree release retry pending")
            return
        rec.pop("worktrees", None)
        # The next part is new work on a new branch, cut from the base the step just landed
        # on. Reusing the merged branch would carry its history (and on a squash-merge
        # project, its conflicts) into the next pull request.
        rec["branch"] = f"agent/issue-{number}-step{len(release.steps_merged(rec)) + 2}"
        release.mark_step(intent, "worktrees")
    if release.needs_step(intent, "status"):
        # Back to review until the worker's turn starts and takes it to In progress: a card
        # held by the worker cap must not read as being worked on.
        if not _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, release.IN_REVIEW)):
            return
        release.mark_step(intent, "status")
    if release.needs_step(intent, "queued"):
        snapshot = outcome.live_snapshots.get(key)
        _queue_engine_instruction(
            state, port, outcome, ref=key,
            cid=f"{_STEP_CID_PREFIX}{key}:{','.join(sorted(merged))}",
            instruction=_step_instruction(merged, branch=str(rec.get("branch") or "")),
            url=getattr(snapshot, "url", "") or "",
            now=now,
            author="engine:release-step",
        )
        release.mark_step(intent, "queued")
    _resolve_release_control(state, port, outcome, rec, status="done")
    _finish_processing(state, port, outcome, rec)
    release.end_step_release(rec, merged=merged)
    outcome.report.releases += 1
    outcome.log.append(f"release: {key} step merged -> continuing")


def _step_instruction(merged: list[str], *, branch: str) -> str:
    """What the worker is told when a step of its card has merged (#2730)."""
    named = ", ".join(f"`{ref}`" for ref in merged)
    return (
        f"Your step pull request {named} has merged (and its production deploy is green, "
        "where this project watches one). The issue is still open: carry on with the next "
        "part of this card.\n\n"
        f"Start the next part on a fresh branch from the current base, `{branch}`. The poller "
        "released the merged branch's worktree, so a new one is cut for you; if your checkout "
        "is still on the merged branch, fetch the base and create the new branch from it "
        "before you commit anything.\n\n"
        "Finish as usual. If the work still needs another merge before it can go on, open "
        "another step pull request; otherwise open a normal completion pull request that "
        "closes this card."
    )


def _block_release(
    state: dict, port: EnginePort, outcome: CycleOutcome, *, key: str, rec: dict,
    ready_to_merge: str | None = None,
    heads: dict[str, str | None] | None = None,
) -> None:
    """Stop a release visibly: reopen if needed, park it, and say why — ownership stays.

    Where it parks is *Ready to merge* for a project that made that column, and *Blocked*
    otherwise (#1099). Blocked was never quite true of a release that failed on red CI or a
    timeout: the work is approved and the way forward is to try the merge again, which the
    engine's own repair-exhausted wording already told people to do — while ``admit`` refused
    ``/merge`` from Blocked, so the advice named an action the next wake would reject.
    """
    repo, number = _split(key)
    intent = rec.get("blocked_pending") or release.arm_blocked(rec, "Release blocked")
    if heads:
        # Kept with the intent, so a park finished on a later wake still records them.
        intent["heads"] = {ref: str(sha) for ref, sha in heads.items() if sha}
    if not _cancel_armed_auto_merges(port, outcome, key=key, rec=rec):
        return
    if release.needs_step(intent, "reopened"):
        if not _do(port, outcome, Effect(EffectKind.REOPEN_ISSUE, repo, number)):
            return
        release.mark_step(intent, "reopened")
    parks_in = ready_to_merge or config.Role.BLOCKED.value
    if release.needs_step(intent, "status"):
        if not _do(port, outcome, Effect(EffectKind.SET_STATUS, repo, number, parks_in)):
            return
        release.mark_step(intent, "status")
    if release.needs_step(intent, "comment"):
        _do(port, outcome, _comment_effect(state, repo, number, intent.get("why") or ""))
        release.mark_step(intent, "comment")
    merged = intent.get("merged") or []
    _resolve_release_control(state, port, outcome, rec, status="rejected")
    for stale in (
        "awaiting_merge", "awaiting_deploy", "mode", "fix", "review_heads", "review_diffs",
        "review_trees", "release_only",
    ):
        rec.pop(stale, None)
    # The intent goes once the park lands, so its reason is kept with the column and lease it
    # parked under: a reader shows it only while that exact park is current (#2235).
    rec["release_blocked"] = {
        "why": str(intent.get("why") or ""),
        "status": parks_in,
        "generation": str(rec.get("generation") or ""),
    }
    parked_on = intent.get("heads") if isinstance(intent.get("heads"), dict) else {}
    if parked_on:
        # The heads it parked on, so a later push ends the park (#2771).
        rec["release_blocked"]["heads"] = parked_on
    release.clear_finalization(rec, "blocked_pending")
    rec["status"] = parks_in
    if merged:
        rec["merged"] = merged
        # These merges have had their release. Parked on a red deploy, a step must wait for
        # the deploy recheck or a person, not be released again as a hand merge (#2730).
        release.note_step_release_started(rec, merged)
    else:
        rec.pop("merged", None)
        release.end_deploy_recheck(rec)  # nothing merged, so there is no deploy to wait for
    outcome.log.append(f"release: {key} blocked")


def _recheck_deploys(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    installed: list | None = None,
    mode: ext.Mode = ext.Mode.LIVE,
    ready_to_merge: str | None = None,
    auto_deploy_repos: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Finish a release that parked on a red production deploy, once the deploy came good.

    This is the other half of the park (#2069). ``_gate_deploy`` stops for a human when the
    deploy goes bad, and it is right to: the merge is already on the base branch, so the only
    retry available would be pushing more code at a broken production. But six cards proved
    that the park was being read as "never look again". Every one of them had merged, gone
    live and been healthy for days by the time anyone noticed, and each sat open the whole
    time because the single red reading was the last reading ever taken.

    So the card stays parked, with its reason, and the deploy is simply re-read. Nothing is
    merged, pushed or re-run; the only thing that can end the park is the same gate saying the
    same word — ``ok`` — that would have finished the release on the first pass. Anything else
    leaves it exactly where it is, silently, so a genuinely broken deploy still reads as a
    broken deploy and still waits for the human it stopped for.
    """
    parks = {config.Role.BLOCKED.value}
    if ready_to_merge:
        parks.add(ready_to_merge)
    for key in list(_cards(state)):
        rec = _cards(state).get(key)
        if rec is None or rec.get("stopping") or rec.get("mode"):
            continue
        if rec.get("status") not in parks or not rec.get("merged"):
            continue
        watch = release.deploy_recheck(rec)
        if watch is None:
            # Seeding is a once-per-card repair for parks that predate this, not a standing
            # second chance. A card that has been offered one and did not take it is done
            # being asked; a card that ended its own re-read is not handed a fresh one.
            if rec.get("deploy_recheck_seeded"):
                continue
            watch = _seed_deploy_recheck(
                port, outcome, key=key, rec=rec, now=now,
                auto_deploy_repos=auto_deploy_repos,
            )
            if watch is None:
                continue
        if release.deploy_recheck_expired(rec, now):
            release.end_deploy_recheck(rec)
            outcome.log.append(f"release: {key} deploy recheck gave up")
            continue
        builds = _read_deploy_states(
            port, outcome, key=key, watch=watch, installed=installed, mode=mode
        )
        statuses = {drepo: info.get("status") or "pending" for drepo, info in builds.items()}
        if not statuses or not all(status == "ok" for status in statuses.values()):
            continue
        links = [
            f"[{drepo}]({info['link']})" for drepo, info in builds.items() if info.get("link")
        ]
        release.end_deploy_recheck(rec)
        release.arm_done(
            rec, merged=rec.get("merged") or [], deploy_links=links, recovered=True
        )
        outcome.log.append(f"release: {key} deploy recovered -> finishing")
        _finish_release(state, port, outcome, key=key, rec=rec, now=now)


def _seed_deploy_recheck(
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    key: str,
    rec: dict,
    now: datetime,
    auto_deploy_repos: frozenset[str] | set[str],
) -> dict | None:
    """Give a park made before #2069 a deploy re-read of its own, once and only once.

    These records dropped their watch when they parked, so the commit that shipped is not on
    the record any more. It is not *gone*, though — it is on the merged pull requests, which
    is where the forward path reads it from in the first place. So this rebuilds the same
    watch the release would have carried had it never parked: repository to merge commit, and
    from there the one ordinary deploy gate answers the one ordinary question. There is no
    second protocol and no time-based stand-in for a commit; a park whose commit cannot be
    read is simply not re-read.

    The precondition is that every pull request the release was meant to carry is *proven
    merged* — not merely that none is still open. An absence is not a proof: a pull request
    closed without merging leaves nothing open either, and reading that as "everything
    shipped" would close a card half of whose release never landed. So an open one and a
    closed-unmerged one both refuse, and only a linked set that is entirely merged seeds.

    Everything after that guard is derived from the *current* proven set, never from the
    ``merged`` snapshot the card parked with. The two can differ: a linked pull request can
    merge after the park and before the first re-read, and in another code repository. Seeding
    from the snapshot would then pass every current-link guard and still ask for deploy
    evidence only about the repositories that had merged back then — closing the card with no
    evidence at all for the one that merged since. So the stored set is refreshed here, which
    also keeps the closing comment naming what actually shipped.
    """
    verdict, proven = _release_complete(port, rec, key)
    if verdict is _Complete.NOT_YET:
        # Unprovable *now* is not unprovable. Leaving the one-shot marker alone is the whole
        # point: an open pull request on this wake may be merged by the next one, and consuming
        # the card's only re-read on a "no" that was really a "not yet" suppresses it forever.
        outcome.log.append(f"release: {key} deploy recheck not provable yet")
        return None
    if verdict is _Complete.NEVER:
        rec["deploy_recheck_seeded"] = True  # a decisive answer spends the one re-read
        outcome.log.append(f"release: {key} deploy recheck refused, release set incomplete")
        return None
    states = port.pr_states(proven)
    if states is None:
        outcome.log.append(f"release: {key} deploy recheck not provable yet")
        return None  # a failed read is not an answer, so it must not spend the re-read either
    builds: dict[str, dict] = {}
    for ref in proven:
        pr_repo, _ = _split(ref)
        if pr_repo not in auto_deploy_repos:
            continue
        pr = states.get(ref)
        if pr is None or not pr.merge_commit:
            # The repository deploys and the commit is unreadable right now. Saying nothing is
            # the honest answer, and it stays askable: the next wake may read it.
            outcome.log.append(f"release: {key} deploy recheck has no merge commit for {ref}")
            return None
        builds[pr_repo] = {"sha": pr.merge_commit, "status": "pending"}
    rec["deploy_recheck_seeded"] = True  # a decisive answer spends the one re-read
    if not builds:
        # Nothing in this release set deploys under the current configuration, so there is no
        # deploy evidence to be had and the park's reason cannot be shown to have gone away.
        outcome.log.append(f"release: {key} deploy recheck refused, no deploying repository")
        return None
    rec["merged"] = proven
    release.begin_deploy_recheck(rec, builds=builds, now=now)
    outcome.log.append(f"release: {key} deploy recheck seeded")
    return release.deploy_recheck(rec)


class _Complete(Enum):
    """Is this parked card's release set provably, entirely merged right now? (#2069)"""

    NOT_YET = "not-yet"   # cannot be established this wake; ask again, spend nothing
    NEVER = "never"       # a linked pull request was abandoned: it can never be complete
    NOW = "now"           # every linked pull request is merged, and these are the refs


def _release_complete(port: EnginePort, rec: dict, key: str) -> tuple[_Complete, list[str]]:
    """The one eligibility rule for re-reading a historical park, and its evidence.

    Three review rounds turned this into separate branches that each meant something subtly
    different, and the bug was in the difference: "still open" and "abandoned" were both
    treated as final, so a card whose pull request merged an hour after the park had already
    spent its only re-read on a wake that answered too early.

    They are not the same answer. An open pull request is a *pending* one — it may merge
    before the next wake, so nothing is decided and nothing is spent. A pull request closed
    without merging is decided: that release set can never be complete, and the card stays
    parked for a human. Only a linked set that is entirely merged is complete, and then the
    refs returned here are the sole source for both ``rec["merged"]`` and the deploy-watch
    repositories — never the snapshot the card parked with, which can name fewer repositories
    than actually shipped.
    """
    repo, number = _split(key)
    linked = _linked(port, rec, repo, number)
    if linked is None:
        return _Complete.NOT_YET, []  # the read failed; that is not evidence of anything
    if linked.closed_unmerged:
        return _Complete.NEVER, []
    if linked.open:
        return _Complete.NOT_YET, []
    proven = [str(ref) for ref in linked.merged if ref and "#" in str(ref)]
    if not proven:
        return _Complete.NOT_YET, []  # nothing merged to prove yet, so nothing to decide
    return _Complete.NOW, proven


# --- phase 6: the extension hooks (#179) ---


def _observe(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    installed: list,
    *,
    now: datetime,
    mode: ext.Mode,
    store: StateStore,
    daily_cap: int | None = None,
    issue_repos: frozenset[str] = frozenset(),
    spec_only_label: str = "",
) -> None:
    """Let each observe hook propose work, and queue what it asked for as ordinary commands.

    A proposal is not a privilege: it enters the same durable ledger with the same exact-once
    identity and FIFO order as a human's instruction, and from there it is subject to
    ownership, the worker cap, leases and every other rule. The extension's stable key is what
    makes it exact-once — an extension that keeps proposing the same work never queues twice.

    ``daily_cap`` is the project's ceiling on proposals honored in one UTC day (#371). It is
    enforced here rather than inside the extension because an extension policing its own
    budget is trusting the thing being bounded. What it refuses is not lost: a proposal an
    extension still wants is offered again on the next wake, under the next day's budget.
    """
    if mode is ext.Mode.LIVE:
        drain_staged_proposals(
            state, port, outcome, now=now, store=store, daily_cap=daily_cap,
            issue_repos=issue_repos, spec_only_label=spec_only_label,
        )
    for extension in installed:
        if not extension.implements(ext.Hook.OBSERVE):
            continue
        response = ext.invoke(
            extension,
            ext.Hook.OBSERVE,
            context=_observe_context(
                state,
                extension=extension.name,
                quiet=bool(outcome.quiet),
                works_router=outcome.works_router,
            ),
            mode=mode,
        )
        _note_extension(outcome, extension.name, response)
        if mode is ext.Mode.LIVE and response.ok and response.memory is not None:
            # Kept only from a live wake that answered: a dry run must leave no trace, and a
            # skipped extension keeps what it had rather than forgetting it.
            state.setdefault(_EXTENSION_MEMORY, {})[extension.name] = response.memory
        honor_proposals(
            state, port, outcome, extension.name, ext.honored(response, mode),
            now=now, store=store, daily_cap=daily_cap, issue_repos=issue_repos,
            spec_only_label=spec_only_label,
        )


def _observe_context(
    state: dict, *, extension: str = "", quiet: bool = False, works_router: bool = False
) -> dict:
    """What an observe hook may see: the cards, its own settled keys and the quiet fact.

    ``quiet`` is the engine's own decision for this wake, not the host's raw hint: the hint
    says the streams look silent, and the engine still forces a full wake once
    ``webhook_skip_s`` has elapsed. An extension reading the hint would skip exactly the wake
    that exists to bound trusting an at-most-once channel, so it is told the decision instead.

    It matters because an extension's GitHub reads are its own — a different token, outside the
    engine's port — so the quiet skip could never reach them. `chatgpt_nudge` listing open pull
    requests once per repository per wake was a floor of ~920 calls an hour that no amount of
    quiet could lower, on the same credential the engine was carefully saving.

    Never leases (#1709).

    ``settled`` is every proposal key this extension has already had decided — opened, in the
    control ledger, or staged in the manual inbox — with the ``ext:<name>:`` namespace stripped,
    because a key only means anything inside the extension that minted it. An extension that
    bounds how much it proposes per wake needs this: without it, a bound spent on keys the
    engine will only drop again leaves genuinely new work unproposed for as long as the
    extension's source keeps reporting the old ones, which for Sentry is a full day.
    """
    return {
        "cards": [
            {
                "ref": key,
                "status": rec.get("status"),
                "mode": rec.get("mode"),
                "queued": len(_ids(rec, "pending")),
                # How many rounds a worker has delivered on this card (#1840). A round that
                # delivers by publishing evidence rather than by pushing a commit leaves the
                # pull request head exactly where it was, so an extension watching heads has
                # nothing to see. This is the edge it cannot otherwise find.
                "reports": int(rec.get("worker_rounds") or 0),
                # What a worker still holding the card has offered for review (#2350). Present
                # only where the project reviews beside verification, and only while a worker
                # holds the card, so an extension reads its presence as "review only these".
                **(
                    {"review_ready": ready_review.observe_row(rec)}
                    if ready_review.enabled(state) and rec.get("status") == _IN_PROGRESS
                    else {}
                ),
                # A person paused the card (#2548): an extension asks for nothing on it.
                **({"paused": True} if rec.get("paused") else {}),
            }
            for key, rec in _cards(state).items()
        ],
        "settled": sorted(_settled_keys(state, extension)) if extension else [],
        # What this extension's last live observe asked to be handed back (#2691).
        "memory": extension_memory(state, extension),
        # True when this wake trusted the webhook streams and asked GitHub nothing. An
        # extension that reads GitHub should do the same, and must treat it as "not read"
        # rather than "nothing there" — the distinction its own state already depends on.
        "quiet": bool(quiet),
        # The project switched its Works to the deck (``works_router``, #2895 step 5, ADR 0008):
        # an issue no card here holds is a Work the deck runs, so an extension that acts on pull
        # requests leaves those alone. Cards held at the switch are handed to the deck.
        "works_router": bool(works_router),
    }


def extension_memory(state: dict, extension: str) -> dict:
    """The memory this extension last left with the engine, or an empty one."""
    memory = (state.get(_EXTENSION_MEMORY) or {}).get(extension) if extension else None
    return memory if isinstance(memory, dict) else {}


def _settled_keys(state: dict, extension: str) -> set[str]:
    """The bare proposal keys this extension would be refused for re-proposing."""
    prefix = f"ext:{extension}:"
    settled = {
        cid[len(prefix):]
        for cid in (*state.get(_OPENED, {}), *queue.controls(state))
        if str(cid).startswith(prefix)
    }
    for item in state.get(_PROPOSAL_INBOX, []):
        if not isinstance(item, dict) or str(item.get("extension") or "") != extension:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        key = str(payload.get("key") or "")
        if key:
            settled.add(key)
    return settled


_PROPOSALS = "proposals"
# Per extension, the object its last live observe answered as ``memory`` (#2691).
_EXTENSION_MEMORY = "extension_memory"
_PROPOSAL_INBOX = "proposal_inbox"
# Keys whose card this engine has already opened, and the number it opened (#393). Separate from
# the control ledger on purpose: `queue.is_seen` answers "is there a control for this key", and a
# control is a working record that may be pruned. Re-proposing a pruned key would re-queue an
# instruction, which is harmless, or open a second GitHub issue, which is not. This ledger only
# ever grows, because "we already filed this" never stops being true.
_OPENED = "opened"
# Keys whose creation was begun but not finished: the durable intent written before the API call,
# so a crash in between is recoverable by looking rather than by guessing.
_OPENING = "opening"


def _open_marker(cid: str) -> str:
    """The marker written into an opened issue's body, so it can be recognized again."""
    return f"<!-- poller-opened: {cid} -->"


def _proposal_budget(state: dict, now: datetime) -> dict:
    """This UTC day's proposal tally, rolled over when the day turns.

    Durable, so the ceiling survives a restart: a cap that forgets what it already spent every
    time the poller is redeployed is not a budget guard at all.
    """
    today = now.astimezone(UTC).date().isoformat()
    record = state.get(_PROPOSALS)
    if not isinstance(record, dict) or record.get("date") != today:
        record = {"date": today, "honored": 0}
        state[_PROPOSALS] = record
    return record


def stage_proposals(state: dict, name: str, requests: tuple[ext.Request, ...]) -> int:
    """Put operator-requested proposals in the durable inbox for the next wake."""
    inbox = state.setdefault(_PROPOSAL_INBOX, [])
    pending = {
        (str(item.get("extension") or ""), str((item.get("payload") or {}).get("key") or ""))
        for item in inbox if isinstance(item, dict)
    }
    added = 0
    for request in requests:
        if request.kind not in (ext.RequestKind.QUEUE_WORK, ext.RequestKind.OPEN_WORK):
            continue
        if not valid_proposal_request(request):
            continue
        key = str(request.payload.get("key") or request.payload.get("instruction") or "")
        cid = f"ext:{name}:{key}"
        if (
            not key
            or (name, key) in pending
            or queue.is_seen(state, cid)
            or cid in state.get(_OPENED, {})
        ):
            continue
        inbox.append(
            {"extension": name, "kind": request.kind.value, "payload": dict(request.payload)}
        )
        pending.add((name, key))
        added += 1
    if not inbox:
        state.pop(_PROPOSAL_INBOX, None)
    return added


def drain_staged_proposals(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    store: StateStore,
    daily_cap: int | None,
    issue_repos: frozenset[str],
    spec_only_label: str = "",
) -> None:
    """Honor the manual inbox first, retaining anything the daily cap or I/O defers."""
    remaining: list[dict] = []
    for item in state.get(_PROPOSAL_INBOX, []):
        if not isinstance(item, dict):
            continue
        try:
            request = ext.Request(
                kind=ext.RequestKind(str(item.get("kind") or "")),
                payload=item.get("payload") if isinstance(item.get("payload"), dict) else {},
            )
        except ValueError:
            continue
        name = str(item.get("extension") or "")
        key = str(request.payload.get("key") or request.payload.get("instruction") or "")
        cid = f"ext:{name}:{key}"
        if key and (queue.is_seen(state, cid) or cid in state.get(_OPENED, {})):
            continue
        if _permanently_refused_staged(
            request, issue_repos=issue_repos, spec_only_label=spec_only_label
        ):
            outcome.log.append(f"proposals: dropped malformed or refused request {cid!r}")
            continue
        if daily_cap is not None and _proposal_budget(state, now)["honored"] >= daily_cap:
            remaining.append(item)
            continue
        if not honor_proposals(
            state, port, outcome, name, (request,), now=now, store=store,
            daily_cap=daily_cap, issue_repos=issue_repos, spec_only_label=spec_only_label,
        ):
            remaining.append(item)
    if remaining:
        state[_PROPOSAL_INBOX] = remaining
        if daily_cap is not None and _proposal_budget(state, now)["honored"] >= daily_cap:
            outcome.log.append(
                f"proposals: daily cap ({daily_cap}) reached — manual requests wait for tomorrow"
            )
    else:
        state.pop(_PROPOSAL_INBOX, None)


def _permanently_refused_staged(
    request: ext.Request, *, issue_repos: frozenset[str], spec_only_label: str = ""
) -> bool:
    """True when retrying this stored shape can never change the answer."""
    if not valid_proposal_request(request):
        return True
    if request.kind is ext.RequestKind.OPEN_WORK:
        return str(request.payload.get("repo") or "") not in issue_repos or (
            bool(request.payload.get("spec_only")) and not spec_only_label
        )
    return False


def valid_proposal_request(request: ext.Request) -> bool:
    """Whether this request contains every field its honoring path requires."""
    payload = request.payload
    if not str(payload.get("repo") or ""):
        return False
    if request.kind is ext.RequestKind.OPEN_WORK:
        return bool(
            str(payload.get("key") or "")
            and str(payload.get("title") or "").strip()
        )
    if request.kind is not ext.RequestKind.QUEUE_WORK:
        return False
    if not str(payload.get("key") or payload.get("instruction") or ""):
        return False
    try:
        int(payload.get("number"))
    except (TypeError, ValueError):
        return False
    return True


def honor_proposals(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    name: str,
    requests: tuple[ext.Request, ...],
    *,
    now: datetime,
    store: StateStore,
    daily_cap: int | None,
    issue_repos: frozenset[str],
    spec_only_label: str = "",
) -> int:
    """Honor extension proposals through one cap, repository, and exact-once path."""
    honored = 0
    capped = False
    for request in requests:
        if request.kind not in (ext.RequestKind.QUEUE_WORK, ext.RequestKind.OPEN_WORK):
            continue
        if daily_cap is not None and _proposal_budget(state, now)["honored"] >= daily_cap:
            if not capped:
                outcome.log.append(
                    f"proposals: daily cap ({daily_cap}) reached — the rest wait for tomorrow"
                )
                capped = True
            continue
        if request.kind is ext.RequestKind.OPEN_WORK:
            repo = str(request.payload.get("repo") or "")
            if repo not in issue_repos:
                outcome.log.append(
                    f"{name}: refused to open a card in {repo!r}, which this project does not "
                    "hold issues in"
                )
                continue
            if request.payload.get("spec_only") and not spec_only_label:
                # Opened without the label, the card would start a build nobody approved.
                outcome.log.append(
                    f"{name}: refused a spec-only card, because this project names no "
                    "spec_only_label"
                )
                continue
            queued = _open_proposal(
                state, port, outcome, name, request.payload, now=now, store=store,
                spec_only_label=spec_only_label,
            )
        else:
            queued = _queue_proposal(state, name, request.payload, now=now)
            if queued:
                outcome.log.append(
                    f"{name} proposed work on "
                    f"{request.payload.get('repo')}#{request.payload.get('number')}"
                )
        if queued:
            outcome.report.commands_ingested += 1
            honored += 1
    return honored


def _is_proposal_batch(state: dict, cids: list[str]) -> bool:
    """True when every instruction in this batch came from an extension proposal.

    A batch a human contributed to is that human's request, and an autofix budget must not
    throttle a person who asked for something — so a mixed batch dispatches under the ordinary
    worker cap alone.
    """
    controls = queue.controls(state)
    records = [controls.get(cid) or {} for cid in cids]
    return bool(records) and all(_proposed(record) for record in records)


def _open_proposal(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    name: str,
    payload: dict,
    *,
    now: datetime,
    store: StateStore,
    spec_only_label: str = "",
) -> bool:
    """Open a card for a proposal that has none, then queue the work on it — exactly once (#393).

    Queueing a control is idempotent; creating a GitHub issue is not. So the order is: establish
    that this key has never been filed, write the intent durably, act, then record the result. A
    crash anywhere in that sequence leaves an ``opening`` record, and the next wake **looks for the
    issue before creating one** — because the failure being guarded against is a duplicate, and a
    read that could not be performed is not evidence of absence.
    """
    repo = str(payload.get("repo") or "")
    key_part = str(payload.get("key") or "")
    title = str(payload.get("title") or "").strip()
    if not repo or not key_part or not title:
        return False
    cid = f"ext:{name}:{key_part}"
    if cid in state.get(_OPENED, {}) or queue.is_seen(state, cid):
        return False

    opening = state.setdefault(_OPENING, {})
    marker = _open_marker(cid)
    number: int | None = None
    if cid in opening:
        # `since` is when the intent was written, so the lookup can tell "not there" from "this
        # page never reached back far enough to say".
        since = str(opening[cid].get("at") or now.astimezone(UTC).isoformat())
        found, conclusive = port.find_marked_issue(repo, marker, since=since)
        if not conclusive:
            # Unknown, not absent. Creating now is the one mistake this path exists to avoid.
            outcome.log.append(f"{name}: cannot yet confirm whether {key_part} was filed")
            return False
        number = found
    else:
        opening[cid] = {"repo": repo, "key": key_part, "at": now.astimezone(UTC).isoformat()}
        # Durable before the side effect, so a crash is recoverable by the branch above.
        store.checkpoint(state)

    spec_only = bool(payload.get("spec_only")) and bool(spec_only_label)
    if number is None:
        instruction = str(payload.get("instruction") or "")
        url = str(payload.get("url") or "")
        body = "\n\n".join(part for part in (instruction, url, marker) if part)
        # The spec-only label goes on in the same write as the ownership label (#2874): a card
        # that exists without it, even briefly, would be dispatched as a build.
        labels = (spec_only_label,) if spec_only else ()
        number = port.open_issue(repo, title, body, labels=labels)
    if number is None:
        # The intent stays, so the next wake looks before trying again.
        outcome.log.append(f"{name}: could not open a card for {key_part}")
        return False

    opening.pop(cid, None)
    state.setdefault(_OPENED, {})[cid] = {"repo": repo, "number": int(number)}
    queued = _queue_proposal(
        state, name, {**payload, "number": int(number)}, now=now, spec_only=spec_only
    )
    outcome.log.append(f"{name} opened {repo}#{number} for {key_part}")
    return queued


def _queue_proposal(
    state: dict, name: str, payload: dict, *, now: datetime, spec_only: bool = False
) -> bool:
    """Turn an honored proposal into a queued instruction, exactly once.

    ``spec_only`` is only ever the opening path's word that it labelled the card spec-only
    (#2874); a payload's own field is never trusted here, because a card without the label
    would be read as an approved spec once its worker started.
    """
    repo = str(payload.get("repo") or "")
    try:
        number = int(payload.get("number"))
    except (TypeError, ValueError):
        return False
    key_part = str(payload.get("key") or payload.get("instruction") or "")
    if not repo or not key_part:
        return False
    cid = f"ext:{name}:{key_part}"
    if queue.is_seen(state, cid):
        return False
    queue.controls(state)[cid] = {
        "id": cid,
        "repo": repo,
        "number": number,
        "kind": intake.CommandKind.CLAUDE.value,
        "payload": str(payload.get("instruction") or ""),
        "body": "",
        "url": str(payload.get("url") or ""),
        "author": f"extension:{name}",
        "created_at": now.astimezone(UTC).isoformat(),
        "status": _NEW,
        **({"spec_only": True} if spec_only else {}),
    }
    # Tallied whether or not a ceiling is set, so turning one on later starts from a number
    # that already means something rather than from zero mid-day.
    _proposal_budget(state, now)["honored"] += 1
    return True


def _run_extension_command(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    control: dict,
    *,
    commands: dict,
    now: datetime,
    mode: ext.Mode,
) -> None:
    """Hand a declared extension command to its extension, and honor only what it may ask for.

    The command already passed the same authorization the engine applies to its own commands
    during intake; the tier an extension declares refines that, and those semantics belong to
    #110's rules in #180. What is settled here is the routing: the extension answers with
    requests, and each one goes through the ordinary paths — a proposal becomes a queued
    instruction, nothing becomes a privileged action.
    """
    key = queue.card_key(control["repo"], control["number"])
    word = str(control.get("payload") or "").split(maxsplit=1)[0].lower()
    entry = commands.get(word)
    if entry is None:
        # The extension that declared this word is gone or could not be asked this wake.
        _reject(port, outcome, control, "no-extension-for-command")
        outcome.log.append(f"extension command {word!r} on {key}: no extension answers for it")
        return
    extension, declared = entry
    response = ext.invoke(
        extension,
        ext.Hook.COMMAND,
        context={
            "command": control.get("payload"),
            "word": word,
            "tier": declared.tier,
            "ref": key,
            "author": control.get("author"),
        },
        mode=mode,
    )
    _note_extension(outcome, extension.name, response)
    if not response.ok:
        control["status"] = _NEW  # retry next wake rather than losing the request
        return
    honored = 0
    for request in ext.honored(response, mode):
        if request.kind is ext.RequestKind.QUEUE_WORK and _queue_proposal(
            state, extension.name, {**request.payload, "repo": control["repo"],
                                    "number": control["number"]}, now=now
        ):
            honored += 1
        elif request.kind is ext.RequestKind.COMMENT:
            _do(
                port, outcome,
                _comment_effect(
                    state,
                    control["repo"],
                    int(control["number"]),
                    str(request.payload.get("body") or ""),
                ),
            )
    # The command itself is finished the moment the extension has answered: whatever follow-up
    # it asked for is now ordinary queued work with its own identity, so leaving this one
    # in-flight would only make the ledger re-gate and re-run it every wake.
    _mark_controls(state, port, outcome, [str(control["id"])], status="done",
                   reaction=_REACTION_DONE)
    outcome.log.append(
        f"extension command {word!r} on {key} handled by {extension.name}"
        + (f", queued {honored}" if honored else "")
    )


def _extension_health(
    outcome: CycleOutcome, installed: list, state: dict, *, mode: ext.Mode
) -> None:
    """Ask each health hook how it is, and record it — a skipped one degrades, never fails.

    A refused extension is asked too, though it will not answer: `invoke` refuses it and the
    refusal is what lands in the record. Skipping it because it implements no health hook would
    leave an extension the wake deliberately did not run reported as nothing at all.

    Which file resolved, and any hook it declares that the Manifest does not allow, ride along
    in the record (#2158): what runs is deployed software, so the record is where a reader sees
    which build a project picked up and what it would do if the Manifest allowed more.
    """
    for extension in installed:
        if not (extension.refused or extension.implements(ext.Hook.HEALTH)):
            continue
        response = ext.invoke(extension, ext.Hook.HEALTH, context={}, mode=mode)
        _note_extension(outcome, extension.name, response)
        record = outcome.report.extension_health.get(extension.name)
        if record is None:
            continue
        if extension.source:
            record["source"] = extension.source
        if extension.ignored_hooks:
            record["ignored_hooks"] = sorted(hook.value for hook in extension.ignored_hooks)


def _note_extension(outcome: CycleOutcome, name: str, response) -> None:
    """Fold one extension's answer into this wake's health report."""
    if response.ok:
        outcome.report.extension_health.setdefault(name, {}).update(
            response.health or {"state": "ok"}
        )
        return
    outcome.report.extension_failures += 1
    outcome.report.extension_health[name] = {"state": "degraded", "reason": response.degraded}
    outcome.log.append(f"extension skipped: {response.degraded}")


def extension_release_states(
    installed: list, *, builds: dict, mode: ext.Mode = ext.Mode.LIVE
) -> tuple[dict, list[str]]:
    """The deploy states the release-gate hooks report, for the guarded release's gate.

    A release-gate extension is the source of deploy truth when one is enabled; a host with no
    such extension keeps using the port. An extension that was skipped contributes nothing, so
    its repository stays pending and the release waits rather than finishing on no evidence.
    """
    return ext.release_states(installed, context={"builds": builds}, mode=mode)
