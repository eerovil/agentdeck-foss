"""A command the deck asks for without a GitHub comment (#2324, #2419).

The deck's Merge button used to hand ``/merge`` to GitHub for the boss to post (#2289), because
the engine authorizes a release comment by *who wrote it*. The boss decided the button should
simply merge, and that the comment is only a transport: the deck may express the operator's
intent to the release pipeline directly. Everything the release does after that is unchanged.

The rest of the deck's command buttons followed (#2419): ``/merge review`` and the fixed
``/claude`` instructions (Rebase, Retry, Fix findings, ...) were still copied for the boss to
paste on GitHub. Posting them as the engine's own account would not have worked, because the
engine never reads its own account's comments as commands (``authorization.Policy.bot``). So
they travel the same way as Merge: a request carries the exact comment the button stands for,
the wake parses it with the board's own grammar (``intake.parse_control``), and it becomes the
control that comment would have become. ``/claude``, ``/merge``, ``/merge review`` and
``/reviewed`` are accepted; ``/claude stop`` and extension words are not deck commands.

``/reviewed`` joined for the Claude fallback reviewer (#2736): its verdict has no GitHub
comment the engine would read as a command, so it travels here and meets exactly the checks
an owner's comment meets in ``_accept_reviewed`` — the head it names, the digest of the diff it
read, the card's linked pull requests. A deck request is already trusted with ``/merge``, which
asks for more than a verdict does.

**A request is a file, not a state edit.** The web process must never wait on the lock a wake
holds for its whole run, so the deck drops one small JSON file per press into this instance's
``deck-releases/`` directory (written atomically: temp file, then rename). The next wake turns
each file into an ordinary control in the command ledger — the same record the comment
becomes — and ``_process_controls`` queues an instruction or gates a release exactly as it
would the comment. A release is admitted, deferred or refused by ``_gate_release``: open,
owned, idle card in *In review* or *Ready to merge*, a linked pull request, then the guarded
release (update, repair, auto-merge, deploy watch, Done or parked). A refusal is posted on the
issue as it is for a comment.

**Exactly once.** The control id is ``deck:<request id>``, and the request id is the press's own
client action id, so a file read twice, or left behind by a wake that died after queueing it,
is the same ledger record and is refused by ``queue.enqueue``. A file is removed only after the
checkpoint that made its control durable. A ``/reviewed`` verdict whose control was *refused* is
the one exception (#2772): it did nothing, so the same id written again later — ``agentdeck
deck-request`` derives its id from the card and the command, so a re-sent verdict always
repeats it — replaces the refused record and is checked afresh. Any other repeat is logged.

**What it skips is authorization, deliberately.** A comment's author is checked against the
Work or Release tier; a deck request carries no GitHub identity to check. The dashboard is
reachable only on a trusted network, and workers can already write to GitHub as the engine's own
authority, so the boss chose not to build a device check for these buttons. Hardening the
human-versus-agent boundary end to end is a separate project.

An agent once used the same door through a session API (#2723); the deck retired it with the
Poller's cards (#3025). A worker's ``reopen`` of a card it can no longer take back in place
writes the same request itself, ``by=worker-reopen`` (``worker_control``, #2845).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from . import queue
from .intake import CommandKind, parse_control
from .state import write_json_atomic

DIRECTORY = "deck-releases"
AUTHOR_PREFIX = "deck:"
ID_PREFIX = "deck:"
# The commands the deck may ask for. Anything else a request names is discarded.
KINDS = frozenset(
    {CommandKind.CLAUDE, CommandKind.MERGE, CommandKind.MERGE_REVIEW, CommandKind.REVIEWED}
)
# The ones whose gate reads GitHub live, by the ledger's spelling of a kind.
RELEASE_KINDS = frozenset({CommandKind.MERGE.value, CommandKind.MERGE_REVIEW.value})
# The whole comment a request stands for. The buttons' are fixed sentences; a reviewer's
# findings (#2736) are a whole review, so the bound is GitHub's own comment size.
MAX_COMMAND = 65_536
# The ledger status of a refused command, as ``cycle._REJECTED`` spells it.
REFUSED = "rejected"

_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")
_KEY_RE = re.compile(r"[A-Za-z0-9_.-]+#[1-9][0-9]*")


class RequestError(ValueError):
    """A request that cannot be written, in words for the person who pressed."""


def directory(state_path: str | Path) -> Path:
    """Where one instance's waiting deck requests live: beside its ``state.json``."""
    return Path(state_path).expanduser().parent / DIRECTORY


