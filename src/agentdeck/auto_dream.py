"""Run /dream on an account's leftover weekly usage before its week resets (#3336).

Whatever an account has not used by its weekly reset is lost, so the owner asked for that
leftover to go to /dream. Every ``CHECK_INTERVAL_S`` the web process reads each Claude and
Codex account's shared usage reading — the same file ``dream.py`` reads — and starts at most
one dream chat, the way the New chat box starts one:

* **early**: the week resets within ``EARLY_WITHIN_H`` hours and at least
  ``EARLY_MIN_LEFT_PCT`` of it is unused: ``/dream <label> 5``;
* **final**: the week resets within ``FINAL_WITHIN_H`` hour: ``/dream <label> <99 - used>``,
  so the week is spent up to ``FINAL_TARGET_PCT`` (owner's choice in the chat).

Each run happens at most once per weekly window per account, even when its dream then refuses.
A skip (stale reading, a full 5-hour meter, another dream holding ``dream.lock``) records
nothing, so a later check can still start it. ``dream.py`` itself lifts its 80% weekly stop near
the reset and stops a run once the week resets, so a run never spends the next week.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import UsageSnapshot

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 600.0
EARLY_WITHIN_H = 5
EARLY_MIN_LEFT_PCT = 10
EARLY_BUDGET = 5
FINAL_WITHIN_H = 1
FINAL_TARGET_PCT = 99
FINAL_MIN_BUDGET = 2
# dream.py refuses at 80% minus its 5-point minimum margin on the 5-hour meter.
FIVE_HOUR_STOP_PCT = 75
# dream.py's own USAGE_MAX_AGE_S and LOCK_STALE_S.
READING_MAX_AGE_S = 1800.0
LOCK_STALE_S = 6 * 3600
DREAM_PROVIDERS = ("claude_code", "codex")


@dataclass(frozen=True)
class Run:
    account_key: str
    label: str
    kind: str  # "early" or "final"
    budget: int
    window: str  # the weekly reset this run belongs to, rounded to the hour
    week_ends: str = ""  # that reset exactly: dream.py refuses to start once it passed

    @property
    def message(self) -> str:
        ends = f" --week-ends {self.week_ends}" if self.week_ends else ""
        return f"/dream {self.label} {self.budget}{ends}"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(base).expanduser() / "agentdeck"


def window_key(resets_at: datetime) -> str:
    """The reset rounded to the hour: the reported time moves by fractions of a second."""
    moment = resets_at.astimezone(UTC) + timedelta(minutes=30)
    return moment.replace(minute=0, second=0, microsecond=0).isoformat()


def lock_held(path: Path, now: float) -> bool:
    """Whether a live dream holds ``dream.lock``: ``dream.py``'s own ``lock_is_live`` rule.

    The lock names its holder by pid and start time, so a recycled pid is not taken for it.
    """
    try:
        held = json.loads(path.read_text())
        heartbeat = float(held.get("heartbeat", 0))
        pid, start = int(held.get("pid") or 0), str(held.get("start") or "")
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    if now - heartbeat >= LOCK_STALE_S:
        return False
    if pid and start:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        return stat.rsplit(")", 1)[-1].split()[19] == start
    return True


def _number(value: object) -> bool:
    """A percentage dream.py accepts: a number, never a bool."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def candidates(
    readings: Iterable[tuple[str, str, UsageSnapshot]], state: dict, now: datetime
) -> tuple[list[Run], list[str]]:
    """Every run due now, first to start first, and why each other account is skipped.

    ``readings`` holds ``(account_key, label, snapshot)``; ``state`` is the file's
    ``{account_key: {kind: window}}``.
    """
    due: list[tuple[int, datetime, Run]] = []
    skipped: list[str] = []
    for key, label, snapshot in readings:
        if key.partition(":")[0] not in DREAM_PROVIDERS:
            continue
        used, resets_at = snapshot.seven_day_pct, snapshot.seven_day_resets_at
        if not _number(used) or resets_at is None:
            continue
        left = resets_at - now
        if left <= timedelta(0) or left > timedelta(hours=EARLY_WITHIN_H):
            continue
        window = window_key(resets_at)
        done = state.get(key) if isinstance(state.get(key), dict) else {}
        if left <= timedelta(hours=FINAL_WITHIN_H):
            kind, budget = "final", int(FINAL_TARGET_PCT - used)
            if budget < FINAL_MIN_BUDGET:
                continue
        else:
            kind, budget = "early", EARLY_BUDGET
            if 100 - used < EARLY_MIN_LEFT_PCT:
                continue
        if done.get(kind) == window:
            continue
        fetched = snapshot.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
        if (now - fetched).total_seconds() > READING_MAX_AGE_S:
            skipped.append(f"{key}: the usage reading is stale")
            continue
        five_hour = snapshot.five_hour_pct
        if not _number(five_hour) and key.partition(":")[0] != "codex":
            # dream.py refuses a Claude reading without one; only Codex may lack it (#2560).
            skipped.append(f"{key}: the 5-hour meter is missing")
            continue
        if _number(five_hour) and five_hour >= FIVE_HOUR_STOP_PCT:
            skipped.append(f"{key}: the 5-hour meter is at {five_hour:.0f}%")
            continue
        run = Run(key, label, kind, budget, window, resets_at.astimezone(UTC).isoformat())
        due.append((0 if kind == "final" else 1, resets_at, run))
    due.sort(key=lambda item: (item[0], item[1]))
    return [run for _, _, run in due], skipped


