"""Point the deck's issue links at the issue's newest comment (#2424).

A consumer of the raw webhook streams, and the filter that reads what it wrote. An
``issue_comment`` delivery names its comment, so this keeps one small row per issue in the
shared GitHub metadata cache — the newest comment id seen — and every deck link that opens
an issue gets ``#issuecomment-<id>`` from it at render time. No GitHub read, ever: an issue
this has not seen a comment on keeps its plain URL.

* **Its own consumer name, not the pill cache's.** A new name starts at offset 0, so the
  first start replays the live streams already on disk and existing issues get their anchor
  on day one instead of from their next comment.
* **The comment id is the version.** GitHub comment ids only grow, so the larger id wins
  whatever order deliveries arrive in: a replay, a rotation or two comments in the same
  second can never move the anchor back. This is the tailer's merge-by-newest rule with the
  comment's own id standing in for ``updated_at``, which two comments can share.
* **Issues only.** A pull request's conversation comment arrives as the same event with
  ``issue.pull_request`` set, and is skipped here, so a pull request never gets a row.
* **A deleted newest comment clears the anchor** until the next comment. The row keeps the
  largest id it has ever seen, so a replayed delivery for the deleted comment cannot bring
  it back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Iterable
from pathlib import Path

from .github_cache import GitHubMetadata, GitHubMetadataCache
from .kanban import card_ref
from .webhook_tailer import Delivery, WebhookTailer, WebhookWatcher

log = logging.getLogger(__name__)

CONSUMER = "issue-latest-comment"

# ``<owner/repo lowercased>#<number>``, the spelling ``kanban.card_ref`` returns.
_KEY = "issue-latest-comment:v1:{ref}"


def cache_key(ref: str) -> str:
    return _KEY.format(ref=ref.lower())


def _comment_id(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class LatestCommentService:
    """Tail the streams and keep each issue's newest comment id. Web-process owned."""

    def __init__(
        self,
        streams_root: Path | str,
        *,
        cache: GitHubMetadataCache | None = None,
        enabled: bool = True,
        watcher: WebhookWatcher | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._cache = cache or GitHubMetadataCache()
        self._tailer = WebhookTailer(streams_root, consumer=CONSUMER)
        # The web process's one shared watch (#2865); None opens a private one.
        self._watcher = watcher
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="issue-latest-comment")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            try:
                async for batch in self._tailer.watch(stop_event=self._stop, watcher=self._watcher):
                    self.apply_batch(batch)
                    self._tailer.commit()
                return  # the stop event ended the watch
            except asyncio.CancelledError:
                raise
            except Exception:
                # A link without its anchor is the whole cost of this failing, so keep going.
                log.exception("issue latest-comment pass failed; retrying")
                await asyncio.sleep(5)

    def apply_batch(self, batch: Iterable[Delivery]) -> None:
        for delivery in batch:
            self.apply(delivery)

    def apply(self, delivery: Delivery) -> None:
        """Apply one delivery. Idempotent, and indifferent to arrival order."""
        if delivery.event != "issue_comment":
            return
        payload = delivery.payload
        issue = payload.get("issue")
        comment = payload.get("comment")
        if not isinstance(issue, dict) or not isinstance(comment, dict):
            return
        if issue.get("pull_request") is not None:
            return
        number = issue.get("number")
        comment_id = _comment_id(comment.get("id"))
        if not isinstance(number, int) or isinstance(number, bool) or comment_id is None:
            return
        key = cache_key(f"{delivery.stream}#{number}")
        held = self._cache.peek(key).value
        held = held if isinstance(held, dict) else {}
        newest = _comment_id(held.get("id"))
        seen = _comment_id(held.get("seen")) or 0
        action = payload.get("action")
        if action == "created":
            if comment_id <= seen:
                return
            value = {"id": comment_id, "seen": comment_id}
        elif action == "deleted":
            if comment_id != newest:
                return
            value = {"id": None, "seen": seen}
        else:
            return
        # Terminal: nothing ages this out, and no fetch exists to refresh it.
        self._cache.offer(key, GitHubMetadata(value, terminal=True))


# One cache handle per cache file, so a render does not re-open the file per link and a
# test that points ``AGENTDECK_GITHUB_CACHE`` somewhere else is read there.
_caches: dict[Path, GitHubMetadataCache] = {}


def _default_cache() -> GitHubMetadataCache:
    path = Path(
        os.environ.get("AGENTDECK_GITHUB_CACHE", "~/.cache/agentdeck/github_metadata.sqlite3")
    ).expanduser()
    cache = _caches.get(path)
    if cache is None:
        cache = _caches[path] = GitHubMetadataCache(path)
    return cache


def issue_href(url: object, cache: GitHubMetadataCache | None = None) -> object:
    """``url`` ending at the issue's newest comment, when this deck has seen one.

    Anything that is not a plain GitHub issue URL — a pull request, a chat, a link that
    already carries a fragment such as ``#new_comment_field``, ``None`` — comes back
    unchanged, and so does an issue with no known comment.
    """
    if not isinstance(url, str) or "/issues/" not in url:
        return url
    ref = card_ref(url)
    if ref is None:
        return url
    comment_id = newest_comment_id(ref, cache)
    if comment_id is None:
        return url
    return f"{url.strip()}#issuecomment-{comment_id}"


def newest_comment_id(ref: str, cache: GitHubMetadataCache | None = None) -> int | None:
    """The newest comment id seen on issue ``ref`` (``owner/repo#n``), ``None`` when unknown."""
    try:
        held = (cache or _default_cache()).peek(cache_key(ref)).value
    except Exception:  # a caller must go on whatever the cache file is doing
        log.debug("latest-comment read failed for %s", ref, exc_info=True)
        return None
    return _comment_id(held.get("id")) if isinstance(held, dict) else None