def request_path(state_path: str | Path, request_id: str) -> Path:
    return directory(state_path) / f"{request_id}.json"


def write(
    state_path: str | Path,
    *,
    request_id: str,
    key: str,
    command: str = "/merge",
    by: str = "",
    reviewer: bool = False,
    now: datetime | None = None,
) -> Path:
    """Leave one command request for the next wake, atomically, and return its path.

    ``key`` is the engine's own card key (``repo#number``, as ``state["issues"]`` spells it).
    ``command`` is the exact comment the request stands for (``/merge``, ``/merge review`` or a
    ``/claude`` instruction). Writing the same ``request_id`` twice rewrites the same file,
    which the wake reads as one request; it never becomes a second command.

    ``reviewer`` marks a request the Claude fallback reviewer sent (#2736). Its control is then
    read as an outside reviewer's, the way the ChatGPT reviewer's app comment is: findings
    reject the offered head, and an instruction cannot start work on a card nobody handed over.
    """
    if not _ID_RE.fullmatch(request_id or ""):
        raise RequestError("the request carried no usable id")
    if not _KEY_RE.fullmatch(key or ""):
        raise RequestError(f"{key!r} is not a Poller card key")
    parsed = _parse(command)
    if parsed is None:
        raise RequestError(f"{str(command)[:60]!r} is not a command the deck may ask for")
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    path = request_path(state_path, request_id)
    record = {
        "id": request_id,
        "kind": parsed.kind.value,
        "command": command,
        "key": key,
        "by": by,
        "requested_at": moment.isoformat(),
    }
    if reviewer:
        record["reviewer"] = True
    write_json_atomic(path, record)
    return path


def is_deck_control(control: dict) -> bool:
    """Was this control asked for from the deck, with no GitHub comment behind it?"""
    return str((control or {}).get("author") or "").startswith(AUTHOR_PREFIX)


def accepts(command: object) -> bool:
    """Is ``command`` one the deck may hand a Poller (#2814)?"""
    return _parse(command) is not None


def _parse(command: object):
    """The board's own reading of ``command``, or ``None`` when it is not a deck command."""
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND:
        return None
    parsed = parse_control(command)
    return parsed if parsed is not None and parsed.kind in KINDS else None


def _control(raw: object) -> dict | None:
    """The ledger record a well-formed request becomes, or ``None``."""
    if not isinstance(raw, dict):
        return None
    request_id = str(raw.get("id") or "")
    key = str(raw.get("key") or "")
    if not _ID_RE.fullmatch(request_id) or not _KEY_RE.fullmatch(key):
        return None
    command = raw.get("command")
    if command is None and raw.get("kind") == "merge":
        # A Merge written before requests carried their command (#2324), still waiting when
        # this code was deployed.
        command = "/merge"
    parsed = _parse(command)
    if parsed is None:
        return None
    repo, _, number = key.partition("#")
    if parsed.kind is CommandKind.REVIEWED:
        # The same several facts a comment's verdict is recorded with (``cycle._intake``).
        payload: object = {
            "verdict": parsed.verdict,
            "head": parsed.head,
            "digest": parsed.digest,
            "targets": list(parsed.targets),
        }
    else:
        # What a comment's record carries: the instruction for ``/claude``, the named pull
        # requests for a release that named any.
        payload = list(parsed.targets) if parsed.targets else (parsed.instruction or None)
    by = re.sub(r"[^A-Za-z0-9_.@-]", "", str(raw.get("by") or ""))[:40] or "operator"
    control = {
        "id": f"{ID_PREFIX}{request_id}",
        "repo": repo,
        "number": int(number),
        "kind": parsed.kind.value,
        "payload": payload,
        "body": command,
        "url": "",
        "author": f"{AUTHOR_PREFIX}{by}",
        "created_at": str(raw.get("requested_at") or ""),
        "status": "new",
    }
    if raw.get("reviewer") is True:
        control["app"] = True
    return control


