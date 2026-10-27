"""Revive chats whose turn the runtime's death cut off (#1547).

``restart_nudge.py`` wakes a chat that *asked* to be woken. Nobody asks to be
woken from a crash they did not expect: when the runtime was OOM-killed on
2026-09-05 three chats sat mid-turn and nothing in the deck brought them back.
Two of them had their crash written into their own transcripts as "the user
doesn't want to proceed with this tool use" — the boss never saw those questions
and refused nothing.

So this module works the other way round: instead of a registry written before
the death, it reads what the death left behind:

* ``turn_open_at`` — the worker host's own durable record that this chat owes an
  answer, opened at the moment the payload is written to the child and closed by a
  result that really ended the turn. A worker host that keeps one is believed over
  anything deduced.
* ``turn_closed_at`` — the same host's record that somebody *ended* the turn on
  purpose: a stop, a park, an acknowledged interrupt. It outranks both readings
  below, because no terminal result is guaranteed after one and the deduction would
  otherwise resume cancelled work. It counts only while it is newer than both of the
  stamps a new turn writes, so a later authorized message reopens the chat's claim
  even when that message died before its delivery stamp was written.
* ``last_delivery_at > last_result_at`` — a turn was delivered and never produced
  a terminal result. The same fact, deduced from two timestamps, for a worker
  host that publishes no marker.
* outstanding background shell tasks on an otherwise idle worker — the chat ended
  its turn waiting for a notification the dead runtime was going to send it.

The deduction is kept but is no longer trusted alone, because it was wrong for
every Claude chat the 2026-09-11 deploy restart killed (#1844): the CLI emits a
final ``result`` as it is torn down, so the death stamped a result *newer* than
the delivery and three interrupted turns read on disk as finished ones. It also
cannot see a delivery that dies in the same second it arrives, which is how one
chat lost a queued message outright.

An idle chat has no open turn, its result timestamp ahead of its delivery, and no
outstanding work, so it produces no candidate at all and is left alone.

A chat the dashboard started has no worker key and therefore no worker record, so
a second source reads the providers' own session scan: a deck-owned chat whose
transcript ends mid-turn, with nothing live behind it, was cut off by the same
death. Only chats a session-addressed delivery can actually reach are looked at.

Two durable files sit beside the restart-nudge registry, so canary isolation of
that path carries them with it:

* the **exit marker**, written "running" at boot and flipped "clean" by the
  runtime's lifespan teardown. That teardown runs on a graceful stop and is
  skipped entirely by ``SIGKILL``, which makes the marker the only honest answer
  to "was the last exit a crash?". It chooses the wording: a planned restart cuts
  a turn off exactly as dead as an OOM does, and the chat on the other side needs
  telling either way. It also decides whether the ledger below applies (#2965).
* the **ledger**, one row per chat we revived, so a chat that is killed *again on
  the revival turn we gave it* by a crash is not handed another one. A
  memory-hungry chat must not be able to crash the host in a loop, so that one
  stops and is flagged for the boss instead. A planned restart is not the chat's
  doing, so a turn it cuts off is revived even when it was a revival turn.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Why a chat is a survivor. Only the message wording depends on this.
CUT_OFF = "cut_off"
BACKGROUND = "background"
# A chat the dashboard started, which has no worker key and so no worker record:
# the only thing its death left behind is its own transcript.
SESSION_CUT_OFF = "session_cut_off"

# A survivor's cut-off delivery is stamped by the host *during* the deliver call,
# and the ledger stamps its own time once that call returns — so the host clock is
# always the earlier of the two. Anything meaningfully later than the ledger's
# stamp is therefore a genuinely newer delivery, not our own revival read back.
LEDGER_CLOCK_SLACK_S = 1.0

_CRASH_OPENING = (
    "The AgentDeck runtime was killed while your turn was running, and systemd "
    "restarted it."
)
_RESTART_OPENING = (
    "The AgentDeck runtime was restarted while your turn was running, so the "
    "current code is now live."
)

# Said to every revived chat, because the transcript it is about to read back is
# actively misleading about what happened to it.
_CUT_OFF_TRUTH = (
    " Your turn was cut off by that, and none of it was the boss: an interruption, "
    "a rejected tool use, or a question that looks answered near the end of your "
    "transcript is this death and nothing else."
)
_BACKGROUND_TRUTH = (
    " You had ended your turn waiting on background work, and that work died with "
    "the runtime — the notification you were waiting for is never coming."
)
_GUIDANCE = (
    " Pick the task back up from where it actually got to. If you were waiting for "
    "an answer to a question, the boss never saw it, so ask it again. If you were "
    "waiting on a background command or agent, re-run or re-check it rather than "
    "waiting."
)

# Shown on the deck for a chat that died on its own revival. Deliberately a
# `waiting` report: the loop stops here and a person has to look.
LOOP_ATTENTION_DETAIL = (
    "Auto-revival stopped: this chat was revived after the last runtime death and "
    "was cut off again on that revival turn. It has not been revived again."
)


def revival_message(
    reason: str,
    *,
    previous_exit_clean: bool | None,
    unread: Sequence[Mapping[str, object]] = (),
    dropped: int = 0,
) -> str:
    """The one sentence-set a revived chat is told, and why it is being told it.

    ``previous_exit_clean`` is None on the first boot after this feature ships,
    when there is no marker to read yet. Unknown is reported as a death rather
    than as a planned restart: claiming a clean restart we cannot prove would tell
    a chat its transcript is trustworthy when it may not be.

    ``unread`` is what the worker host kept of the messages written into the
    cut-off turn that the chat never read. The dead CLI's queue was their only
    copy, so they travel inside this one revival write rather than as messages
    of their own: one delivery id, nothing a retry can send twice (#2898).
    ``dropped`` counts older ones the host could not keep, which are named
    rather than quoted.
    """
    opening = _RESTART_OPENING if previous_exit_clean else _CRASH_OPENING
    truth = _BACKGROUND_TRUTH if reason == BACKGROUND else _CUT_OFF_TRUTH
    return f"{opening}{truth}{_GUIDANCE}{unread_messages_section(unread, dropped=dropped)}"


def late_revival_message(
    reason: str,
    *,
    previous_exit_clean: bool | None,
    cut_off_at: float,
    unread: Sequence[Mapping[str, object]] = (),
    dropped: int = 0,
) -> str:
    """The revival for a chat another message reached first (#3073).

    The account had no free slot when the revival was due, and before one freed
    something else woke the chat: a job notice, a reviewer, a person. That turn
    read Claude's bare "tool call interrupted" stub and nothing saying a restart
    caused it, so dropping the revival left five Works on 2026-10-13 thinking
    their turn had simply ended. The boot text cannot go instead: it calls
    everything near the end of the transcript the death, and by now that end is
    somebody's real message. This one dates the death and says what came after
    it is real.
    """
    at = time.strftime("%H:%M", time.localtime(cut_off_at))
    death = (
        f"The AgentDeck runtime was restarted at about {at} while a turn of yours was "
        "running, so the current code is now live."
        if previous_exit_clean
        else f"The AgentDeck runtime was killed at about {at} while a turn of yours "
        "was running, and systemd restarted it."
    )
    truth = (
        " You had ended that turn waiting on background work, and that work died "
        "with the runtime — the notification you were waiting for is never coming."
        if reason == BACKGROUND
        else " That turn was cut off by it, and none of the cut-off was the boss: an "
        "interruption or a rejected tool use from before then is this death."
    )
    return (
        f"{death}{truth} This note could not reach you then: the account had no free "
        "slot, and another message got to you first. Everything you were sent after "
        "the restart is real. If your turns since then already picked the task back "
        "up, carry on. Otherwise pick it up from where it actually got to: a question "
        "you were waiting on was never seen, so ask it again, and a background "
        "command or agent you were waiting on needs re-running or re-checking."
        f"{unread_messages_section(unread, dropped=dropped)}"
    )


def unread_messages_section(
    unread: Sequence[Mapping[str, object]], *, dropped: int = 0
) -> str:
    """The lost messages, quoted oldest first, or "" when there are none."""
    messages = [
        entry for entry in unread if isinstance(entry.get("text"), str) and entry["text"]
    ]
    dropped = max(dropped, 0)
    if not messages and not dropped:
        return ""
    count = len(messages)
    older = (
        f"\n\nUp to {dropped} older message{'s' if dropped != 1 else ''} sent into that "
        "turn may also have been lost: too many were waiting to keep them all, so "
        "they cannot be quoted. Tell the boss, in your reply, that they may have to "
        "send those again."
        if dropped
        else ""
    )
    if not messages:
        return older
    parts = [
        f"\n\n{count} message{'s' if count != 1 else ''} reached you while that turn "
        "was running and never got to you: the CLI holds a message sent mid-turn "
        "until the turn is ready for it, and it died first. They are quoted below, "
        "oldest first. Read them and act on them as if they had just been sent."
    ]
    for number, entry in enumerate(messages, start=1):
        at = entry.get("at")
        sent = (
            f", sent {time.strftime('%H:%M', time.localtime(at))}"
            if isinstance(at, int | float) and at > 0
            else ""
        )
        images = entry.get("images")
        lost = (
            f" (it also carried {images} image{'s' if images != 1 else ''}, which "
            "could not be kept; ask for them again if you need them)"
            if isinstance(images, int) and images > 0
            else ""
        )
        parts.append(f"\n\n--- Message {number}{sent}{lost} ---\n{entry['text']}")
    parts.append("\n\n--- End of the lost messages ---")
    return older + "".join(parts)


@dataclass(frozen=True)
class Survivor:
    """One chat that was depending on the dead runtime to make progress."""

    session_key: str
    provider_id: str
    label: str
    # Empty for a chat the dashboard started: it has no keyed worker at all.
    worker_key: str
    reason: str
    last_delivery_at: float
    background_tasks: frozenset[str] = frozenset()

    def delivery_id(self, boot_stamp: float) -> str:
        """Stable at-most-once identity for this boot's revival turn."""
        return f"revive:{self.session_key}:{int(boot_stamp)}"


@dataclass(frozen=True)
class RevivalRecord:
    """What we last delivered to one chat, so we can tell a loop from new work."""

    session_key: str
    delivery_id: str
    delivered_at: float
    boot_stamp: float
    background_tasks: frozenset[str] = frozenset()


BackgroundLookup = Callable[[str, str, str], Iterable[str]]


def find_survivors(
    snapshots: Mapping[tuple[str, str], Mapping[str, object]],
    *,
    now: float,
    ttl_s: float,
    background_tasks: BackgroundLookup | None = None,
) -> list[Survivor]:
    """Read every worker host snapshot for chats the last death interrupted.

    ``snapshots`` is keyed by ``(provider_id, label)`` and holds whatever
    ``KeyedWorkerHost.snapshot()`` returned, so this stays a pure function over
    the durable read model and needs no runtime to test.

    A worker that is *live with a turn running* is skipped. That is not a
    hypothetical: Codex app-server backends live in their own transient services
    and survive a runtime restart with their turns intact, so their records read
    exactly like a cut-off turn while the turn is in fact still going.
    """
    survivors: list[Survivor] = []
    for (provider_id, label), snapshot in sorted(snapshots.items()):
        workers = snapshot.get("workers") if isinstance(snapshot, Mapping) else None
        if not isinstance(workers, Mapping):
            continue
        for worker_key, worker in sorted(workers.items()):
            survivor = _survivor(
                provider_id,
                str(label),
                str(worker_key),
                worker,
                now=now,
                ttl_s=ttl_s,
                background_tasks=background_tasks,
            )
            if survivor is not None:
                survivors.append(survivor)
    return survivors


def _survivor(
    provider_id: str,
    label: str,
    worker_key: str,
    worker: object,
    *,
    now: float,
    ttl_s: float,
    background_tasks: BackgroundLookup | None,
) -> Survivor | None:
    if not isinstance(worker, Mapping):
        return None
    session_id = worker.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        # No lineage means nothing to resume and no session key to deliver to.
        return None
    if worker.get("live") and (
        worker.get("effective_activity") or worker.get("turn_active")
    ):
        return None
    last_delivery_at = _stamp(worker.get("last_delivery_at"))
    last_result_at = _stamp(worker.get("last_result_at"))
    # The durable "this chat still owes an answer" marker, where the worker host
    # keeps one. It is the honest fact; the timestamp comparison below is only a
    # deduction from it, and a wrong one whenever the death writes a result of its
    # own on the way out (#1844).
    turn_open_at = _stamp(worker.get("turn_open_at"))
    # When somebody deliberately ended the turn — a stop, a park, an acknowledged
    # interrupt. Nobody is owed a revival of work they cancelled themselves.
    turn_closed_at = _stamp(worker.get("turn_closed_at"))
    # The same staleness rule the nudge registry uses: a chat cut off last month is
    # not owed a revival on today's boot.
    if now - max(last_delivery_at, last_result_at, turn_open_at) > ttl_s:
        return None
    session_key = f"{provider_id}:{label}:{session_id.strip()}"
    # A deliberate end outranks both readings below, and has to: no terminal
    # ``result`` is guaranteed after one — ``_terminate`` escalates to SIGKILL —
    # so the deduction would otherwise still say "delivered, never answered" and
    # resume work an operator cancelled. It suppresses only the work it actually
    # ended, which is why it is measured against the *later* of the two stamps a
    # new turn writes. The open marker is stamped before the child write and the
    # delivery stamp only after the write and the initialization wait both
    # succeed, so a message that died in that window has an open turn and a
    # delivery stamp still older than the cancellation — and the cancellation
    # must not speak for it. A record written before these fields existed carries
    # neither stamp and keeps the old behaviour.
    if turn_closed_at > 0.0 and turn_closed_at >= max(last_delivery_at, turn_open_at):
        return None
    # Either reading is enough. The marker is what catches a killed turn; the
    # comparison stays for the worker hosts that publish no marker, where it is
    # still the only thing there is to go on. The marker is read only for a chat
    # whose process is gone, because that is the whole claim it makes — a chat
    # still running does not need telling that its turn was cut off.
    if (turn_open_at > 0.0 and not worker.get("live")) or last_delivery_at > last_result_at:
        return Survivor(
            session_key=session_key,
            provider_id=provider_id,
            label=label,
            worker_key=worker_key,
            reason=CUT_OFF,
            # When the cut-off turn opened, which is what the loop guard needs:
            # a chat that took a continuation turn after its last revival opened
            # one without a delivery of ours, and reads as a loop without this.
            last_delivery_at=max(last_delivery_at, turn_open_at),
        )
    if background_tasks is None:
        return None
    try:
        outstanding = frozenset(
            str(task) for task in background_tasks(provider_id, label, worker_key)
        )
    except Exception:  # noqa: BLE001 -- one unreadable transcript must not end the scan
        log.exception("revival: could not read background tasks for %s", session_key)
        return None
    if not outstanding:
        return None
    return Survivor(
        session_key=session_key,
        provider_id=provider_id,
        label=label,
        worker_key=worker_key,
        reason=BACKGROUND,
        last_delivery_at=last_delivery_at,
        background_tasks=outstanding,
    )


def _stamp(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


@dataclass(frozen=True)
class SessionCandidate:
    """One deck-owned chat with no worker key, as its transcript left it.

    The keyed scan above cannot see these: a chat the dashboard started has no
    worker record, so its two timestamps do not exist. What it does have is a
    transcript, and the provider scan already derives from it whether a turn was
    still running when everything stopped.
    """

    session_key: str
    provider_id: str
    label: str
    # The provider's own reading of "a turn is in flight" — an Active Turn, or one
    # that has since crossed the stall threshold because nothing is left to move
    # it. An idle chat has neither and never becomes a candidate.
    mid_turn: bool
    # The chat is running *now*, on this boot. Codex threads live in their own
    # transient service and can outlive the runtime with their turns intact.
    live: bool
    last_activity: float


def find_session_survivors(
    candidates: Iterable[SessionCandidate],
    *,
    now: float,
    ttl_s: float,
    exclude: frozenset[str] | set[str] = frozenset(),
) -> list[Survivor]:
    """Chats the death cut off that no worker record could have told us about.

    ``exclude`` carries every session this boot has already written to *and*
    every session a keyed worker covers, because a keyed chat is also a
    transcript on disk and must not be judged twice by two different rules.
    """
    survivors: list[Survivor] = []
    for candidate in sorted(candidates, key=lambda item: item.session_key):
        if candidate.session_key in exclude or candidate.live:
            continue
        if not candidate.mid_turn:
            continue
        if now - candidate.last_activity > ttl_s:
            continue
        survivors.append(
            Survivor(
                session_key=candidate.session_key,
                provider_id=candidate.provider_id,
                label=candidate.label,
                worker_key="",
                reason=SESSION_CUT_OFF,
                last_delivery_at=candidate.last_activity,
            )
        )
    return survivors


def revival_looped(
    survivor: Survivor,
    previous: RevivalRecord | None,
    *,
    previous_exit_clean: bool | None = None,
) -> bool:
    """True when what we are looking at is the revival *we* delivered, killed.

    For a cut-off turn that means no delivery landed after our own: the turn that
    never finished is the revival turn. For a chat waiting on background work it
    means no background task has appeared that we did not already tell it about —
    otherwise a chat whose old task is simply never collected would be revived
    once per boot forever.

    A dashboard-started chat is judged harder, because its transcript carries no
    result timestamp to compare against: any revival we already delivered inside
    the ledger's window stops the next one. A chat that finished the revival turn
    is idle and never reaches here at all, so what this refuses is a chat that is
    *still* mid-turn after we woke it — which is the loop the card is about.

    A cut-off turn killed by a *planned* restart is never a loop (#2965). The guard is
    there for a chat that crashes the host, and a deploy is not that chat's doing. A
    revival turn can sit for hours on a question it re-asked, and deploys land many
    times a day: counting the next one as a loop left a dozen chats on 2026-10-13
    with their question gone and nothing to wake them. The host's open-turn record
    says the turn really was cut off, so this revives once per planned restart at
    most. A crash, or an exit nobody can vouch for, still stops at the second death.
    """
    if previous is None:
        return False
    if survivor.reason == CUT_OFF and previous_exit_clean is True:
        return False
    if survivor.reason == SESSION_CUT_OFF:
        return True
    if survivor.reason == BACKGROUND:
        return not (survivor.background_tasks - previous.background_tasks)
    return survivor.last_delivery_at <= previous.delivered_at + LEDGER_CLOCK_SLACK_S


class RuntimeExitMarker:
    """Durable "did the last runtime exit cleanly?" flag.

    Written running at boot and clean by the lifespan teardown, which uvicorn runs
    on a graceful stop and never gets to run under ``SIGKILL``.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def claim(self) -> bool | None:
        """Read the previous exit and mark this boot running. None if unknown."""
        previous = self._read()
        self._write(clean=False)
        return previous

    def mark_clean(self) -> None:
        self._write(clean=True)

    def _read(self) -> bool | None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("revival: ignoring unreadable exit marker at %s", self.path)
            return None
        clean = raw.get("clean") if isinstance(raw, dict) else None
        return clean if isinstance(clean, bool) else None

    def _write(self, *, clean: bool) -> None:
        payload = {"clean": clean, "stamped_at": time.time()}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=1))
            temporary.replace(self.path)
        except OSError:
            # A marker we cannot write reads as unknown next boot, which errs
            # towards the crash wording. It must never stop the runtime booting.
            log.exception("revival: could not write the exit marker at %s", self.path)


class RevivalLedger:
    """Durable record of the revivals already delivered, for the loop guard."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, RevivalRecord]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("revival: ignoring unreadable ledger at %s", self.path)
            return {}
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(sessions, dict):
            return {}
        records: dict[str, RevivalRecord] = {}
        for session_key, row in sessions.items():
            if not isinstance(session_key, str) or not isinstance(row, dict):
                continue
            delivered_at = row.get("delivered_at")
            if not isinstance(delivered_at, int | float):
                continue
            tasks = row.get("background_tasks")
            records[session_key] = RevivalRecord(
                session_key=session_key,
                delivery_id=str(row.get("delivery_id") or ""),
                delivered_at=float(delivered_at),
                boot_stamp=float(row.get("boot_stamp") or 0.0),
                background_tasks=frozenset(
                    str(task) for task in tasks if isinstance(task, str)
                )
                if isinstance(tasks, list)
                else frozenset(),
            )
        return records

    def record(self, record: RevivalRecord, *, now: float, ttl_s: float) -> None:
        """Store one revival and drop rows too old to guard anything."""
        records = self.load()
        records[record.session_key] = record
        keep = {
            key: value
            for key, value in records.items()
            if now - value.delivered_at <= ttl_s
        }
        payload = {
            "sessions": {
                key: {
                    "delivery_id": value.delivery_id,
                    "delivered_at": value.delivered_at,
                    "boot_stamp": value.boot_stamp,
                    "background_tasks": sorted(value.background_tasks),
                }
                for key, value in sorted(keep.items())
            }
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=1))
            temporary.replace(self.path)
        except OSError:
            # Losing the ledger costs a duplicate revival next boot at worst, which
            # is better than a runtime that will not start.
            log.exception("revival: could not write the ledger at %s", self.path)


def revival_paths(registry_path: Path) -> tuple[Path, Path]:
    """``(exit marker, ledger)`` beside the nudge registry, sharing its isolation."""
    registry_path = Path(registry_path)
    stem = registry_path.name.removesuffix(".json")
    return (
        registry_path.with_name(f"{stem}-exit.json"),
        registry_path.with_name(f"{stem}-revivals.json"),
    )
