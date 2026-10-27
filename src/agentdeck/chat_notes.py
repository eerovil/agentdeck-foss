"""Notes a chat shows and its agent never sees (#3227).

The first kind is a GitHub comment the deck's own account posted on the chat's issue or one
of its pull requests: the agent wrote it, so the person reading the chat sees what it said
on GitHub, in place, without the comment going back into the agent's context.

A consumer of the raw webhook streams. Each ``issue_comment`` by one of this host's own
logins (``work_router.configured_own_logins``: the agent account and the App's bot) is kept
in the ``chat_notes`` table under the chat that owns its issue or pull request when it
arrives (``work_router.owner_of_issue`` / ``owner_of_pull``). A comment no chat owns is
dropped.

* **Its own consumer name, and a first start skips the history.** Older comments were never
  in front of anyone reading a chat; replaying them would place a week of comments into
  whichever chat owns each item now, and read each one's rendering from GitHub.
* **Once, the comments already on disk are filled in** (``backfill``): every stored stream,
  its archives included, is read once after the first scan of sessions, so a chat started
  before this shipped shows its agent's comments from the log's whole span (about a week).
  ``chat-notes-backfill.json`` beside the streams records that it ran.
* **The comment's ``updated_at`` is the version**, so a replayed or late ``created`` cannot
  undo an edit. A ``deleted`` removes the note.
* **One GitHub read per created or edited comment** for GitHub's own rendering
  (``body_html``), so formatting, tables and images look as they do on GitHub. Until it
  lands, or if it fails, the card shows the plain text.
* **Private images go through the deck.** A screenshot embedded as a
  ``github.com/<owner>/<repo>/blob/<sha>/<path>?raw=1`` link needs a GitHub login to load,
  so ``rendered_html`` points it at ``/github-image/…``, which fetches that one file as
  the repository's authority. The route serves only a link some kept note shows, and only
  an image type.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx

from . import gh_cli
from .db import ChatNote
from .webhook_tailer import Delivery, WebhookTailer, WebhookWatcher

log = logging.getLogger(__name__)

CONSUMER = "chat-notes"
BACKFILL_MARKER = "chat-notes-backfill.json"
_BACKFILL_BATCH = 200
TOPIC = "chat_notes"
_READ_TIMEOUT_S = 20.0

# The image types the route serves. Never SVG: it can carry script.
IMAGE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

_NAME = r"[A-Za-z0-9_.-]+"
# A private blob link as GitHub's rendering keeps it: an ``src`` attribute naming one file
# at one commit, with ``?raw=1`` or ``?raw=true``.
_BLOB_SRC = re.compile(
    rf'src="https://github\.com/({_NAME})/({_NAME})/blob/([0-9a-f]{{40}})/([^"?#]+)'
    r'\?raw=(?:1|true)"'
)


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def note_from(delivery: Delivery, own_logins: frozenset[str]) -> tuple[str, ChatNote] | None:
    """``(action, note)`` for an own comment, with ``chat_id`` still empty, else ``None``."""
    if delivery.event != "issue_comment":
        return None
    payload = delivery.payload
    action = payload.get("action")
    issue = payload.get("issue")
    comment = payload.get("comment")
    if action not in ("created", "edited", "deleted"):
        return None
    if not isinstance(issue, dict) or not isinstance(comment, dict):
        return None
    user = comment.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str) or login.casefold() not in own_logins:
        return None
    number = _positive_int(issue.get("number"))
    comment_id = _positive_int(comment.get("id"))
    if number is None or comment_id is None:
        return None
    created = comment.get("created_at")
    updated = comment.get("updated_at") or created
    url = comment.get("html_url")
    if not isinstance(created, str) or not isinstance(updated, str) or not isinstance(url, str):
        return None
    body = comment.get("body")
    return action, ChatNote(
        repository=delivery.stream.casefold(),
        comment_id=comment_id,
        chat_id="",
        number=number,
        is_pull=issue.get("pull_request") is not None,
        author=login,
        url=url,
        body=body if isinstance(body, str) else "",
        body_html=None,
        created_at=created,
        updated_at=updated,
    )


def rendered_html(body_html: str | None) -> str | None:
    """GitHub's rendering with each private blob image pointed at the deck's image route."""
    if not body_html:
        return None
    return _BLOB_SRC.sub(lambda m: f'src="/github-image/{m[1]}/{m[2]}/{m[3]}/{m[4]}"', body_html)


def blob_link(owner: str, repo: str, sha: str, path: str) -> str:
    """The text a note's rendering holds for this image, which the route checks for."""
    return f"https://github.com/{owner}/{repo}/blob/{sha}/{path}?raw="


