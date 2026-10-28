"""A Work that stopped because AgentDeck broke, reported for a fix (#2888).

The deck's Needs retry group holds Works that stopped and that nothing restarts on its own.
Some got there for an ordinary reason — red CI, a failed test run, a host that was full — and
some because AgentDeck itself broke: a worker that died through every restart, a reviewer Run
that failed, a start refused for a reason other than room, a chat whose turn died. The deck
tells the two apart (``work_attention.OperatorAttention.bug``) and leaves one request file per
broken Work here, beside the state of the instance that manages AgentDeck's own issues. Its next
wake folds each request into one record per *cause*, and files that record as one labelled
issue the way it files a forgiven test's fix issue (``cycle._file_bug_issues``).

**A request is a file, not a state edit**: the web process must never wait on the lock a
wake holds. **Exactly once**: a request's id is derived
from the Work, the cause and the Run, so the deck writing it again on every pass — or after a
restart — rewrites one file, and the engine remembers the ids it has taken, so a Work flapping
in and out of the group is reported once per Run.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .state import write_json_atomic

DIRECTORY = "bug-reports"
# The record this instance keeps per cause, and the request ids it has already taken.
RECORDS = "needs_retry_bug_issues"
SEEN = "needs_retry_bug_seen"
# Enough to cover every Work a deck shows at once, several times over.
MAX_SEEN = 500
# How many Works one filing or one comment lists; the rest are counted.
MAX_EVIDENCE = 20

_ID_RE = re.compile(r"[0-9a-f]{32}")
_CAUSE_RE = re.compile(r"[a-z_]{1,40}(:[A-Za-z0-9_.-]{1,60})?")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
# The free-text fields a request carries, and how long each may be.
_TEXT = {"work_id": 200, "work_url": 300, "reason": 300, "run_id": 80, "session_key": 200,
         "revision": 64, "at": 40}


def directory(state_path: str | Path) -> Path:
    """Where one instance's waiting bug reports live: beside its ``state.json``."""
    return Path(state_path).expanduser().parent / DIRECTORY


def request_id(work_id: str, cause: str, run_id: str) -> str:
    """The one id a Work, cause and Run is ever reported under."""
    return hashlib.sha256(f"{work_id}\n{cause}\n{run_id}".encode()).hexdigest()[:32]


def write(
    state_path: str | Path,
    *,
    repo: str,
    work_id: str,
    cause: str,
    run_id: str = "",
    reason: str = "",
    work_url: str = "",
    session_key: str = "",
    why: dict | None = None,
    revision: str = "",
    now: datetime | None = None,
) -> Path | None:
    """Leave one bug report for the next wake, atomically, and return its path.

    ``repo`` is the instance's own spelling of the repository to file in (``agentdeck``).
    Returns None for a report that names no usable repository or cause, and writes nothing
    when the same report's file is already waiting.
    """
    if not _REPO_RE.fullmatch(repo or "") or not _CAUSE_RE.fullmatch(cause or ""):
        return None
    ident = request_id(work_id, cause, run_id)
    path = directory(state_path) / f"{ident}.json"
    if path.exists():
        return path
    record = {
        "id": ident,
        "repo": repo,
        "cause": cause,
        "work_id": work_id,
        "work_url": work_url,
        "run_id": run_id,
        "reason": reason,
        "session_key": session_key,
        "why": why or {},
        "revision": revision,
        "at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
    }
    write_json_atomic(path, record)
    return path


def _evidence(raw: object) -> dict | None:
    """The record one well-formed request becomes, or None."""
    if not isinstance(raw, dict):
        return None
    ident = str(raw.get("id") or "")
    repo = str(raw.get("repo") or "")
    cause = str(raw.get("cause") or "")
    if not (_ID_RE.fullmatch(ident) and _REPO_RE.fullmatch(repo) and _CAUSE_RE.fullmatch(cause)):
        return None
    found = {"id": ident, "repo": repo, "cause": cause}
    for name, limit in _TEXT.items():
        found[name] = str(raw.get(name) or "")[:limit]
    why = raw.get("why")
    # Kept as the deck wrote it, bounded: it is evidence for a reader, never a decision.
    text = json.dumps(why if isinstance(why, dict) else {}, sort_keys=True, default=str)
    found["why"] = text if len(text) <= 4000 else text[:3999] + "…"
    return found


