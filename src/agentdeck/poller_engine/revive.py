"""Reviving a worker that ended without reporting, instead of parking the card (#802, #835).

A card only leaves *In progress* on its worker's own lease-bound terminal marker. When the
worker never posts one, the card has no trustworthy outcome at all — and the engine's first
answer was to publish *Needs More Info* and let a human restart it by hand.

That is the wrong answer twice over. It was wrong for the ending #802 was about — a worker
**killed** mid-turn by a usage limit or an OOM, with a PR half-open and ten uncommitted files
in a worktree — because nothing about an interruption needs a decision. And it is wrong for
the ending that is left: a turn that reached its own end and said nothing (#835, seen on #791
and #788). That worker's silence is still not success, but a *human* is not the only thing
that can find out what the work reached. Another worker, handed the same card and the same
worktree, can look — and unlike the human it is already there.

So every reportless ending the persistent Runtime reports is restarted rather than parked.
The Runtime owns the fact (``run_execution.py``, #2159); this module only decides what the
Work does next:

* ``died`` — the worker's process is gone and it never produced a result for this Run. That
  is a kill (OOM, ``kill -9``, a crashed CLI): nothing chose to stop.
* ``ended``, with any subtype or none — the turn is over. An error subtype says it was
  killed; a CLI's own word for a normal finish, or an unreadable subtype, says only that it
  stopped without reporting.
* ``abandoned`` — the worker's turn never ended and it did nothing at all for hours (#1400).
  The Runtime has already stopped it by the time the host records this.
* ``stopped`` — the Runtime stopped the Run's execution, and nothing on this card asked it to.

``involuntary()`` still tells those two apart, because they are not the same story to tell on
the card and not the same thing to ask the next worker to do — but they get the same answer.

A Runtime that cannot tell — a restart it has not yet reconciled, a host it cannot read —
answers ``uncertain``, and the host records nothing: unknown is not an ending.

Nothing here infers success from a branch, a dirty worktree, or an open PR. The revived worker
is told what is already there and must still post its own terminal report; the card leaves
*In progress* on that report or on the cap, never on the engine's reading of leftovers.

One count keeps a revive from becoming a loop: the Run attempts. It is capped, so a card that
keeps ending silently still reaches a human after ``MAX_REVIVES`` tries rather than burning
both accounts forever; and it is cleared by any ordinary dispatch, because a fresh instruction
is fresh work, not a third attempt at the old one.

A turn the provider refused before it ran is not an attempt, so it spends nothing and has no
count of its own (#1519, #2159). What stops that from looping is the Runtime's scheduler: an
account that refuses is stepped over, and while every account is blocked ``ensure_delivery``
refuses the next Run before it starts, which the engine simply retries later.

Revives use the host's configured account order. Model usage is observational and never
reroutes work (#1497), regardless of how the previous CLI described its ending.
"""

from __future__ import annotations

from enum import Enum

# How many times the engine restarts one card's work before it hands the card to a human.
# Two, because one revive covers the ordinary "the account ran out mid-turn" case and a
# second covers dying again on the account it moved to; a card that cannot survive either
# is not failing at random, and a human should see it.
MAX_REVIVES = 2

_REVIVES = "revives"

# The execution states the engine reads from the persistent Runtime's outcome for a Run
# (``run_execution.py``, #2159, #2200); the engine no longer works them out from worker
# snapshots and quiet ages, nor from receipts a host wrote down.
ENDED = "ended"
DIED = "died"
ABANDONED = "abandoned"
# The Runtime stopped the Run's execution. For a stopping card it is the proof the stop waits
# for; for a card nobody here stopped, it is an ending like the others.
STOPPED = "stopped"

# The endings the Runtime can prove happened. Anything else (`gone`, or a word a later
# Runtime invents) says nothing about whether a turn finished, so it is only reported.
# ``abandoned`` is here because it only counts once the Runtime has stopped the wedged worker
# (#1400, #2159).
ENDINGS = frozenset({ENDED, DIED, ABANDONED, STOPPED})
# The Runtime has no execution for a Run it was sent: nothing ran and nothing is running.
GONE = "gone"

