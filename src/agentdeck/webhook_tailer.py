"""Tail the raw per-repository GitHub webhook streams for one named consumer (#1637).

`github_webhook_log.py` writes the streams; this reads them. It is the shared plumbing
decided in #1626, and every consumer embeds it in its own process rather than subscribing
to a broker: the pill status cache and the poller wake watcher live in the web process,
so their offsets survive a frontend deploy on disk. There is no watcher process, which is
what lets one consumer fail without touching another.

What is shared is the *wake*, not the reading (#2865). The web process holds one
`WebhookWatcher` on the streams root; an append wakes every consumer registered with it,
and each consumer then reads from its own committed offset with its own tailer. That
offset is also the catch-up: a filesystem event the watch missed costs only latency,
because the next append's read picks up everything after the offset. Nothing re-reads the
streams on a timer.

## Hint over truth

**A delivery says when to look. It never says what is true.** That is not a style
preference, it is what GitHub actually promises, measured in #1625:

* Delivery is **at-most-once**. A failed delivery is never retried, so a web restart
  during a deploy silently drops whatever arrived in that window.
* **Ordering is not guaranteed.** The next delivery to arrive may carry an *older*
  snapshot than the one you already applied.
* A payload over 25 MB is **not delivered at all**, with no marker on either side. A
  quiet stream and a dropped event look identical.

Two rules fall out, and both bind every consumer of this module:

1. **Merge by the newest payload `updated_at`, never by arrival order.** Apply a
   delivery only when its own timestamp is newer than the state you hold, and drop it
   otherwise. Neither `received_at` nor the order this module yields records in is a
   safe key.
2. **Never skip, delay or stretch a poll because the stream looks healthy.** The
   30-second poller stays the thing that decides what is true (ADR 0005, as narrowed by
   the #1623 map). Webhooks accelerate and enrich it; they do not stand in for it, and a
   consumer that treats a flowing stream as permission to stop polling has turned a hint
   back into truth. `last_delivery_seen` exists for display and diagnosis, and is the
   accessor most likely to be misread this way — see its own docstring. #1631 carves the
   one deliberate, operator-approved exception: the poller's quiet skip may defer a wake's
   *live GitHub queries* — never the wake itself — while its own tailer read proves no
   delivery landed, the watcher heartbeat proves this plumbing alive, and a hard ceiling
   (`webhook_skip_s`, 900s) bounds how long an at-most-once channel is trusted. Absence of
   any proof restores full polling.

## Delivery semantics of this module

**At-least-once, per consumer.** A stream's byte offset is committed only after the
consumer has processed the batch, so a crash between reading and committing replays those
records rather than losing them — and a state write that itself fails leaves the committed
offsets and the pending batch both untouched, so the commit can just be retried. A bounded
set of recently-seen `X-GitHub-Delivery` GUIDs
absorbs the ordinary replay; the dedupe window is finite, so beyond it the consumer's own
idempotence — rule 1 above — is what keeps a replay harmless. That ordering is deliberate:
duplicating a delivery costs a redundant merge, dropping one costs stale state until the
next poll corrects it.

The GUID is the whole dedupe key. It identifies the *event*, not the delivery attempt, so
a redelivery reuses it and two hooks pointed at the same endpoint share it (#1625 measured
both here). Keying on `(delivery, hook)` would let the same event through twice.

## Rotation

#1622 rotates a full stream by gzipping it aside and **truncating the live file in place**
under the appender's lock, so the inode never changes and an offset cannot be carried over
by watching the inode. This module detects it two ways instead: the file shrinking below
the committed offset, and a fingerprint of the stream's first bytes changing. Either
resets that stream to zero and re-reads it, which the GUID dedupe absorbs.

A rotation always hides some records from every consumer: the append that fills the
stream rotates it before anyone can read that delivery, and whatever landed since a
consumer's last read went into the archive with it. #2754 lost a new issue's `opened`
and `labeled` deliveries that way, so the poller judged the repository quiet and started
the card a quarter of an hour late. So on a rotation the tailer first finishes the
archived `.jsonl.N.gz` whose head fingerprint matches the one it committed, from the
committed offset, then reads any newer archives whole, then the live file from zero.
When no archive matches — a consumer down for more rotations than are kept, or one that
never fingerprinted the stream — it replays the live file alone, and the poller
underneath is what heals that gap.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import hashlib
import json
import logging
import os
import tempfile
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from watchfiles import Change, awatch

from .github_webhook_log import Repository

log = logging.getLogger(__name__)

# How many recent delivery GUIDs a consumer remembers. Sized against what a replay
# actually costs: the window only has to cover the records between the last commit and a
# crash, which is one batch, plus the head of a freshly truncated stream. A few thousand
# GUIDs is tens of kilobytes of state file and covers both with room to spare.
DEDUPE_WINDOW = 4096

# Bytes of the stream head hashed into the rotation fingerprint. One record is far larger
# than this, so the window always lands inside the first delivery — enough to tell one
# generation of a stream from the next, cheap enough to read on every poll.
_HEAD_FINGERPRINT_BYTES = 512

# watchfiles settings for `WebhookWatcher`. A batch of appends from a busy repository should
# arrive as one wake rather than one per file, and 200 ms of coalescing is invisible
# against GitHub's own delivery latency (documented as "up to a few minutes" worst case).
_WATCH_DEBOUNCE_MS = 200
_WATCH_STEP_MS = 50

# How long a watch that failed (the streams root vanished, inotify ran out of watches) waits
# before it is opened again. A back-off after an error, not a cadence: a healthy watch never
# waits on it.
_WATCH_RETRY_S = 5.0

_STATE_VERSION = 1


@dataclass
class Delivery:
    """One stored webhook delivery, decoded.

    `payload` is the parsed JSON body — the exact bytes GitHub sent, back out of the
    record's base64. It is decoded lazily, because a consumer usually filters on `event`
    first and most payloads it sees are not its business.

    Applying one of these to any cached state is subject to the hint-over-truth rules in
    this module's docstring: compare `payload["<subject>"]["updated_at"]` against what you
    hold, and do not treat `received_at` or yield order as a version.
    """

    stream: str
    """The `owner/repo` slug of the stream this record came from."""

    delivery_id: str
    """`X-GitHub-Delivery` — the dedupe key, and the same value for a redelivery."""

    event: str
    """`X-GitHub-Event`, e.g. `issues`, `pull_request`, `workflow_run`."""

    received_at: str
    """When AgentDeck stored it. Transport metadata, never a version for merging."""

    headers: Mapping[str, str]
    raw_body: bytes
    offset: int
    """Byte offset in the stream just past this record's line."""

    _payload: dict | None = field(default=None, repr=False, compare=False)

    @property
    def payload(self) -> dict:
        """The parsed JSON body, or `{}` if it will not parse.

        A body that does not decode is not worth raising over: the stream is diagnostic
        data that survives crashes and rotations, and one unreadable record must not stop
        a consumer reading the rest.
        """
        if self._payload is None:
            try:
                decoded = json.loads(self.raw_body)
            except (ValueError, UnicodeDecodeError):
                decoded = {}
            self._payload = decoded if isinstance(decoded, dict) else {}
        return self._payload

    @property
    def repository(self) -> Repository:
        owner, _, name = self.stream.partition("/")
        return Repository(owner=owner, name=name)


