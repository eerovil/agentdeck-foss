"""Durable provider-account health circuits.

A hard provider quota is account-global evidence: once observed, fresh work must not be sent to
that account on every wake. This module persists that evidence beside the shared usage cache so
web/runtime restarts do not forget it. Recovery is evidence-based: a trustworthy reset boundary
may expire the circuit, or a later successful usage snapshot below the provider's hard refusal
boundary proves the account can be considered again.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class AccountCircuit:
    account: str
    reason: str
    observed_at: datetime
    clears_at: datetime | None = None
    evidence: str = ""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return _aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def circuit_path(cache_dir: Path, account: str) -> Path:
    safe = str(account).replace(":", "-", 1).replace("/", "_")
    return cache_dir / f"account-health-{safe}.json"


def _read(cache_dir: Path | None, account: str) -> AccountCircuit | None:
    if cache_dir is None or not account:
        return None
    try:
        raw = json.loads(circuit_path(cache_dir, account).read_text())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("account") != account
        or raw.get("kind") != "hard_quota"
    ):
        return None
    observed = _parse_time(raw.get("observed_at"))
    if observed is None:
        return None
    return AccountCircuit(
        account=account,
        reason=str(raw.get("reason") or "provider hard quota"),
        observed_at=observed,
        clears_at=_parse_time(raw.get("clears_at")),
        evidence=str(raw.get("evidence") or ""),
    )


def clear(cache_dir: Path | None, account: str) -> bool:
    if cache_dir is None or not account:
        return False
    try:
        circuit_path(cache_dir, account).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def open_circuit(
    cache_dir: Path | None,
    account: str,
    *,
    now: datetime | None = None,
) -> AccountCircuit | None:
    circuit = _read(cache_dir, account)
    if circuit is None:
        return None
    moment = _aware(now or datetime.now(UTC))
    if circuit.clears_at is not None and circuit.clears_at <= moment:
        clear(cache_dir, account)
        return None
    return circuit


def refusal_detail(
    cache_dir: Path | None,
    account: str,
    *,
    now: datetime | None = None,
) -> tuple[str | None, datetime | None]:
    circuit = open_circuit(cache_dir, account, now=now)
    if circuit is None:
        return None, None
    reset = (
        f" until {circuit.clears_at.isoformat()}"
        if circuit.clears_at is not None
        else " until fresh provider health evidence"
    )
    return f"hard quota circuit open ({circuit.reason}){reset}", circuit.clears_at


def trip_hard_quota(
    cache_dir: Path | None,
    account: str,
    *,
    reason: str,
    evidence: str = "",
    clears_at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    if cache_dir is None or not account:
        return False
    moment = _aware(now or datetime.now(UTC))
    reset = _aware(clears_at) if clears_at is not None else None
    # A hard refusal observed now is stronger evidence than a stale reset timestamp in the
    # same payload. Only a boundary still ahead of this observation may reopen by time alone;
    # otherwise require a later healthy meter snapshot.
    if reset is not None and reset <= moment:
        reset = None
    payload = {
        "version": 1,
        "account": account,
        "kind": "hard_quota",
        "reason": reason[:200],
        "observed_at": moment.isoformat(),
        "clears_at": reset.isoformat() if reset is not None else None,
        "evidence": evidence[:500],
    }
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".account-health-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle)
            os.replace(tmp, circuit_path(cache_dir, account))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        return False
    return True


def reconcile_healthy_usage(
    cache_dir: Path | None,
    account: str,
    *,
    fetched_at: datetime,
    percentages: tuple[float | None, ...],
) -> bool:
    """Close only on newer complete evidence that every relevant window is below its limit."""
    circuit = open_circuit(cache_dir, account, now=fetched_at)
    if circuit is None or _aware(fetched_at) <= circuit.observed_at:
        return False
    if not percentages or any(value is None for value in percentages):
        return False
    reported = [float(value) for value in percentages if value is not None]
    if max(reported) >= 100.0:
        return False
    return clear(cache_dir, account)


def structured_reset_at(payload: object) -> datetime | None:
    """Read only explicit machine reset fields; prose reset guesses are not trustworthy."""
    reset_keys = {"reset_at", "resets_at", "reset_time", "reset_timestamp"}
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if str(key).lower() in reset_keys:
                    parsed = _parse_time(value)
                    if parsed is not None:
                        return parsed
                if isinstance(value, (dict, list, tuple)):
                    stack.append(value)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
    return None


_STATED_RESET = re.compile(
    r"\bresets\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<period>am|pm)\s*\((?P<zone>[^()]+)\)\s*$",
    re.IGNORECASE,
)


def stated_reset_at(evidence: str, *, now: datetime | None = None) -> datetime | None:
    """Parse Claude's exact provider-authored quota reset line.

    This is deliberately narrower than general prose parsing: the caller has already proved
    that ``evidence`` is the provider's short synthetic hard-quota answer. A named IANA zone
    and a 12-hour clock are required, matching the wire shape observed on this host. The next
    occurrence is the boundary; arbitrary phrases such as "tomorrow at noon" stay unknown.
    """
    match = _STATED_RESET.search(evidence.strip())
    if match is None:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        return None
    try:
        zone = ZoneInfo(match.group("zone").strip())
    except (ValueError, ZoneInfoNotFoundError):
        return None
    local_now = _aware(now or datetime.now(UTC)).astimezone(zone)
    hour = hour % 12 + (12 if match.group("period").lower() == "pm" else 0)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)