# How long after an ended turn its uncollected background commands still hold the card (#2890).
# A command Claude moves to the background wakes the chat when it finishes, so the turn ending
# is a pause, not the end. A command that never finishes (a server left running) must not hold
# the card forever, so past this window the ending is handled as any other.
BACKGROUND_HOLD_S = 2 * 3600

# The Runtime's own states and reasons (``run_execution.py``), spelled here because the engine
# never imports the app.
_NOT_STARTED = "not_started"
_REFUSED_PREFIX = "refused:"
# An ``ended`` reason that is the Runtime's bookkeeping, not the CLI's word for the turn.
_BOOKKEEPING_REASONS = frozenset({"result", "worker_released", "no_turn_observed"})

# A turn that reached its own end. Every CLI has its own word for that and none of them agree,
# so this holds all of them rather than one provider's (#1781): Claude Code says
# ``success``, Codex says ``completed`` — the raw ``turn/completed`` status, next to ``failed``
# and ``interrupted``. Knowing only ``success`` meant every Codex worker that finished normally
# without reporting was announced on its card as "killed mid-turn (completed)", which was true
# of all 25 reportless Codex endings this host has ever seen and of none of the workers.
CLEAN_SUBTYPES = frozenset({"success", "completed"})

# A CLI's word for "the turn ended and I have no status for it". Codex writes ``unknown`` when
# the completion event carries no status at all, which is its name for the unnamed subtype
# below — silence about the cause, not a named failure. It is not evidence of a kill for the
# same reason an empty subtype is not.
UNKNOWN_SUBTYPES = frozenset({"unknown"})

# The Runtime's ``ended`` reason for a turn the account's usage limit cut short. The CLI calls
# that turn ``success``; the Runtime reads the limit from the worker instead (#2862), and since
# it is not a clean subtype, ``involuntary()`` reads it as the interruption it is.
USAGE_LIMIT = "usage_limit"

class ReviveDecision(Enum):
    REVIVE = "revive"  # re-dispatch this card's work under a fresh lease
    PARK = "park"      # publish Needs More Info and let a human decide
    REPORT = "report"  # say what was observed and change nothing


def attempts(rec: dict) -> int:
    """How many times this card's work has already been revived."""
    value = rec.get(_REVIVES)
    return value if isinstance(value, int) and value > 0 else 0


def involuntary(*, observed: str, subtype: str) -> bool:
    """Is this ending positive evidence that the worker was stopped rather than finished?

    Both kinds of reportless ending are revived; this only decides which story the card is
    told and what the next worker is asked to expect — an interrupted turn or a finished one
    that never said so.
    """
    if observed in (DIED, ABANDONED, STOPPED):
        # An abandoned worker did not reach the end of its turn — it stopped in the middle of
        # one and never moved again, which is an interruption whatever caused it.
        return True
    if observed != ENDED:
        return False
    return (
        bool(subtype)
        and subtype not in CLEAN_SUBTYPES
        and subtype not in UNKNOWN_SUBTYPES
    )


