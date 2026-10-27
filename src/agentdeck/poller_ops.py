"""Reading and steering Poller Instances from AgentDeck (#182, designed in #107).

A Poller Instance is a oneshot timer job: it wakes, works, and exits, so there is no process to
call between wakes. Everything here follows from that.

* **Observation is reading its durable artifacts** — the state file, the health record it writes
  every wake, its activation record, and systemd's view of the timer. Never a live channel.
* **Freshness is a first-class signal, separate from state.** What the instance last reported and
  whether that is still believable are different questions, so the view carries both: an overdue
  instance reads as *stale — last seen N ago*, never as current. This is what stops a dead poller
  from showing a green light.
* **Pause and resume are direct writes.** There is nothing to call between wakes, so a control
  edits the instance's state file under its lock. The next wake reads it, and a paused instance
  keeps waking (and stays fresh) but starts no work. Pressing Pause twice leaves it paused, so a
  repeated press needs no id.

Nothing here reaches into engine internals: it reads the same durable files the engine publishes,
so the two can be deployed independently (#116).
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .poller_engine import activation, deck_releases
from .poller_engine import health as engine_health
from .poller_engine import queue as engine_queue
from .poller_engine.config import AUTO_RELEASE, DEFAULT_OWNERSHIP_LABEL, Role, verify_turn
from .poller_engine.revive import MAX_REVIVES
from .poller_engine.state import EnginePaths, StateStore

if TYPE_CHECKING:
    from .poller_nudge import ExternalReviews

DEFAULT_INTERVAL_S = engine_health.DEFAULT_INTERVAL_S


def humanize_age(seconds: float) -> str:
    """An age in words an operator can scan. Every age on the poller page reads the same way."""
    count = int(seconds)
    if count < 90:
        return f"{count}s ago"
    if count < 5400:
        return f"{count // 60}m ago"
    if count < 172800:
        return f"{count // 3600}h ago"
    return f"{count // 86400}d ago"


@dataclass(frozen=True)
class TimerView:
    """What systemd says about this instance's timer, or nothing when it cannot be asked."""

    known: bool = False
    active: bool = False
    next_wake: str = ""
    last_result: str = ""

    @property
    def firing(self) -> bool:
        return self.known and self.active


@dataclass
class InstanceView:
    """One instance as an operator should see it: what it said, and whether to believe it."""

    instance: str
    exists: bool = False
    health: object | None = None            # engine_health.HealthVerdict
    paused: bool = False
    pause_note: str = ""
    last_cycle_at: str = ""
    age_seconds: float | None = None
    working: tuple[str, ...] = ()
    blocked: tuple[dict, ...] = ()
    queue: tuple[dict, ...] = ()
    budget_holding: bool = False
    errors: tuple[str, ...] = ()
    last_error: str = ""
    # Which extension degraded the instance and why, in its own words (#2384).
    degraded_extensions: tuple[str, ...] = ()
    last_error_age_seconds: float | None = None
    # A failure the wakes since have not repeated is history, not the instance's state (#2384).
    last_error_current: bool = False
    version: dict = field(default_factory=dict)
    activation: dict = field(default_factory=dict)
    timer: TimerView = field(default_factory=TimerView)

    @property
    def state_label(self) -> str:
        """The one word an operator reads first, freshness included."""
        if not self.exists:
            return "not installed"
        verdict = getattr(self.health, "state", None)
        return getattr(verdict, "value", "unknown")

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(
            reason.value for reason in getattr(self.health, "reasons", ()) or ()
        )

    @property
    def freshness(self) -> str:
        return getattr(getattr(self.health, "freshness", None), "value", "unknown")

    @property
    def believable(self) -> bool:
        """Is the last report recent enough to act on?"""
        return self.freshness == "fresh"

    @property
    def last_seen(self) -> str:
        """How long ago the instance last reported, in words an operator can scan."""
        if self.age_seconds is None:
            return "never"
        return humanize_age(self.age_seconds)

    @property
    def last_error_when(self) -> str:
        """When the last failure happened, so a weeks-old one does not read as today's."""
        if self.last_error_age_seconds is None:
            return ""
        return humanize_age(max(self.last_error_age_seconds, 0))

    @property
    def summary(self) -> str:
        """A single line for a list row: state, freshness, and what it is doing."""
        parts = [self.state_label]
        if not self.exists:
            return self.state_label
        if not self.believable:
            parts.append(f"last seen {self.last_seen}")
        if self.paused:
            parts.append("paused")
        if self.working:
            parts.append(f"{len(self.working)} working")
        elif self.exists:
            parts.append("idle")
        if self.blocked:
            # Said in the summary line because "idle" on its own invites a restart of a poller
            # that is deliberately waiting for somebody else's card to close (#640).
            parts.append(f"{len(self.blocked)} blocked")
        return " · ".join(parts)


def _systemctl(*args: str, runner=None) -> tuple[int, str]:
    run = runner or _run
    return run(["systemctl", "--user", *args])


