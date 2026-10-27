"""Unit tests for the shared FileCache memoizer."""

import json
import os
import threading
from collections import OrderedDict

from agentdeck.providers._filecache import (
    FileCache,
    SharedFileCache,
    mtime_sig,
    size_mtime_sig,
)


class _ReportingLock:
    """The cache's lock, setting ``blocked`` when a thread other than ``owner`` has to wait.

    Lets a hit that is holding the lock wait exactly until the eviction is stuck behind
    it, instead of sleeping out a fixed timeout to give the eviction its chance."""

    def __init__(self, lock, blocked: threading.Event) -> None:
        self._lock = lock
        self._blocked = blocked
        self.owner: threading.Thread | None = None

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            if threading.current_thread() is not self.owner:
                self._blocked.set()
            self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()


def test_miss_computes_once_then_hits_without_recomputing(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text("a\n")
    calls = []

    def compute(p):
        calls.append(p)
        return p.read_text()

    cache: FileCache[str] = FileCache(size_mtime_sig)
    assert cache.get(path, compute, "default") == "a\n"
    assert cache.get(path, compute, "default") == "a\n"  # second call is a hit
    assert len(calls) == 1  # compute ran exactly once


def test_signature_change_recomputes(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text("one\n")
    cache: FileCache[str] = FileCache(size_mtime_sig)
    assert cache.get(path, lambda p: p.read_text(), "") == "one\n"
    # A different size (and mtime_ns) invalidates the entry.
    path.write_text("two two\n")
    assert cache.get(path, lambda p: p.read_text(), "") == "two two\n"


def test_missing_file_returns_default_and_is_not_cached(tmp_path):
    path = tmp_path / "nope.jsonl"
    calls = []

    def compute(p):
        calls.append(p)
        return "computed"

    cache: FileCache[str] = FileCache(mtime_sig)
    assert cache.get(path, compute, "fallback") == "fallback"  # stat fails
    assert calls == []  # compute never ran
    # Once the file appears, the next read recomputes (nothing was cached).
    path.write_text("x\n")
    assert cache.get(path, compute, "fallback") == "computed"
    assert len(calls) == 1


def test_signature_functions(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text(json.dumps({"a": 1}))
    assert isinstance(mtime_sig(path), float)
    sig = size_mtime_sig(path)
    assert isinstance(sig, tuple) and len(sig) == 2


def test_file_cache_can_bound_long_lived_path_inventories(tmp_path):
    cache: FileCache[str] = FileCache(size_mtime_sig, max_entries=2)
    for index in range(3):
        path = tmp_path / f"{index}.jsonl"
        path.write_text(str(index))
        assert cache.get(path, lambda item: item.read_text(), "") == str(index)
    assert len(cache._store) == 2


def test_file_cache_hit_and_eviction_are_thread_safe(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("first")
    second.write_text("second")
    cache: FileCache[str] = FileCache(size_mtime_sig, max_entries=1)
    cache.get(first, lambda item: item.read_text(), "")
    first_read = threading.Event()
    # Set once the eviction has finished, or is waiting on the lock the hit holds.
    second_finished = threading.Event()
    lock = cache._lock = _ReportingLock(cache._lock, second_finished)

    class BlockingHitStore(OrderedDict):
        def get(self, key, default=None):
            value = super().get(key, default)
            if key == str(first):
                first_read.set()
                second_finished.wait(timeout=0.5)
            return value

    cache._store = BlockingHitStore(cache._store)
    errors = []

    def read(path):
        try:
            cache.get(path, lambda item: item.read_text(), "")
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            if path == second:
                second_finished.set()

    hit_thread = lock.owner = threading.Thread(target=read, args=(first,))
    hit_thread.start()
    assert first_read.wait(timeout=1)
    eviction_thread = threading.Thread(target=read, args=(second,))
    eviction_thread.start()
    hit_thread.join(timeout=2)
    eviction_thread.join(timeout=2)

    assert not hit_thread.is_alive()
    assert not eviction_thread.is_alive()
    assert errors == []


def test_shared_cache_reuses_one_process_parse_in_another(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text("one\n")
    cache_dir = tmp_path / "cache"
    calls = []

    def compute(item):
        calls.append(item)
        return item.read_text()

    first: SharedFileCache[str] = SharedFileCache(
        size_mtime_sig, "test-v1", cache_dir=cache_dir
    )
    second: SharedFileCache[str] = SharedFileCache(
        size_mtime_sig, "test-v1", cache_dir=cache_dir
    )
    assert first.get(path, compute, "") == "one\n"
    assert second.get(path, compute, "") == "one\n"
    assert calls == [path]


def test_shared_cache_recomputes_after_signature_change(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text("one\n")
    cache_dir = tmp_path / "cache"
    first: SharedFileCache[str] = SharedFileCache(
        size_mtime_sig, "test-v1", cache_dir=cache_dir
    )
    assert first.get(path, lambda item: item.read_text(), "") == "one\n"

    path.write_text("two two\n")
    second: SharedFileCache[str] = SharedFileCache(
        size_mtime_sig, "test-v1", cache_dir=cache_dir
    )
    assert second.get(path, lambda item: item.read_text(), "") == "two two\n"


def test_shared_cache_refresh_replaces_a_value_with_the_same_signature(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text("one\n")
    cache_dir = tmp_path / "cache"
    first: SharedFileCache[str] = SharedFileCache(
        mtime_sig, "test-refresh", cache_dir=cache_dir
    )
    assert first.get(path, lambda item: item.read_text(), "") == "one\n"
    before = path.stat()
    path.write_text("two\n")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert first.refresh(path, lambda item: item.read_text(), "") == "two\n"
    second: SharedFileCache[str] = SharedFileCache(
        mtime_sig, "test-refresh", cache_dir=cache_dir
    )
    assert second.get(path, lambda item: item.read_text(), "") == "two\n"


def test_shared_cache_isolates_parser_source_versions(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text("value\n")
    version = tmp_path / "parser.py"
    version.write_text("VERSION = 1\n")
    cache_dir = tmp_path / "cache"
    calls = []

    def compute(item):
        calls.append(item)
        return item.read_text()

    first: SharedFileCache[str] = SharedFileCache(
        size_mtime_sig,
        "test",
        cache_dir=cache_dir,
        version_paths=(version,),
    )
    assert first.get(path, compute, "") == "value\n"

    version.write_text("VERSION = 2\n")
    second: SharedFileCache[str] = SharedFileCache(
        size_mtime_sig,
        "test",
        cache_dir=cache_dir,
        version_paths=(version,),
    )
    assert second.get(path, compute, "") == "value\n"
    assert calls == [path, path]


def test_shared_cache_bounds_local_values_and_lock_stripes(tmp_path):
    cache = SharedFileCache(
        size_mtime_sig,
        "test",
        cache_dir=tmp_path / "cache",
        local_entries=2,
    )
    for index in range(3):
        path = tmp_path / f"{index}.jsonl"
        path.write_text(str(index))
        assert cache.get(path, lambda item: item.read_text(), "") == str(index)

    assert len(cache._store) == 2
    assert len(list(cache._cache_dir.glob(".lock-*"))) <= 3


def test_shared_cache_hit_and_eviction_are_thread_safe(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("first")
    second.write_text("second")
    cache = SharedFileCache(
        size_mtime_sig,
        "thread-safe",
        cache_dir=tmp_path / "cache",
        local_entries=1,
    )
    cache.get(first, lambda item: item.read_text(), "")
    first_read = threading.Event()
    # Set once the eviction has finished, or is waiting on the lock the hit holds.
    second_finished = threading.Event()
    lock = cache._store_lock = _ReportingLock(cache._store_lock, second_finished)

    class BlockingHitStore(OrderedDict):
        def get(self, key, default=None):
            value = super().get(key, default)
            if key == str(first):
                first_read.set()
                second_finished.wait(timeout=0.5)
            return value

    cache._store = BlockingHitStore(cache._store)
    errors = []

    def read(path):
        try:
            cache.get(path, lambda item: item.read_text(), "")
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            if path == second:
                second_finished.set()

    hit_thread = lock.owner = threading.Thread(target=read, args=(first,))
    hit_thread.start()
    assert first_read.wait(timeout=1)
    eviction_thread = threading.Thread(target=read, args=(second,))
    eviction_thread.start()
    hit_thread.join(timeout=2)
    eviction_thread.join(timeout=2)

    assert not hit_thread.is_alive()
    assert not eviction_thread.is_alive()
    assert errors == []


def test_shared_cache_bounds_local_values_by_serialized_bytes(tmp_path):
    path = tmp_path / "large.jsonl"
    path.write_text("source")
    cache = SharedFileCache(
        size_mtime_sig,
        "test",
        cache_dir=tmp_path / "cache",
        local_bytes=32,
    )

    assert cache.get(path, lambda _item: "x" * 1024, "") == "x" * 1024
    assert cache._store == {}
    assert cache._local_bytes == 0


def test_shared_cache_disables_pickle_disk_cache_without_private_runtime(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = tmp_path / "source.jsonl"
    path.write_text("value")
    cache = SharedFileCache(size_mtime_sig, "test")

    assert cache._cache_dir is None
    assert cache.get(path, lambda item: item.read_text(), "") == "value"


def test_shared_cache_rejects_symlinked_runtime_root(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(linked))

    cache = SharedFileCache(size_mtime_sig, "test")

    assert cache._cache_dir is None


def test_shared_cache_prunes_old_parser_versions(tmp_path):
    root = tmp_path / "cache"
    for index in range(3):
        old = root / f"old-{index}"
        old.mkdir(parents=True)
        (old / "entry.pickle").write_bytes(b"old")

    path = tmp_path / "current.jsonl"
    path.write_text("current")
    cache = SharedFileCache(size_mtime_sig, "current", cache_dir=root)
    cache._MAX_VERSION_DIRS = 2
    assert cache.get(path, lambda item: item.read_text(), "") == "current"
    assert len([directory for directory in root.iterdir() if directory.is_dir()]) <= 2


def test_shared_cache_keeps_three_active_parser_generations(tmp_path):
    """Live, canary, and replacement parser namespaces must coexist (#1505)."""
    root = tmp_path / "cache"
    namespace_count = 7 * 3
    for index in range(namespace_count - 1):
        old = root / f"active-{index}"
        old.mkdir(parents=True)
        # Sparse files exercise the aggregate byte gate without consuming the
        # equivalent disk blocks. Together these represent a 600 MiB cache,
        # which the former 512 MiB cap pruned during an active handoff.
        with (old / "entry.pickle").open("wb") as entry:
            entry.truncate(30 * 1024 * 1024)

    path = tmp_path / "current.jsonl"
    path.write_text("current")
    cache = SharedFileCache(size_mtime_sig, "current", cache_dir=root)
    assert cache.get(path, lambda item: item.read_text(), "") == "current"

    assert len([directory for directory in root.iterdir() if directory.is_dir()]) == (
        namespace_count
    )


def test_shared_cache_prunes_periodically_not_on_every_write(tmp_path):
    """A full directory walk per write is what made a large cache unusable.

    The old spelling reset ``_writes_since_prune`` to zero after pruning, so the
    very next write re-entered its own "first write" branch and pruned again.
    At 1024 records that is invisible; at an account's whole rollout history it
    is thousands of directory walks per scan (#1335).
    """
    cache = SharedFileCache(size_mtime_sig, "test", cache_dir=tmp_path / "cache")
    prunes = 0
    real_prune = cache._prune

    def counting_prune():
        nonlocal prunes
        prunes += 1
        real_prune()

    cache._prune = counting_prune
    writes = cache._PRUNE_EVERY_WRITES * 3
    for index in range(writes):
        path = tmp_path / f"{index}.jsonl"
        path.write_text(str(index))
        cache.get(path, lambda item: item.read_text(), "")

    # One prune for the first write of the process, then one per interval.
    assert prunes == 1 + (writes - 1) // cache._PRUNE_EVERY_WRITES
    assert prunes < writes


def test_shared_cache_version_sweep_is_rarer_than_its_own_prune(tmp_path):
    """The cross-namespace sweep stats every record in every version directory,
    so it must not run on every prune of a history-sized cache (#1335)."""
    cache = SharedFileCache(size_mtime_sig, "test", cache_dir=tmp_path / "cache")
    sweeps = 0
    real_sweep = cache._prune_versions

    def counting_sweep():
        nonlocal sweeps
        sweeps += 1
        real_sweep()

    cache._prune_versions = counting_sweep
    # Small intervals keep the run to a few dozen writes; the ratio is what is checked.
    cache._PRUNE_EVERY_WRITES = 4
    cache._VERSION_SWEEP_EVERY_PRUNES = 4
    prunes_wanted = cache._VERSION_SWEEP_EVERY_PRUNES * 2
    for index in range(cache._PRUNE_EVERY_WRITES * prunes_wanted):
        path = tmp_path / f"{index}.jsonl"
        path.write_text(str(index))
        cache.get(path, lambda item: item.read_text(), "")

    # Still swept on the process's first prune, so a restart always collects.
    assert sweeps >= 1
    assert sweeps <= 1 + prunes_wanted // cache._VERSION_SWEEP_EVERY_PRUNES


def test_shared_cache_entry_caps_are_per_instance(tmp_path):
    """One cache holds whole rendered transcripts and another holds a few bytes
    per historical file; a single class-wide cap cannot size both (#1335)."""
    root = tmp_path / "cache"
    cache = SharedFileCache(
        size_mtime_sig,
        "test",
        cache_dir=root,
        local_entries=64,
        max_entries=32,
    )
    assert cache._max_entries == 32
    assert cache._max_bytes == SharedFileCache._MAX_BYTES

    for index in range(48):
        path = tmp_path / f"{index}.jsonl"
        path.write_text(str(index))
        cache.get(path, lambda item: item.read_text(), "")
    cache._prune()

    assert len(list(cache._cache_dir.glob("*.pickle"))) <= 32

    default_cache = SharedFileCache(size_mtime_sig, "other", cache_dir=root)
    assert default_cache._max_entries == SharedFileCache._MAX_ENTRIES
