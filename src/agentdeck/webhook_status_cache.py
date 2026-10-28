"""Feed the shared GitHub status cache from the webhook streams (#1627, #1642, #1681).

The first consumer of the raw webhook streams: a `WebhookTailer` in the web process
that keeps the pill-facing cache rows fresh without spending GitHub API quota.

* A ``pull_request`` event **writes through**: its payload carries every field the
  cached row holds, so the row is replaced in place — guarded last-writer-wins by the
  payload's ``updated_at``, because delivery order proves nothing (#1625). The write
  restarts the ordinary TTL, exactly as a fetch would; while events flow, polled reads
  approach zero, and when the stream goes quiet the normal TTL brings polling back.
* An ``issues`` event does both halves of the same move, and so does an
  ``issue_comment`` event, whose payload is the same issue snapshot with its new comment
  count (#2238). It **revises** the one row it is about in the repository's cached
  open-issues list (#2226): the row's title, labels and state change in place, a closed
  issue leaves the list and an opened one joins it. The list keeps the age of its last real
  read, so it is re-read exactly when it would have been. Dropping the whole list
  instead, as this did before, left every title on a busy repository falling back to a
  chat's name until the next sweep. A change that would add or remove a row of a list
  already at the read's cap still drops it: absence from a capped list proves nothing,
  and a shorter one would claim it did. And it **writes through** to the per-issue row
  the session pill actually reads (#1681).

* A ``sub_issues`` event writes both issues it names through the same way, sets the
  child's ``parent`` on its row and marks the parent's row stale, since the payload's count
  can predate the change (#3622, ``work_tree``).

Anything that does not map cleanly invalidates the affected key rather than guessing.

A pull-request write that moves what a pill *shows* calls ``on_pull_change`` (#2042).
That is the one thing #1627 amended out and this card put back: without it the fresh
row sat on disk until Deckhand's next 60-second pass happened to sweep it up, so a
merge the deck already knew about took up to a minute to reach the screen. The
callback says only *look now* — never what is true — so the reader decides whether
anything really moved before any pixel changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import wayfinder_map
from .github_cache import GitHubMetadata, GitHubMetadataCache
from .webhook_tailer import Delivery, WebhookTailer, WebhookWatcher

log = logging.getLogger(__name__)

CONSUMER = "pill-status-cache"

# The keys this consumer owns are spelled exactly as their readers spell them:
# ``git_context._pull_cache_key``, ``issue_context``'s issue-list key and
# ``kanban.KanbanTitleCache._cache_key``. A drifted literal would warm rows nothing
# reads — which is exactly how issue pills went unfed until #1681.
_PR_KEY = "pr-ref:v1:{repository}:{number}"
_ISSUE_LIST_KEY = "issue-list:v1:{repository}"
_ISSUE_KEY = "issue:v1:{repository}#{number}"
# How many open issues ``IssueResolver`` reads per repository (``MAX_ISSUES``): a list this
# long may have been cut off, so a row added or removed here would be a guess.
_ISSUE_LIST_CAP = 100
# The ``issues`` actions that move a parent's count of done sub-issues (#3622).
_COUNT_MOVES = frozenset({"closed", "reopened", "deleted", "transferred"})

# The row fields a pill is drawn from — every field ``git_context._pull_request``
# reads, and deliberately not ``updatedAt``. GitHub bumps ``updated_at`` for a
# comment or a label, which moves nothing on screen; treating that as a change
# would wake the reader for every chatty pull request in the repository.
_PULL_DISPLAY_FIELDS = (
    "number",
    "title",
    "url",
    "state",
    "isDraft",
    "mergedAt",
    "headRefName",
    "baseRefName",
    # A push to the branch moves this and nothing else on the row, and it is what the
    # ChatGPT review chip compares against (#2088) — so without it here a `synchronize`
    # delivery would be written and then read as changing nothing worth waking for.
    "headRefOid",
)
# The pull request's size, row field by payload key (#2236). Not a pill field: the Work
# overview reads the row on its own cadence, so a size change wakes nothing.
_PULL_SIZE_FIELDS = (
    ("additions", "additions"),
    ("deletions", "deletions"),
    ("changedFiles", "changed_files"),
)


def _auto_merge(value: object) -> dict | None:
    """The payload's ``auto_merge`` as ``gh pr view``'s ``autoMergeRequest``: None when off."""
    if not isinstance(value, dict):
        return None
    enabled = value.get("enabled_at")
    return {"enabledAt": enabled if isinstance(enabled, str) else None}


def _keep_file_list(held: object, value: dict) -> None:
    """Carry the held row's per-file list onto a payload that has none (#2331).

    A delivery never carries ``files``, and the Work size is summed from it. The list only
    holds for the diff it was read from: the same head and base branch, and the delivery's own
    totals equal to the held row's. The base branch can move under an unchanged name and head
    and change the diff; the totals are the payload's own word on that diff, so any change
    drops the list and the Work overview's backfill reads it again. Matching totals are an
    approximation, not the diff's identity — a moved base can reshuffle files under the same
    totals — and the operator accepted that on #2331 rather than drop every open pull
    request's list on each merge to its base.
    """
    if not isinstance(held, dict) or not isinstance(held.get("files"), list):
        return
    if not value.get("headRefOid") or any(
        held.get(field) != value.get(field) for field in _FILE_LIST_FIELDS
    ):
        return
    value["files"] = held["files"]


# What a held file list must still agree with to be carried onto a delivery (#2331).
_FILE_LIST_FIELDS = ("headRefOid", "baseRefName", *(field for field, _ in _PULL_SIZE_FIELDS))


def _pull_display_changed(held: object, value: dict) -> bool:
    """Whether this write moves what a pill shows, against the row it replaces.

    No held row counts as a change: the reader's in-memory context was drawn from
    something else — an older row that has since been dropped, or a fetch made
    before this process started — so only the sweep can tell whether it still
    agrees, and it is cheap enough to let it answer.
    """
    if not isinstance(held, dict):
        return True
    return any(held.get(field) != value.get(field) for field in _PULL_DISPLAY_FIELDS)


def _issue_repository(issue: dict, stream: str) -> str:
    """The ``owner/repo`` an issue payload is in, from its own ``repository_url``; else the
    stream's. A sub-issue and its parent need not share a repository (#3622)."""
    source = issue.get("repository_url")
    if isinstance(source, str) and "/repos/" in source:
        owner, _, name = source.rsplit("/repos/", 1)[1].partition("/")
        if owner and name and "/" not in name:
            return f"{owner}/{name}"
    return stream