def ingest(
    state: dict, folder: Path, *, repos: frozenset[str] = frozenset()
) -> tuple[list[Path], list[str], int]:
    """Fold every waiting report into its cause's record, each exactly once.

    ``repos`` are this project's issue repositories, the only ones a report may ask to file in;
    naming none permits none, as for an extension's proposal. Returns the files to
    remove once the caller has checkpointed state, one log line per report, and how many were
    new. A file that cannot be read right now is left for the next wake.
    """
    done: list[Path] = []
    log: list[str] = []
    added = 0
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return done, log, added
    seen = state.setdefault(SEEN, [])
    records = state.setdefault(RECORDS, {})
    allowed = {item.lower() for item in repos}
    for path in paths:
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.append(f"bug report {path.name} unreadable ({exc}); left for next wake")
            continue
        except ValueError as exc:
            log.append(f"bug report {path.name} is not JSON ({exc}); discarded")
            done.append(path)
            continue
        done.append(path)
        evidence = _evidence(raw)
        if evidence is None:
            log.append(f"bug report {path.name} malformed; discarded")
            continue
        if evidence["repo"].lower() not in allowed:
            log.append(f"bug report {path.name} names {evidence['repo']!r}; not ours, discarded")
            continue
        if evidence["id"] in seen:
            continue
        seen.append(evidence["id"])
        del seen[:-MAX_SEEN]
        repo, cause = evidence["repo"], evidence["cause"]
        record = records.setdefault(f"{repo}::{cause}", {"repo": repo, "cause": cause})
        record.setdefault("pending", []).append(evidence)
        added += 1
        log.append(f"bug report: {evidence['work_id']} stopped on {cause}")
    return done, log, added


def discard(paths: list[Path]) -> None:
    """Remove reports whose records the caller has checkpointed."""
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# --- what the filed issue says ---------------------------------------------------------------

# Each cause in a reader's words, and where to start looking.
_CAUSES = {
    "worker_died": (
        "A worker stopped without a report after every automatic restart",
        "Read the last worker chat and the runtime log around its end.",
    ),
    "run_failed": (
        "A worker Run failed or ended in a state nothing restarts",
        "Read the Run's last chat and the poller log for that generation.",
    ),
    "review_run_failed": (
        "A reviewer's Run failed (not a review that asked for changes)",
        "Read the review chat and the poller's review log.",
    ),
    "start_refused": (
        "A worker never started: the host refused the start for a reason other than room",
        "Read the poller log for the refused dispatch and the runtime's refusal.",
    ),
    "chat_stopped": (
        "A chat's turn died or failed mid-way",
        "Read the chat's last turn and the runtime log.",
    ),
}


def describe(cause: str) -> tuple[str, str]:
    """``(what happened, where to look)`` for a cause, its refusal name kept."""
    base, _, detail = cause.partition(":")
    what, look = _CAUSES.get(base, ("A Work stopped in Needs retry", "Read the Work's chats."))
    return (f"{what} (`{detail}`)" if detail else what), look


def title(cause: str) -> str:
    what, _ = describe(cause)
    text = f"Needs retry bug: {what.replace('`', '')}"
    return text if len(text) <= 200 else text[:199] + "…"


def evidence_lines(pending: list[dict]) -> str:
    lines = []
    for item in pending[:MAX_EVIDENCE]:
        work = item.get("work_url") or item.get("work_id") or "a Work"
        parts = [f"- {work}"]
        if item.get("reason"):
            parts.append(f"— {item['reason']}")
        facts = ", ".join(
            f"{name} `{item[name]}`"
            for name in ("run_id", "session_key", "revision", "at")
            if item.get(name)
        )
        if facts:
            parts.append(f"({facts})")
        lines.append(" ".join(parts))
        if item.get("why") and item["why"] != "{}":
            lines.append(
                "  <details><summary>Status facts</summary>\n\n"
                f"  `{item['why']}`\n  </details>"
            )
    more = len(pending) - MAX_EVIDENCE
    if more > 0:
        lines.append(f"- … and {more} more")
    return "\n".join(lines)


def body(cause: str, pending: list[dict], marker: str) -> str:
    what, look = describe(cause)
    return (
        f"{what}, so a Work landed in the deck's **Needs retry** group and nothing will restart "
        "it on its own. That is an AgentDeck bug rather than a problem with the work itself, "
        "so the deck filed this issue (#2888).\n\n"
        f"{evidence_lines(pending)}\n\n"
        f"{look} Find why it happened and fix that; pressing Retry on the Work only unsticks "
        "that one Work.\n\n"
        f"{marker}"
    )


def update(cause: str, pending: list[dict]) -> str:
    what, _ = describe(cause)
    return f"It happened again — {what[0].lower()}{what[1:]}:\n\n{evidence_lines(pending)}"