def _parse_received_at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _later_of(held: str, candidate: str) -> str:
    """The newer of two stored `received_at` strings.

    Compared as instants rather than as text: every record the appender writes uses the
    same UTC format, but a record recovered from an older or hand-edited stream need not,
    and a lexicographic compare on mixed offsets would silently pick the wrong one.
    """
    if not candidate:
        return held
    if not held:
        return candidate
    held_at = _parse_received_at(held)
    candidate_at = _parse_received_at(candidate)
    if held_at is None:
        return candidate
    if candidate_at is None:
        return held
    return candidate if candidate_at > held_at else held


def _head_size(raw: object, has_head: bool) -> int:
    if not has_head:
        return 0
    try:
        size = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _HEAD_FINGERPRINT_BYTES
    return size if 0 < size <= _HEAD_FINGERPRINT_BYTES else _HEAD_FINGERPRINT_BYTES


@dataclass
class _StreamState:
    offset: int = 0
    head: str = ""
    # How many bytes `head` hashes: fewer than `_HEAD_FINGERPRINT_BYTES` while the stream
    # is still shorter than that. Comparing a later fingerprint over the same span is what
    # keeps a short stream that merely grew from looking rotated, and lets a rotation find
    # its archive (#2754).
    head_size: int = 0
    last_delivery_at: str = ""