def image_type(path: str) -> str | None:
    return IMAGE_TYPES.get(path.rsplit(".", 1)[-1].casefold()) if "." in path else None


async def read_image(cache_dir: Path, owner: str, repo: str, sha: str, path: str) -> bytes | None:
    """The file at ``sha``, read once as the repository's authority and kept on disk: a
    file at a commit never changes."""
    key = hashlib.sha256(f"{owner}/{repo}@{sha}:{path}".casefold().encode()).hexdigest()
    cached = cache_dir / key
    with contextlib.suppress(OSError):
        return cached.read_bytes()
    # The JSON form, not ``Accept: …raw``: the worker ``gh`` shim cannot carry binary
    # output, and this route is also run from worker shells (the evidence script).
    try:
        code, out = await gh_cli.run(
            ["gh", "api", f"repos/{owner}/{repo}/contents/{path}?ref={sha}"],
            timeout=_READ_TIMEOUT_S,
        )
        found = json.loads(out) if code == 0 else None
        data = None
        if isinstance(found, dict) and found.get("encoding") == "base64":
            data = base64.b64decode(found.get("content") or "")
        elif isinstance(found, dict) and isinstance(found.get("download_url"), str):
            # Over 1 MB the API leaves ``content`` out; the download link carries its own
            # short-lived token.
            async with httpx.AsyncClient(timeout=_READ_TIMEOUT_S) as client:
                answer = await client.get(found["download_url"])
            data = answer.content if answer.status_code == 200 else None
    except (OSError, TimeoutError, ValueError, httpx.HTTPError) as exc:
        log.info("chat note image %s/%s@%s:%s unreadable: %s", owner, repo, sha, path, exc)
        return None
    if not data:
        return None
    with contextlib.suppress(OSError):
        cache_dir.mkdir(parents=True, exist_ok=True)
        partial = cached.with_suffix(".part")
        partial.write_bytes(data)
        partial.replace(cached)
    return data


async def read_body_html(repository: str, comment_id: int) -> str | None:
    """GitHub's own rendering of one comment, or ``None`` when it cannot be read."""
    try:
        code, out = await gh_cli.run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github.html+json",
                f"repos/{repository}/issues/comments/{comment_id}",
            ],
            timeout=_READ_TIMEOUT_S,
        )
    except (OSError, TimeoutError) as exc:
        log.info("chat note %s comment %s unreadable: %s", repository, comment_id, exc)
        return None
    if code != 0:
        return None
    try:
        html = json.loads(out).get("body_html")
    except (ValueError, AttributeError):
        return None
    return html if isinstance(html, str) else None


Owner = tuple[str, str]