def observation(outcome: object) -> dict | None:
    """The ending one Runtime execution outcome proves, or None when it proves none (#2200).

    ``outcome`` is ``GET /executions/outcome``'s answer. The answer is ``observed``, ``subtype``
    (the CLI's own word for how an ``ended`` turn finished), ``refused`` and ``account``.
    ``running``, ``waiting_human``, ``uncertain`` and an unreadable answer are None: unknown is
    never an ending, and a restart is unknown. ``abandoned`` is None too until the Runtime has
    stopped the wedged worker; it then reads as ``stopped`` with the reason ``abandoned``.

    A refusal is a claim that no work was attempted, so it takes evidence about this delivery
    (#1519): the provider's own word, which the Runtime reports as a ``refused:`` reason, or
    the account being provably blocked when the route was frozen. A turn that reached its own
    clean end ran, whatever else is true.

    An ``ended`` turn that was not refused also carries ``background_tasks``, the background
    commands its chat has not collected, ``ended_at``, when it ended (#2890), and
    ``session_key``, the chat it ended in, or ``""`` when the Runtime did not name one (#2902).
    """
    if not isinstance(outcome, dict):
        return None
    state = str(outcome.get("execution_state") or "")
    reason = str(outcome.get("reason_code") or "")
    provider = str(outcome.get("provider") or "")
    label = str(outcome.get("account") or "")
    account = f"{provider}:{label}" if provider and label else label
    if state == _NOT_STARTED:
        return {"observed": GONE, "subtype": "", "refused": "", "account": account}
    if state == STOPPED and reason == ABANDONED:
        state = ABANDONED
    elif state not in ENDINGS or state == ABANDONED:
        return None
    if state == ENDED and reason.startswith(_REFUSED_PREFIX):
        return {
            "observed": ENDED,
            "subtype": "refused",
            "refused": reason[len(_REFUSED_PREFIX):],
            "account": account,
        }
    subtype = reason if state == ENDED and reason not in _BOOKKEEPING_REASONS else ""
    refused = (
        str(outcome.get("refused_at_delivery") or "")
        if involuntary(observed=state, subtype=subtype)
        else ""
    )
    seen = {"observed": state, "subtype": subtype, "refused": refused, "account": account}
    if state == ENDED:
        tasks = outcome.get("background_tasks")
        seen["background_tasks"] = (
            sorted({str(task) for task in tasks if task}) if isinstance(tasks, list) else []
        )
        ended_at = outcome.get("terminal_result_at")
        seen["ended_at"] = (
            float(ended_at)
            if isinstance(ended_at, int | float) and not isinstance(ended_at, bool)
            else None
        )
        session = str(outcome.get("session_id") or "")
        named = provider and label and session
        seen["session_key"] = f"{provider}:{label}:{session}" if named else ""
    return seen


def background_wait(observation: dict, *, now: float) -> list[str]:
    """The background commands an ended turn is still waiting on, or ``[]`` (#2890).

    Only for a turn that reached its own end within ``BACKGROUND_HOLD_S``; an ending with no
    readable time proves no recent wait, so it holds nothing.
    """
    if observation.get("observed") != ENDED or observation.get("refused"):
        return []
    tasks = observation.get("background_tasks")
    ended_at = observation.get("ended_at")
    if not isinstance(tasks, list) or not tasks or not isinstance(ended_at, float):
        return []
    if now - ended_at >= BACKGROUND_HOLD_S:
        return []
    return [str(task) for task in tasks]


def decide(
    rec: dict, *, observed: str, dispatch_ok: bool, refused: bool = False
) -> ReviveDecision:
    """What to do about one reportless ending (see the module docstring)."""
    del dispatch_ok  # retired compatibility argument; usage never gates a revive (#1497)
    if observed not in ENDINGS:
        return ReviveDecision.REPORT
    if refused:
        # The account turned the turn away, so this card has not had an attempt yet — its
        # budget is untouched and it is handed out again. Without this a card burns both
        # attempts on an account that was never going to run anything, and reaches a human
        # having done no work at all (#1519).
        return ReviveDecision.REVIVE
    if attempts(rec) >= MAX_REVIVES:
        return ReviveDecision.PARK
    return ReviveDecision.REVIVE


def record(rec: dict) -> int:
    """Count one revive attempt and return the new total.

    Counted before the dispatch is emitted, not after it lands: a spawn whose board write
    is refused still installs a real dispatch the next wake settles, so an attempt that is
    only counted on success is an attempt that can repeat without bound.
    """
    total = attempts(rec) + 1
    rec[_REVIVES] = total
    return total


def reset(rec: dict) -> None:
    """Forget the counts. Any ordinary dispatch is new work, not another try at the old."""
    rec.pop(_REVIVES, None)