def _run(command: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return done.returncode, (done.stdout or "").strip()


def timer_view(instance: str, *, runner=None) -> TimerView:
    """Ask systemd about the timer. An unanswerable question is 'unknown', not 'off'."""
    unit = f"agentdeck-poller@{instance}.timer"
    code, out = _systemctl(
        "show", unit, "-p", "ActiveState", "-p", "NextElapseUSecRealtime", "-p", "Result",
        runner=runner,
    )
    if code != 0 or not out:
        return TimerView(known=False)
    values = dict(
        line.split("=", 1) for line in out.splitlines() if "=" in line
    )
    return TimerView(
        known=True,
        active=values.get("ActiveState") == "active",
        next_wake=values.get("NextElapseUSecRealtime", ""),
        last_result=values.get("Result", ""),
    )


def read_instance(
    root: str | Path,
    instance: str,
    *,
    now: datetime | None = None,
    interval_s: int = DEFAULT_INTERVAL_S,
    runner=None,
) -> InstanceView:
    """Build one instance's view from its durable files plus systemd — never a live call."""
    moment = now or datetime.now(UTC)
    base = Path(root).expanduser() / "instances" / instance
    view = InstanceView(instance=instance, timer=timer_view(instance, runner=runner))
    state_path = base / "state.json"
    if not state_path.exists() and not (base / "state.json.bak").exists():
        return view

    store = StateStore(EnginePaths.from_base(state_path))
    try:
        state = store.read()
    except Exception:
        # A state file this AgentDeck cannot read is a fact to show, not a page that fails.
        view.errors = ("durable state could not be read",)
        view.exists = True
        return view

    view.exists = True
    record = engine_health.record(state)
    view.health = engine_health.evaluate(state, now=moment, interval_s=interval_s)
    view.paused = engine_health.is_paused(state)
    view.pause_note = engine_health.pause_note(state)
    view.last_cycle_at = str(record.get("last_cycle_at") or "")
    view.age_seconds = engine_health.age_seconds(record, moment)

    last = record.get("last_report") or {}
    view.budget_holding = bool(last.get("budget_holds"))
    view.errors = tuple(str(item) for item in last.get("errors") or ())
    view.degraded_extensions = tuple(
        f"{name}: {value.get('reason') or 'degraded'}"
        for name, value in sorted((last.get("extensions") or {}).items())
        if isinstance(value, dict) and value.get("state") == "degraded"
    )
    view.last_error = str(record.get("last_error") or "")
    view.last_error_age_seconds = engine_health.age_seconds(
        {"last_cycle_at": record.get("last_error_at")}, moment
    )
    view.last_error_current = int(record.get("error_streak") or 0) > 0
    view.working = tuple(
        key for key, card in (state.get("issues") or {}).items()
        if card.get("status") == "In progress"
    )
    # A card waiting on a dependency looks exactly like an idle one from outside, which is the
    # one way this could read as a broken poller rather than a working one (#640). Only cards
    # that have never started are shown, because that is the only state the hold applies to.
    view.blocked = tuple(
        {"card": key, "by": _card_blockers(card)}
        for key, card in (state.get("issues") or {}).items()
        if isinstance(card, dict) and _card_blockers(card)
    )
    view.queue = tuple(
        {
            "id": command.get("id"),
            "card": f"{command.get('repo')}#{command.get('number')}",
            "kind": command.get("kind"),
            "status": command.get("status"),
            "author": command.get("author"),
        }
        for command in engine_queue.pending(state, statuses={"new", "queued", "processing"})
    )
    view.activation = _read_json(base / "activation.json")
    # Read through the one normalizer, so a record the staged activation wrote before #2157
    # still shows who activated it without the project having to be applied again.
    audit = activation.audit_view(view.activation)
    view.version = {
        "contract": audit["contract"],
        "config": audit["config_fingerprint"],
        "approved_by": audit["activated_by"],
    }
    return view


def _card_review_passed_heads(card: dict) -> tuple[str, ...]:
    """The commits this card's review bit was green for when the engine last read it (#2121).

    Empty unless that reading positively found the bit green: red and unknown both say
    nothing here, and unknown is never green anywhere else either.

    The heads are the reading's own, so a caller must still check its commit is among them —
    a reading taken before the head moved covers a commit that is no longer what would merge.
    Written by ``poller_engine.release.record_bits``; every field is re-read defensively
    because this is engine state on disk, which a rollback or an older engine can shape
    differently, and an unreadable reading must say nothing rather than crash a row.
    """
    record = card.get("release_bits")
    if not isinstance(record, dict):
        return ()
    bits = record.get("bits")
    if not isinstance(bits, dict) or bits.get("review-passed") is not True:
        return ()
    heads = record.get("heads")
    if not isinstance(heads, dict):
        return ()
    return tuple(str(sha) for sha in heads.values() if sha)


def _card_release_reading(card: dict, org: str) -> tuple[tuple[tuple[str, str], ...], bool]:
    """The engine's last release-bits reading (#2121): ``((pull request URL, head), ...)`` for
    every pull request it read, and whether it positively found the review bit red (#2405).

    The pull requests are the ones the engine itself links to the card, which can differ from
    the ones the deck finds, so a reader can tell a pull request the engine never reads. Empty
    and False when there is no reading or it cannot be read; unknown is never red.
    """
    record = card.get("release_bits")
    if not isinstance(record, dict):
        return (), False
    heads = record.get("heads")
    bits = record.get("bits")
    if not isinstance(heads, dict) or not isinstance(bits, dict):
        return (), False
    found = []
    for ref, sha in heads.items():
        repo, _, number = str(ref).strip().rpartition("#")
        if not repo or not number.isdigit() or not isinstance(sha, str) or not sha:
            continue
        found.append((f"https://github.com/{_qualified(org, repo)}/pull/{int(number)}", sha))
    if not found:
        return (), False
    return tuple(sorted(found)), bits.get("review-passed") is False


def _card_untested_heads(card: dict, org: str) -> tuple[tuple[str, str], ...]:
    """The pull requests the engine's last reading found no green run on (#2718), as
    ``((URL, head), ...)``. Empty unless that reading positively found the tests bit red.

    Engine state on disk, read defensively like the rest of the reading: anything unreadable
    says nothing. A reader must still check each head is the pull request's current one.
    """
    record = card.get("release_bits")
    if not isinstance(record, dict):
        return ()
    bits = record.get("bits")
    untested = record.get("untested")
    if not isinstance(bits, dict) or bits.get("tests-pass") is not False:
        return ()
    if not isinstance(untested, dict):
        return ()
    found = []
    for ref, sha in untested.items():
        repo, _, number = str(ref).strip().rpartition("#")
        if not repo or not number.isdigit() or not isinstance(sha, str) or not sha:
            continue
        found.append((f"https://github.com/{_qualified(org, repo)}/pull/{int(number)}", sha))
    return tuple(sorted(found))


def _card_review_pass_heads(card: dict) -> tuple[str, ...]:
    """The commits the engine's kept review pass (``release_review_pass``) was recorded at.

    Unlike ``_card_review_passed_heads`` this is the stored pass, not a wake's reading of the
    bits, so it is still there while the card rests waiting on a blocker (#2367). It is shown,
    never acted on: only a head still among these is covered by it.
    """
    record = card.get("release_review_pass")
    heads = record.get("heads") if isinstance(record, dict) else None
    if not isinstance(heads, dict):
        return ()
    return tuple(str(sha) for sha in heads.values() if sha)


def _card_review_offer(card: dict) -> str:
    """``reviewing``, ``rejected`` or ``""`` for the live worker's offered head (#2350).

    Engine state on disk, read defensively. Only the current generation's offer on a card still
    in progress means anything: an offer outlives its worker in the record until the next
    dispatch clears it, and a resting card's review is read the ordinary way.
    """
    offer = card.get("ready_for_review")
    if not isinstance(offer, dict) or card.get("status") != "In progress":
        return ""
    generation = str(card.get("generation") or "")
    if not generation or str(offer.get("generation") or "") != generation:
        return ""
    if not isinstance(offer.get("heads"), dict) or not offer["heads"]:
        return ""
    return "rejected" if offer.get("rejected") else "reviewing"


def _card_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _card_release_blocked(card: dict) -> str:
    """Why the engine means to block the release for a person (``blocked_pending``), or ""."""
    pending = card.get("blocked_pending")
    if not isinstance(pending, dict):
        return ""
    return str(pending.get("why") or "").strip() or "the release is blocked"


# Record fields that mean a release or a dispatch is in flight again, so a park is over.
_RELEASE_MOVING = (
    "dispatch", "mode", "blocked_pending", "done_pending", "merge_start_pending",
    "awaiting_merge", "awaiting_deploy", "fix", "dispatch_failed",
)


def _same_status(card: dict, status: object) -> bool:
    return str(status or "").strip().casefold() == str(card.get("status") or "").strip().casefold()


def _card_release_parked(card: dict) -> str:
    """Why the engine parked this card's release, while that exact park is still current.

    ``_block_release`` stamps the reason with the column it parked in and the lease it parked
    under. A later Run, a column move or a release starting again ends it, so a reason from an
    earlier park is never read as the current one (#2235).
    """
    parked = card.get("release_blocked")
    if not isinstance(parked, dict) or any(card.get(field) for field in _RELEASE_MOVING):
        return ""
    why = str(parked.get("why") or "").strip()
    if not why or not _same_status(card, parked.get("status")):
        return ""
    if str(parked.get("generation") or "") != str(card.get("generation") or ""):
        return ""
    return why


def _card_parked_as(card: dict, role: str) -> str:
    """What a Needs More Info park stands in for, while the card still rests there (#2235).

    ``human_review_pending``: a worker's finish the board had no column for. ``prompt``: a
    worker stopped on a person's prompt in its chat, which the engine parks by itself (#2887).
    """
    parked_as = str(card.get("parked_as") or "")
    if (
        parked_as not in {"human_review_pending", "prompt"}
        or role != "needs_more_info"
        or card.get("dispatch")
    ):
        return ""
    return parked_as


def _external_review(reviews: ExternalReviews | None, pull_request_url: str) -> dict:
    """``BoardCard`` fields for one card's pull request under the instance's reviewer.

    ``reviews`` is ``poller_nudge.read_external_reviews``: the one reader of the extension's
    own state, which honours the settlement it records rather than recompiling its policy.
    """
    parts = pull_request_url.rstrip("/").split("/")
    if reviews is None or len(parts) < 4 or parts[-2] != "pull" or not parts[-1].isdigit():
        return {}
    verdict, detail = reviews.judge(f"{parts[-4]}/{parts[-3]}", int(parts[-1]))
    if verdict == "awaiting":
        return {"external_review": "awaiting", "external_review_chat": detail}
    return {"external_review": verdict}


def _card_blockers(card: dict) -> tuple[str, ...]:
    """Everything one card is currently waiting on another card for, in one list.

    Two holds reach here and a reader does not care which is which: the dependency an
    unstarted card declared (#640), and the one its worker reported when it stopped (#1230).
    Both answer "this card is not idle, it is waiting on that one" — the state that would
    otherwise read as a broken poller. A card that has started stops carrying the first while
    it works, since a dependency then gates nothing — except while it rests in Blocked: the
    engine resumes that card by itself once those dependencies close (#1588), so it is waiting
    on them, not on a person (#2241). The order below is just the record's.

    A third source used to reach here: the card writing the same files this one would. That
    gate is gone (#2081), so colliding cards both run and neither is blocked by the other.
    """
    if not isinstance(card, dict):
        return ()
    gating = not card.get("started") or card.get("status") == Role.BLOCKED.value
    declared = (card.get("blocked_by") or ()) if gating else ()
    return tuple(
        str(ref)
        for ref in dict.fromkeys((*declared, *(card.get("blocked_until") or ())))
        if str(ref)
    )


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def read_all(root: str | Path, **kwargs) -> list[InstanceView]:
    """Every installed instance, in name order."""
    return [read_instance(root, name, **kwargs) for name in _instance_names(root)]


def _instance_names(root: str | Path) -> list[str]:
    instances = Path(root).expanduser() / "instances"
    try:
        return sorted(child.name for child in instances.iterdir() if child.is_dir())
    except OSError:
        return []


# --- reusing a read of the durable poller files while they have not moved ---

#: Every file an engine-published read decodes, per instance: the TOML manifest
#: and the four JSON files a ``StateStore`` load opens.
_DURABLE_INPUTS = (
    "poller.toml",
    # Where the chatgpt-nudge extension's reviewer is configured; its state file, wherever the
    # settings put it, joins the fingerprint in ``_durable_signature`` (#2216).
    "overlay.toml",
    "state.json",
    "state.json.bak",
    "controls.json",
    "controls.json.bak",
)

#: (reader, root) -> (fingerprint, result). One entry per reader per poller root,
#: which in practice is a handful.
_DURABLE_MEMO: dict[tuple[str, ...], tuple[tuple, object]] = {}


#: instance directory -> (its files' fingerprint, the external reviewer's state file).
#: Re-resolved only when that fingerprint moves, so a signature stays a handful of ``stat``
#: calls rather than a TOML parse per read.
_REVIEW_STATE_PATHS: dict[str, tuple[tuple, Path | None]] = {}


def _durable_signature(root: str | Path) -> tuple:
    """A (size, mtime) fingerprint of every durable file these readers decode.

    That includes the external reviewer's state file (#2216) wherever the instance's settings
    put it: ``read_board`` reads it, so a review starting or giving up must be a new board.
    """
    parts: list[tuple] = []
    for instance in _instance_names(root):
        parts.extend(_instance_signature(root, instance))
    return tuple(parts)


def _instance_signature(root: str | Path, instance: str) -> tuple:
    """``_durable_signature``'s part for one instance: its own files and its reviewer's."""
    base_root = Path(root).expanduser() / "instances"
    mine: list[tuple] = []
    for name in _DURABLE_INPUTS:
        try:
            stat = (base_root / instance / name).stat()
        except OSError:
            mine.append((instance, name))
        else:
            mine.append((instance, name, stat.st_size, stat.st_mtime_ns))
    parts = list(mine)
    directory, fingerprint = str(base_root / instance), tuple(mine)
    resolved = _REVIEW_STATE_PATHS.get(directory)
    if resolved is None or resolved[0] != fingerprint:
        from . import poller_nudge  # imports this module: resolved at call time

        path = poller_nudge.external_review_state_file(root, instance)[1]
        resolved = _REVIEW_STATE_PATHS[directory] = (fingerprint, path)
    state_file = resolved[1]
    if state_file is not None:
        try:
            stat = state_file.stat()
        except OSError:
            parts.append((instance, str(state_file)))
        else:
            parts.append((instance, str(state_file), stat.st_size, stat.st_mtime_ns))
    return tuple(parts)


def _durable_memo[T](name: str, root: str | Path, compute: Callable[[str | Path], T]) -> T:
    """``compute(root)``, reused for as long as the files it reads have not moved.

    The engine republishes these on its wake cycle — every ~30 s on this host —
    with ``os.replace`` of a fresh temp file, so size and mtime move whenever the
    content does. The deck reads them far more often than that: once per attached
    chat stream every five seconds, and again on every triage refresh. Stating
    all five instances costs ~100 us against ~11 ms to decode the 1.6 MB they
    hold, so asking before decoding is the whole saving (#1362).

    Only readers whose result is immutable and content-determined may use this.
    The fingerprint is taken *before* the decode, so a view built while a writer
    was mid-publish is stored under the pre-write fingerprint and replaced by the
    next call rather than served on.
    """
    key = (name, str(Path(root).expanduser()))
    signature = _durable_signature(root)
    remembered = _DURABLE_MEMO.get(key)
    if remembered is not None and remembered[0] == signature:
        return remembered[1]  # type: ignore[return-value]
    value = compute(root)
    _DURABLE_MEMO[key] = (signature, value)
    return value


def forget_durable_reads() -> None:
    """Drop every memoized read. For tests that rewrite state within one mtime tick."""
    _DURABLE_MEMO.clear()
    _REVIEW_STATE_PATHS.clear()


# --- the board as a read model: which repositories hold issues, and each card's status ---


@dataclass(frozen=True)
class BoardScope:
    """What one instance's published manifest says about where its issues live."""

    instance: str
    org: str = ""
    # No GitHub Project behind this instance: card status is a `status:*` label (#1481). The
    # deck reads status out of engine state either way, so this only says which fact makes the
    # scope complete — a boardless instance has no project number to be missing.
    boardless: bool = False
    project_number: int = 0
    ownership_label: str = ""
    ownership_labels: tuple[str, ...] = ()
    # The label that limits a card to a written spec (``board.spec_only_label``), or empty.
    spec_only_label: str = ""
    # ``owner/repo`` for every ``[[repos]]`` entry with ``holds_issues = true``.
    issue_repositories: tuple[str, ...] = ()
    # ``owner/repo`` of the repository declared ``holds_artifacts``, and each issue
    # repository that borrows another's checkout paired with the one it borrows (#2143).
    artifact_repositories: tuple[str, ...] = ()
    borrowed_workspaces: tuple[tuple[str, str], ...] = ()
    cards: tuple[BoardCard, ...] = ()
    readable: bool = True
    state_readable: bool = True
    # The instance's ``state.json``, set only when it was read: where a deck request for one of
    # its repositories goes when the card itself is not in state any more (#2748).
    state_path: str = ""

    @property
    def usable(self) -> bool:
        return self.readable and bool(self.org)

    @property
    def complete(self) -> bool:
        backend = self.boardless or self.project_number > 0
        return self.usable and backend and self.state_readable


@dataclass(frozen=True)
class BoardDispatch:
    """The card's outstanding dispatch, as the engine durably recorded it (#2141).

    Present only while a delivery is in flight: the engine drops it once the host
    acknowledges the delivery or the card is parked. ``run_id`` is the generation (#2142).
    """

    id: str
    action: str = ""
    run_id: str = ""
    work_id: str = ""
    attempts: int = 0
    sent_at: str = ""
    refusal: str = ""  # the host's name for the latest positive refusal, if any
    clean: bool | None = None  # None: no attempt has ended yet
    steer: bool = False
    continuation: bool = False
    supersedes: str = ""


@dataclass(frozen=True)
class BoardCard:
    """One card the engine is tracking, as the deck may read it.

    Board status is deliberately taken from the engine's own durable state rather
    than re-queried from GitHub Projects: the engine already writes what column it
    put the card in, and a second Projects query from the web process would spend
    the very GraphQL quota the manifest's ``graphql_reserve`` is holding back.
    """

    repository: str  # owner/repo, as the manifest spells the owner
    number: int
    status: str = ""
    in_review: bool = False
    done: bool = False  # the card reached the project's own done column
    ownership: bool = False  # the engine holds this card (ownership label applied)
    started: bool = False
    # The pull request the worker reported through the instance interface. The
    # engine wrote it from the worker's own words, so it costs no GitHub read —
    # and it names a pull request in another repository, which a head-branch
    # guess cannot (#1031). It is a link, not a state: whether it is still open
    # has to be confirmed elsewhere.
    pull_request_url: str = ""
    # Every pull request the card names (#2226): ``pull_request_url`` first, then the
    # engine's ``review_prs``. A card is judged by all of them, not only the first.
    pull_requests: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    worktrees: tuple[str, ...] = ()
    # The commits the engine's own review bit is currently green for (#2121). Empty on every
    # card whose last bits reading was red, unknown, or never taken. This is not the same
    # question as "is there a verdict for this commit": the engine carries a pass across a
    # rebase when the diff is byte-identical, so a commit can be covered here that no verdict
    # was ever recorded against — and the deck cannot work that out for itself, because the
    # digests it turns on cost GitHub reads a row must not make.
    review_passed_heads: tuple[str, ...] = ()
    # The commits the engine's kept review pass was recorded at (#2367): display only.
    review_pass_heads: tuple[str, ...] = ()
    # Where a head the card's live worker offered for review has got to (#2350): ``reviewing``
    # while the outside reviewer reads it beside the worker's verification, ``rejected`` once
    # that review sent it back, empty otherwise — and always empty once the worker is gone.
    review_offer: str = ""
    # What the engine's release machinery is doing with the card (#2216), read straight from
    # its state so a Work can say where review has got to without a second state machine:
    # ``mode`` is ``merge-review`` while a reviewer is armed or running and ``merge`` once a
    # pass is being released; ``release_blocked`` is the engine's durable intent to hand the
    # release to a person. ``status_role`` is the role of a column a review can leave the card in.
    review_mode: str = ""
    release_blocked: str = ""
    status_role: str = ""
    # A release the bits started (``auto_release``), and a human ``/merge review`` approval
    # bound to heads (``review_heads``): these are what make a merge one that followed a
    # review. A plain ``/merge`` carries none of them (#2216 review).
    auto_release: bool = False
    review_approved: bool = False
    # The card's project asks for a missing verification itself (#2364).
    verifies_itself: bool = False
    # The card's project merges a passed review on its own (``auto_release``, #2101); without
    # it a pass waits for a person's ``/merge`` (#2399).
    releases_itself: bool = False
    # The heads the engine last asked a verify turn for (``rec["verify_asked"]``, #2364).
    verify_asked_heads: tuple[str, ...] = ()
    # The heads an outside reviewer gave a verdict on that the engine has not recorded yet:
    # a ``reviewed`` request waiting for the next wake (#2760).
    verdicts_on_the_way: tuple[str, ...] = ()
    # The card's repositories whose CI is the verification, as ``owner/repo`` (#2364), and the
    # engine's last CI reading per pull request: ``(owner/repo, number, head, state)``.
    ci_repos: tuple[str, ...] = ()
    ci_checks: tuple[tuple[str, int, str, str], ...] = ()
    # The card's repositories with no tests, whose review pass on a head is its verification
    # (``verification = "review_only"``, #2729), as ``owner/repo`` (#2832).
    review_only_repos: tuple[str, ...] = ()
    # The card's repositories that deploy what merges (``auto_deploys``), as ``owner/repo``:
    # the engine watches a deploy after it merges there (#2760).
    deploy_repos: tuple[str, ...] = ()
    # The engine's last release-bits reading (#2405): each pull request it read, as
    # ``(URL, head)``, and whether that reading found the review bit red. The engine asks for a
    # verify turn only on a green review bit, and reads CI only for the pull requests it links.
    release_reading: tuple[tuple[str, str], ...] = ()
    review_bit_red: bool = False
    # The pull requests that reading found no green run on, as ``(URL, head)`` (#2718): on a
    # card shipping several, the tests bit can wait on a companion's head nobody ran.
    untested_heads: tuple[tuple[str, str], ...] = ()
    # The external reviewer (``review_program``, #1820) for this card's pull request, from the
    # chatgpt-nudge extension's state: "" when the instance has none or the card no pull
    # request, else ``awaiting`` (a review chat is open), ``given_up`` (chats kept closing
    # without reviewing) or ``configured`` (none open now). ``external_review_chat`` is the
    # open chat, ``owner/repo#n``.
    external_review: str = ""
    external_review_chat: str = ""
    # The instance's reviewer itself (``poller_nudge.ExternalReviews``), so a reader can judge
    # every pull request the card names against the pull request's current head (#2226).
    reviewer: ExternalReviews | None = field(default=None, compare=False, hash=False, repr=False)
    # What the Run trace reads (#2141): the card's current lease (its Run), its outstanding
    # dispatch, and whether a spawn that never landed parked it. ``state_path`` and
    # ``state_key`` locate the host's receipts for this card; neither is identity.
    generation: str = ""
    # When the engine last started the card: the earliest the current Run can be. Used only to
    # tell a Run from before the Runtime recorded anything from a Run it should have (#2201).
    started_at: str = ""
    # When the engine last looked at the card's issue on GitHub: an issue closed after it is
    # closed whatever column the card still rests in (#2226).
    last_live_check: str = ""
    dispatch: BoardDispatch | None = None
    dispatch_failed: bool = False
    dispatch_failed_clean: bool = False
    dispatch_failed_at: str = ""
    dispatch_failed_refusal: str = ""  # the host's last positive refusal before the park
    dispatch_failed_refusal_final: bool = False  # ...and the final attempt was that refusal
    dispatch_rearmed: bool = False  # the park's one free retry is already spent
    # How many times the engine has already restarted this card's worker after it ended
    # without a report (``revive.py``); an ordinary dispatch starts the count over.
    revives: int = 0
    # Why the engine parked the release, kept while that exact park is current (#2235); the
    # pending intent above is gone once the park lands.
    release_parked: str = ""
    # ``human_review_pending`` while a Needs More Info park stands in for that missing column.
    parked_as: str = ""
    # The pull requests the engine recorded as merged for this card (``rec["merged"]``), as
    # URLs: the engine's word outlasts a cached row that still says open (#2332).
    merged: tuple[str, ...] = ()
    # The step pull requests the card merged and carried on past (``rec["steps_merged"]``),
    # as URLs (#2730): merged, yet nothing for a person to close.
    steps_merged: tuple[str, ...] = ()
    # The engine is watching for a deploy of this card's merge (``awaiting_deploy``), and
    # since when that deploy could first be seen; and whether a failed deploy is being
    # re-read (``deploy_recheck``) (#2332).
    awaiting_deploy: bool = False
    deploy_since: str = ""
    deploy_recheck: bool = False
    # What the current lease's worker last told a person in its terminal comment (#2332), ""
    # when that comment was from another lease or never read.
    park_note: str = ""
    # The engine's last read found the issue paused (``rec["paused"]``, #2548), and the label
    # that pauses this instance's cards, "" when its Manifest names none.
    paused: bool = False
    paused_label: str = ""
    state_path: str = ""
    state_key: str = ""

    @property
    def identity(self) -> str:
        return f"{self.repository.lower()}#{self.number}"

    @property
    def revives_left(self) -> int:
        """How many more reportless endings the engine restarts before it asks a person."""
        return max(MAX_REVIVES - self.revives, 0)

    def as_json(self) -> dict:
        return {
            "repository": self.repository,
            "number": self.number,
            "status": self.status,
            "in_review": self.in_review,
            "done": self.done,
            "ownership": self.ownership,
            "started": self.started,
            "pull_request_url": self.pull_request_url,
            "blocked_by": list(self.blocked_by),
            "worktrees": list(self.worktrees),
        }


def _resolved(path: str | Path) -> Path | None:
    """A comparable form of a recorded path — ``/home`` and ``/var/home`` are one
    directory on this host, and a card and a session can name it either way."""
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


@dataclass(frozen=True)
class BoardView:
    """Every instance's cards in one mapping, plus whether the read was whole.

    ``complete`` carries the same rule ``GitContexts`` states for pull requests:
    **an incomplete lookup is not absence.** An instance whose manifest or state
    file could not be read leaves ``complete`` false, so a caller reports its
    cards as *unknown* rather than reporting that nothing is on the board.
    """

    cards: Mapping[str, BoardCard] = field(default_factory=dict)
    scopes: tuple[BoardScope, ...] = ()
    complete: bool = True

    def card(self, repository: str, number: int) -> BoardCard | None:
        return self.cards.get(f"{repository.lower()}#{number}")

    @property
    def issue_repositories(self) -> tuple[str, ...]:
        """Every ``owner/repo`` any instance declares with ``holds_issues``, deduped."""
        seen: dict[str, str] = {}
        for scope in self.scopes:
            for repository in scope.issue_repositories:
                seen.setdefault(repository.lower(), repository)
        return tuple(seen[key] for key in sorted(seen))

    def worktrees_beside(self, path: str | Path | None) -> tuple[str, ...]:
        """The other checkouts the card owning ``path`` records, ``path`` excluded.

        A worker is launched in one worktree and may register more — often in a
        different repository, which is where the work then happens. The card is
        the only record of that, so a reader asking "what has this chat changed?"
        has to start from the card rather than from the launch directory (#1120).

        Matched on the directory rather than on a session key because the card
        stores the paths and the launch cwd identifies one of them; a path no
        card claims simply yields nothing.
        """
        if path is None:
            return ()
        wanted = _resolved(path)
        if wanted is None:
            return ()
        found: list[str] = []
        for card in self.cards.values():
            resolved = {item: _resolved(item) for item in card.worktrees}
            if wanted not in resolved.values():
                continue
            for item, item_path in resolved.items():
                if item_path != wanted and item not in found:
                    found.append(item)
        return tuple(found)

    def managing(self, repository: str) -> tuple[BoardScope, ...]:
        """Every readable instance whose manifest holds ``repository``'s issues (#2748).

        A closed card the engine has released is gone from state, so ``card`` finds nothing;
        a person's ``/claude`` on GitHub still reaches it through the instance that reads that
        repository, and a deck request has to find the same instance.
        """
        lowered = repository.lower()
        return tuple(
            scope
            for scope in self.scopes
            if scope.state_path
            and any(item.lower() == lowered for item in scope.issue_repositories)
        )

    def ownership_label(self, repository: str) -> str:
        """The label that hands work to the engine for this repository, if declared."""
        lowered = repository.lower()
        for scope in self.scopes:
            if any(item.lower() == lowered for item in scope.issue_repositories):
                return scope.ownership_label
        return ""

    def ownership_labels(self, repository: str) -> tuple[str, ...]:
        """Every label that hands work to the engine for this repository."""
        lowered = repository.lower()
        for scope in self.scopes:
            if any(item.lower() == lowered for item in scope.issue_repositories):
                return scope.ownership_labels or (
                    (scope.ownership_label,) if scope.ownership_label else ()
                )
        return ()


# The columns a card can rest in while or after it is reviewed, by role, with the engine's own
# default spelling (``poller_engine.config.Role``). Human review pending is the one only a
# person writes; Ready to merge is where a review that asked for changes or a failed merge
# lands; Blocked and Needs More Info are where a reviewer can park the card.
_REVIEW_ROLE_DEFAULTS = (
    ("in_review", "In review"),
    ("ready_to_merge", "Ready to merge"),
    ("human_review_pending", "Human review pending"),
    ("blocked", "Blocked"),
    ("needs_more_info", "Needs More Info"),
)


@dataclass(frozen=True)
class _ManifestFacts:
    org: str = ""
    boardless: bool = False
    project_number: int = 0
    ownership_label: str = ""
    ownership_labels: tuple[str, ...] = ()
    spec_only_label: str = ""
    paused_label: str = ""
    in_review_status: str = "In review"
    done_status: str = "Done"
    # The board's own spelling, casefolded, of each column a review can leave a card resting
    # in, keyed to its role name (#2216). Only these roles: a Work reads review from them.
    review_roles: tuple[tuple[str, str], ...] = ()
    issue_repos: tuple[str, ...] = ()
    artifact_repos: tuple[str, ...] = ()
    borrowed: tuple[tuple[str, str], ...] = ()
    # The engine asks for a missing verification itself (the verify turn ``auto_release`` or
    # ``verify_turn`` switches on, #2364/#2375), so a reviewed card with no green run waits
    # on the deck, not on a person.
    verifies_itself: bool = False
    # The engine merges a passed review itself (``auto_release``, #2101). Without it a pass
    # waits for a person's ``/merge``, never for the deck (#2399).
    releases_itself: bool = False
    # The repositories whose GitHub checks are the verification (``verification =
    # "github_checks"``, #2364), as the manifest names them.
    ci_repos: tuple[str, ...] = ()
    # The repositories whose review pass is the verification (``verification =
    # "review_only"``, #2729, #2832), as the manifest names them.
    review_only_repos: tuple[str, ...] = ()
    # The repositories that deploy what merges (``auto_deploys``, #2760).
    deploy_repos: tuple[str, ...] = ()
    # Every ``[[repos]]`` id, issue repository or not, as the manifest names it.
    repos: tuple[str, ...] = ()
    # The project's Works hear their own GitHub events (``works_router``, #2895).
    works_router: bool = False
    # ``[policy] merge_method``: how a release merges (#2895 step 2 merges in the deck).
    merge_method: str = "merge"
    # The ``[authorization]`` slot as the engine parses it (``authorization.parse_policy``):
    # who holds each tier, for the deck's own release (#2895). ``None`` when absent.
    authorization: Any = None
    # ``[staging]`` (#3180): the project's branch staging copies, and which of its
    # repositories deploy into them. ``None`` when the manifest declares none.
    staging: Any = None
    staging_repos: tuple[str, ...] = ()
    readable: bool = True


def _card_ci_checks(card: dict, org: str) -> tuple[tuple[str, int, str, str], ...]:
    """The engine's CI reading per pull request (``rec["ci_checks"]``, #2364), defensively."""
    raw = card.get("ci_checks")
    if not isinstance(raw, dict):
        return ()
    found = []
    for ref, reading in raw.items():
        repo, _, number = str(ref).rpartition("#")
        if not repo or not number.isdigit() or not isinstance(reading, dict):
            continue
        head, state = reading.get("head"), reading.get("state")
        if not isinstance(head, str) or not head or not isinstance(state, str):
            continue
        repository = repo if "/" in repo else f"{org}/{repo}"
        found.append((repository, int(number), head, state))
    return tuple(sorted(found))


def _card_verify_asked(card: dict) -> tuple[str, ...]:
    asked = card.get("verify_asked")
    if not isinstance(asked, dict):
        return ()
    return tuple(str(sha) for sha in asked.values() if isinstance(sha, str) and sha)


def _verdicts_on_the_way(state: dict, state_path: Path) -> dict[str, tuple[str, ...]]:
    """Each card's heads an outside reviewer gave a verdict on that the engine has not recorded
    yet (#2760), by the engine's card key: a request still waiting beside ``state.json``, or one
    the ledger took and has not acted on."""
    found: dict[str, list[str]] = {}
    waiting = deck_releases.waiting_verdicts(deck_releases.directory(state_path))
    ledger = [
        control
        for control in (state.get("controls") or {}).values()
        if isinstance(control, dict)
        and control.get("kind") == "reviewed"
        and control.get("status") == "new"
    ]
    for control in (*waiting, *ledger):
        payload = control.get("payload")
        head = str(payload.get("head") or "") if isinstance(payload, dict) else ""
        if len(head) >= 7:
            key = f"{control.get('repo') or ''}#{control.get('number') or ''}"
            found.setdefault(key, []).append(head)
    return {key: tuple(heads) for key, heads in found.items()}


def _manifest(path: Path) -> _ManifestFacts:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, ValueError):  # TOMLDecodeError is a ValueError
        return _ManifestFacts(readable=False)
    boardless = "board" not in raw
    board = raw.get("board")
    # No [board] table is the boardless declaration (#1481): the project keeps card status in
    # `status:*` labels, and what survives the table's absence is authored at the top level.
    if boardless:
        board = raw
    elif not isinstance(board, dict):
        # Match the authoritative config parser: a present malformed board is invalid, not an
        # alternate spelling of the boardless declaration.
        return _ManifestFacts(readable=False)
    org = board.get("org")
    project_number = 0 if boardless else board.get("project_number")
    ownership = board.get("ownership_label")
    configured = [ownership] if isinstance(ownership, str) else ownership
    ownership_labels = tuple(
        item.strip()
        for item in (configured if isinstance(configured, list) else [])
        if isinstance(item, str) and item.strip()
    ) or (DEFAULT_OWNERSHIP_LABEL,)
    spec_only = board.get("spec_only_label")
    paused = board.get("paused_label")
    statuses = board.get("statuses")
    statuses = statuses if isinstance(statuses, dict) else {}
    in_review = statuses.get("in_review", "In review")
    done = statuses.get("done", "Done")
    review_roles = tuple(
        (spelling.strip().casefold(), role)
        for role, default in _REVIEW_ROLE_DEFAULTS
        if isinstance(spelling := statuses.get(role, default), str) and spelling.strip()
    )
    capabilities = raw.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    policy = raw.get("policy")
    merge_method = policy.get("merge_method") if isinstance(policy, dict) else None
    authorization = raw.get("authorization")
    repos = raw.get("repos")
    issue_repos = tuple(
        str(entry.get("id")).strip()
        for entry in (repos if isinstance(repos, list) else [])
        if isinstance(entry, dict) and entry.get("holds_issues") and entry.get("id")
    )
    return _ManifestFacts(
        org=org.strip() if isinstance(org, str) else "",
        boardless=boardless,
        project_number=(
            project_number
            if isinstance(project_number, int) and not isinstance(project_number, bool)
            else 0
        ),
        # A project that names no ownership label still has one: the engine's own
        # default is what decides which cards it picks up, so the deck must read
        # the same value rather than reporting that the project declared nothing.
        ownership_label=ownership_labels[0],
        ownership_labels=ownership_labels,
        spec_only_label=spec_only.strip() if isinstance(spec_only, str) else "",
        paused_label=paused.strip() if isinstance(paused, str) else "",
        in_review_status=(
            in_review.strip()
            if isinstance(in_review, str) and in_review.strip()
            else "In review"
        ),
        # The column a finished card ends in. Read from the project rather than
        # assumed, so a board that renames it does not leave finished cards
        # sitting on the front page forever (#1025).
        done_status=(
            done.strip() if isinstance(done, str) and done.strip() else "Done"
        ),
        review_roles=review_roles,
        issue_repos=issue_repos,
        verifies_itself=verify_turn(capabilities),
        releases_itself=bool(capabilities.get(AUTO_RELEASE)),
        merge_method=(
            merge_method if merge_method in {"merge", "squash", "rebase"} else "merge"
        ),
        authorization=_authorization(authorization),
        **_staging(raw.get("staging")),
        ci_repos=tuple(
            str(entry.get("id")).strip()
            for entry in (repos if isinstance(repos, list) else [])
            if isinstance(entry, dict)
            and entry.get("id")
            and str(entry.get("verification") or "").strip() == "github_checks"
        ),
        review_only_repos=tuple(
            str(entry.get("id")).strip()
            for entry in (repos if isinstance(repos, list) else [])
            if isinstance(entry, dict)
            and entry.get("id")
            and str(entry.get("verification") or "").strip() == "review_only"
        ),
        artifact_repos=tuple(
            str(entry.get("id")).strip()
            for entry in (repos if isinstance(repos, list) else [])
            if isinstance(entry, dict) and entry.get("holds_artifacts") and entry.get("id")
        ),
        deploy_repos=tuple(
            str(entry.get("id")).strip()
            for entry in (repos if isinstance(repos, list) else [])
            if isinstance(entry, dict) and entry.get("auto_deploys") is True and entry.get("id")
        ),
        borrowed=tuple(
            (str(entry.get("id")).strip(), entry["workspace"].strip())
            for entry in (repos if isinstance(repos, list) else [])
            if isinstance(entry, dict)
            and entry.get("id")
            and isinstance(entry.get("workspace"), str)
            and entry["workspace"].strip()
        ),
        repos=tuple(
            str(entry.get("id")).strip()
            for entry in (repos if isinstance(repos, list) else [])
            if isinstance(entry, dict) and entry.get("id")
        ),
        works_router=capabilities.get("works_router") is True,
        readable=True,
    )