def _api_issue(url: object) -> tuple[str, int] | None:
    """``(owner/repo, number)`` from an issue's API URL, as ``parent_issue_url`` holds it."""
    if not isinstance(url, str) or "/repos/" not in url:
        return None
    owner, _, rest = url.rsplit("/repos/", 1)[1].partition("/")
    name, _, tail = rest.partition("/issues/")
    if not owner or not name or "/" in name or not tail.isdigit():
        return None
    return (f"{owner}/{name}", int(tail))


def _parse_updated_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class WebhookStatusCacheService:
    """Tail the streams and keep the status cache warm. Web-process owned.

    Restart-safe by construction: the tailer's offsets and the cache it fills are
    files every process run shares, and a batch is committed only after it has been
    applied, so a deploy mid-batch replays idempotent writes rather than losing them.
    """

    def __init__(
        self,
        streams_root: Path | str,
        *,
        cache: GitHubMetadataCache | None = None,
        enabled: bool = True,
        on_pull_change: Callable[[], None] | None = None,
        on_tree_change: Callable[[], None] | None = None,
        on_map_change: Callable[[], None] | None = None,
        watcher: WebhookWatcher | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._cache = cache or GitHubMetadataCache()
        self._tailer = WebhookTailer(streams_root, consumer=CONSUMER)
        # The web process's one shared watch (#2865); None opens a private one.
        self._watcher = watcher
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._on_pull_change = on_pull_change
        self._pull_moved = False
        # A sub-issue linked, unlinked, or closed under its parent (#3622): the Work overview
        # rebuilds its parent rows, as the issue's own pass would not for seconds or minutes.
        self._on_tree_change = on_tree_change
        self._tree_moved = False
        # A wayfinder map's cached body, sub-issue list or "blocked by" list dropped (#3627):
        # its open Work pages are built again, which the overview's own revision would not say.
        self._on_map_change = on_map_change
        self._map_moved = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="webhook-status-cache")

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
                # The streams are diagnostic data that survive crashes; this consumer
                # must too. Whatever broke, the poller underneath still corrects the
                # cache — losing the loop would silently lose the quota saving.
                log.exception("webhook status cache pass failed; retrying")
                await asyncio.sleep(5)

    # -- applying one delivery ---------------------------------------------------

    def apply(self, delivery: Delivery) -> None:
        """Apply one delivery to the cache. Idempotent; safe to replay."""
        self.apply_batch((delivery,))

    def apply_batch(self, batch: Iterable[Delivery]) -> None:
        """Apply a batch, then signal the reader at most once for the whole batch.

        One signal, not one per delivery: a merge arrives beside its own review,
        label and status events, and the reader's sweep reads the finished cache
        either way, so waking it per delivery would only repeat the same sweep.
        """
        self._pull_moved = False
        self._tree_moved = False
        for delivery in batch:
            self._apply(delivery)
        self._notify_pull_change()
        self._notify_tree_change()
        if self._map_moved and self._on_map_change is not None:
            self._map_moved = False
            try:
                self._on_map_change()
            except Exception:
                log.exception("webhook status cache map subscriber failed")

    def _notify_tree_change(self) -> None:
        """Tell the Work overview to look, once a batch, if it moved a sub-issue link or count
        (#3622). Like ``_notify_pull_change``: a failing subscriber never stops the tail."""
        if not self._tree_moved or self._on_tree_change is None:
            return
        self._tree_moved = False
        try:
            self._on_tree_change()
        except Exception:
            log.exception("webhook status cache tree subscriber failed")

    def _apply(self, delivery: Delivery) -> None:
        if delivery.event == "pull_request":
            self._apply_pull(delivery)
        elif delivery.event in {"issues", "issue_comment"}:
            # An ``issue_comment`` payload carries the whole issue too, with its new comment
            # count: a hidden Work comes back on the next pass, not the next poll (#2238).
            self._apply_issue(delivery)
        elif delivery.event == "sub_issues":
            self._apply_sub_issue(delivery)
        elif delivery.event == "issue_dependencies":
            self._apply_dependency(delivery)
        if delivery.event == "issues":
            self._drop_map_rows(delivery)

    def _drop_map_rows(self, delivery: Delivery) -> None:
        """Drop what a wayfinder map's diagram read of this issue, whatever changed (#3627).

        One rule, the owner's: every ``issues`` delivery may change something a map shows of
        the issue, its title, state, labels (a ticket's type) or body (a map's fog and
        destination). So any of them marks the issue's own body row and its parent's cached
        sub-issue list out of date, and one that hit a held row redraws the open map pages.
        That redraw reads them again behind the page, so the diagram follows GitHub without
        a timer."""
        payload = delivery.payload
        issue = payload.get("issue")
        number = issue.get("number") if isinstance(issue, dict) else None
        if not isinstance(number, int):
            return
        repository = _issue_repository(issue, delivery.stream)
        self._drop_map_row(
            wayfinder_map.cache_key(wayfinder_map.ISSUE_BODY_KEY, repository, number)
        )
        parent = _api_issue(self._parent_url(issue, delivery.stream))
        if parent is not None:
            self._drop_map_row(wayfinder_map.cache_key(wayfinder_map.SUB_ISSUES_KEY, *parent))

    def _parent_url(self, issue: dict, stream: str) -> object:
        """The issue's parent's API URL: the payload's, else its cached row's (#3654).

        No ``issues`` or ``issue_comment`` delivery carries ``parent_issue_url``; the row
        has it from a ``sub_issues`` delivery or a read."""
        if "parent_issue_url" in issue:
            return issue["parent_issue_url"]
        number = issue.get("number")
        if not isinstance(number, int):
            return None
        repository = _issue_repository(issue, stream).lower()
        held = self._cache.peek(_ISSUE_KEY.format(repository=repository, number=number)).value
        return held.get("parent") if isinstance(held, dict) else None

    def _apply_dependency(self, delivery: Delivery) -> None:
        """A "blocked by" link added or removed (#3627): mark the blocked issue's cached
        dependency list out of date, which a wayfinder map's diagram draws as arrows."""
        issue = delivery.payload.get("blocked_issue")
        number = issue.get("number") if isinstance(issue, dict) else None
        if isinstance(number, int):
            repository = _issue_repository(issue, delivery.stream)
            self._drop_map_row(
                wayfinder_map.cache_key(wayfinder_map.BLOCKED_BY_KEY, repository, number)
            )
        else:
            # A payload of another shape: every list this repository's issues hold.
            self._drop_map_row(
                f"{wayfinder_map.BLOCKED_BY_KEY}{delivery.stream.lower()}#", prefix=True
            )

    def _drop_map_row(self, key: str, *, prefix: bool = False) -> None:
        """Mark a map's cached read out of date; one that was held moves its open pages (#3627).

        One key is kept with its value (#3739): the page draws it at once while it is read
        again behind it, rather than waiting on GitHub. A prefix, the dependency fallback's,
        still drops every row under it."""
        if self._cache.invalidate(key) if prefix else self._cache.expire(key):
            self._map_moved = True

    def _notify_pull_change(self) -> None:
        """Tell the reader to look, if this batch moved a pull request on screen.

        A subscriber that raises must not take the tail loop down with it: the
        cache write already happened and is the durable half, and the reader has
        its own periodic sweep as a backstop.
        """
        if not self._pull_moved:
            return
        self._pull_moved = False
        if self._on_pull_change is None:
            return
        try:
            self._on_pull_change()
        except Exception:
            log.exception("webhook status cache change subscriber failed")

    def _apply_pull(self, delivery: Delivery) -> None:
        payload = delivery.payload
        pull = payload.get("pull_request")
        number = pull.get("number") if isinstance(pull, dict) else None
        if not isinstance(number, int):
            # No number means no key to correct either; nothing can be stale under it.
            return
        key = _PR_KEY.format(repository=delivery.stream.lower(), number=number)
        value = self._pull_value(pull)
        if value is None:
            # A payload this consumer cannot read is a reason to drop the row, not to
            # guess at it: the next reader refetches and the poll is the truth.
            self._cache.invalidate(key)
            return
        held = self._cache.peek(key).value
        if not self._is_newer(held, value):
            return
        _keep_file_list(held, value)
        self._cache.offer(
            key,
            GitHubMetadata(value, terminal=value["state"] == "closed"),
        )
        if _pull_display_changed(held, value):
            self._pull_moved = True

    @staticmethod
    def _pull_value(pull: dict) -> dict | None:
        """The webhook payload reshaped to the row ``gh pr view`` would store.

        Field spelling follows ``git_context._PR_FIELDS`` — the reader's contract —
        including ``updatedAt``, which both this write and the polled fetch now carry
        so the last-writer-wins guard compares like with like.
        """
        title = pull.get("title")
        url = pull.get("html_url")
        state = pull.get("state")
        updated_at = pull.get("updated_at")
        if not all(isinstance(item, str) for item in (title, url, state, updated_at)):
            return None
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
        return {
            "number": pull.get("number"),
            "title": title,
            "url": url,
            "state": state,
            "isDraft": bool(pull.get("draft")),
            "mergedAt": pull.get("merged_at"),
            "headRefName": head.get("ref"),
            "baseRefName": base.get("ref"),
            "headRefOid": head.get("sha"),
            "updatedAt": updated_at,
            # Whether GitHub merges it by itself (#2275), in ``gh pr view``'s shape; a payload
            # without the key leaves the row without, which reads as not armed.
            **(
                {"autoMergeRequest": _auto_merge(pull["auto_merge"])}
                if "auto_merge" in pull
                else {}
            ),
            # The pull request's size (#2236); a payload without it leaves the row without.
            **{
                field: pull[key]
                for field, key in _PULL_SIZE_FIELDS
                if isinstance(pull.get(key), int) and not isinstance(pull.get(key), bool)
            },
        }

    def _apply_issue(self, delivery: Delivery) -> None:
        repository = delivery.stream.lower()
        list_key = _ISSUE_LIST_KEY.format(repository=repository)
        payload = delivery.payload
        issue = payload.get("issue")
        number = issue.get("number") if isinstance(issue, dict) else None
        if not isinstance(number, int):
            self._cache.invalidate(list_key)
            return
        key = _ISSUE_KEY.format(repository=repository, number=number)
        value = self._issue_value(issue)
        if value is None:
            self._cache.invalidate(key)
            self._cache.invalidate(list_key)
            return
        if delivery.event == "issues" and payload.get("action") in {"deleted", "transferred"}:
            # The issue left the repository: its payload still reads open.
            self._cache.invalidate(list_key)
        else:
            self._revise_issue_list(list_key, issue, value)
        if delivery.event == "issues" and payload.get("action") in _COUNT_MOVES:
            self._expire_parent(self._parent_url(issue, delivery.stream))
        held = self._cache.peek(key).value
        if not self._is_newer(held, value):
            return
        if "parent" not in value and isinstance(held, dict) and "parent" in held:
            # No webhook payload carries ``parent_issue_url`` (``_parent_url``): a comment
            # must not drop the link (#3654).
            value["parent"] = held["parent"]
        self._cache.offer(
            key,
            GitHubMetadata(value, terminal=value["state"] == "closed"),
        )

    def _expire_parent(self, url: object) -> None:
        """A sub-issue closed, reopened or left: its parent's sub-issue count moved (#3622).

        The child's payload does not carry the parent's new count, so the parent's row
        (``url``, from ``_parent_url``) is marked stale for the next read, even a closed
        parent's day-long one."""
        parts = url.rsplit("/repos/", 1)[-1].split("/") if isinstance(url, str) else []
        if len(parts) == 4 and parts[2] == "issues" and parts[3].isdigit():
            repository = f"{parts[0]}/{parts[1]}".lower()
            key = _ISSUE_KEY.format(repository=repository, number=int(parts[3]))
            # Already stale (a link just now) still moves the parent's row: tell the overview.
            if self._cache.expire(key) or self._cache.peek(key).value is not None:
                self._tree_moved = True

    def _apply_sub_issue(self, delivery: Delivery) -> None:
        """A sub-issue linked to or unlinked from its parent (#3622).

        The payload carries both issues whole. Each is written through like an ``issues``
        delivery, and then the link itself is set on the child's row whatever its
        ``updatedAt`` says: GitHub need not move it for a link, and the snapshots may predate
        the change. The parent's count is not taken from the payload but read again.
        """
        payload = delivery.payload
        action = payload.get("action")
        parent, child = payload.get("parent_issue"), payload.get("sub_issue")
        if not isinstance(parent, dict) or not isinstance(child, dict):
            return
        for issue in (parent, child):
            # Each as its own repository's ``issues`` delivery: a sub-issue may be anywhere.
            self._apply_issue(
                replace(
                    delivery,
                    stream=_issue_repository(issue, delivery.stream),
                    event="issues",
                    _payload={"action": "edited", "issue": issue},
                )
            )
        linked = action in {"sub_issue_added", "parent_issue_added"}
        if not linked and action not in {"sub_issue_removed", "parent_issue_removed"}:
            return
        self._tree_moved = True
        self._revise_issue_field(
            delivery.stream, child, "parent", parent.get("url") if linked else None
        )
        # Its list of sub-issues, which a wayfinder map draws (#3627), is read again.
        self._drop_map_row(
            wayfinder_map.cache_key(
                wayfinder_map.SUB_ISSUES_KEY,
                _issue_repository(parent, delivery.stream),
                parent.get("number") if isinstance(parent.get("number"), int) else -1,
            )
        )
        # The payload's ``parent_issue`` can predate the change: live, both removal events
        # carried the count from before the unlink. So the parent's row is read again rather
        # than trusted, a closed parent's day-long one included (#3622 live check).
        number = parent.get("number")
        if isinstance(number, int):
            repository = _issue_repository(parent, delivery.stream).lower()
            self._cache.expire(_ISSUE_KEY.format(repository=repository, number=number))

    def _revise_issue_field(self, stream: str, issue: dict, field: str, value: object) -> None:
        """Set one field on an issue's cached row; a row not cached is left to its next read."""
        number = issue.get("number")
        if not isinstance(number, int):
            return
        repository = _issue_repository(issue, stream).lower()
        key = _ISSUE_KEY.format(repository=repository, number=number)
        held = self._cache.peek(key).value
        if isinstance(held, dict) and held.get(field) != value:
            self._cache.revise(key, {**held, field: value})

    def _revise_issue_list(self, list_key: str, issue: dict, value: dict) -> None:
        """Move the one row this delivery is about in the cached open-issues list (#2226).

        Nothing cached is left alone: the next read fetches the list whole. A snapshot older
        than the row held is ignored, by the same ``updatedAt`` rule as every other row.
        """
        held = self._cache.peek(list_key).value
        if not isinstance(held, list) or value["is_pr"]:
            return
        row = self._list_row(issue, value)
        if row is None:
            self._cache.invalidate(list_key)
            return
        number = row["number"]
        existing = next(
            (item for item in held if isinstance(item, dict) and item.get("number") == number),
            None,
        )
        if existing is not None and not self._is_newer(existing, value):
            return
        open_now = value["state"] == "open"
        if (existing is not None) != open_now and len(held) >= _ISSUE_LIST_CAP:
            self._cache.invalidate(list_key)
            return
        if not open_now:
            rows = [item for item in held if item is not existing]
        elif existing is None:
            rows = [*held, row]
        else:
            rows = [row if item is existing else item for item in held]
        self._cache.revise(list_key, rows)

    @staticmethod
    def _list_row(issue: dict, value: dict) -> dict | None:
        """The webhook payload as one row of ``gh issue list --json`` (``_ISSUE_FIELDS``)."""
        url = issue.get("html_url")
        if not isinstance(url, str) or not url:
            return None
        return {
            "number": issue["number"],
            "title": value["title"],
            "url": url,
            "state": value["state"].upper(),
            "labels": [
                {"name": item["name"]}
                for item in issue.get("labels") or ()
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ],
            "assignees": [
                {"login": item["login"]}
                for item in issue.get("assignees") or ()
                if isinstance(item, dict) and isinstance(item.get("login"), str)
            ],
            "updatedAt": value["updatedAt"],
            "createdAt": issue.get("created_at"),
        }

    @staticmethod
    def _issue_value(issue: dict) -> dict | None:
        """The webhook payload reshaped to the row ``kanban._ISSUE_JQ`` would store.

        ``is_pr`` and ``merged`` are written rather than omitted because
        ``kanban.status_label`` reads them to tell a merged pull request from a closed
        issue. GitHub does not send ``issues`` for pull requests, so both are normally
        false; they are derived from the payload anyway rather than assumed.
        """
        title = issue.get("title")
        state = issue.get("state")
        updated_at = issue.get("updated_at")
        if not all(isinstance(item, str) and item for item in (title, state, updated_at)):
            return None
        pull = issue.get("pull_request")
        pull = pull if isinstance(pull, dict) else None
        return {
            "title": title.strip(),
            "state": state,
            "state_reason": issue.get("state_reason"),
            "is_pr": pull is not None,
            "merged": bool(pull and pull.get("merged_at")),
            "updatedAt": updated_at,
            # When GitHub closed it; ``updatedAt`` also moves on a later comment (#2219).
            "closedAt": issue.get("closed_at") if state == "closed" else None,
            # Its label names (#2548: a paused Work); a payload without them leaves them out.
            **(
                {
                    "labels": [
                        item["name"]
                        for item in issue["labels"]
                        if isinstance(item, dict) and isinstance(item.get("name"), str)
                    ]
                }
                if isinstance(issue.get("labels"), list)
                else {}
            ),
            # How many comments it has (#2238); a payload without the count leaves it out.
            **(
                {"comments": issue["comments"]}
                if isinstance(issue.get("comments"), int)
                and not isinstance(issue.get("comments"), bool)
                else {}
            ),
            # Its first assignee's login, or None (#3731: which deck works it), as
            # ``kanban._ISSUE_JQ`` stores it; a payload without the field leaves it out.
            **(
                {"assignee": _assignee_login(issue["assignee"])}
                if "assignee" in issue
                else {}
            ),
            # Its parent issue and its sub-issues' counts (#3622, ``work_tree``), as
            # ``kanban._ISSUE_JQ`` stores them; a payload without the fields leaves them out.
            **({"parent": issue["parent_issue_url"]} if "parent_issue_url" in issue else {}),
            **(
                {"subIssues": issue["sub_issues_summary"]}
                if isinstance(issue.get("sub_issues_summary"), dict)
                else {}
            ),
        }

    @staticmethod
    def _is_newer(held: object, value: dict) -> bool:
        """Last-writer-wins on ``updatedAt`` — never on arrival order (#1625).

        A held row without ``updatedAt`` predates this consumer (or came from an older
        polled shape); it cannot be compared, so the delivery wins and stamps the row
        comparable for every event after it.
        """
        if not isinstance(held, dict):
            return True
        held_at = _parse_updated_at(held.get("updatedAt"))
        offered_at = _parse_updated_at(value.get("updatedAt"))
        if held_at is None or offered_at is None:
            return True
        return offered_at >= held_at


def _assignee_login(user: object) -> str | None:
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) and login else None