def read_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def record(path: Path, run: Run) -> None:
    state = read_state(path)
    entry = state.get(run.account_key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry[run.kind] = run.window
    state[run.account_key] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=1, sort_keys=True))
    temporary.replace(path)


async def settled(answer: Awaitable) -> tuple[bool, str]:
    """Whether a queued start counts as made, by the provider's answer to it.

    Only a clean refusal (no session, so nothing launched) leaves the run to a later check.
    An unknown answer may have launched a chat, so it counts: a second dream would cost more
    than a missed one.
    """
    try:
        result = await answer
    except Exception:  # noqa: BLE001 - unknown: it may have launched
        log.exception("auto dream: no answer to the start")
        return True, "no answer from the provider"
    if result is not None and not result.accepted and not result.session_id:
        return False, str(result.reason or "the start was refused")
    return True, ""


class AutoDream:
    """The loop: one check every ``CHECK_INTERVAL_S``, the first after one interval."""

    def __init__(
        self,
        *,
        readings: Callable[[], list[tuple[str, str, UsageSnapshot]]],
        start: Callable[[Run], Awaitable[tuple[bool, str]]],
        directory: Path | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._readings = readings
        self._start = start
        self._directory = directory or state_dir()
        self._sleep = sleep
        self._task: asyncio.Task | None = None
        self._last_skip: tuple[str, ...] = ()

    @property
    def state_path(self) -> Path:
        return self._directory / "auto-dream.json"

    async def check(self, now: datetime | None = None) -> Run | None:
        moment = now or datetime.now(UTC)
        runs, skipped = candidates(self._readings(), read_state(self.state_path), moment)
        if runs and lock_held(self._directory / "dream.lock", moment.timestamp()):
            skipped.append("another dream holds dream.lock")
            runs = []
        if tuple(skipped) != self._last_skip and skipped:
            log.info("auto dream skipped: %s", "; ".join(skipped))
        self._last_skip = tuple(skipped)
        if not runs:
            return None
        run = runs[0]
        accepted, reason = await self._start(run)
        if not accepted:
            log.warning("auto dream %s for %s not started: %s", run.kind, run.account_key, reason)
            return None
        record(self.state_path, run)
        log.info("auto dream %s started for %s: %s", run.kind, run.account_key, run.message)
        return run

    async def _run(self) -> None:
        while True:
            await self._sleep(CHECK_INTERVAL_S)
            try:
                await self.check()
            except Exception:
                log.exception("auto dream check failed")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="auto-dream")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
