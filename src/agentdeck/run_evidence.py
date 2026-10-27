"""What each Run's verification jobs tested, kept after the queue forgets them (#2148).

The job queue prunes a finished row a day after it is announced, which is right for a queue and
wrong for evidence: a Run's A1/B4 has to still say A1/B4 after the worktree is gone and the
Work has moved on to A2/B4. So when a verification job finishes, the fields that make it
evidence — who it was for, how it ended and the repository-keyed snapshots — are appended here
before the queue records the job as finished, one JSON line per job, flushed to disk.

One file per Work, and nothing here removes a line: not by age and not by a global count, so
one busy Work can never push another Work's history out. Retention follows the Work — deleting
a Work's file is the Work lifecycle's decision (Work-owned workspace claims are #2149), not the
queue's.

It is the job row's own shape, trimmed, so ``poller_engine.verification`` reads a line exactly
as it reads a row; there is no second evidence model. The job id is the identity. The jobs the
queue runs *for* a verification -- reruns of its failing tests, a run on its merge base
(#2382) -- are kept in the same Work's file, because whether that verification counts as green
is worked out from it and them together, long after the queue forgot the rows.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

__all__ = ["DEFAULT_DIRNAME", "RunEvidenceLedger"]

DEFAULT_DIRNAME = "run_evidence"
# Where a verification job correlated to no Work is kept; a Work id cannot take this name.
_NO_WORK = "_no_work"

# The job-row fields a verification line keeps: identity, correlation, outcome and what ran.
FIELDS = (
    "id",
    "purpose",
    "work_id",
    "run_id",
    "state",
    "result_class",
    "exit_code",
    "submitted_at",
    "finished_at",
    "command",
    "lane",
    "tested_workspace",
    "verified_workspace",
    "tested_workspaces",
    "verified_workspaces",
    # What it failed, and what a follow-up was for and ran (#2382).
    "failures",
    "failures_complete",
    "follows",
    "follow_kind",
    "follow_tests",
    # Which failures the merge base already has; the rest are never forgiven (#2552).
    "base_present",
)


class RunEvidenceLedger:
    """Append-only verification evidence, one file per Work."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def path(self, work_id: str) -> Path:
        name = quote(work_id, safe="") if work_id else _NO_WORK
        return self.directory / f"{name}.jsonl"

    def record(self, row: dict) -> bool:
        """Durably append one finished verification job's evidence.

        True only once the line is on disk (or was already); the caller treats False as a
        completion that did not happen. A repeat of a job id is a no-op.
        """
        job_id = str(row.get("id") or "")
        if not job_id:
            return False
        path = self.path(str(row.get("work_id") or ""))
        if any(entry.get("id") == job_id for entry in self._read(path)):
            return True
        entry = {key: row[key] for key in FIELDS if row.get(key) not in (None, "", [], {})}
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+b") as handle:
                # A crash mid-append leaves a torn last line; start a fresh one rather than
                # gluing this entry onto it, where the reader would drop both.
                handle.seek(0, os.SEEK_END)
                torn = False
                if handle.tell():
                    handle.seek(-1, os.SEEK_END)
                    torn = handle.read(1) != b"\n"
                handle.write((("\n" if torn else "") + line).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            log.exception("run evidence: could not append to %s", path)
            return False
        return True

    def rows(self, *, work_id: str = "") -> list[dict]:
        """One Work's recorded lines, oldest first; every Work's when none is named."""
        if work_id:
            return self._read(self.path(work_id))
        try:
            paths = sorted(self.directory.glob("*.jsonl"))
        except OSError:
            return []
        return [entry for path in paths for entry in self._read(path)]

    @staticmethod
    def _read(path: Path) -> list[dict]:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            log.warning("run evidence: could not read %s", path)
            return []
        found: list[dict] = []
        for line in text.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line from a crash mid-append
            if isinstance(entry, dict):
                found.append(entry)
        return found
