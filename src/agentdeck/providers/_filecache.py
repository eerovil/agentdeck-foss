"""A path-keyed memoizer invalidated by a file's stat signature.

Both providers memoize cheap transcript reads (metadata, last event, context
tokens, delegation markers) so an idle transcript isn't re-parsed on every scan.
The stat→signature→lookup→recompute→store ceremony was identical across five
sites; this owns it once. A ``stat()`` failure yields the caller's default and
is not cached, so a transient error self-heals on the next read.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import pickle
import shutil
import stat
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable
from pathlib import Path


def mtime_sig(path: Path) -> Hashable:
    """Freshness by modification time — for reads that only care that the file
    changed since the last parse (metadata, context tokens)."""
    return path.stat().st_mtime


def size_mtime_sig(path: Path) -> Hashable:
    """Freshness by (size, mtime_ns) — finer-grained, for tail reads that must
    catch a same-second rewrite (last event, delegation markers)."""
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


class FileCache[T]:
    """One path-keyed memo, invalidated when ``signature(path)`` changes."""

    def __init__(
        self,
        signature: Callable[[Path], Hashable],
        *,
        max_entries: int | None = None,
    ) -> None:
        self._signature = signature
        self._max_entries = max_entries
        self._store: OrderedDict[str, tuple[Hashable, T]] = OrderedDict()
        # Scan preparation runs in a worker thread while liveness and detail
        # reads may touch the same provider cache on the event loop. Protect
        # only the small OrderedDict mutations; parsing remains outside the
        # lock so a worker-thread miss cannot make HTTP wait for its parse.
        self._lock = threading.Lock()

    def get(self, path: Path, compute: Callable[[Path], T], default: T) -> T:
        """Cached ``compute(path)``, recomputed when the file's signature moves.

        Returns ``default`` without caching when the file cannot be ``stat``-ed."""
        try:
            signature = self._signature(path)
        except OSError:
            return default
        key = str(path)
        with self._lock:
            hit = self._store.get(key)
            if hit is not None and hit[0] == signature:
                self._store.move_to_end(key)
                return hit[1]
        value = compute(path)
        with self._lock:
            self._store[key] = (signature, value)
            self._store.move_to_end(key)
            if self._max_entries is not None:
                while len(self._store) > self._max_entries:
                    self._store.popitem(last=False)
        return value


def _shared_cache_dir() -> Path | None:
    """Return a private runtime cache root, or disable disk sharing safely.

    Pickle is appropriate only inside the owning user's private runtime tree.
    A predictable fallback under shared ``/tmp`` would let another local user
    precreate a record that this process then executes while unpickling.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        return None
    runtime_root = Path(runtime_dir)
    try:
        root_stat = runtime_root.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_ISLNK(root_stat.st_mode)
        or root_stat.st_uid != os.geteuid()
        or root_stat.st_mode & 0o022
    ):
        return None
    cache_root = runtime_root / "agentdeck-filecache"
    try:
        cache_root.mkdir(mode=0o700, exist_ok=True)
        cache_stat = cache_root.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISDIR(cache_stat.st_mode)
        or stat.S_ISLNK(cache_stat.st_mode)
        or cache_stat.st_uid != os.geteuid()
        or cache_stat.st_mode & 0o077
    ):
        return None
    return cache_root


