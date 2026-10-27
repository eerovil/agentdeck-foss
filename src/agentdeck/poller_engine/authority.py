"""Who authorized this card's work (#1659, #2056).

``authorization.py`` decides *whether* a login may steer a project. This records *which*
login already did, so the engine can tell a card somebody with accepted authority put in play
from one nobody did.

This used to be stamped per dispatch as well, for the superseded worker's handoff report to
inherit. The report is gone (#2159): a new Run only starts once the Runtime proves the old one
can no longer write, so no superseded worker is left running to report.

An engine-authored instruction (an engine round, an extension proposal) is recorded the
same way under its own non-person author. That is not a person's authority and is not
pretending to be one; it is the provenance the ledger already carries.
"""

from __future__ import annotations

_WORK = "work_authority"


def record(rec: dict | None, *, login: str, source: str, at: str = "") -> None:
    """Remember the authorized actor who last handed this card's work to the engine."""
    if rec is None or not login:
        return
    rec[_WORK] = {"login": str(login), "source": str(source), "at": str(at or "")}


def current(rec: dict | None) -> dict:
    """The card's standing work authority, or an empty mapping when none was recorded."""
    value = (rec or {}).get(_WORK)
    return dict(value) if isinstance(value, dict) and value.get("login") else {}
