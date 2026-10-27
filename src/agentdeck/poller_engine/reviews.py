"""What each merge review decided about one commit, kept after the card forgets it.

A verdict is posted as an issue comment, and the only record of *which commit* it covered
lives on the card as ``review_heads`` — which ``release.handback`` and
``release.consume_approval`` drop the moment the release progression consumes the verdict.
Nothing therefore remembers that a particular commit was reviewed, what the verdict was, or
what happened next (#1122).

The engine does not write the deck's database: the database belongs to the web process.
So a consumed verdict is written here as one small file and the
host drains it on a later wake, the same engine-decides / host-delivers split every other
outward effect uses.

One file per ``(repo, sha)``, named by digest, because that is the key the record is asked
for: a second verdict on the same commit replaces the first rather than accumulating, and
the directory stays bounded by the commits actually reviewed. Every write and read fails
open — losing a history record must never cost a release.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from . import state

SUFFIX = ".review"


def record_path(directory, repo: str, sha: str) -> Path:
    """One record per reviewed commit. The name is a digest so no SHA can escape the path."""
    digest = hashlib.sha256(f"{repo}@{sha}".encode()).hexdigest()
    return Path(directory) / f"{digest}{SUFFIX}"


def record(
    directory,
    *,
    repo: str,
    sha: str,
    pull_request: str,
    issue_repo: str,
    issue_number: int,
    verdict: str,
    result: str,
    generation: str,
    now: datetime,
) -> Path | None:
    """Write one consumed verdict atomically; None when the filesystem refused it.

    A half-written record must never read as one, so this lands through the same temp-file
    and rename dance the acknowledgement receipts use.
    """
    if not repo or not sha:
        return None
    path = record_path(directory, repo, sha)
    body = {
        "repo": repo,
        "sha": sha,
        "pull_request": pull_request,
        "issue_repo": issue_repo,
        "issue_number": int(issue_number),
        "verdict": verdict,
        "result": result,
        "generation": generation,
        "reviewed_at": now.astimezone(UTC).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w") as handle:
            json.dump(body, handle, indent=2)
            handle.flush()
            if state.durable_writes():
                os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        return None
    return path


def pending(directory) -> list[tuple[Path, dict]]:
    """Every readable record waiting to be drained, oldest name first.

    An unreadable or half-written file is skipped rather than raising: one damaged record
    must not stop the rest of the history reaching the database.
    """
    try:
        names = sorted(Path(directory).glob(f"*{SUFFIX}"))
    except OSError:
        return []
    out: list[tuple[Path, dict]] = []
    for name in names:
        try:
            with open(name) as handle:
                body = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(body, dict) and body.get("repo") and body.get("sha"):
            out.append((name, body))
    return out


def clear(path) -> bool:
    """Discard a drained record; True when it is gone (including never having existed)."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        return False
    return True
