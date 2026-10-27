"""The durable per-repository record of every GitHub webhook AgentDeck receives (#1601).

This is the whole of stage one: a delivery arrives, and its exact bytes land in the file
belonging to the repository it came from. Nothing here reads a payload for its meaning,
wakes a Poller Instance, or turns an event into an AgentDeck concept. The only thing it
takes out of the payload is the repository's identity, because that is what decides which
file the record belongs in.

Three properties are the point, and each costs one deliberate decision:

* **The original bytes survive.** The body is stored base64-encoded, so a payload
  containing newlines can never split a record and a payload that is not valid UTF-8 can
  never be lossily re-encoded. ``base64.b64decode(record["raw_body_base64"])`` returns
  what GitHub sent, byte for byte.
* **A record belongs to exactly one repository.** ``owner`` and ``repo`` come from the
  delivery's own ``repository`` object and are checked against a conservative filename
  charset. A delivery whose repository cannot be established is refused, never appended
  to a shared bucket — a shared bucket would destroy the one invariant this stage exists
  to provide.
* **Concurrent deliveries cannot interleave.** Each append takes an exclusive
  ``flock`` on the stream file and writes one line under it. ``O_APPEND`` alone is
  atomic only below ``PIPE_BUF``, and a GitHub payload is comfortably larger than that.

A fourth property arrived with #1622, once the public ingress was actually live: **the
streams are bounded.** A full stream is rotated aside and gzipped by the same append that
filled it, under the same lock, so rotation can never race an append. That is bookkeeping
about bytes on disk and nothing more — which delivery is old is not a reading of what any
delivery meant.

Deliberately absent, per the card: no dedupe, no routing, no event filter, no consumer.
An unknown event type is stored exactly like a known one, because nothing here knows
what a known one is.
"""

from __future__ import annotations

import base64
import fcntl
import gzip
import hashlib
import hmac
import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DELIVERY_HEADER = "X-GitHub-Delivery"
EVENT_HEADER = "X-GitHub-Event"
SIGNATURE_HEADER = "X-Hub-Signature-256"

# GitHub's own documented ceiling: it will not deliver a payload larger than 25 MB, and
# it truncates or drops the event rather than sending one. Matching that exactly is the
# only bound that keeps this stage's promise — every delivery that reaches AgentDeck is
# kept — while still refusing an unbounded body. A smaller "nothing we subscribe to comes
# close" figure would silently turn a legitimate large push or release event into a 413
# and lose it from the stream, which is precisely the failure this file exists to prevent.
MAX_BODY_BYTES = 25 * 1024 * 1024

# When a live stream reaches this size it is rotated aside, and this many rotations are
# kept before the oldest is dropped. Two deliveries at GitHub's ceiling already exceed the
# threshold, so a rotation always holds at least one whole delivery. The bound a host can
# actually plan around is ROTATE_BYTES plus the compressed rotations — JSON of this shape
# compresses roughly ten to one, so the tail costs a few megabytes rather than 150.
ROTATE_BYTES = 50 * 1024 * 1024
KEEP_ROTATIONS = 3

# Transport metadata worth keeping for diagnostics. The signature header is deliberately
# not among them: it is derived from the shared secret, and an event stream is not the
# place to accumulate material about a credential.
RETAINED_HEADERS = (
    DELIVERY_HEADER,
    EVENT_HEADER,
    "X-GitHub-Hook-ID",
    "X-GitHub-Hook-Installation-Target-ID",
    "X-GitHub-Hook-Installation-Target-Type",
    "Content-Type",
    "User-Agent",
)

# Conservative on purpose: GitHub's own owner and repository names are a subset of this,
# and anything outside it has no business becoming a path component on this host.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RepositoryUnknown(ValueError):
    """The delivery does not name a repository we can safely file it under."""


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