def _qualified(org: str, repo: str) -> str:
    """``owner/repo`` for a manifest repository id, which may name the repo alone."""
    return repo if "/" in repo else f"{org}/{repo}"


@dataclass(frozen=True)
class LabelTarget:
    """One issue repository and every label that hands its issues to the engine."""

    repository: str
    ownership_labels: tuple[str, ...]


def issue_label_targets(root: str | Path) -> tuple[LabelTarget, ...]:
    """Every installed instance's issue repositories with their ownership labels (#2505).

    Read from the manifests alone, never from ``state.json``: the ``gh`` shim asks this
    once per ``gh issue create`` from a fresh process, where ``read_board``'s memo is
    always empty and decoding every instance's state would cost it most of a second.
    Instances come in name order, so a repository two manifests declare keeps the first
    one's canonical label.
    """
    base_root = Path(root).expanduser() / "instances"
    found: list[LabelTarget] = []
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not facts.readable or not facts.org:
            continue
        for repo in facts.issue_repos:
            found.append(LabelTarget(_qualified(facts.org, repo), facts.ownership_labels))
    return tuple(found)


def works_router_repositories(root: str | Path) -> frozenset[str]:
    """Lowercased ``owner/repo`` of every repository a ``works_router`` project declares (#2895).

    Manifest-only, like ``issue_label_targets``: the Work event router asks this for the
    repository of each delivery, and a project is switched by its own manifest, never by
    what its engine last wrote.
    """
    base_root = Path(root).expanduser() / "instances"
    found: set[str] = set()
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if facts.readable and facts.org and facts.works_router:
            found.update(_qualified(facts.org, repo).lower() for repo in facts.repos)
    return frozenset(found)


