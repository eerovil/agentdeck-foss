"""What an issue's comments say about its Work, read from the webhook log (#3855).

Another deck's Work (#3834) has its status in that deck's database, which this deck cannot
reach. Its issue says some of it: the spec an agent posted for approval, the reviewer's
findings at a pull request head, and the reviewer's ``/reviewed pass`` at a head. A consumer of
the raw webhook streams, like ``issue_latest_comment``: every ``issue_comment`` delivery carries
the comment's body, so this keeps per issue the newest spec comment and, for each pull request
head a reviewer named, the newest verdict on it, in the shared GitHub metadata cache. No GitHub
read, ever. A verdict is kept by head, not by kind: a Work with two pull requests has a verdict
for each, and a pass on one never covers findings on the other (#3855 review). Known limit,
accepted by the owner on #3855: two pull requests of one Work at the same commit (into
different target branches) share that commit's verdict, though a ``/reviewed`` line names one.

* **Its own consumer name.** It starts at offset 0, so the first start replays the streams
  already on disk. An issue whose comments are older than the kept log has no marks, which
  reads as no verdict, never a wrong one.
* **Newest by comment id.** Ids only grow, so a replay or a delivery out of order never moves a
  mark back. An edit counts for the comment it edits: one edited out of a kind leaves it, and a
  deleted one is cleared. At most ``MAX_HEADS`` heads are kept, the newest.
* **Issues only.** A pull request's conversation comment arrives as the same event with
  ``issue.pull_request`` set, and is skipped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .github_cache import GitHubMetadata, GitHubMetadataCache
from .issue_latest_comment import LatestCommentService, _comment_id, _default_cache
from .poller_engine.github import BOT_MARKER, _is_review_comment
from .poller_engine.intake import parse_verdicts
from .webhook_tailer import Delivery
from .work_signals import SPEC_MARKER

log = logging.getLogger(__name__)

CONSUMER = "issue-marks"
_KEY = "issue-marks:v1:{ref}"

SPEC = "spec"
FINDINGS = "findings"
PASSED = "passed"
# How many reviewed heads an issue keeps: far more than one Work's rounds of review need.
MAX_HEADS = 20

# The reviewer's findings heading names the head it read: "Review findings for PR #12 at <sha>".
_FINDINGS_HEAD_RE = re.compile(r"Review findings for PR #\d+ at ([0-9a-fA-F]{7,40})")


def cache_key(ref: str) -> str:
    return _KEY.format(ref=ref.lower())


@dataclass(frozen=True)
class Mark:
    """One comment: its id, when it was posted, its link, the head it names and its kind."""

    id: int
    at: str = ""
    url: str = ""
    head: str = ""
    kind: str = ""

    def names(self, head: str) -> bool:
        """Whether this mark is about ``head``, a full commit id; a short head counts."""
        return bool(self.head and head) and head.lower().startswith(self.head.lower())


@dataclass(frozen=True)
class IssueMarks:
    spec: Mark | None = None
    verdicts: tuple[Mark, ...] = ()

    def verdict(self, head: str) -> Mark | None:
        """The newest verdict on ``head`` (``FINDINGS`` or ``PASSED``), or None."""
        return max(
            (mark for mark in self.verdicts if mark.names(head)),
            key=lambda mark: mark.id,
            default=None,
        )


def classify(body: object) -> dict[str, str]:
    """What a comment body is: ``{SPEC: ""}``, or each head it gives a verdict on with that
    verdict (``FINDINGS`` or ``PASSED``); empty for anything else. A findings handoff that names
    no head is no verdict."""
    if not isinstance(body, str):
        return {}
    if SPEC_MARKER in body:
        return {SPEC: ""}
    found: dict[str, str] = {}
    for verdict in parse_verdicts(body, bot_marker=BOT_MARKER) or ():
        if verdict.is_valid and verdict.head:
            # ``changes-needed`` and ``no-code`` both say the reviewer found something.
            found[verdict.head.lower()] = PASSED if verdict.verdict == "pass" else FINDINGS
    if found:
        return found
    if _is_review_comment({"body": body}):
        match = _FINDINGS_HEAD_RE.search(body)
        if match:
            return {match.group(1).lower(): FINDINGS}
    return {}


def _held_id(value: object) -> int:
    return (_comment_id(value.get("id")) or 0) if isinstance(value, dict) else 0


class IssueMarksService(LatestCommentService):
    """Tail the streams and keep each issue's newest spec and each head's newest verdict.
    Web-process owned."""

    consumer = CONSUMER

    def apply(self, delivery: Delivery) -> bool:
        """Apply one delivery; True when another deck's Work may read differently now.

        That is when a mark changed, or when a comment landed on an issue with a spec: the spec
        waits only while it is the newest comment (``issue_latest_comment``). Idempotent, and
        indifferent to arrival order.
        """
        if delivery.event != "issue_comment":
            return False
        payload = delivery.payload
        issue = payload.get("issue")
        comment = payload.get("comment")
        if not isinstance(issue, dict) or not isinstance(comment, dict):
            return False
        if issue.get("pull_request") is not None:
            return False
        number = issue.get("number")
        comment_id = _comment_id(comment.get("id"))
        if not isinstance(number, int) or isinstance(number, bool) or comment_id is None:
            return False
        action = payload.get("action")
        if action not in {"created", "edited", "deleted"}:
            return False
        key = cache_key(f"{delivery.stream}#{number}")
        held = self._cache.peek(key).value
        held = held if isinstance(held, dict) else {}
        spec = held.get(SPEC)
        heads = dict(held.get("heads")) if isinstance(held.get("heads"), dict) else {}
        found = classify(comment.get("body")) if action != "deleted" else {}

        def mark(**extra: str) -> dict:
            return {
                "id": comment_id,
                "at": str(comment.get("created_at") or ""),
                "url": str(comment.get("html_url") or ""),
                **extra,
            }

        # This comment's own earlier marks go first: it may have been edited or deleted.
        if _held_id(spec) == comment_id:
            spec = None
        heads = {head: value for head, value in heads.items() if _held_id(value) != comment_id}
        if SPEC in found:
            if comment_id >= _held_id(spec):
                spec = mark()
        else:
            for head, kind in found.items():
                if comment_id >= _held_id(heads.get(head)):
                    heads[head] = mark(kind=kind)
        newest = sorted(heads.items(), key=lambda item: _held_id(item[1]), reverse=True)
        value = {SPEC: spec, "heads": dict(newest[:MAX_HEADS])}
        if value != {SPEC: held.get(SPEC), "heads": held.get("heads") or {}}:
            # Terminal: nothing ages this out, and no fetch exists to refresh it.
            self._cache.offer(key, GitHubMetadata(value, terminal=True))
            return True
        return spec is not None


def _mark(value: object, head: str = "") -> Mark | None:
    if not isinstance(value, dict):
        return None
    comment_id = _comment_id(value.get("id"))
    if comment_id is None:
        return None
    return Mark(
        comment_id,
        at=str(value.get("at") or ""),
        url=str(value.get("url") or ""),
        head=head,
        kind=str(value.get("kind") or ""),
    )


def issue_marks(ref: str, cache: GitHubMetadataCache | None = None) -> IssueMarks:
    """Issue ``ref``'s (``owner/repo#n``) marks; none when this deck has seen no such comment."""
    try:
        held = (cache or _default_cache()).peek(cache_key(ref)).value
    except Exception:  # a caller must go on whatever the cache file is doing
        log.debug("issue marks read failed for %s", ref, exc_info=True)
        return IssueMarks()
    if not isinstance(held, dict):
        return IssueMarks()
    heads = held.get("heads") if isinstance(held.get("heads"), dict) else {}
    verdicts = (_mark(value, str(head)) for head, value in heads.items())
    return IssueMarks(
        spec=_mark(held.get(SPEC)),
        verdicts=tuple(mark for mark in verdicts if mark is not None and mark.kind),
    )
