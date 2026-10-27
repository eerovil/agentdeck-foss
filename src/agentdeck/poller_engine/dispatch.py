"""Dispatch directives and at-most-once acknowledgement (#176 slice 3).

The engine never delivers to a worker itself: the async worker client belongs to
the host adapter, which owns delivery. It mints a leased dispatch, durably records
the send intent, and emits a **directive** over a stable protocol; an app-side adapter
performs the real delivery and writes a **dispatch-ack** receipt keyed by the
exact ``(generation, dispatch_id)``.

At-most-once is enforced by ``reconcile``: a dispatch that has been sent and is
within the acknowledgement window is *awaited*, never re-sent, so a slow or
uncertain delivery can never spawn a duplicate worker. A genuinely lost dispatch
is retried only after the timeout, under the same generation and delivery id, up to a
bounded number of attempts; the runtime deduplicates that id, so a retry of a spawn that
did land replays its outcome rather than starting a second worker (#2824).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum

from ..models import IssueRef
from ..work import work_id_for
from . import leases

DISPATCH_ACK_TIMEOUT_S = 90
DISPATCH_MAX_ATTEMPTS = 3
# How many times a steer may re-establish its outcome under the same lease before the engine
# accepts that the outcome is never coming (#1886). A steer is exempt from the cap above for a
# real reason — the runtime deduplicates its stable delivery id, so asking again writes nothing
# twice, and a fresh lease would invalidate the worker whose active turn the message may already
# have joined. What that reasoning does not supply is an ending. A delivery the ledger has
# recorded as `uncertain` is refused on every future attempt by design, so "keep asking until the
# outcome is established" asks a question with a permanent answer, forever: `agentdeck#1840`
# reached attempt 797 over nineteen hours, one refusal per window, while the card still read
# `In progress` to anybody looking at the board.
#
# Twenty windows is half an hour of asking. Far beyond a slow acknowledgement — the case the
# exemption is for — and far short of a day spent on a card that cannot move.
#
# This is the backstop, not the main defence: a refusal the engine can recognise as permanent
# ends the dispatch at the first one, and the bound is what catches the refusals nobody has
# classified and the silence that leaves no refusal at all.
STEER_MAX_ATTEMPTS = 20
# How long a card parked by a spawn that never landed waits before the engine tries it
# once more on its own (#1491). Long enough that the transient thing which parked it —
# host capacity, above all — has plainly passed, short enough that the card is not lost
# for the rest of the day if nobody is watching the board.
DISPATCH_REARM_DELAY_S = 1800


def work_id(owner: str, repo: str, number: int) -> str:
    """The canonical ``work_id`` of a card's issue (#2140, #2142).

    The deck's own ``agentdeck.work.work_id_for``: the id is a pure function of the source,
    so the engine computes it rather than looking it up.
    """
    return work_id_for(IssueRef(f"{owner.strip()}/{repo.strip()}", int(number)))


def stamp_work(dispatch: dict, *, owner: str, repo: str, number: int) -> None:
    """Record the Work this dispatch belongs to on its durable record (#2142).

    Called before the send intent is checkpointed, so the directive's Work is the one the
    engine wrote down. ``owner`` is the project's GitHub owner; with none there is no Work
    to name, and the dispatch stays uncorrelated rather than carrying a guess.
    """
    if owner.strip():
        dispatch["work_id"] = work_id(owner, repo, number)


class DispatchDecision(Enum):
    ACKNOWLEDGED = "acknowledged"  # receipt seen; the send is complete
    AWAITING = "awaiting"          # sent within the window; hold, never re-send
    RETRY = "retry"                # send (first attempt) or re-send with a fresh generation
    EXHAUSTED = "exhausted"        # max attempts reached without acknowledgement


def record_run_session(rec: dict, session_url: str, generation: str) -> None:
    """Remember which chat took this card's Run, as the host reported it (#2155).

    Only a link for the Run that holds the card counts: a late receipt from a superseded Run
    must not make the next dispatch continue the wrong chat.
    """
    if session_url and generation and generation == leases.current(rec):
        rec["run_session"] = {"session_url": session_url, "generation": generation}


def _session_of(value: object, generation: str) -> str:
    """The recorded session link, when it was recorded for exactly ``generation``.

    A card holding no lease has no generation to match. Without that guard, an empty recorded
    generation and an empty current one read as "the same lease" and a link from some earlier
    life of the card would be carried into a fresh one.
    """
    if not isinstance(value, dict) or not generation or value.get("generation") != generation:
        return ""
    return str(value.get("session_url") or "").strip()


def new_dispatch(
    rec: dict,
    action: str,
    *,
    generation: str | None = None,
    allow_over_budget: bool = False,
    avoid_account: str = "",
    continuation: bool = False,
) -> dict:
    """Create and install a leased dispatch on the card (fresh generation unless given)."""
    # Every dispatch carries the card's chat, not only a continuation (#2127). The account a
    # dispatch lands on is chosen on headroom, so an ordinary follow-up moves provider whenever
    # the last one ran out of quota — and without this the card's second half became a second,
    # unconnected chat on the deck. The link is bookkeeping about which conversation this card
    # is; `continuation` is a claim that an earlier turn did work it never reported. They were
    # read from the same `if` and they are not the same fact.
    previous_generation = leases.current(rec)
    previous_session_url = _session_of(rec.get("run_session"), previous_generation)
    gen = generation if generation is not None else leases.bump(rec)
    dispatch = {
        "id": uuid.uuid4().hex,
        "action": action,
        "generation": gen,
        # The Run this dispatch belongs to (#2142). It *is* the generation — no second Run id
        # is minted — recorded under the Work-domain name so a reader of the durable dispatch
        # needs no rule to find it. Its Work is stamped by `stamp_work` once the card's owner
        # is known, before the send intent is made durable.
        "run_id": gen,
        "attempts": 1,
        "sent_at": None,
    }
    if allow_over_budget:
        dispatch["allow_over_budget"] = True
    if avoid_account:
        # Carried on the dispatch, not on the card: it is a property of *this* attempt, so a
        # retry under a fresh generation keeps it and the next ordinary dispatch does not.
        dispatch["avoid_account"] = avoid_account
    if continuation:
        # This attempt takes over from a worker that ended without reporting (#835), so the
        # work may be partly or entirely done already. Like `avoid_account` it belongs to the
        # attempt rather than the card: a retry under a fresh generation is still the same
        # continuation, and the next ordinary dispatch is not one.
        dispatch["continuation"] = True
        if previous_generation and previous_generation != gen:
            # Whose turn this successor is taking over. A continuation goes to the worker
            # already on the card, so until this delivery actually lands there is nobody for
            # the fence to keep out — and the delivery can be refused a whole wake later, most
            # often because that very worker is still mid-turn. `leases.outgoing` reads this to
            # keep the predecessor's control command alive for exactly that gap (#1801).
            dispatch["supersedes"] = previous_generation
    if previous_session_url:
        # The session link is generation-bound, so capture it before minting the successor
        # lease. It lets a worker that moves accounts or providers recover choices made in
        # the old conversation instead of asking the user to make them again (#1228), and it
        # keeps the card's whole run one chat on the deck instead of one per account (#2127).
        dispatch["previous_session_url"] = previous_session_url
    rec["dispatch"] = dispatch
    return dispatch


def new_steer(rec: dict, control_ids: list[str]) -> dict:
    """Install an active-turn follow-up without replacing the worker's lease."""
    dispatch = new_dispatch(
        rec,
        "followup",
        generation=leases.current(rec),
    )
    dispatch["steer"] = True
    dispatch["control_ids"] = list(control_ids)
    return dispatch


def mark_sent(dispatch: dict, now: datetime) -> None:
    """Record the durable send intent timestamp (call before emitting the directive)."""
    dispatch["sent_at"] = now.astimezone(UTC).isoformat()


def next_attempt(rec: dict, dispatch: dict) -> None:
    """Prepare a retry under the same lease, Run and delivery id, and bump the count.

    A retry used to mint a fresh generation to disable a predecessor whose spawn might have
    landed unacknowledged. The runtime now keys every delivery by its id and freezes the Run
    that id belongs to (#2142), so a retry under a new Run was refused for good as
    ``delivery_correlation_conflict`` exactly when the first attempt had landed — and the
    fresh generation then fenced that one live worker as stale (#2824). Under the same Run
    the runtime replays what it did instead: a finished spawn acknowledges, an unfinished
    one answers ``uncertain``, and nothing can start a second worker.
    """
    del rec  # the lease is deliberately kept; see above
    dispatch["attempts"] = int(dispatch.get("attempts") or 1) + 1
    dispatch["sent_at"] = None


def retry_steer(dispatch: dict) -> None:
    """Retry one uncertain steer under the same live lease and delivery id."""
    dispatch["attempts"] = int(dispatch.get("attempts") or 1) + 1
    dispatch["sent_at"] = None


def steer_exhausted(dispatch: dict, *, max_attempts: int = STEER_MAX_ATTEMPTS) -> bool:
    """Has this steer asked for its outcome as many times as it is going to (#1886)?

    Asked before retrying rather than after, so the count is the number of attempts made and
    not one more than that. A steer with no attempts recorded has not run out of anything.
    """
    return int(dispatch.get("attempts") or 0) >= max_attempts


def observe_attempt(dispatch: dict, *, clean: bool, refusal: str | None = None) -> None:
    """Record what became of one attempt: positively refused, or outcome unknown.

    ``clean`` is the host's deferral receipt, which exists only for a refusal decided before
    any provider write. An attempt that simply timed out without one may have started a
    worker whose acknowledgement was lost, and that possibility is permanent: it is ANDed in,
    never reset, so a dispatch that goes ambiguous once stays ambiguous.

    ``refusal`` is the name the host gave that refusal, kept so the card can be told which of
    the two failures this was (#1886). "Nobody answered" and "the host said no, and will say
    no again" both end a dispatch, and a person needs a different thing from each.
    """
    dispatch["clean"] = bool(dispatch.get("clean", True)) and clean
    if refusal:
        dispatch["refusal"] = refusal


def last_refusal(dispatch: dict) -> str | None:
    """The host's name for the most recent positive refusal of this dispatch, if any."""
    return str(dispatch.get("refusal") or "") or None


def provably_unwritten(dispatch: dict) -> bool:
    """Did every attempt of this dispatch provably fail before touching a provider?"""
    return bool(dispatch.get("clean"))


def mark_given_up(
    rec: dict,
    now: datetime,
    *,
    clean: bool,
    refusal: str | None = None,
    final_refused: bool = False,
) -> None:
    """Park a card whose spawn never landed, and start the clock on its one free retry.

    Only a park whose every attempt was positively refused earns that retry. Retrying an
    unknown outcome under a fresh delivery id is the one way to get two workers onto one
    card, so an ambiguous park is fenced until a person resolves it.

    ``refusal`` is the host's name for the last refusal, kept beside the park because the
    dispatch that carried it is dropped with the park (#2141). Nothing decides on it: it
    tells a reader *why* the card parked, for example ``at_capacity``. It is the latest
    *positive* refusal, which an ambiguous dispatch keeps from an earlier attempt, so
    ``dispatch_failed_refusal_final`` says whether the final attempt was itself that refusal.
    """
    rec["dispatch_failed"] = True
    rec["dispatch_failed_at"] = now.astimezone(UTC).isoformat()
    if clean:
        rec["dispatch_failed_clean"] = True
    else:
        rec.pop("dispatch_failed_clean", None)
    if refusal:
        rec["dispatch_failed_refusal"] = refusal
    else:
        rec.pop("dispatch_failed_refusal", None)
    if refusal and (final_refused or clean):
        rec["dispatch_failed_refusal_final"] = True
    else:
        rec.pop("dispatch_failed_refusal_final", None)


def rearmable(
    rec: dict, now: datetime, *, delay_s: int = DISPATCH_REARM_DELAY_S
) -> bool:
    """Is this parked card due its single automatic retry?

    Three things have to be true. The park must be **provably clean** — every attempt
    positively refused before any provider write — because a fresh delivery id for a send
    that may already have spawned a worker is precisely the duplicate at-most-once exists to
    prevent. The cooldown must have passed. And the free retry must be unspent: it is once
    per stretch of failure, not once per wake, because ``dispatch_rearmed`` is cleared only
    by a delivery that landed or by a person's new instruction.

    A card parked before this existed carries neither flag and is left where it is; its
    comment is the recovery path, exactly as it was when it was parked.
    """
    if not rec.get("dispatch_failed") or rec.get("dispatch_rearmed"):
        return False
    if not rec.get("dispatch_failed_clean"):
        return False
    parked_at = rec.get("dispatch_failed_at")
    if not parked_at:
        return False
    age = _age_seconds(str(parked_at), now)
    return age is not None and age >= delay_s


def mark_rearmed(rec: dict) -> None:
    """Spend the free retry and let the ordinary start path pick the card up."""
    rec.pop("dispatch_failed", None)
    rec.pop("dispatch_failed_at", None)
    rec.pop("dispatch_failed_clean", None)
    rec.pop("dispatch_failed_refusal", None)
    rec.pop("dispatch_failed_refusal_final", None)
    rec["dispatch_rearmed"] = True


def clear_rearm(rec: dict) -> None:
    """Forget the park entirely: something landed, so the next one earns its own retry."""
    rec.pop("dispatch_failed", None)
    rec.pop("dispatch_failed_at", None)
    rec.pop("dispatch_failed_clean", None)
    rec.pop("dispatch_failed_refusal", None)
    rec.pop("dispatch_failed_refusal_final", None)
    rec.pop("dispatch_rearmed", None)


# The adapter's refusal of a dispatch whose worktree it could not prepare, followed by
# ``: <why>``. Written before any delivery, so it is a clean refusal, and the notice names the
# worktree instead of saying nobody answered (#2816).
WORKSPACE_REFUSAL = "workspace_unprepared"


def workspace_problem(refused: str | None) -> str:
    """Why the worktree could not be prepared, when that is what refused the dispatch."""
    head, _, why = str(refused or "").partition(":")
    return (why.strip() or "no detail given") if head == WORKSPACE_REFUSAL else ""


def gave_up_notice(
    attempts: int,
    *,
    status: str,
    rearming: bool,
    clean: bool = True,
    refused: str | None = None,
    permanent: bool = False,
    boundary: bool = False,
) -> str:
    """The comment a card gets when its spawn never landed (#1491).

    A card parked for a *transport* failure used to be indistinguishable from one blocked on
    real work — the exact complaint of #470, and still true after #482 gave the log a reason
    the log is the only place that reason appeared. So the card says it in its own thread:
    what was tried, that nothing was acknowledged, that the queued instructions were kept,
    and how it comes back.

    ``boundary`` is a message to a worker that may still be running rather than a spawn that
    never started, and ``refused``/``permanent`` say the host answered "no" instead of not
    answering at all (#1886). All three change what the reader has to go and check, so the
    notice says which one happened rather than describing every park the same way.
    """
    tried = f"{attempts} attempt(s)" if attempts else "every attempt allowed"
    if boundary:
        parts = [
            "**A message to this card's worker could not be delivered.**",
            f"The engine tried {tried} and never got an acknowledgement, so the card has "
            f"been moved to *{status}*. Whether a worker is still running on it is exactly "
            "what the engine cannot establish, which is why it stopped asking instead of "
            "asking forever. This is a delivery failure, not a judgement about the work.",
        ]
    elif workspace := workspace_problem(refused):
        parts = [
            "**No worker could be started for this card: its worktree could not be prepared.**",
            f"{workspace.rstrip('.')}. Nothing was sent to any worker, and the card has been "
            f"moved to *{status}* after {tried}.",
        ]
    else:
        parts = [
            "**No worker could be started for this card.**",
            f"The dispatch was tried {tried} and none was ever acknowledged, so nothing is "
            f"running on it and it has been moved to *{status}*. This is a delivery failure, "
            "not a judgement about the work.",
        ]
    parts.append("The queued instructions were kept, so nothing has to be re-typed.")
    if rearming:
        minutes = int(DISPATCH_REARM_DELAY_S // 60)
        parts.append(
            f"Every attempt was positively refused before anything was written, so the engine "
            f"will try once more on its own in about {minutes} minutes. If that attempt also "
            "fails, the card stays here until a person acts — post a `/claude` comment to try "
            "again. Moving the board column back does **not** re-arm it."
        )
    elif refused and permanent:
        parts.append(
            f"Nobody was silent here: the host **refused** the delivery outright "
            f"(`{refused}`), and no later attempt can be answered differently. An earlier "
            "delivery under this same id has an outcome nobody can establish, and at-most-once "
            "forbids writing it again — so the engine stopped rather than ask a question with "
            "a permanent answer. Check whether a worker is running on this card, then post a "
            "`/claude` comment: that starts a fresh delivery, which does not conflict. Moving "
            "the board column back does **not** re-arm it."
        )
    elif not clean:
        parts.append(
            "At least one attempt ended with **no answer at all**, so it cannot be ruled out "
            "that a worker was started and only its acknowledgement was lost. Retrying that by "
            "itself could put a second worker on this card, so the engine will not: the card "
            "waits for a person. Check whether a worker is running on it, then post a "
            "`/claude` comment to try again. Moving the board column back does **not** "
            "re-arm it."
        )
    else:
        parts.append(
            "This was already the retry, so the card now stays here until a person acts — "
            "post a `/claude` comment to try again. Moving the board column back does "
            "**not** re-arm it."
        )
    if refused and not permanent and not workspace_problem(refused):
        # Which failure this was, in one sentence, for the parks that are not permanent: a
        # host saying no needs a different thing from a reader than a host saying nothing.
        parts.append(f"The host's last word on it was a refusal (`{refused}`), not silence.")
    return " ".join(parts)


def rearming_notice() -> str:
    """The comment a parked card gets when the engine picks it back up by itself."""
    return (
        "**Trying to start a worker again.** This card was parked because no worker could "
        "be started for it, and the engine is now making its one automatic retry with the "
        "same queued instructions. If this attempt does not land either, the card comes "
        "back here and waits for a person."
    )


def build_directive(
    rec: dict,
    dispatch: dict,
    *,
    repo: str,
    number: int,
    ref: str,
    messages: list | None = None,
) -> dict:
    """The stable directive the app-side adapter consumes to deliver this dispatch."""
    directive = {
        "action": dispatch["action"],
        "repo": repo,
        "number": number,
        "ref": ref,
        "generation": dispatch["generation"],
        "dispatch_id": dispatch["id"],
        # The Run as the durable dispatch recorded it (#2142). Never derived here: a dispatch
        # without one is uncorrelated, and the host refuses to send it.
        "run_id": str(dispatch.get("run_id") or ""),
        "messages": messages or [],
    }
    if dispatch.get("work_id"):
        # The Work this Run belongs to, as recorded on the durable dispatch (#2142).
        directive["work_id"] = dispatch["work_id"]
    if rec.get("branch"):
        directive["branch"] = rec["branch"]
    artifacts = dispatch.get("command_artifacts")
    if isinstance(artifacts, dict):
        directive["command_artifacts"] = dict(artifacts)
    if dispatch["action"] == "merge-review" and rec.get("review_prs"):
        # Every open pull request this card would release, discovered by the refresh that runs
        # immediately before this dispatch (#1338). A reviewer given only the issue reviews the
        # one pull request on its own branch and passes the rest unread, and the poller then
        # merges all of them. The engine still says only *which* pull requests are in the
        # release, never what to conclude about them.
        directive["pull_requests"] = list(rec["review_prs"])
    if dispatch.get("allow_over_budget"):
        directive["allow_over_budget"] = True
    if dispatch.get("avoid_account"):
        # A hint, never a rule: the host owns account choice, and this only says which account
        # just killed a worker on this card (#802). A host that has nowhere else to send it
        # sends it here anyway, which is still better than not reviving the card at all.
        directive["avoid_account"] = dispatch["avoid_account"]
    if dispatch.get("continuation"):
        # The host renders the paragraph; the engine says only that this dispatch inherits an
        # unreported turn's work, never what that work reached (#835).
        directive["continuation"] = True
    if dispatch.get("previous_session_url"):
        # Which chat this card already is, on every dispatch. The host turns it into the
        # successor fence that makes the next session the next leg of that chat rather than a
        # chat of its own (#2127); it says nothing about what the previous turn reached.
        directive["previous_session_url"] = dispatch["previous_session_url"]
    if dispatch.get("steer"):
        directive["steer"] = True
    if rec.get("spec_only"):
        directive["spec_only"] = True
    elif dispatch.get("spec_approved"):
        # The build of a spec a person approved by removing the label (#2706).
        directive["spec_approved"] = True
    return directive


def _age_seconds(sent_at: str, now: datetime) -> float | None:
    try:
        sent = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (now.astimezone(UTC) - sent).total_seconds()


def reconcile(
    dispatch: dict,
    *,
    acknowledged: bool,
    now: datetime,
    budget_ok: bool = True,
    timeout_s: int = DISPATCH_ACK_TIMEOUT_S,
    max_attempts: int = DISPATCH_MAX_ATTEMPTS,
) -> DispatchDecision:
    """Decide the next action for an outstanding dispatch (see module docstring)."""
    if acknowledged:
        return DispatchDecision.ACKNOWLEDGED
    sent_at = dispatch.get("sent_at")
    age = _age_seconds(sent_at, now) if sent_at else None
    if age is not None and age <= timeout_s:
        # Sent and still within the window — hold, regardless of budget, so we never re-send.
        return DispatchDecision.AWAITING
    del budget_ok  # retired compatibility argument; usage never gates a retry (#1497)
    if sent_at and int(dispatch.get("attempts") or 1) >= max_attempts:
        return DispatchDecision.EXHAUSTED
    return DispatchDecision.RETRY