def works_router_start_labels(root: str | Path) -> dict[str, frozenset[str]]:
    """The ownership labels that start a Work, by issue repository of a switched project.

    Lowercased ``owner/repo`` to lowercased labels (#2895 step 5b): the deck starts a
    ``works_router`` project's Work when one of these lands on an issue no chat owns yet.
    Manifest-only, as ``works_router_repositories`` is.
    """
    base_root = Path(root).expanduser() / "instances"
    found: dict[str, frozenset[str]] = {}
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not (facts.readable and facts.org and facts.works_router):
            continue
        labels = frozenset(label.lower() for label in facts.ownership_labels)
        for repo in facts.issue_repos:
            found.setdefault(_qualified(facts.org, repo).lower(), labels)
    return found


def works_router_hold_labels(root: str | Path) -> dict[str, frozenset[str]]:
    """The labels that keep a switched project's Work from starting, by issue repository.

    Lowercased ``owner/repo`` to its manifest's lowercased ``paused_label`` (#2951): a person
    put it on the issue to hold it back, so the missed-start backstop leaves it alone. Not the
    ``spec_only_label``: a spec-only issue starts an agent that writes the spec (#3134).
    Manifest-only, as ``works_router_start_labels`` is.
    """
    base_root = Path(root).expanduser() / "instances"
    found: dict[str, frozenset[str]] = {}
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not (facts.readable and facts.org and facts.works_router):
            continue
        labels = frozenset({facts.paused_label.lower()} if facts.paused_label else ())
        for repo in facts.issue_repos:
            found.setdefault(_qualified(facts.org, repo).lower(), labels)
    return found


