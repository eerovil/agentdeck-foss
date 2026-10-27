"""Durable command queue + scan cursor (#176 slice 2).

Deterministic, stdlib-only operations over the engine state payload (the dict a
``StateStore`` transaction yields). No GitHub I/O lives here — the actual comment
fetch and authorization are the caller's job; this module owns the invariants:

* **Exact-once identity** — commands are keyed by GitHub comment id, so a comment
  is ingested at most once across polls (dedup).
* **FIFO order** — pending commands sort by ``(created_at, comment id)``.
* **Scan cursor** — a per-instance timestamp bounds idle work; the cursor advances
  only to a moment the scan actually proved, and only when the caller reports a
  fully successful scan (a scan or permission failure must not skip comments).
  The next scan floor overlaps the cursor by several wakes, because GitHub's
  comment list is not instantaneous and a comment can be created before a scan
  yet be absent from its response.
* **Intake before dispatch** — the caller ingests into this durable ledger and
  persists it (one ``StateStore`` transaction) *before* any dispatch, so a crash
  after queueing never produces a duplicate worker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

DEFAULT_SCAN_LOOKBACK_S = 300

# How often a wake happens (systemd/agentdeck-poller@.timer: OnUnitInactiveSec=120).
# The overlap is stated as a multiple of it on purpose: an overlap shorter than one
# wake leaves a gap no later scan ever looks at, so the two numbers are one decision,
# not two constants that happen to be compatible. Re-scanning is free — `is_seen`
# dedupes by comment id — so the only cost of a wide window is one extra API page.
WAKE_INTERVAL_S = 120
CURSOR_OVERLAP_WAKES = 3
DEFAULT_CURSOR_OVERLAP_S = WAKE_INTERVAL_S * CURSOR_OVERLAP_WAKES

_CURSOR_KEY = "control_since"
_CONTROLS_KEY = "controls"


def card_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def is_bot_login(login: object) -> bool:
    """True for a login GitHub only ever gives an app: the ``[bot]`` suffix.

    The suffix is all a *label* event or a stored command record carries — there is no user
    object to read a type off — so this is the one question askable everywhere a login is known.
    """
    return str(login or "").endswith("[bot]")


def is_bot_comment(comment: dict) -> bool:
    """True for comments authored by any bot account (independent of the bot marker)."""
    user = comment.get("user") or {}
    return user.get("type") == "Bot" or is_bot_login(user.get("login"))


def controls(state: dict) -> dict:
    """The command ledger, keyed by comment id (created on first access)."""
    return state.setdefault(_CONTROLS_KEY, {})


def is_seen(state: dict, comment_id: object) -> bool:
    return str(comment_id) in controls(state)


def build_record(
    comment: dict, kind: str, payload: object = None, *, repo: str, status: str = "new"
) -> dict | None:
    """Map a raw comment + classified command into a ledger record, or None if unusable.

    ``kind`` is the command kind string (e.g. from ``intake.CommandKind.value``);
    ``payload`` carries the instruction for a claude command, or the wrapped kind
    for an unauthorized one, mirroring the reference ledger. ``repo`` is the repo
    the comment was scanned from (the scan is per-repo).
    """
    issue_url = (comment.get("issue_url") or "").rstrip("/")
    try:
        number = int(issue_url.rsplit("/", 1)[-1])
    except ValueError:
        return None
    cid = str(comment.get("id"))
    return {
        "id": cid,
        "repo": repo,
        "number": number,
        "kind": kind,
        "payload": payload,
        "body": comment.get("body") or "",
        "url": comment.get("html_url") or "",
        "author": (comment.get("user") or {}).get("login") or "",
        "created_at": comment.get("created_at") or "",
        "status": status,
    }


def enqueue(state: dict, record: dict) -> bool:
    """Add a record to the ledger unless its comment id is already present.

    Returns True if newly added (exact-once), False if it was a duplicate.
    """
    ledger = controls(state)
    cid = str(record["id"])
    if cid in ledger:
        return False
    ledger[cid] = record
    return True


def sort_key(control: dict) -> tuple[str, int]:
    """FIFO ordering key: creation time, then the monotonic comment id."""
    try:
        cid = int(control["id"])
    except (KeyError, ValueError, TypeError):
        cid = 0
    return (control.get("created_at") or "", cid)


def pending(
    state: dict,
    *,
    repo: str | None = None,
    number: int | None = None,
    statuses: set[str] | None = None,
) -> list[dict]:
    """Ledger records in FIFO order, optionally filtered to one card and/or statuses."""
    out = []
    for control in controls(state).values():
        if repo is not None and control.get("repo") != repo:
            continue
        if number is not None and control.get("number") != number:
            continue
        if statuses is not None and control.get("status") not in statuses:
            continue
        out.append(control)
    return sorted(out, key=sort_key)


def get_cursor(state: dict) -> str | None:
    return state.get(_CURSOR_KEY)


def scan_floor(
    state: dict,
    *,
    now: datetime,
    lookback_s: int = DEFAULT_SCAN_LOOKBACK_S,
    overlap_s: int = DEFAULT_CURSOR_OVERLAP_S,
) -> str:
    """The ISO timestamp to fetch comments from: cursor minus overlap, else now minus lookback."""
    cursor = _moment(get_cursor(state))
    if cursor is not None:
        return (cursor - timedelta(seconds=overlap_s)).isoformat()
    return (now.astimezone(UTC) - timedelta(seconds=lookback_s)).isoformat()


def record_scan(
    state: dict,
    scan_to: datetime,
    *,
    comments: list[dict] | tuple[dict, ...] = (),
    overlap_s: int = DEFAULT_CURSOR_OVERLAP_S,
) -> None:
    """Advance the cursor to what this scan proved. Called only after a fully successful scan.

    The cursor is the newest comment the scan actually returned — not the wall clock, which
    would claim a moment GitHub never confirmed. A comment created just before the scan can be
    missing from its response, and moving the cursor past it would drop the command for good.

    An idle scan proves nothing, so the cursor would never move and the window would grow
    without end; ``scan_to - overlap`` is the floor that keeps it moving. That is also the
    residual risk: a comment missing from a response that *did* return something newer is only
    caught because the next floor overlaps by several wakes.

    ``comments`` are the raw comments as returned, keyed on ``updated_at`` because that is the
    field GitHub's ``since`` filters on. A timestamp after ``scan_to`` (clock skew) is ignored
    rather than trusted, and the cursor never moves backwards.
    """
    scan_to = scan_to.astimezone(UTC)
    cursor = scan_to - timedelta(seconds=overlap_s)
    for comment in comments:
        seen = _moment(comment.get("updated_at") or comment.get("created_at"))
        if seen is not None and cursor < seen <= scan_to:
            cursor = seen
    current = _moment(get_cursor(state))
    if current is not None and current > cursor:
        return
    state[_CURSOR_KEY] = cursor.isoformat()


def _moment(value: object) -> datetime | None:
    """One GitHub/ISO timestamp as an aware UTC datetime, or None if it is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
