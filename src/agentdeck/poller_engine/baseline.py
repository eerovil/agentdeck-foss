"""Whether a failing verification run is this branch's fault or the base branch's (#1737).

A worker that runs six thousand tests and sees four of them fail has learned nothing until it
knows whether those four also fail on the commit it branched from.  Today it finds out by
running the base suite itself, which is why #1718 paid for the same six thousand tests several
times over.  Base-branch health is host state; it should be looked up, not rediscovered.

So a run over a *clean* tree is recorded as a reusable base result, keyed by the repository,
the exact commit, the command and the environment it ran in.  When a branch's verification
fails, its failures are compared against the result for its own merge base and the outcome is
one of three things:

``branch_regression``
    At least one failure is not in the base result.  Blocks and retries exactly as before.
``pre_existing``
    Every failure is also in the base result.  Reported, and not investigated again.
``comparison_unavailable``
    There is no trustworthy comparable result, or the failure list could not be read in full.
    Behaves exactly as it did before this existed.

The bar for saying anything other than ``comparison_unavailable`` is deliberately high, in both
directions.  Calling a real regression pre-existing would let a broken branch through, and it is
the only one of the three mistakes that is silent, so every input has to be *proved* comparable:
a provable clean base snapshot, the same command fingerprint, the same environment, and a
failure list that is known to be complete rather than merely the part that fit.

Nothing here reads a file or runs a command -- the host extracts the failure list where the log
already is, and this module decides what it means.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

__all__ = [
    "BRANCH_REGRESSION",
    "COMPARISON_UNAVAILABLE",
    "Comparison",
    "MAX_FAILURES",
    "PRE_EXISTING",
    "baseline_entry",
    "baseline_key",
    "brief",
    "compare",
    "extract_failures",
    "lookup_key",
]

BRANCH_REGRESSION = "branch_regression"
PRE_EXISTING = "pre_existing"
COMPARISON_UNAVAILABLE = "comparison_unavailable"

# Past this many distinct failures nobody is reading a list anyway, and a run that broke this
# comprehensively is not one to wave through on a comparison.  The list is kept but marked
# incomplete, which lands it in ``comparison_unavailable``.
MAX_FAILURES = 200

# pytest prints this immediately before the one list of failures that is complete by
# construction.  Seeing the header is how a bounded read of the end of a log proves it holds
# the whole list rather than the tail of one.
_SUMMARY_HEADER = "short test summary info"
_SUMMARY_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")


def extract_failures(text: str, *, succeeded: bool = False) -> tuple[tuple[str, ...], bool]:
    """The failing test ids in a run's output, and whether that list is provably complete.

    ``text`` is the end of a log, so a list is only complete when the summary header that
    opens it is inside the part that was read.  A command that is not pytest prints no such
    header and therefore never compares -- the honest answer for output nobody can parse.

    ``succeeded`` short-circuits all of that, because a run that exited zero failed nothing and
    needs no output read to prove it.  Without this the feature has no clean-base case at all:
    pytest prints the summary section only when it has something to put in it, so an all-pass
    suite -- exactly the green baseline every card wants to compare against -- would be filed
    as an unreadable failure list and never recorded.
    """
    if succeeded:
        return (), True
    header = text.rfind(_SUMMARY_HEADER)
    if header < 0:
        return (), False
    seen: list[str] = []
    unique: set[str] = set()
    for line in text[header:].splitlines()[1:]:
        match = _SUMMARY_LINE.match(line.strip())
        if match is None:
            continue
        node = match.group(1)
        if node not in unique:
            unique.add(node)
            seen.append(node)
    if len(seen) > MAX_FAILURES:
        return tuple(sorted(seen[:MAX_FAILURES])), False
    return tuple(sorted(seen)), True


def baseline_key(
    *,
    repo_identity: str,
    head: str,
    command_fingerprint: str,
    environment: str,
) -> str:
    """The identity a reusable result is stored and found under.

    Every part is required.  Invalidation needs no expiry rule: a new base commit, a different
    command or a rebuilt runner image simply produces a key nothing was ever stored under.
    """
    if not (repo_identity and head and command_fingerprint and environment):
        return ""
    return hashlib.sha256(
        json.dumps(
            {
                "repo": repo_identity,
                "head": head,
                "command": command_fingerprint,
                "environment": environment,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def baseline_entry(job: dict) -> dict | None:
    """This finished job as a reusable base result, or None if it is not one.

    A job qualifies only when it tested a clean provable tree -- ``head`` alone then describes
    what ran, which is the whole point of storing it -- and when its failure list is known to
    be complete.  The result may be red; a red base is exactly the case this exists for.
    """
    workspace = job.get("tested_workspace")
    if not isinstance(workspace, dict) or not workspace.get("provable"):
        return None
    if not workspace.get("clean"):
        return None
    if job.get("result_class") not in {"success", "failure"}:
        return None
    if not bool(job.get("failures_complete")):
        return None
    key = baseline_key(
        repo_identity=str(workspace.get("repo_identity") or ""),
        head=str(workspace.get("head") or ""),
        command_fingerprint=str(workspace.get("command_fingerprint") or ""),
        environment=str(job.get("environment") or ""),
    )
    if not key:
        return None
    return {
        "key": key,
        "repo_identity": str(workspace.get("repo_identity") or ""),
        "head": str(workspace.get("head") or ""),
        "command_fingerprint": str(workspace.get("command_fingerprint") or ""),
        "environment": str(job.get("environment") or ""),
        "failures": [str(item) for item in job.get("failures") or ()],
        "result_class": str(job.get("result_class") or ""),
        "job_id": str(job.get("id") or ""),
        "recorded_at": float(job.get("finished_at") or 0.0),
    }


def lookup_key(job: dict) -> str:
    """The key of the base result this branch job would need, or empty if it can have none."""
    workspace = job.get("tested_workspace")
    if not isinstance(workspace, dict):
        return ""
    return baseline_key(
        repo_identity=str(workspace.get("repo_identity") or ""),
        head=str(workspace.get("base_head") or ""),
        command_fingerprint=str(workspace.get("command_fingerprint") or ""),
        environment=str(job.get("environment") or ""),
    )


@dataclass(frozen=True)
class Comparison:
    """What a failing run's failures were attributed to."""

    verdict: str
    branch_only: tuple[str, ...] = ()
    pre_existing: tuple[str, ...] = ()
    base_head: str = ""
    base_job_id: str = ""
    reason: str = ""

    @property
    def blocks(self) -> bool:
        """Whether this failure still stops the card.

        Only a proved pre-existing failure does not.  An unavailable comparison blocks, because
        not knowing is not the same as knowing it was already broken.
        """
        return self.verdict != PRE_EXISTING

    def as_record(self) -> dict:
        return {
            "verdict": self.verdict,
            "branch_only": list(self.branch_only),
            "pre_existing": list(self.pre_existing),
            "base_head": self.base_head,
            "base_job_id": self.base_job_id,
            "reason": self.reason,
        }

    @classmethod
    def from_record(cls, record: object) -> Comparison | None:
        if not isinstance(record, dict) or not record.get("verdict"):
            return None
        return cls(
            verdict=str(record.get("verdict") or ""),
            branch_only=tuple(str(item) for item in record.get("branch_only") or ()),
            pre_existing=tuple(str(item) for item in record.get("pre_existing") or ()),
            base_head=str(record.get("base_head") or ""),
            base_job_id=str(record.get("base_job_id") or ""),
            reason=str(record.get("reason") or ""),
        )