def works_router_evidence_repositories(root: str | Path) -> dict[str, str]:
    """Each switched project's evidence repository, by lowercased issue repository (#3098).

    The ``owner/repo`` its manifest declares ``holds_artifacts`` (#1791), where a Work agent
    commits its screenshots; a project that declares none is left out. Manifest-only, as
    ``works_router_start_labels`` is.
    """
    base_root = Path(root).expanduser() / "instances"
    found: dict[str, str] = {}
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not (facts.readable and facts.org and facts.works_router and facts.artifact_repos):
            continue
        evidence = _qualified(facts.org, facts.artifact_repos[0])
        for repo in facts.issue_repos:
            found.setdefault(_qualified(facts.org, repo).lower(), evidence)
    return found


def works_router_accounts(root: str | Path) -> dict[str, tuple[str, ...] | None]:
    """Each ``works_router`` repository's worker accounts, in preference order (#2895).

    The machine-local overlay's ``[worker] account`` then ``fallback_accounts``, as the Poller
    picks its worker's account: the deck starts a switched project's Work on the account the
    project chose (a work project on its work subscription), not on whichever is free first.
    An empty tuple means the project names no preference. ``None`` means its overlay did not
    answer, so a caller must fail closed rather than choose an account the project may not name.
    """
    base_root = Path(root).expanduser() / "instances"
    found: dict[str, tuple[str, ...] | None] = {}
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not (facts.readable and facts.org and facts.works_router):
            continue
        try:
            with (base_root / instance / "overlay.toml").open("rb") as handle:
                worker = tomllib.load(handle).get("worker")
        except (OSError, tomllib.TOMLDecodeError):
            ordered = None
        else:
            if worker is None:
                ordered = ()
            elif not isinstance(worker, dict):
                ordered = None
            else:
                first = worker.get("account")
                rest = worker.get("fallback_accounts")
                valid = (first is None or isinstance(first, str)) and (
                    rest is None
                    or isinstance(rest, list)
                    and all(isinstance(key, str) for key in rest)
                )
                keys = [first] if isinstance(first, str) else []
                keys += rest if isinstance(rest, list) else []
                ordered = (
                    tuple(dict.fromkeys(key.strip() for key in keys if key.strip()))
                    if valid
                    else None
                )
        for repo in facts.issue_repos:
            found.setdefault(_qualified(facts.org, repo).lower(), ordered)
    return found