class SharedFileCache[T]:
    """A file-signature memo shared by live and canary web processes.

    The in-memory entry is the normal hot path. On a local miss, a per-path
    lock lets one process parse while the other reuses its atomically published
    result. ``namespace`` versions the pickled value independently for each
    parser, so a canary on newer code never consumes an incompatible record.
    """

    _FORMAT_VERSION = 1
    _MAX_ENTRIES = 1024
    _MAX_BYTES = 256 * 1024 * 1024
    # Live, canary, and a replacement worker can legitimately use different
    # parser generations at once. Seven shared parser namespaces per generation
    # need room to coexist until the handoff finishes. The root byte cap allows
    # the same three generations at their observed sizes while remaining the
    # hard bound across all generations.
    _MAX_VERSION_DIRS = 32
    _MAX_ROOT_BYTES = 1024 * 1024 * 1024
    _PRUNE_EVERY_WRITES = 64
    _VERSION_SWEEP_EVERY_PRUNES = 16

    def __init__(
        self,
        signature: Callable[[Path], Hashable],
        namespace: str,
        *,
        cache_dir: Path | None = None,
        version_paths: Iterable[Path] = (),
        local_entries: int = 512,
        local_bytes: int = 64 * 1024 * 1024,
        max_entries: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        # A cache whose scan walks every historical transcript needs a record
        # count in the thousands, while one holding whole rendered transcripts
        # needs a byte budget and few entries. Sharing one class constant made
        # the cheap-record caches evict themselves on the walk that fills them,
        # which is the whole cost this tier exists to remove (#1335).
        self._max_entries = self._MAX_ENTRIES if max_entries is None else max_entries
        self._max_bytes = self._MAX_BYTES if max_bytes is None else max_bytes
        self._signature = signature
        root = cache_dir if cache_dir is not None else _shared_cache_dir()
        version = hashlib.sha256(namespace.encode())
        for path in version_paths:
            try:
                version.update(path.read_bytes())
            except OSError:
                version.update(str(path).encode())
        namespace_dir = version.hexdigest()[:16]
        self._cache_dir = root / namespace_dir if root is not None else None
        self._local_entries = local_entries
        self._local_byte_limit = local_bytes
        self._local_bytes = 0
        self._store: OrderedDict[str, tuple[Hashable, T, int]] = OrderedDict()
        self._store_lock = threading.Lock()
        self._sizes: dict[str, int] = {}
        self._writes_since_prune = 0
        self._pruned_once = False
        self._prunes_since_version_sweep = 0

    def _record_path(self, path: Path) -> Path:
        assert self._cache_dir is not None
        key = hashlib.sha256(str(path.absolute()).encode()).hexdigest()
        return self._cache_dir / f"{key}.pickle"

    def _remember(
        self, key: str, signature: Hashable, value: T, record_bytes: int
    ) -> None:
        with self._store_lock:
            self._sizes[key] = record_bytes
            previous = self._store.pop(key, None)
            if previous is not None:
                self._local_bytes -= previous[2]
            if record_bytes > self._local_byte_limit:
                return
            self._store[key] = (signature, value, record_bytes)
            self._local_bytes += record_bytes
            self._store.move_to_end(key)
            while (
                len(self._store) > self._local_entries
                or self._local_bytes > self._local_byte_limit
            ):
                _old_key, (_old_signature, _old_value, old_bytes) = self._store.popitem(
                    last=False
                )
                self._local_bytes -= old_bytes

    @property
    def byte_limit(self) -> int:
        """The tighter of the in-memory and on-disk byte caps."""
        return min(self._local_byte_limit, self._max_bytes)

    def record_bytes(self, paths: Iterable[Path]) -> int:
        """Last known record size of ``paths``, resident or evicted.

        For sizing checks: an evicted entry is exactly the one a cap that is
        too small would hide, so sizes outlive the entries they describe.
        """
        with self._store_lock:
            return sum(self._sizes.get(str(path), 0) for path in paths)

    def _read_record(
        self, record_path: Path, signature: Hashable
    ) -> tuple[T, int] | None:
        try:
            with record_path.open("rb") as cache_file:
                version, cached_signature, value = pickle.load(cache_file)
            record_bytes = record_path.stat().st_size
        except (OSError, EOFError, pickle.PickleError, AttributeError, ImportError):
            return None
        if version != self._FORMAT_VERSION or cached_signature != signature:
            return None
        return (value, record_bytes)

    def _write_record(
        self, record_path: Path, signature: Hashable, value: T
    ) -> int:
        assert self._cache_dir is not None
        fd, temporary = tempfile.mkstemp(
            prefix=f".{record_path.stem}-", dir=self._cache_dir
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as cache_file:
                pickle.dump(
                    (self._FORMAT_VERSION, signature, value),
                    cache_file,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
                cache_file.flush()
            os.replace(temporary_path, record_path)
            record_bytes = record_path.stat().st_size
            self._writes_since_prune += 1
            # The first write of a process prunes, so a restart always collects
            # what the previous one left; after that it is every Nth write. The
            # old spelling reset the counter to zero and then re-fired its own
            # `== 1` first-write branch, so every single write paid a full
            # directory walk — invisible at 1024 records, crippling at 3482.
            if (
                not self._pruned_once
                or self._writes_since_prune >= self._PRUNE_EVERY_WRITES
            ):
                self._prune()
                self._pruned_once = True
                self._writes_since_prune = 0
            return record_bytes
        finally:
            temporary_path.unlink(missing_ok=True)

    def _prune(self) -> None:
        """Bound each parser namespace without adding work to cache hits."""
        assert self._cache_dir is not None
        records: list[tuple[int, int, Path]] = []
        for path in self._cache_dir.glob("*.pickle"):
            try:
                stat = path.stat()
            except OSError:
                continue
            records.append((stat.st_mtime_ns, stat.st_size, path))
        records.sort(reverse=True)
        total = 0
        for index, (_mtime_ns, size, path) in enumerate(records):
            total += size
            if index >= self._max_entries or total > self._max_bytes:
                try:
                    path.unlink()
                except OSError:
                    pass
        # The cross-namespace sweep stats every record in every version
        # directory, so it is far more expensive than pruning this one. It only
        # collects directories left by superseded parser code, which nothing
        # produces mid-run, so it runs once per process and rarely after.
        if self._prunes_since_version_sweep % self._VERSION_SWEEP_EVERY_PRUNES == 0:
            self._prune_versions()
        self._prunes_since_version_sweep += 1

    def _prune_versions(self) -> None:
        """Bound parser-version directories across long-running deployments."""
        versions: list[tuple[int, int, Path]] = []
        for directory in self._cache_dir.parent.iterdir():
            if not directory.is_dir():
                continue
            try:
                mtime_ns = directory.stat().st_mtime_ns
                size = sum(
                    path.stat().st_size
                    for path in directory.glob("*.pickle")
                    if path.is_file()
                )
            except OSError:
                continue
            versions.append((mtime_ns, size, directory))
        versions.sort(reverse=True)
        total = 0
        for index, (_mtime_ns, size, directory) in enumerate(versions):
            total += size
            if directory == self._cache_dir:
                continue
            if index >= self._MAX_VERSION_DIRS or total > self._MAX_ROOT_BYTES:
                shutil.rmtree(directory, ignore_errors=True)

    def get(self, path: Path, compute: Callable[[Path], T], default: T) -> T:
        """Return one coherent parsed value for the file's current signature."""
        try:
            signature = self._signature(path)
        except OSError:
            return default
        key = str(path)
        with self._store_lock:
            hit = self._store.get(key)
            if hit is not None and hit[0] == signature:
                self._store.move_to_end(key)
                return hit[1]

        if self._cache_dir is None:
            value = compute(path)
            try:
                record_bytes = len(
                    pickle.dumps(
                        (self._FORMAT_VERSION, signature, value),
                        protocol=pickle.HIGHEST_PROTOCOL,
                    )
                )
            except (pickle.PickleError, TypeError, AttributeError):
                return value
            self._remember(key, signature, value, record_bytes)
            return value

        try:
            self._cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            record_path = self._record_path(path)
            # A fixed set of striped locks bounds directory growth while still
            # letting unrelated files parse concurrently across processes.
            lock_path = self._cache_dir / f".lock-{record_path.stem[:2]}"
            with lock_path.open("a+b") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                shared = self._read_record(record_path, signature)
                if shared is not None:
                    value, record_bytes = shared
                    self._remember(key, signature, value, record_bytes)
                    return value

                value = compute(path)
                try:
                    final_signature = self._signature(path)
                except OSError:
                    return value
                if final_signature == signature:
                    record_bytes = self._write_record(record_path, signature, value)
                    self._remember(key, signature, value, record_bytes)
                return value
        except OSError:
            # A cache-directory or lock failure must not make transcripts
            # unreadable. The ordinary parse remains the safe fallback.
            return compute(path)

    def refresh(self, path: Path, compute: Callable[[Path], T], default: T) -> T:
        """Recompute and publish a value even when its coarse signature is unchanged."""
        try:
            signature = self._signature(path)
        except OSError:
            return default
        value = compute(path)
        try:
            if self._signature(path) != signature:
                return value
        except OSError:
            return value
        key = str(path)
        if self._cache_dir is None:
            try:
                record_bytes = len(
                    pickle.dumps(
                        (self._FORMAT_VERSION, signature, value),
                        protocol=pickle.HIGHEST_PROTOCOL,
                    )
                )
            except (pickle.PickleError, TypeError, AttributeError):
                return value
            self._remember(key, signature, value, record_bytes)
            return value
        try:
            self._cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            record_path = self._record_path(path)
            lock_path = self._cache_dir / f".lock-{record_path.stem[:2]}"
            with lock_path.open("a+b") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                record_bytes = self._write_record(record_path, signature, value)
                self._remember(key, signature, value, record_bytes)
        except OSError:
            pass
        return value
