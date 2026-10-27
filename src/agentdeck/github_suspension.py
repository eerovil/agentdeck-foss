"""Notice when GitHub has suspended the identity the deck speaks as (#2682).

When GitHub suspended the ``eerovil-agent`` user, every ``gh`` call for ``eerovil/*``
answered ``403 Sorry. Your account was suspended`` — and the deck showed that only as
unrelated-looking read failures. This module keeps one fact per authority: GitHub's
last answer said it is suspended. ``gh_cli`` reports every call's outcome here, the
topbar pill, ``/healthz`` and the chat board read it back.

Held in this process's memory only. A web restart forgets it, and the next failing
call finds it again within minutes; a call that succeeds as the same authority clears
it, so a reinstated account or App installation goes green by itself.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from .poller_engine import github_authority
from .poller_engine.github_authority import Authority

log = logging.getLogger(__name__)

# GitHub's own words. A suspended user gets "Sorry. Your account was suspended."; a
# suspended App installation gets "This installation has been suspended" — on the
# token mint as well as on a call with a token minted before it.
_SUSPENDED = re.compile(
    r"account (?:was|has been) suspended|installation (?:was|has been) suspended", re.IGNORECASE
)
_DETAIL_MAX = 200


@dataclass(frozen=True)
class Suspension:
    authority: str  # the authority's name, e.g. ``agent`` or ``app:eerovil``
    login: str  # who GitHub sees: ``eerovil-agent``, ``agentdeck-eerovil[bot]``
    owners: tuple[str, ...]  # owners this authority spoke for when it was suspended
    since: float
    detail: str  # GitHub's words, one line

    def as_dict(self) -> dict[str, object]:
        return {
            "authority": self.authority,
            "login": self.login,
            "owners": list(self.owners),
            "since": self.since,
            "detail": self.detail,
        }


_lock = threading.Lock()
_suspended: dict[str, Suspension] = {}
_listeners: list[Callable[[], None]] = []


def is_suspension(text: str | bytes | None) -> bool:
    if not text:
        return False
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return bool(_SUSPENDED.search(text))


def _detail(text: str | bytes) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    line = next((line for line in text.splitlines() if _SUSPENDED.search(line)), "")
    line = "".join(ch for ch in line if ch.isprintable()).strip()
    return line[: _DETAIL_MAX - 1] + "…" if len(line) > _DETAIL_MAX else line


def observe(
    authority: Authority | None,
    *,
    ok: bool,
    text: str | bytes | None = None,
    now: float | None = None,
) -> None:
    """Record one call's outcome as ``authority``.

    A success clears the authority; a failure whose output carries GitHub's suspension
    words marks it; any other failure changes nothing, because a 404 or a timeout says
    nothing about the account.
    """
    if authority is None:
        return
    name = authority.name
    owner = authority.owner
    with _lock:
        held = _suspended.get(name)
        if ok:
            _suspended.pop(name, None)
        elif is_suspension(text):
            owners = tuple(sorted({*(held.owners if held else ()), owner} - {""}))
            _suspended[name] = Suspension(
                authority=name,
                login=authority.login,
                owners=owners,
                since=held.since if held else (time.time() if now is None else now),
                detail=_detail(text or ""),
            )
        changed = _suspended.get(name) != held
        listeners = tuple(_listeners) if changed else ()
    for listener in listeners:
        try:
            listener()
        except Exception:  # noqa: BLE001 -- telling the pill must never fail a gh call
            log.exception("github suspension listener failed")


def add_listener(listener: Callable[[], None]) -> Callable[[], None]:
    """Call ``listener()`` whenever the held suspensions change; returns its remover.

    The deploy pill's slot shows these, so the collector is told rather than asking
    every few seconds (#2865). It may be called from whichever thread made the call.
    """
    with _lock:
        _listeners.append(listener)

    def remove() -> None:
        with _lock, contextlib.suppress(ValueError):
            _listeners.remove(listener)

    return remove


def _still_speaks(authority: str, owner: str) -> bool:
    try:
        return github_authority.authority_for(owner).name == authority
    except github_authority.Refusal:
        return False


def suspensions() -> tuple[Suspension, ...]:
    """Every authority GitHub last called suspended, oldest first.

    Only for owners it still speaks for: once ``github-authority.toml`` moves an owner
    to another authority — the fix for exactly this — the old one stops being shown
    without waiting for a restart. Comparable, so the collector's deploy-pill watch
    (woken by ``add_listener`` and by a write to that file) publishes exactly when
    this changes.
    """
    with _lock:
        held = sorted(_suspended.values(), key=lambda item: (item.since, item.authority))
    shown = []
    for item in held:
        owners = tuple(owner for owner in item.owners if _still_speaks(item.authority, owner))
        if owners:
            shown.append(item if owners == item.owners else replace(item, owners=owners))
    return tuple(shown)


def for_owner(owner: str | None) -> Suspension | None:
    """The suspension that covers ``owner``, if its authority was seen suspended for it."""
    key = str(owner or "").strip().lower()
    if not key:
        return None
    return next((item for item in suspensions() if key in item.owners), None)


def sentence(item: Suspension) -> str:
    """The one line a person reads, wherever it shows."""
    owners = ", ".join(f"{owner}/*" for owner in item.owners) or "its owners"
    return (
        f"GitHub account suspended: {item.login} ({item.authority}) is refused for "
        f"{owners}, so every GitHub read and write the deck makes there fails."
    )


def reset() -> None:
    """Forget everything. For tests."""
    with _lock:
        _suspended.clear()