def _staging(raw: object) -> dict[str, Any]:
    """The manifest's ``[staging]`` table (#3180), or nothing when it is absent or unusable.

    ``environment_prefix`` starts the GitHub deployment environment of each branch copy,
    ``repos`` are the repositories that deploy into one, and ``links`` the copy's addresses as
    ``{label, url}`` tables whose ``url`` holds ``{slug}``.
    """
    from .staging_deploys import StagingSite

    if not isinstance(raw, dict):
        return {}
    prefix = raw.get("environment_prefix")
    repos = raw.get("repos")
    if not isinstance(prefix, str) or not prefix.strip() or not isinstance(repos, list):
        return {}
    links = tuple(
        (entry["label"].strip(), entry["url"].strip())
        for entry in raw.get("links") or ()
        if isinstance(entry, dict)
        and isinstance(entry.get("label"), str)
        and isinstance(entry.get("url"), str)
        and entry["label"].strip()
        and entry["url"].strip().startswith("https://")
    )
    return {
        "staging": StagingSite(prefix.strip(), links),
        "staging_repos": tuple(
            repo.strip() for repo in repos if isinstance(repo, str) and repo.strip()
        ),
    }


def _authorization(raw: object) -> Any:
    """The manifest's ``[authorization]`` slot parsed as the engine parses it."""
    from .poller_engine.authorization import parse_policy

    return parse_policy(raw if isinstance(raw, dict) else {})


