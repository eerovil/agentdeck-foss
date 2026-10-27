"""Durable engine state: versioned schema, atomic backup IO, and the transaction lock.

This is slice 1 of the shared Poller Engine core (#176). It rebuilds the durable
state surface from the reference engine as a deep module: a single ``StateStore``
hides path derivation, atomic crash-safe writes, last-valid backup fallback, the
cross-process file lock, and — new in #176 — an explicit schema version with a
migration ladder (the reference engine had no versioned state; #106 requires one).

Accepted commands are published to their own file *before* the worker state, and merged
back over it on load, so a rolled-back or lost state generation can never forget a command
the engine has already answered (see ``ledger.py``).

The store exposes three operations:

* ``transaction()`` — take the lock, load the current state (migrating an older
  on-disk schema, failing closed on a newer one), yield a mutable dict, and on
  clean exit publish it atomically while keeping the previous value as ``.bak``.
  An exception inside the block persists nothing.
* ``checkpoint()`` — publish mid-transaction without releasing the lock, for the
  intents a cycle must make durable before it acts on them (see ``cycle.py``).
* ``read()`` — a lock-free point-in-time read for observers (#107). Writes are
  atomic (``os.replace``), so a lock-free read always sees a complete file.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import ledger

# Bump when the durable state shape changes incompatibly, and add the matching
# entry to ``_MIGRATIONS``. The engine is pinned to exactly one current version:
# an older on-disk schema is migrated up; a newer one fails closed (a newer engine
# must have written it), mirroring the Manifest contract rule in #108/#116.
CURRENT_SCHEMA_VERSION = 2


class StateError(Exception):
    """Base class for durable-state failures."""


class StateSchemaError(StateError):
    """On-disk state is a schema this engine cannot read (newer, or no migration)."""


@dataclass(frozen=True)
class EnginePaths:
    """The durable file paths for one Poller Instance, derived from a base state path."""

    state: Path
    backup: Path
    lock: Path
    controls: Path
    controls_backup: Path
    ownership: Path
    dispatch_ack_dir: Path
    dispatch_defer_dir: Path
    review_verdict_dir: Path

    @classmethod
    def from_base(cls, state_path: str | os.PathLike[str]) -> EnginePaths:
        state = Path(state_path)
        directory = state.parent
        return cls(
            state=state,
            backup=state.with_name(state.name + ".bak"),
            lock=state.with_name(state.name + ".lock"),
            controls=directory / "controls.json",
            controls_backup=directory / "controls.json.bak",
            ownership=directory / "ownership.json",
            dispatch_ack_dir=directory / "dispatch-acks",
            dispatch_defer_dir=directory / "dispatch-deferrals",
            # What each merge review decided about one commit, written by the engine and
            # drained into the deck's database by the host (#1122). The engine owns the
            # fact; only the host can reach the database.
            review_verdict_dir=directory / "review-verdicts",
        )


# --- low-level durable-JSON primitives (reused by the controls/ownership ledgers) ---

# Whether a durable publish waits for the disk. Production always does, which is the whole
# point of the write below; a test suite that publishes this file thousands of times cannot
# observe the difference and should not pay for it.
#
# Nothing about what a *reader* sees changes either way: the temp file plus ``os.replace`` is
# what makes a publish atomic, and the flushes only decide whether it survives a power cut. A
# publish costs about 19ms with them and about 2ms without, so a poller test doing twenty
# state checkpoints spends most of its wall clock here (#1473).
FSYNC_ENV = "AGENTDECK_FSYNC"


def durable_writes() -> bool:
    """Read the switch per call, so setting it does not depend on import order."""
    return os.environ.get(FSYNC_ENV, "1") != "0"


def write_json_atomic(path: Path, value: object) -> None:
    """Write JSON crash-safely: temp file + fsync, atomic rename, then fsync the directory."""
    durable = durable_writes()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as handle:
        json.dump(value, handle, indent=2)
        handle.flush()
        if durable:
            os.fsync(handle.fileno())
    os.replace(tmp, path)
    if not durable:
        return
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


@dataclass(frozen=True)
class LoadedJson:
    """A durable read's value together with which file actually supplied it.

    Callers used to answer "did the primary file parse?" with a second
    ``json.load`` of the same file, so every load of a 230 KB state file paid
    for two decodes of it — and the board read on top paid for a third (#1362).
    The read already knows which candidate it took; saying so costs nothing.
    """

    value: dict
    #: The candidate that parsed as a dict, or ``None`` when neither did.
    source: Path | None = None


def load_json_with_backup(path: Path, backup: Path, default: dict) -> LoadedJson:
    """Read a durable JSON dict, falling back to the last-valid ``.bak`` on loss/corruption."""
    for candidate in (path, backup):
        try:
            with open(candidate) as handle:
                value = json.load(handle)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return LoadedJson(value, candidate)
    return LoadedJson(dict(default), None)


def read_json_with_backup(path: Path, backup: Path, default: dict) -> dict:
    """The value alone, for a caller that does not care which file supplied it."""
    return load_json_with_backup(path, backup, default).value


def save_json_with_backup(path: Path, backup: Path, value: object) -> None:
    """Publish a new value, preserving the previous valid one as the backup first."""
    previous = read_json_with_backup(path, backup, {})
    if previous:
        write_json_atomic(backup, previous)
    write_json_atomic(path, value)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Serialize read-modify-write across the poll and worker processes (exclusive flock)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# --- schema envelope + migration ladder ---

def _m0_to_1(data: dict) -> dict:
    """v0 (legacy bare dict, pre-versioning) → v1: adopt the envelope; payload unchanged."""
    return data


def _m1_to_2(data: dict) -> dict:
    """v1 → v2: forget the file-overlap gate (#2081).

    Two keys outlive the gate on a live instance: ``overlap_hubs`` at the root, a tally that
    had grown to kilobytes, and ``blocked_by_overlap`` on the record of every card the gate
    was holding. Nothing reads either one after this, but a held card must *start* on the
    first wake after the deploy rather than sit on a hold nobody will ever clear — so the
    keys are dropped here instead of being left to rot.
    """
    data.pop("overlap_hubs", None)
    issues = data.get("issues")
    if isinstance(issues, dict):
        for rec in issues.values():
            if isinstance(rec, dict):
                rec.pop("blocked_by_overlap", None)
    return data


# Maps a version to the function that upgrades its payload to the next version.
_MIGRATIONS: dict[int, Callable[[dict], dict]] = {
    0: _m0_to_1,
    1: _m1_to_2,
}


def _unwrap(raw: dict) -> tuple[int, dict]:
    """Split a stored dict into (schema_version, payload). A bare dict is legacy v0."""
    if "schema_version" in raw:
        version = raw.get("schema_version")
        payload = raw.get("data", {})
        if not isinstance(version, int) or not isinstance(payload, dict):
            raise StateSchemaError("malformed state envelope")
        return version, payload
    return 0, raw


def _migrate(payload: dict, from_version: int) -> dict:
    """Bring a payload from its on-disk version up to the current one, or fail closed."""
    if from_version > CURRENT_SCHEMA_VERSION:
        raise StateSchemaError(
            f"state schema v{from_version} is newer than engine v{CURRENT_SCHEMA_VERSION}"
        )
    version = from_version
    while version < CURRENT_SCHEMA_VERSION:
        migrate = _MIGRATIONS.get(version)
        if migrate is None:
            raise StateSchemaError(f"no migration from state schema v{version}")
        payload = migrate(payload)
        version += 1
    return payload


def _wrap(payload: dict) -> dict:
    return {"schema_version": CURRENT_SCHEMA_VERSION, "data": payload}


@dataclass(frozen=True)
class LoadReport:
    """What the last load had to do: fall back, and rescue commands from the ledger."""

    used_backup: bool = False
    recovered: tuple[str, ...] = field(default_factory=tuple)
    #: Did the state file or its backup parse at all? ``read`` returns an empty
    #: payload both for an instance that has never run and for one whose durable
    #: files are gone or corrupt, and a reader that must tell those apart would
    #: otherwise re-decode the same file to find out (#1362).
    found: bool = False


class StateStore:
    """Deep store over one instance's durable state file (see module docstring)."""

    def __init__(self, paths: EnginePaths) -> None:
        self._paths = paths
        self._last_load = LoadReport()

    @property
    def paths(self) -> EnginePaths:
        return self._paths

    @property
    def last_load(self) -> LoadReport:
        """How the most recent load went — the cycle reports this as health (#107)."""
        return self._last_load

    def _load(self, *, now: datetime | None = None) -> dict:
        loaded = load_json_with_backup(self._paths.state, self._paths.backup, {})
        raw = loaded.value
        stored = read_json_with_backup(self._paths.controls, self._paths.controls_backup, {})
        # Decide degradation *before* merging: an unversioned payload is the same object as
        # ``raw``, so the merge would otherwise make an empty state look populated.
        degraded = (bool(raw) and loaded.source != self._paths.state) or (
            not raw and bool(stored)
        )
        version, payload = _unwrap(raw)
        payload = _migrate(payload, version)
        context = ledger.merge(payload, stored)
        recovered = ledger.recover(payload, context, now=now or datetime.now(UTC))
        self._last_load = LoadReport(
            used_backup=degraded,
            recovered=tuple(recovered),
            found=loaded.source is not None,
        )
        return payload

    def read(self) -> dict:
        """Lock-free point-in-time read of the current-schema payload ({} if none yet)."""
        return self._load()

    def checkpoint(self, payload: dict) -> None:
        """Publish mid-transaction, without releasing the lock.

        A cycle must make some intents durable *before* it acts on them — queued
        commands before any dispatch, a lease and send intent before a directive
        leaves — so that a crash can at worst repeat an idempotent step and never
        produce work the durable state does not know about.

        The command ledger is published first: a crash between the two writes can at
        worst replay an idempotent state transition, never forget an accepted command.
        """
        save_json_with_backup(
            self._paths.controls, self._paths.controls_backup, ledger.snapshot(payload)
        )
        save_json_with_backup(self._paths.state, self._paths.backup, _wrap(payload))

    @contextmanager
    def transaction(self) -> Iterator[dict]:
        """Locked read-modify-write. Mutate the yielded dict; it is saved on clean exit."""
        with file_lock(self._paths.lock):
            payload = self._load()
            yield payload
            self.checkpoint(payload)
