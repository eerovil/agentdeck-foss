"""Which required checks a pull request's auto-merge is waiting on that never reported (#2275),
and whether GitHub would merge the pull request's head at all (#2332).

GitHub auto-merge waits for every required status check of the base branch. A check that
never starts — a CI that missed the pull request's event — holds the merge for good, and
nothing about the pull request says so: it is open, reviewed and green on every check that
did run. This module reads the one GraphQL query that tells the two apart and decides, from
local rows only, whether a Work's output is blocked that way.

Pure apart from the query text: the fetch lives in ``git_context`` beside the pull request
read it shares a cache and credentials with.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

# How long auto-merge must have been on before a required check that has not reported
# counts as never started: a CI that picked the event up posts ``pending`` within seconds,
# so ten minutes of silence is not a slow build.
GRACE = timedelta(minutes=10)

# v2 carries GitHub's merge answer too (#2332): a v1 row, which has none, must never read as
# "no conflict", so it is simply not found.
CACHE_KEY = "pr-merge-checks:v2:{repository}:{number}"

# ``refUpdateRule`` is the protection enforced on the viewer, rulesets included;
# ``branchProtectionRule`` is the classic rule itself, which an admin viewer is exempt from
# unless admins are enforced. Either can name a check GitHub will wait for.
QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state
      headRefOid
      mergeable
      mergeStateStatus
      autoMergeRequest { enabledAt }
      baseRef {
        refUpdateRule { requiredStatusCheckContexts }
        branchProtectionRule { requiredStatusCheckContexts }
      }
      commits(last: 1) {
        nodes {
          commit {
            oid
            committedDate
            statusCheckRollup {
              contexts(first: 100) {
                pageInfo { hasNextPage }
                nodes {
                  __typename
                  ... on StatusContext { context }
                  ... on CheckRun { name }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def cache_key(repository: str, number: int) -> str:
    return CACHE_KEY.format(repository=repository.lower(), number=number)


def _strings(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def parse(value: object, *, checked_at: datetime) -> dict | None:
    """The cached row for one GraphQL answer, or None when the answer is not readable.

    ``missing`` is every required check with no status and no check run on the head commit,
    sorted; it is empty whenever the reported list may have been cut off, because a check
    past the first page is not a check that never ran.
    """
    data = value.get("data") if isinstance(value, dict) else None
    repository = data.get("repository") if isinstance(data, dict) else None
    pull = repository.get("pullRequest") if isinstance(repository, dict) else None
    if not isinstance(pull, dict):
        return None
    head = pull.get("headRefOid")
    if not isinstance(head, str) or not head:
        return None
    auto = pull.get("autoMergeRequest")
    enabled = auto.get("enabledAt") if isinstance(auto, dict) else None
    base = pull.get("baseRef") if isinstance(pull.get("baseRef"), dict) else {}
    required: set[str] = set()
    for rule in ("refUpdateRule", "branchProtectionRule"):
        found = base.get(rule)
        if isinstance(found, dict):
            required |= _strings(found.get("requiredStatusCheckContexts"))
    reported: set[str] = set()
    complete = False
    commits = pull.get("commits")
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    commit = nodes[-1].get("commit") if isinstance(nodes, list) and nodes and isinstance(
        nodes[-1], dict
    ) else None
    if isinstance(commit, dict) and commit.get("oid") == head:
        rollup = commit.get("statusCheckRollup")
        if rollup is None:
            complete = True  # nothing has reported on this commit at all
        elif isinstance(rollup, dict) and isinstance(contexts := rollup.get("contexts"), dict):
            page = contexts.get("pageInfo")
            complete = isinstance(page, dict) and page.get("hasNextPage") is False
            for node in contexts.get("nodes") or ():
                if isinstance(node, dict):
                    name = node.get("context") or node.get("name")
                    if isinstance(name, str):
                        reported.add(name)
    return {
        "state": pull.get("state") if isinstance(pull.get("state"), str) else "",
        "head": head,
        "mergeable": _upper(pull.get("mergeable")),
        "mergeState": _upper(pull.get("mergeStateStatus")),
        "autoMergeEnabledAt": enabled if isinstance(enabled, str) else None,
        "missing": sorted(required - reported) if complete else [],
        "checkedAt": checked_at.isoformat(),
        # When the head commit was made (#3199), for a Work summary written before it.
        "headAt": (
            commit.get("committedDate")
            if isinstance(commit, dict)
            and commit.get("oid") == head
            and isinstance(commit.get("committedDate"), str)
            else None
        ),
    }


def _upper(value: object) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


def merge_answer(checks: Mapping[str, Any] | None, head: str) -> tuple[str, str]:
    """GitHub's ``(mergeable, mergeStateStatus)`` for exactly ``head``, else ``("", "")``.

    Read from a ``parse`` row only when that row is about the head the deck holds now: a
    conflict found on an older commit says nothing about the one a person would merge.
    """
    if not isinstance(checks, Mapping) or not head or checks.get("head") != head:
        return "", ""
    return str(checks.get("mergeable") or ""), str(checks.get("mergeState") or "")


def head_committed_at(checks: Mapping[str, Any] | None, head: str) -> datetime | None:
    """When ``head`` was committed, from a ``parse`` row about exactly that head (#3199)."""
    if not isinstance(checks, Mapping) or not head or checks.get("head") != head:
        return None
    return _when(checks.get("headAt"))


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def armed(row: Mapping[str, Any]) -> bool:
    """The cached pull request row says auto-merge is on (``autoMergeRequest``)."""
    return isinstance(row.get("autoMergeRequest"), dict)


def never_ran(row: Mapping[str, Any], checks: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The required checks an armed pull request has waited on past ``GRACE`` without any
    report; () whenever that is not known for this exact head.

    ``row`` is the cached pull request row and ``checks`` its ``parse`` row. Both must say
    auto-merge is on — the pull request row is what a webhook rewrites within seconds of it
    being turned off — and both must be about the same head, because a push restarts CI.
    They must also be about the same arming: auto-merge turned off and on again on that head
    is a new wait with its own grace, so a read made before it is not evidence for it.
    """
    if not checks or not armed(row):
        return ()
    head = row.get("headRefOid")
    if not head or checks.get("head") != head or str(checks.get("state")) != "OPEN":
        return ()
    current = _when(row["autoMergeRequest"].get("enabledAt"))
    enabled = _when(checks.get("autoMergeEnabledAt"))
    if current is not None and enabled != current:
        return ()
    checked = _when(checks.get("checkedAt"))
    if enabled is None or checked is None or checked - enabled < GRACE:
        return ()
    missing = checks.get("missing")
    if not isinstance(missing, list):
        return ()
    return tuple(name for name in missing if isinstance(name, str) and name)