def release_policies(root: str | Path) -> dict[str, object]:
    """Each ``works_router`` repository's merge policy, by lowercased ``owner/repo`` (#2895).

    What the deck's own release (``work_release``) reads in place of the engine's loaded
    manifest: ``auto_release``, the merge method, and which of the project's repositories
    are proven by their review alone or by their GitHub checks. Manifest-only, as
    ``works_router_repositories`` is; a repository two switched projects declare takes the
    first one's policy.
    """
    from .work_release import Policy

    base_root = Path(root).expanduser() / "instances"
    found: dict[str, object] = {}
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not (facts.readable and facts.org and facts.works_router):
            continue
        policy = Policy(
            auto_release=facts.releases_itself,
            merge_method=facts.merge_method,
            review_only=frozenset(
                _qualified(facts.org, repo).lower() for repo in facts.review_only_repos
            ),
            github_checks=frozenset(
                _qualified(facts.org, repo).lower() for repo in facts.ci_repos
            ),
            authorization=facts.authorization,
            spec_only_label=facts.spec_only_label,
            paused_label=facts.paused_label,
            deploys=frozenset(
                _qualified(facts.org, repo).lower() for repo in facts.deploy_repos
            ),
            staging=facts.staging,
            staging_repos=frozenset(
                _qualified(facts.org, repo).lower() for repo in facts.staging_repos
            ),
        )
        for repo in facts.repos:
            found.setdefault(_qualified(facts.org, repo).lower(), policy)
    return found


def _card_pull_request(card: dict) -> str:
    """The pull request link a worker reported for this card, if it reported one."""
    links = card.get("comment_links")
    url = links.get("pull_request_url") if isinstance(links, dict) else None
    return url.strip() if isinstance(url, str) else ""


def _card_refs(raw: object, org: str) -> dict[str, str]:
    """``repo#n`` (or ``owner/repo#n``) entries as pull request URLs, by lowercased URL."""
    found: dict[str, str] = {}
    for entry in raw if isinstance(raw, list) else ():
        repo, _, number = str(entry).strip().partition("#")
        if not repo or not number.isdigit() or ("/" not in repo and not org):
            continue
        url = f"https://github.com/{_qualified(org, repo)}/pull/{int(number)}"
        found.setdefault(url.lower(), url)
    return found


def _card_merged(card: dict, org: str) -> tuple[str, ...]:
    """The pull requests the engine recorded as merged for this card (#2332).

    Step pull requests an earlier release merged before the card carried on are kept apart
    on the record (#2730), and are just as much this Work's merged outputs.
    """
    steps = card.get("steps_merged")
    merged = card.get("merged")
    raw = [
        *(steps if isinstance(steps, list) else ()),
        *(merged if isinstance(merged, list) else ()),
    ]
    return tuple(_card_refs(raw, org).values())


def _card_park_note(card: dict) -> str:
    """The engine's copy of the current lease's terminal comment (#2332), else ""."""
    note = card.get("park_note")
    if not isinstance(note, dict) or not card.get("generation"):
        return ""
    if str(note.get("generation") or "") != str(card.get("generation")):
        return ""
    return str(note.get("text") or "")


def _card_deploy_since(card: dict) -> str:
    """Since when the deploy the engine awaits could first be seen, or ""."""
    watch = card.get("awaiting_deploy")
    return str(watch.get("since") or "") if isinstance(watch, dict) else ""


def _card_pull_requests(card: dict, org: str) -> tuple[str, ...]:
    """Every pull request the card names, as URLs: the worker's link, then ``review_prs``.

    ``review_prs`` is the engine's own list of the pull requests a review covers, spelled
    ``repo#n`` (or ``owner/repo#n``); a card can have either, both or neither (#2226).
    """
    found: dict[str, str] = {}
    first = _card_pull_request(card)
    if first:
        found[first.lower()] = first
    for key, url in _card_refs(card.get("review_prs"), org).items():
        found.setdefault(key, url)
    return tuple(found.values())


class _StateUnreadable(Exception):
    """Neither the state file nor its backup parsed — same scope as a failed read."""


def read_board(root: str | Path) -> BoardView:
    """Every installed instance's declared issue repositories and tracked cards.

    Reads only the durable artifacts the engine publishes — the manifest and the
    state file — so this costs no GitHub quota and works between wakes.

    Reused while those files are untouched — see ``_durable_memo``. Everything a
    ``BoardView`` carries is a frozen dataclass derived from their bytes: the
    load stamps timestamps on records it rescues from the ledger, but no board
    field is one of them, so an identical fingerprint means an identical view.
    """
    return _durable_memo("board", root, _read_board)