def _safe_segment(value: object, *, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RepositoryUnknown(f"delivery names no {what}")
    if text in {".", ".."} or not _SEGMENT_RE.match(text):
        raise RepositoryUnknown(f"{what} {text!r} is not a usable directory name")
    return text


def repository_of(body: bytes) -> Repository:
    """The repository a delivery belongs to, or ``RepositoryUnknown``.

    Only the ``repository`` object is consulted. Organization-level and App-level
    deliveries (``installation``, an org ``ping``) genuinely do not belong to one
    repository, and this stage refuses them rather than inventing a home for them.
    """
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RepositoryUnknown(f"body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RepositoryUnknown("body is not a JSON object")

    repo = payload.get("repository")
    if not isinstance(repo, dict):
        raise RepositoryUnknown("payload carries no repository object")

    full_name = repo.get("full_name")
    if isinstance(full_name, str) and full_name.count("/") == 1:
        owner_part, _, name_part = full_name.partition("/")
    else:
        owner = repo.get("owner")
        owner_part = owner.get("login") if isinstance(owner, dict) else ""
        name_part = repo.get("name")

    return Repository(
        owner=_safe_segment(owner_part, what="repository owner"),
        name=_safe_segment(name_part, what="repository name"),
    )


def verify_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    """``X-Hub-Signature-256`` over the raw body, GitHub's scheme."""
    if not secret or not header_value:
        return False
    prefix, _, digest = header_value.strip().partition("=")
    if prefix != "sha256" or not digest:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest.lower())


def build_record(
    *,
    body: bytes,
    headers: Mapping[str, str],
    repository: Repository,
    received_at: str | None = None,
) -> dict:
    """The transport envelope around one delivery.

    Framing only. Every field outside ``raw_body_base64`` is either something the
    transport told us or something we computed about the bytes; none of it is an
    AgentDeck-specific reading of the event.
    """
    retained = {}
    for name in RETAINED_HEADERS:
        value = headers.get(name) or headers.get(name.lower())
        if value:
            retained[name] = value
    return {
        "received_at": received_at or datetime.now(UTC).isoformat(),
        "delivery_id": retained.get(DELIVERY_HEADER, ""),
        "event": retained.get(EVENT_HEADER, ""),
        "repository": repository.slug,
        "headers": retained,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "raw_body_base64": base64.b64encode(body).decode("ascii"),
    }


class WebhookLog:
    """The append-only streams under one host directory.

    Held on ``app.state`` so a test can point one at ``tmp_path``. There is deliberately
    no fallback to the default directory when the app was assembled without one: a canary
    deck shares this host, and quietly writing into the live stream would corrupt the
    record the live deck keeps.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        secret_env: str = "AGENTDECK_WEBHOOK_SECRET",
        rotate_bytes: int = ROTATE_BYTES,
        keep_rotations: int = KEEP_ROTATIONS,
    ) -> None:
        self.root = Path(root).expanduser()
        self.secret_env = secret_env
        self.rotate_bytes = rotate_bytes
        self.keep_rotations = keep_rotations

    def secret(self) -> str:
        """The shared webhook secret, from the environment only.

        Never from the config file and never written into a stream file: the config file
        is committed-adjacent and the streams are diagnostic data.
        """
        return (os.environ.get(self.secret_env) or "").strip()

    def path_for(self, repository: Repository) -> Path:
        return self.root / repository.owner / f"{repository.name}.jsonl"

    def rotation_path(self, repository: Repository, index: int) -> Path:
        """Where the ``index``-th oldest archived stream lives (1 is the newest)."""
        live = self.path_for(repository)
        return live.with_name(f"{live.name}.{index}.gz")

    def rotations(self, repository: Repository) -> list[Path]:
        """The archived streams that currently exist, newest first."""
        return [
            path
            for path in (
                self.rotation_path(repository, index) for index in range(1, self.keep_rotations + 1)
            )
            if path.exists()
        ]

    def append(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        received_at: str | None = None,
    ) -> tuple[Repository, Path]:
        """Store one delivery. Raises ``RepositoryUnknown`` if it cannot be filed."""
        repository = repository_of(body)
        record = build_record(
            body=body, headers=headers, repository=repository, received_at=received_at
        )
        path = self.path_for(repository)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"

        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # Exclusive for the whole write, so two deliveries arriving at once queue
            # instead of interleaving. O_APPEND still guarantees we never overwrite a
            # neighbour's bytes; the lock is what guarantees whole lines.
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                # os.write is allowed to return a short count, and these records run to
                # megabytes. One unchecked call would leave a truncated line whose body
                # no longer decodes, while the request cheerfully reported success — a
                # silently unrecoverable record is worse than a refused delivery. The
                # loop stays inside the lock, so the remainder cannot be split by another
                # delivery's append.
                data = line.encode("utf-8")
                written = 0
                while written < len(data):
                    sent = os.write(fd, data[written:])
                    if sent <= 0:  # pragma: no cover - the OS raises instead in practice
                        raise OSError("write made no progress on the webhook stream")
                    written += sent
                os.fsync(fd)
                # Still under the same lock, so no other delivery can be mid-append while
                # the stream is archived and emptied. Rotating after the write rather than
                # before it means the threshold is the size a stream is allowed to reach,
                # not a size it is allowed to exceed by one delivery.
                if self.rotate_bytes > 0 and os.fstat(fd).st_size >= self.rotate_bytes:
                    self._rotate(fd, path)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return repository, path

    def _rotate(self, fd: int, path: Path) -> None:
        """Archive the full stream and empty it, with the caller's lock still held.

        The live file is compressed and then **truncated in place** rather than renamed
        aside. That is the decision worth knowing about: the inode never changes, so a
        delivery that was blocked on the lock while this ran appends to the same open file
        afterwards and lands in the new stream, with no chance of writing into an archive
        or into a file nothing can find. A rename would leave that waiting appender holding
        a descriptor onto the file just archived.

        The order — write the archive fully, then empty the source — is chosen so that a
        crash in the middle duplicates records rather than losing them. A stream this stage
        keeps for diagnosis is better repeated than gone.
        """
        keep = self.keep_rotations
        if keep <= 0:
            # Bounded to the live file alone: archive nothing, just start again.
            os.truncate(fd, 0)
            return

        oldest = path.with_name(f"{path.name}.{keep}.gz")
        oldest.unlink(missing_ok=True)
        for index in range(keep - 1, 0, -1):
            older = path.with_name(f"{path.name}.{index}.gz")
            if older.exists():
                older.replace(path.with_name(f"{path.name}.{index + 1}.gz"))

        # Compress under a temporary name and move it into place, so a reader never finds
        # a half-written archive at the name it trusts.
        staging = path.with_name(f"{path.name}.rotating.gz")
        staging.unlink(missing_ok=True)
        archive_fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with (
                os.fdopen(archive_fd, "wb") as raw_archive,
                gzip.GzipFile(filename="", mode="wb", fileobj=raw_archive) as archive,
                path.open("rb") as source,
            ):
                shutil.copyfileobj(source, archive)
            # fsync the archive before the source is emptied; otherwise a power loss
            # between the two leaves neither copy of those records on disk.
            with staging.open("rb") as written:
                os.fsync(written.fileno())
        except OSError:
            staging.unlink(missing_ok=True)
            raise
        staging.replace(path.with_name(f"{path.name}.1.gz"))
        os.truncate(fd, 0)

    def read(self, repository: Repository) -> list[dict]:
        """Every record stored for one repository — for tests and diagnostics.

        A line that will not parse is skipped rather than raised on, so one bad line
        (a half-written file recovered from a crash, say) cannot make the rest
        unreadable.
        """
        try:
            raw = self.path_for(repository).read_text(encoding="utf-8")
        except OSError:
            return []
        records = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records


def raw_body(record: Mapping[str, object]) -> bytes:
    """The exact bytes GitHub sent, back out of a stored record."""
    return base64.b64decode(str(record["raw_body_base64"]))