def ingest(state: dict, folder: Path) -> tuple[list[Path], list[str], int]:
    """Queue every waiting request exactly once.

    Returns the files that may be removed once the caller has checkpointed the ledger, one log
    line per request, and how many became new controls. A file that will not parse is removed
    too, with a log line: leaving it would only fail the same way on every wake. One that cannot
    be read right now is left for the next wake.
    """
    done: list[Path] = []
    log: list[str] = []
    queued = 0
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return done, log, queued
    for path in paths:
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.append(f"deck request {path.name} unreadable ({exc}); left for next wake")
            continue
        except ValueError as exc:
            log.append(f"deck request {path.name} is not JSON ({exc}); discarded")
            done.append(path)
            continue
        control = _control(raw)
        if control is None:
            log.append(f"deck request {path.name} malformed; discarded")
        elif queue.enqueue(state, control) or _requeue_refused(state, control):
            queued += 1
            log.append(
                f"deck {control['kind']} request {control['id']} queued for "
                f"{control['repo']}#{control['number']}"
            )
        else:
            # Said out loud (#2772): a re-sent verdict once vanished here with no trace.
            prior = queue.controls(state).get(control["id"]) or {}
            log.append(
                f"deck {control['kind']} request {control['id']} for "
                f"{control['repo']}#{control['number']} is already in the ledger "
                f"({prior.get('status') or '?'}); not queued again"
            )
        done.append(path)
    return done, log, queued


def _requeue_refused(state: dict, control: dict) -> bool:
    """Queue a re-sent ``/reviewed`` verdict again when its earlier copy was refused (#2772).

    A request's id is its client's, and ``agentdeck deck-request`` derives it from the card and
    the command, so a reviewer re-sending the same verdict after a refusal names the same
    ledger record. A refused record did nothing, so a fresh send of it is a new attempt rather
    than a duplicate. It counts as fresh only when it was written again — a later
    ``requested_at`` than the record holds — so a file a wake failed to remove is not
    re-read as a second attempt.
    """
    if control["kind"] != CommandKind.REVIEWED.value:
        return False  # every other command keeps its exactly-once id
    ledger = queue.controls(state)
    prior = ledger.get(control["id"])
    if not isinstance(prior, dict) or prior.get("status") != REFUSED:
        return False
    if any(prior.get(field) != control[field] for field in ("kind", "repo", "number", "body")):
        return False  # the same id on a different request is not a re-send of this one
    if not control["created_at"] or control["created_at"] <= str(prior.get("created_at") or ""):
        return False
    ledger[control["id"]] = control
    return True


def request_waiting(folder: Path) -> bool:
    """Is any deck request waiting in ``folder`` for the next wake to queue it? (#2671, #2764)

    Read before the wake decides whether it may skip its live GitHub reads, because a press on
    the deck sends GitHub nothing. Every kind counts: a release's gate and a verdict's check of
    its head read GitHub live, and so does the worker a ``/claude`` starts on a card whose last
    worker ended — the branch refresh before it reads the card's pull requests, which a quiet
    wake refuses. Left out, a reviewer's findings sat queued until the 15-minute ceiling (#2764).
    Only the file counts, so it is one full wake per request, as a comment's webhook would be: a
    command already in the ledger and deferred does not hold every wake live.
    Anything unreadable answers no — it is left for ``ingest`` to report, and the queued control
    asks again on the next wake.
    """
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return False
    for path in paths:
        try:
            control = _control(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
        if control is not None:
            return True
    return False


def waiting_verdicts(folder: Path) -> list[dict]:
    """The ``reviewed`` requests still waiting in ``folder`` for a wake to take them (#2760).

    An outside reviewer's verdict reaches the engine as one of these; until a wake records it,
    the reviewer has answered and nothing has happened yet. Unreadable files are skipped.
    """
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return []
    found = []
    for path in paths:
        try:
            control = _control(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
        if control is not None and control["kind"] == CommandKind.REVIEWED.value:
            found.append(control)
    return found


def discard(paths: list[Path]) -> None:
    """Remove requests whose controls are now durable in the ledger."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # read again next wake, and refused there as the same control id