def _card_dispatch(card: dict) -> BoardDispatch | None:
    raw = card.get("dispatch")
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    clean = raw.get("clean")
    try:
        attempts = int(raw.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    return BoardDispatch(
        id=str(raw.get("id")),
        action=str(raw.get("action") or ""),
        run_id=str(raw.get("run_id") or raw.get("generation") or ""),
        work_id=str(raw.get("work_id") or ""),
        attempts=attempts,
        sent_at=str(raw.get("sent_at") or ""),
        refusal=str(raw.get("refusal") or ""),
        clean=clean if isinstance(clean, bool) else None,
        steer=bool(raw.get("steer")),
        continuation=bool(raw.get("continuation")),
        supersedes=str(raw.get("supersedes") or ""),
    )


def _read_board(root: str | Path) -> BoardView:
    """Assemble the board from each instance's own read, in instance order.

    Each instance is decoded again only when its own files moved (#2841). The instances
    republish on their own wakes, so with a fingerprint over all of them together one or
    another had always just written, and the whole board — about twenty megabytes of JSON
    across eleven instances — was decoded again every few seconds on the chat page's path.
    """
    cards: dict[str, BoardCard] = {}
    scopes: list[BoardScope] = []
    complete = True
    for instance in _instance_names(root):
        scope, instance_cards, readable = _read_board_instance_memo(root, instance)
        scopes.append(scope)
        complete = complete and readable
        for entry in instance_cards:
            cards[entry.identity] = entry
    return BoardView(cards=cards, scopes=tuple(scopes), complete=complete)


def _read_board_instance_memo(
    root: str | Path, instance: str
) -> tuple[BoardScope, tuple[BoardCard, ...], bool]:
    """``_read_board_instance``, reused while that instance's own files are untouched.

    The fingerprint is taken before the decode, as in ``_durable_memo``.
    """
    key = ("board-instance", str(Path(root).expanduser()), instance)
    signature = _instance_signature(root, instance)
    remembered = _DURABLE_MEMO.get(key)
    if remembered is not None and remembered[0] == signature:
        return remembered[1]  # type: ignore[return-value]
    value = _read_board_instance(root, instance)
    _DURABLE_MEMO[key] = (signature, value)
    return value


def _read_board_instance(
    root: str | Path, instance: str
) -> tuple[BoardScope, tuple[BoardCard, ...], bool]:
    """One instance's scope, its cards, and whether its manifest and state were readable."""
    base = Path(root).expanduser() / "instances" / instance
    facts = _manifest(base / "poller.toml")
    repositories = (
        tuple(
            f"{facts.org}/{repo}" if "/" not in repo else repo
            for repo in facts.issue_repos
        )
        if facts.org
        else ()
    )
    artifact_repositories = (
        tuple(_qualified(facts.org, repo) for repo in facts.artifact_repos)
        if facts.org
        else ()
    )
    borrowed_workspaces = (
        tuple(
            (_qualified(facts.org, repo), _qualified(facts.org, code))
            for repo, code in facts.borrowed
        )
        if facts.org
        else ()
    )
    if not facts.readable or not facts.org:
        scope = (
            BoardScope(
                instance=instance,
                org=facts.org,
                boardless=facts.boardless,
                project_number=facts.project_number,
                ownership_label=facts.ownership_label,
                ownership_labels=facts.ownership_labels,
                spec_only_label=facts.spec_only_label,
                issue_repositories=repositories,
                readable=facts.readable,
            )
        )
        return scope, (), False
    # `StateStore.read` falls back to a default when both the state file and
    # its backup are unreadable, which would present a corrupt or never-run
    # instance as a board with nothing on it. That is precisely the
    # "incomplete lookup read as absence" mistake, so the read has to say
    # whether it found anything. It now does (`last_load.found`); this used
    # to ask by decoding the same file a second time before reading it, on
    # top of the second decode inside the load itself (#1362).
    state_path = base / "state.json"
    store = StateStore(EnginePaths.from_base(state_path))
    try:
        state = store.read()
        if not store.last_load.found:
            raise _StateUnreadable(state_path)
    except Exception:
        scope = (
            BoardScope(
                instance=instance,
                org=facts.org,
                boardless=facts.boardless,
                project_number=facts.project_number,
                ownership_label=facts.ownership_label,
                ownership_labels=facts.ownership_labels,
                spec_only_label=facts.spec_only_label,
                issue_repositories=repositories,
                state_readable=False,
            )
        )
        return scope, (), False
    from . import poller_nudge  # imports this module: resolved at call time

    external = poller_nudge.read_external_reviews(root, instance)
    verdicts = _verdicts_on_the_way(state, state_path)
    instance_cards: list[BoardCard] = []
    for key, card in (state.get("issues") or {}).items():
        if not isinstance(card, dict):
            continue
        repo_name, _, raw_number = str(key).partition("#")
        if not raw_number.isdigit() or not repo_name:
            continue
        repository = repo_name if "/" in repo_name else f"{facts.org}/{repo_name}"
        # First role wins when a board spells two roles as one column.
        role = dict(reversed(facts.review_roles)).get(
            str(card.get("status") or "").strip().casefold(), ""
        )
        reading, review_red = _card_release_reading(card, facts.org)
        entry = BoardCard(
            repository=repository,
            number=int(raw_number),
            status=str(card.get("status") or ""),
            in_review=(str(card.get("status") or "") == facts.in_review_status),
            done=(str(card.get("status") or "") == facts.done_status),
            # The engine's claim on the card. It used to be the ownership label the engine
            # wrote onto the issue (`engine_labeled`); since #2053 it is state alone
            # (`engine_claim`). Both are read so a card dispatched before the change still
            # reports as claimed.
            ownership=bool(card.get("engine_claim") or card.get("engine_labeled")),
            started=bool(card.get("started")),
            pull_request_url=_card_pull_request(card),
            pull_requests=_card_pull_requests(card, facts.org),
            blocked_by=_card_blockers(card),
            worktrees=tuple(str(path) for path in card.get("worktrees") or ()),
            review_passed_heads=_card_review_passed_heads(card),
            review_pass_heads=_card_review_pass_heads(card),
            review_offer=_card_review_offer(card),
            review_mode=str(card.get("mode") or ""),
            release_blocked=_card_release_blocked(card),
            status_role=role,
            auto_release=bool(card.get("auto_release")),
            review_approved=bool(card.get("review_heads")),
            verifies_itself=facts.verifies_itself,
            releases_itself=facts.releases_itself,
            verify_asked_heads=_card_verify_asked(card),
            verdicts_on_the_way=verdicts.get(str(key), ()),
            ci_repos=tuple(f"{facts.org}/{name}" for name in facts.ci_repos),
            review_only_repos=tuple(
                f"{facts.org}/{name}" for name in facts.review_only_repos
            ),
            deploy_repos=tuple(f"{facts.org}/{name}" for name in facts.deploy_repos),
            ci_checks=_card_ci_checks(card, facts.org),
            release_reading=reading,
            review_bit_red=review_red,
            untested_heads=_card_untested_heads(card, facts.org),
            **_external_review(external, _card_pull_request(card)),
            reviewer=external,
            generation=str(card.get("generation") or ""),
            started_at=str(card.get("started_at") or ""),
            last_live_check=str(card.get("last_live_check") or ""),
            dispatch=_card_dispatch(card),
            dispatch_failed=bool(card.get("dispatch_failed")),
            dispatch_failed_clean=bool(card.get("dispatch_failed_clean")),
            dispatch_failed_at=str(card.get("dispatch_failed_at") or ""),
            dispatch_failed_refusal=str(card.get("dispatch_failed_refusal") or ""),
            dispatch_failed_refusal_final=bool(card.get("dispatch_failed_refusal_final")),
            dispatch_rearmed=bool(card.get("dispatch_rearmed")),
            revives=_card_count(card.get("revives")),
            release_parked=_card_release_parked(card),
            parked_as=_card_parked_as(card, role),
            merged=_card_merged(card, facts.org),
            steps_merged=tuple(_card_refs(card.get("steps_merged"), facts.org).values()),
            awaiting_deploy=isinstance(card.get("awaiting_deploy"), dict),
            deploy_since=_card_deploy_since(card),
            deploy_recheck=isinstance(card.get("deploy_recheck"), dict),
            park_note=_card_park_note(card),
            paused=bool(card.get("paused")),
            paused_label=facts.paused_label,
            state_path=str(state_path),
            state_key=str(key),
        )
        instance_cards.append(entry)
    scope = (
        BoardScope(
            instance=instance,
            org=facts.org,
            boardless=facts.boardless,
            project_number=facts.project_number,
            ownership_label=facts.ownership_label,
            ownership_labels=facts.ownership_labels,
            spec_only_label=facts.spec_only_label,
            issue_repositories=repositories,
            artifact_repositories=artifact_repositories,
            borrowed_workspaces=borrowed_workspaces,
            cards=tuple(instance_cards),
            state_path=str(state_path),
        )
    )
    return scope, tuple(instance_cards), True


# --- controls ---

def set_paused(root: str | Path, instance: str, paused: bool, *, by: str = "") -> None:
    """Pause or resume an instance by editing its state; the next wake honours it."""
    paths = EnginePaths.from_base(Path(root).expanduser() / "instances" / instance / "state.json")
    with StateStore(paths).transaction() as state:
        if paused:
            engine_health.set_paused(state, True, note=f"paused by {by or 'an operator'}")
        else:
            engine_health.set_paused(state, False)