class WebhookTailer:
    """Reads new deliveries for one named consumer, from where that consumer left off.

    Each consumer picks a stable `consumer` name; that name owns a state file holding its
    per-stream byte offsets and its recent-GUID window, so two consumers of the same
    streams advance independently.

    The read-then-commit shape is the whole delivery contract:

    ```python
    tailer = WebhookTailer(config.github_webhook.state_dir, consumer="pill-cache")
    for delivery in tailer.deliveries():
        apply(delivery)          # idempotent, merged by payload updated_at
    tailer.commit()              # only now is the offset durable
    ```

    Nothing is committed until `commit()` returns, so a consumer that dies mid-batch sees
    those records again on its next pass. Calling `deliveries()` again without committing
    re-reads from the last committed offset — an uncommitted pass leaves no trace.

    Not safe to share between processes under one consumer name: the last writer's state
    file wins. Every consumer in #1626 runs in a single process, and a second one wants
    its own name rather than a lock.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        consumer: str,
        state_path: Path | str | None = None,
        dedupe_window: int = DEDUPE_WINDOW,
    ) -> None:
        if not consumer or "/" in consumer or consumer in {".", ".."}:
            raise ValueError(f"consumer name {consumer!r} is not usable as a file name")
        self.root = Path(root).expanduser()
        self.consumer = consumer
        self.dedupe_window = max(0, dedupe_window)
        # Which streams the last read replayed from zero, because they rotated or were
        # truncated. Part of the read's own result rather than a callback, so a consumer
        # cannot be told about a reset on a pass it never gets handed (#1807).
        self._reset_in_last_read: tuple[str, ...] = ()
        self.state_path = (
            Path(state_path).expanduser()
            if state_path is not None
            else self.root.parent / "webhook-tailers" / f"{consumer}.json"
        )
        self._streams: dict[str, _StreamState] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._pending: dict[str, _StreamState] = {}
        self._pending_seen: OrderedDict[str, None] = OrderedDict()
        self._loaded = False

    # -- discovery ---------------------------------------------------------------

    def streams(self) -> list[Repository]:
        """Every repository with a live stream on this host, in stable order.

        Archived `.jsonl.N.gz` generations are not streams — see the module docstring.
        """
        try:
            owners = sorted(entry for entry in self.root.iterdir() if entry.is_dir())
        except OSError:
            return []
        found: list[Repository] = []
        for owner in owners:
            try:
                files = sorted(owner.glob("*.jsonl"))
            except OSError:  # pragma: no cover - a directory vanishing mid-scan
                continue
            found.extend(Repository(owner=owner.name, name=path.stem) for path in files)
        return found

    def path_for(self, repository: Repository) -> Path:
        return self.root / repository.owner / f"{repository.name}.jsonl"

    # -- reading -----------------------------------------------------------------

    def deliveries(self, repositories: Iterable[Repository] | None = None) -> Iterator[Delivery]:
        """Yield every delivery this consumer has not committed yet.

        Deliveries already inside the recent-GUID window are skipped, so the ordinary
        replay after an uncommitted crash or a rotation costs nothing. Records are yielded
        stream by stream in file order — which is *not* event order, per rule 1 in the
        module docstring.

        The generator advances only pending state. Abandoning it part-way through, or
        never calling `commit()`, leaves the committed offsets exactly where they were.
        """
        self._load()
        self._pending = {}
        self._pending_seen = OrderedDict()
        self._reset_in_last_read = ()
        for repository in self.streams() if repositories is None else repositories:
            yield from self._read_stream(repository)

    def read(self, repositories: Iterable[Repository] | None = None) -> list[Delivery]:
        """`deliveries()` drained into a list, for a consumer that wants the batch first."""
        return list(self.deliveries(repositories))

    @property
    def reset_in_last_read(self) -> tuple[str, ...]:
        """The slugs the last read replayed from zero, because they rotated or truncated.

        A consumer whose state is built only from what it read wants this: it has just
        been told the bytes underneath its offset are not the bytes it read, and may
        need to rebuild from the source. It is emphatically **not** a claim that no
        delivery was missed — a dropped delivery raises nothing, anywhere.

        Empty on every ordinary pass, and reset at the start of each read, so read it
        before starting the next one.
        """
        return self._reset_in_last_read

    def _read_stream(self, repository: Repository) -> Iterator[Delivery]:
        path = self.path_for(repository)
        slug = repository.slug
        try:
            size = path.stat().st_size
        except OSError:
            return

        state = self._pending.get(slug) or self._streams.get(slug) or _StreamState()
        offset = state.offset
        head, head_size = self._head_fingerprint(path)
        same_span = (
            self._head_fingerprint(path, state.head_size)[0]
            if state.head and state.head_size != head_size
            else head
        )

        # Rotation and truncation, both of which #1622 performs in place. Either signal
        # alone is enough to reset; together they also cover a stream that rotated and
        # refilled past the old offset between two polls.
        rotated = offset > size or bool(state.head and same_span and same_span != state.head)
        archived: list[tuple[Path, int]] = []
        if rotated:
            archived = self._archived_tail(path, state)
            log.info(
                "webhook stream %s rotated or truncated; replaying it for consumer %s%s",
                slug,
                self.consumer,
                f" after {len(archived)} archive(s)"
                if archived
                else " (no archive matches the saved head; live file only)",
            )
            offset = 0
            self._reset_in_last_read = (*self._reset_in_last_read, slug)

        pending = _StreamState(
            offset=offset, head=head, head_size=head_size, last_delivery_at=state.last_delivery_at
        )
        self._pending[slug] = pending

        for archive, start in archived:
            # The archive's own offsets live in a throwaway state: what is committed for
            # the stream is where the *live* file has been read to.
            cursor = _StreamState(offset=start, last_delivery_at=pending.last_delivery_at)
            try:
                with gzip.open(archive, "rb") as handle:
                    handle.seek(start)
                    yield from self._read_lines(slug, handle, cursor)
            except (OSError, EOFError, gzip.BadGzipFile):
                # An archive that will not decompress must never stop the live read.
                log.warning("could not read webhook archive %s; skipping it", archive.name)
            pending.last_delivery_at = cursor.last_delivery_at

        if offset >= size:
            return

        try:
            handle = path.open("rb")
        except OSError:  # pragma: no cover - the stat above just succeeded
            return
        with handle:
            handle.seek(offset)
            yield from self._read_lines(slug, handle, pending)

    def _read_lines(self, slug: str, handle: BinaryIO, cursor: _StreamState) -> Iterator[Delivery]:
        """Yield the whole records from `handle`'s position on, advancing `cursor`."""
        while True:
            line = handle.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                # A partial final line is an append still in flight. Leave the offset
                # before it so the whole record is read on the next pass.
                break
            cursor.offset += len(line)
            delivery = self._decode(slug, line, cursor.offset)
            if delivery is None:
                continue
            cursor.last_delivery_at = _later_of(cursor.last_delivery_at, delivery.received_at)
            if self._already_seen(delivery.delivery_id):
                continue
            if delivery.delivery_id:
                self._pending_seen[delivery.delivery_id] = None
            yield delivery

    def _archived_tail(self, path: Path, state: _StreamState) -> list[tuple[Path, int]]:
        """The archives to read before the live file after a rotation, oldest first.

        Each entry is an archive and the offset to start it from. The archive whose first
        bytes match the committed head fingerprint holds the bytes the committed offset
        points into, so it is read from that offset; every newer archive was never seen
        and is read whole. Empty when nothing matches, which means replay live only.
        """
        if not state.head:
            return []
        archives: list[tuple[int, Path]] = []
        prefix = f"{path.name}."
        try:
            candidates = list(path.parent.glob(f"{path.name}.*.gz"))
        except OSError:  # pragma: no cover - a directory vanishing mid-scan
            return []
        for candidate in candidates:
            index = candidate.name[len(prefix) : -len(".gz")]
            if index.isdigit():
                archives.append((int(index), candidate))
        archives.sort()
        for position, (_, archive) in enumerate(archives):
            if self._archive_fingerprint(archive, state.head_size) == state.head:
                newer = [candidate for _, candidate in reversed(archives[:position])]
                return [(archive, state.offset), *((candidate, 0) for candidate in newer)]
        return []

    def _archive_fingerprint(self, archive: Path, size: int) -> str:
        try:
            with gzip.open(archive, "rb") as handle:
                head = handle.read(size)
        except (OSError, EOFError, gzip.BadGzipFile):
            return ""
        if len(head) < size:
            return ""
        return hashlib.sha256(head).hexdigest()

    def _decode(self, slug: str, line: bytes, offset: int) -> Delivery | None:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            log.warning("skipping unparseable record in webhook stream %s", slug)
            return None
        if not isinstance(record, dict):
            return None
        try:
            raw = base64.b64decode(str(record.get("raw_body_base64", "")))
        except (ValueError, TypeError):
            log.warning("skipping record with undecodable body in webhook stream %s", slug)
            return None
        return Delivery(
            stream=str(record.get("repository") or slug),
            delivery_id=str(record.get("delivery_id") or ""),
            event=str(record.get("event") or ""),
            received_at=str(record.get("received_at") or ""),
            headers=dict(record.get("headers") or {}),
            raw_body=raw,
            offset=offset,
        )

    def _already_seen(self, delivery_id: str) -> bool:
        if not delivery_id or self.dedupe_window <= 0:
            return False
        return delivery_id in self._seen or delivery_id in self._pending_seen

    def _head_fingerprint(self, path: Path, size: int = _HEAD_FINGERPRINT_BYTES) -> tuple[str, int]:
        """The hash of the stream's first `size` bytes, and how many bytes it covers."""
        try:
            with path.open("rb") as handle:
                head = handle.read(size)
        except OSError:  # pragma: no cover - the caller just stat'd this file
            return "", 0
        if not head:
            return "", 0
        return hashlib.sha256(head).hexdigest(), len(head)

    # -- freshness ---------------------------------------------------------------

    def last_delivery_seen(self, repository: Repository | str) -> datetime | None:
        """When this consumer last read a delivery from one stream, or `None`.

        This is the freshness *hint* #1626 asked the library to expose, and it is the one
        thing here most likely to be misused. It is safe for display and for diagnosis —
        "the stream has been quiet for an hour" is a useful thing to show a person.

        It is not safe as a reason to do less work. A stream can look perfectly healthy
        while a delivery has been silently dropped: GitHub never retries a failed
        delivery, and it never sends a payload over 25 MB at all (#1625). So a recent
        value here does **not** mean state is fresh, and must never gate, delay or stretch
        a poll. It reports what this consumer has read, not what GitHub has sent.
        """
        self._load()
        slug = repository if isinstance(repository, str) else repository.slug
        state = self._pending.get(slug) or self._streams.get(slug)
        if state is None or not state.last_delivery_at:
            return None
        return _parse_received_at(state.last_delivery_at)

    def last_deliveries_seen(self) -> dict[str, datetime]:
        """`last_delivery_seen` for every stream this consumer has read, by slug.

        The same warning applies: a hint for display, never a reason to skip a poll.
        """
        self._load()
        seen: dict[str, datetime] = {}
        for slug in {**self._streams, **self._pending}:
            moment = self.last_delivery_seen(slug)
            if moment is not None:
                seen[slug] = moment
        return seen

    # -- state -------------------------------------------------------------------

    def commit(self) -> None:
        """Make this pass durable: offsets, freshness, and the recent-GUID window.

        Call it **after** the batch has been processed, never before. Everything read
        since the last commit is replayed if this is never reached, which is the whole of
        the at-least-once guarantee.

        The candidate state is built without touching what is already committed, and is
        published to memory only once the file has been written and replaced. A failing
        write therefore raises with the committed offsets *and* the pending batch both
        exactly where they were, so the caller can retry the commit or let the next pass
        replay. Advancing memory first would drop a batch precisely when the durable
        record of it failed — the one moment the guarantee exists for.
        """
        self._load()
        if not self._pending and not self._pending_seen:
            return
        streams = {**self._streams, **self._pending}
        seen = OrderedDict(self._seen)
        for delivery_id in self._pending_seen:
            seen.pop(delivery_id, None)
            seen[delivery_id] = None
        while len(seen) > self.dedupe_window:
            seen.popitem(last=False)

        self._write_state(streams, seen)

        self._streams = streams
        self._seen = seen
        self._pending = {}
        self._pending_seen = OrderedDict()

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # No state, or state we cannot read. Starting from zero replays every live
            # stream once, which the GUID window and consumer idempotence both absorb —
            # the alternative, refusing to start, would leave a consumer permanently down
            # over a corrupted bookkeeping file.
            return
        if not isinstance(raw, dict) or raw.get("version") != _STATE_VERSION:
            return
        for slug, value in (raw.get("streams") or {}).items():
            if not isinstance(value, dict):
                continue
            try:
                offset = int(value.get("offset") or 0)
            except (TypeError, ValueError):
                continue
            self._streams[str(slug)] = _StreamState(
                offset=max(0, offset),
                head=str(value.get("head") or ""),
                # State written before #2754 hashed the full window whenever it had a head.
                head_size=_head_size(value.get("head_size"), bool(value.get("head"))),
                last_delivery_at=str(value.get("last_delivery_at") or ""),
            )
        for delivery_id in raw.get("seen") or []:
            if isinstance(delivery_id, str) and delivery_id:
                self._seen[delivery_id] = None
        while len(self._seen) > self.dedupe_window:
            self._seen.popitem(last=False)

    def _write_state(self, streams: dict[str, _StreamState], seen: Mapping[str, None]) -> None:
        """Put the candidate state on disk, or raise having changed nothing.

        Takes the candidate rather than reading `self`, so `commit()` can call it before
        publishing anything. Nothing in here touches the tailer's own attributes.
        """
        payload = {
            "version": _STATE_VERSION,
            "consumer": self.consumer,
            "streams": {
                slug: {
                    "offset": state.offset,
                    "head": state.head,
                    "head_size": state.head_size,
                    "last_delivery_at": state.last_delivery_at,
                }
                for slug, state in sorted(streams.items())
            },
            "seen": list(seen),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Written aside and renamed, so a crash mid-write leaves the previous offsets
        # rather than a truncated file. Losing them would replay whole streams; the
        # dedupe window is not sized for that. The staging name is unique per write: during
        # a web hot reload the old and the new worker run the same tailers, and a shared
        # `<name>.tmp` let one rename the other's file away mid-commit (#3012).
        fd, temp = tempfile.mkstemp(
            dir=self.state_path.parent, prefix=f".{self.state_path.name}.", suffix=".tmp"
        )
        staging = Path(temp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            staging.replace(self.state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                staging.unlink()
            raise

    # -- watching ----------------------------------------------------------------

    async def watch(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        watcher: WebhookWatcher | None = None,
        heartbeat: Callable[[], None] | None = None,
        keepalive_s: float | None = None,
        yield_on_reset: bool = False,
    ) -> AsyncIterator[list[Delivery]]:
        """Yield batches of new deliveries as they are appended, catching up first.

        The append itself is the signal (#1626): `watcher` is the one inotify watch on the
        streams root, via `watchfiles`, that the web process shares between every consumer
        (#2865). This consumer registers with it, reads once at start — so a consumer that
        was down does not wait for the next delivery to catch up — and then reads again
        each time the watcher says a stream changed. A missed filesystem event costs
        latency and nothing else, because the next append's read re-reads from the same
        committed offset. There is no timed re-read.

        Without a shared `watcher` the call opens a private one for its own lifetime, with
        `keepalive_s` as that watcher's keep-alive; a shared watcher's keep-alive is the
        one it was built with.

        Empty batches are not yielded, and `commit()` is still the caller's to make once
        it has processed a batch.

        ``heartbeat`` is called once per pass and on every keep-alive of the watcher —
        including the keep-alives a quiet stream produces with no read at all — which is
        exactly what makes it a liveness proof (#1631): a caller that touches a file from
        it moves that file's mtime for as long as the watch is actually running, and stops
        the moment it is not. A heartbeat that raises during a pass ends the watch like any
        consumer error, so the caller owns making it harmless.

        This is a latency optimisation over the streams, never a substitute for polling —
        see the hint-over-truth rules in the module docstring.
        """
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        owned = watcher is None
        if watcher is None:
            watcher = WebhookWatcher(self.root, keepalive_s=keepalive_s)
        wake = watcher.subscribe()
        if heartbeat is not None:
            watcher.add_keepalive(heartbeat)
        try:
            # The first read waits for the watch to be open, so an append that lands
            # between this read and the watch cannot sit unseen until the next one.
            if not await _either(watcher.settled, stop_event):
                return
            wake.clear()
            if heartbeat is not None:
                heartbeat()
            batch = self.read()
            if self._worth_yielding(batch, yield_on_reset):
                yield batch
            while await _either(wake, stop_event):
                # Cleared before the read, so an append landing during it wakes us again.
                wake.clear()
                if heartbeat is not None:
                    heartbeat()
                batch = self.read()
                if self._worth_yielding(batch, yield_on_reset):
                    yield batch
        finally:
            watcher.unsubscribe(wake)
            if heartbeat is not None:
                watcher.remove_keepalive(heartbeat)
            if owned:
                await watcher.stop()

    def _worth_yielding(self, batch: list[Delivery], yield_on_reset: bool) -> bool:
        """Is this pass worth waking the consumer for?

        Deliveries always are. A reset is too, for a consumer that asked: a rotation
        whose whole replay sits inside the dedupe window yields nothing, and dropping
        that pass would leave such a consumer's repair waiting on an unrelated future
        delivery — which on a quiet repository is precisely when its rows are wrong and
        nothing is coming to correct them (#1807).
        """
        return bool(batch) or (yield_on_reset and bool(self._reset_in_last_read))


async def _either(wake: asyncio.Event, stop_event: asyncio.Event | None) -> bool:
    """Wait for ``wake`` or ``stop_event``; True when it was the wake and not the stop."""
    if stop_event is not None and stop_event.is_set():
        return False
    if stop_event is None:
        await wake.wait()
        return True
    waits = [asyncio.ensure_future(wake.wait()), asyncio.ensure_future(stop_event.wait())]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiting in waits:
            waiting.cancel()
    return not stop_event.is_set()


class WebhookWatcher:
    """The one watch on the streams root, waking every registered consumer (#2865).

    Before #2865 every consumer opened its own `awatch` with a five-second timeout and
    re-read every stream on each timeout: five consumers, five re-reads every five seconds,
    whether or not anything had arrived. Now the web process holds one of these, and an
    append is the only thing that makes a consumer read. Each consumer still reads through
    its own `WebhookTailer` from its own committed offset, so one consumer failing touches
    no other, and the delivery semantics in the module docstring are each consumer's alone.

    A wake is an `asyncio.Event` per consumer, set on every change and cleared by the
    consumer before it reads, so a burst of appends while a consumer is busy collapses to
    one more read rather than a queue of them.

    ``keepalive_s`` is the one timed thing here, and it reads nothing: when set, the watch
    also returns that often on a quiet stream, and each registered keep-alive callback is
    called. The poller's quiet skip needs that — its heartbeat file is the proof that this
    watch is alive (#1631), and a proof of life has to move while nothing happens. It is a
    keep-alive in the sense of ADR 0007, like the SSE ``: ping``, not a re-read.
    """

    def __init__(self, root: Path | str, *, keepalive_s: float | None = None) -> None:
        self.root = Path(root).expanduser()
        self.keepalive_s = keepalive_s if keepalive_s and keepalive_s > 0 else None
        self._wakes: list[asyncio.Event] = []
        self._keepalives: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Set once the first watch is open (or failed to open, so nobody waits for ever):
        # a consumer's catch-up read waits for it, which is what closes the gap between
        # that read and the watch.
        self.settled = asyncio.Event()
        #: How many times a change woke the consumers. Diagnostic, and what tests count.
        self.wakes = 0

    def subscribe(self) -> asyncio.Event:
        """Register one consumer; its event is set on every change. Starts the watch."""
        wake = asyncio.Event()
        self._wakes.append(wake)
        self.start()
        return wake

    def unsubscribe(self, wake: asyncio.Event) -> None:
        with contextlib.suppress(ValueError):
            self._wakes.remove(wake)

    def add_keepalive(self, callback: Callable[[], None]) -> None:
        self._keepalives.append(callback)

    def remove_keepalive(self, callback: Callable[[], None]) -> None:
        with contextlib.suppress(ValueError):
            self._keepalives.remove(callback)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.get_running_loop().create_task(self._run(), name="webhook-watcher")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def wake_all(self) -> None:
        """Tell every consumer to read. An idle read is one stat per stream."""
        self.wakes += 1
        for wake in tuple(self._wakes):
            wake.set()

    def _keep_alive(self) -> None:
        for callback in tuple(self._keepalives):
            try:
                callback()
            except Exception:  # noqa: BLE001 - a keep-alive must never take the watch down
                log.exception("webhook watcher keep-alive failed")

    async def _run(self) -> None:
        reopened = False
        while not self._stop.is_set():
            try:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                if reopened:
                    # The watch was down: whatever landed meanwhile raised no event.
                    self.wake_all()
                # Set before `awatch` opens its watch, but no waiter runs until this task
                # next suspends, and `awatch` opens the watch synchronously before its
                # first suspension — so a consumer resumed by this reads after the watch
                # exists.
                self.settled.set()
                async for changes in awatch(
                    self.root,
                    debounce=_WATCH_DEBOUNCE_MS,
                    step=_WATCH_STEP_MS,
                    watch_filter=_stream_change,
                    recursive=True,
                    stop_event=self._stop,
                    rust_timeout=int(self.keepalive_s * 1000) if self.keepalive_s else 0,
                    yield_on_timeout=self.keepalive_s is not None,
                ):
                    if changes:
                        self.wake_all()
                    self._keep_alive()
                return  # the stop event ended the watch
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("webhook watcher failed; reopening it")
                self.settled.set()
                reopened = True
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=_WATCH_RETRY_S)


def _stream_change(change: Change, path: str) -> bool:
    """Only a live stream file is a wake. Archives and state files are not.

    A new owner directory is one too: the first delivery for a new owner creates the
    directory and writes its stream at once, and the write can land before the recursive
    watch has reached inside the directory. With no timed re-read behind it (#2865) that
    delivery would wait for the next append, so the directory's creation wakes a read —
    which finds the stream, because the watch is extended before the event is delivered.
    """
    if path.endswith(".jsonl"):
        return True
    return change == Change.added and os.path.isdir(path)


@contextlib.contextmanager
def committing(tailer: WebhookTailer) -> Iterator[WebhookTailer]:
    """Commit the tailer on a clean exit from the block, and not on an exception.

    Sugar over the read-then-commit shape for a consumer whose whole batch handling fits
    in one block:

    ```python
    with committing(tailer) as t:
        for delivery in t.deliveries():
            apply(delivery)
    ```
    """
    yield tailer
    tailer.commit()