class ChatNotesService:
    """Tail the streams and keep each own comment as a note on its chat. Web-process owned."""

    def __init__(
        self,
        streams_root: Path | str,
        *,
        store: Any,
        owner_of_issue: Callable[[str, int], Owner | None],
        owner_of_pull: Callable[[str, int], Owner | None],
        own_logins: Callable[[], frozenset[str]],
        on_change: Callable[[], object] = lambda: None,
        read_html: Callable[[str, int], Any] = read_body_html,
        scanned: Callable[[], Any] | None = None,
        enabled: bool = True,
        watcher: WebhookWatcher | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._tailer = WebhookTailer(streams_root, consumer=CONSUMER)
        self._watcher = watcher
        self._store = store
        self._owner_of_issue = owner_of_issue
        self._owner_of_pull = owner_of_pull
        self._own_logins = own_logins
        self._on_change = on_change
        self._read_html = read_html
        self._scanned = scanned
        self._marker = Path(streams_root).expanduser().parent / BACKFILL_MARKER
        self._task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="chat-notes")
        if not self._marker.exists():
            self._backfill_task = asyncio.create_task(self._backfill(), name="chat-notes-backfill")

    async def stop(self) -> None:
        backfill, self._backfill_task = self._backfill_task, None
        if backfill is not None:
            backfill.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await backfill
        task, self._task = self._task, None
        if task is None:
            return
        self._stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def skip_history(self) -> bool:
        """A first start reads from the end: see the module docstring."""
        if self._tailer.state_path.exists():
            return False
        for _ in self._tailer.deliveries():
            pass
        self._tailer.commit()
        return True

    async def _run(self) -> None:
        self.skip_history()
        while True:
            try:
                async for batch in self._tailer.watch(stop_event=self._stop, watcher=self._watcher):
                    await self.apply(batch)
                    self._tailer.commit()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                # A note missing from a chat is the whole cost of this failing.
                log.exception("chat notes pass failed; retrying")
                await asyncio.sleep(5)

    async def _backfill(self) -> None:
        try:
            # Owners come from the sessions, so wait for their first scan.
            if self._scanned is not None:
                await self._scanned()
            await self.backfill()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Not marked done, so the next start tries again.
            log.exception("chat notes backfill failed")

    async def backfill(self) -> int:
        """Apply every own comment on disk, oldest archive first, then mark it done.

        Overlap with the live tail is harmless: an unchanged note is not stored or read
        again. Returns how many comments were looked at.
        """
        seen = 0
        for repository in self._tailer.streams():
            path = self._tailer.path_for(repository)
            archives = sorted(
                (int(p.name[len(path.name) + 1 : -3]), p)
                for p in path.parent.glob(f"{path.name}.*.gz")
                if p.name[len(path.name) + 1 : -3].isdigit()
            )
            sources = [p for _, p in reversed(archives)] + [path]
            batch: list[Delivery] = []
            for source in sources:
                for delivery in _comment_records(source, repository.slug):
                    batch.append(delivery)
                    if len(batch) >= _BACKFILL_BATCH:
                        seen += len(batch)
                        await self.apply(batch)
                        batch = []
            if batch:
                seen += len(batch)
                await self.apply(batch)
        self._marker.write_text(json.dumps({"done": True, "comments": seen}))
        log.info("chat notes backfill looked at %d comments", seen)
        return seen

    def _owner(self, note: ChatNote) -> Owner | None:
        find = self._owner_of_pull if note.is_pull else self._owner_of_issue
        return find(note.repository, note.number)

    async def apply(self, batch: Iterable[Delivery]) -> None:
        """Keep, update or drop the notes ``batch`` names, then render the new versions."""
        own = self._own_logins()
        changed = False
        to_render: dict[tuple[str, int], ChatNote] = {}
        for delivery in batch:
            found = note_from(delivery, own)
            if found is None:
                continue
            action, note = found
            key = (note.repository, note.comment_id)
            if action == "deleted":
                changed = self._store.delete_chat_note(*key) or changed
                to_render.pop(key, None)
                continue
            owner = self._owner(note)
            if owner is None:
                continue
            note = note._replace(chat_id=owner[0])
            if self._store.save_chat_note(note):
                changed = True
                to_render[key] = note
        if changed:
            self._on_change()
        rendered = False
        for note in to_render.values():
            html = await self._read_html(note.repository, note.comment_id)
            if html and self._store.set_chat_note_html(
                note.repository, note.comment_id, note.updated_at, html
            ):
                rendered = True
        if rendered:
            self._on_change()


def _comment_records(path: Path, slug: str) -> Iterable[Delivery]:
    """The ``issue_comment`` deliveries in one stored stream file, live or archived.

    A file that cannot be read or decompressed raises."""
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rb") as handle:
            for line in handle:
                # Cheap filter before decoding the body.
                if b'"issue_comment"' not in line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("event") != "issue_comment":
                        continue
                    raw = base64.b64decode(str(record.get("raw_body_base64", "")))
                except (ValueError, TypeError, AttributeError):
                    continue
                yield Delivery(
                    stream=str(record.get("repository") or slug),
                    delivery_id=str(record.get("delivery_id") or ""),
                    event="issue_comment",
                    received_at=str(record.get("received_at") or ""),
                    headers=dict(record.get("headers") or {}),
                    raw_body=raw,
                    offset=0,
                )
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        # Raised on, not skipped: the pass is then not marked done and the next start
        # reads this file again rather than losing its comments for good.
        log.warning("chat notes backfill could not read %s: %s", path, exc)
        raise