def compare(job: dict, entry: object) -> Comparison:
    """Attribute one failing job's failures, given the base result stored for it (or none)."""
    workspace = job.get("tested_workspace")
    workspace = workspace if isinstance(workspace, dict) else {}
    base_head = str(workspace.get("base_head") or "")
    if not workspace.get("provable"):
        return Comparison(
            COMPARISON_UNAVAILABLE, reason="this run's workspace could not be proved"
        )
    if not base_head:
        return Comparison(
            COMPARISON_UNAVAILABLE,
            reason="this checkout's base commit could not be resolved",
        )
    if not bool(job.get("failures_complete")):
        return Comparison(
            COMPARISON_UNAVAILABLE,
            base_head=base_head,
            reason="this run's failure list could not be read in full",
        )
    if not isinstance(entry, dict) or entry.get("key") != lookup_key(job):
        return Comparison(
            COMPARISON_UNAVAILABLE,
            base_head=base_head,
            reason=(
                f"no trustworthy result for base {base_head[:12] or 'unknown'} "
                "with this command and environment"
            ),
        )
    failures = tuple(str(item) for item in job.get("failures") or ())
    known = {str(item) for item in entry.get("failures") or ()}
    branch_only = tuple(name for name in failures if name not in known)
    shared = tuple(name for name in failures if name in known)
    base_job_id = str(entry.get("job_id") or "")
    if branch_only or not failures:
        # No failures at all with a failing exit code means the failure was never in the test
        # list -- a collection error, a crashed runner. That is not attributable either.
        return Comparison(
            BRANCH_REGRESSION if failures else COMPARISON_UNAVAILABLE,
            branch_only=branch_only,
            pre_existing=shared,
            base_head=base_head,
            base_job_id=base_job_id,
            reason="" if failures else "this run failed without naming a failing test",
        )
    return Comparison(
        PRE_EXISTING,
        pre_existing=shared,
        base_head=base_head,
        base_job_id=base_job_id,
    )


_MAX_LISTED = 20


def brief(comparison: Comparison) -> str:
    """What the card and the next worker brief are told, in one short block."""
    base = comparison.base_head[:12] or "unknown"
    if comparison.verdict == PRE_EXISTING:
        lines = [
            f"Verification failed, and every failure already fails on base {base}. "
            "Classified `pre-existing base failure`: it is not this branch's regression and "
            "must not be investigated again here.",
            _listed("Already failing on the base", comparison.pre_existing),
        ]
    elif comparison.verdict == BRANCH_REGRESSION:
        lines = [
            f"Verification failed with failures this branch introduced against base {base}. "
            "Classified `branch regression`: fix these.",
            _listed("Introduced by this branch", comparison.branch_only),
        ]
        if comparison.pre_existing:
            lines.append(
                _listed(
                    "Also failing, but already failing on the base (do not chase these)",
                    comparison.pre_existing,
                )
            )
    else:
        lines = [
            "Verification failed and could not be compared with the base branch. "
            f"Classified `comparison unavailable`: {comparison.reason or 'no reason recorded'}. "
            "Treat every failure as this branch's until shown otherwise.",
        ]
    if comparison.base_job_id:
        lines.append(f"Base result: job {comparison.base_job_id}.")
    return "\n".join(line for line in lines if line)


def _listed(heading: str, names: tuple[str, ...]) -> str:
    if not names:
        return ""
    shown = list(names[:_MAX_LISTED])
    body = "\n".join(f"- {name}" for name in shown)
    omitted = len(names) - len(shown)
    if omitted > 0:
        body += f"\n- ... and {omitted} more"
    return f"{heading} ({len(names)}):\n{body}"
