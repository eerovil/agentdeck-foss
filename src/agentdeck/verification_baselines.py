"""Where the host keeps a reusable base-branch verification result (#1737).

The heavy-command queue already runs the suites this stores.  A run over a clean tree — the
live checkout on ``master``, a merge probe, a card that happened to verify an unedited base —
is a fact about the *repository at that commit*, not about the card that paid for it, so it is
kept here and every later card compares against it instead of running the base suite again.

Small on purpose.  One JSON file next to ``jobs.json``, one entry per
``poller_engine.baseline.baseline_key``, newest wins, bounded by count and age.  Nothing here
decides anything: what qualifies as a base result and what a comparison means live in
``poller_engine.baseline`` so they stay pure and testable, and the poller engine reads this
file without going through the runtime.

Losing an entry costs one ``comparison_unavailable``, which is what the world looked like
before this existed, so every failure path here degrades quietly rather than raising.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["BaselineLedger", "DEFAULT_FILENAME"]

DEFAULT_FILENAME = "verification_baselines.json"
# A base commit older than this is behind too many merges for any live card to share it.
MAX_AGE_S = 14 * 24 * 3600.0
MAX_ENTRIES = 200


class BaselineLedger:
    """Reusable base-branch results, keyed by commit, command and environment."""

    def __init__(
        self,
        path: Path,
        *,
        max_entries: int = MAX_ENTRIES,
        max_age_s: float = MAX_AGE_S,
        clock: Any = time.time,
    ) -> None:
        self.path = Path(path)
        self._max_entries = max_entries
        self._max_age_s = max_age_s
        self._clock = clock

    def lookup(self, key: str) -> dict | None:
        """The stored result for ``key``, or None when there is none.

        Age is not a filter here. The key names an exact commit, so an old entry for it is
        still a true statement about that commit; the age bound below is housekeeping, and
        applying it on read as well would let this and the poller engine — which reads the
        same file directly — disagree about the same entry.
        """
        if not key:
            return None
        return next((entry for entry in self._read() if entry.get("key") == key), None)

    def record(self, entry: dict) -> bool:
        """Store one base result, replacing any earlier result for the same key."""
        key = str(entry.get("key") or "")
        if not key:
            return False
        now = self._clock()
        # The age bound compacts what was already here and never judges what is arriving: a
        # run that just finished is fresh by definition, whatever its clock says.
        kept = [
            item
            for item in self._read()
            if item.get("key") != key
            and now - float(item.get("recorded_at") or 0.0) <= self._max_age_s
        ]
        kept.append(dict(entry))
        kept.sort(key=lambda item: float(item.get("recorded_at") or 0.0))
        return self._write(kept[-self._max_entries :])

    def _read(self) -> list[dict]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            log.warning("baselines: could not read %s; treating it as empty", self.path)
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _write(self, entries: list[dict]) -> bool:
        temporary = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(entries, indent=1), encoding="utf-8")
            os.replace(temporary, self.path)
            return True
        except OSError:
            log.exception("baselines: could not write %s", self.path)
            return False
