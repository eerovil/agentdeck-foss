from __future__ import annotations

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from agentdeck.github_cache import GitHubMetadata, GitHubMetadataCache


async def test_open_value_is_shared_for_five_minutes_and_force_bypasses_ttl(tmp_path):
    path = tmp_path / "github.sqlite3"
    first = GitHubMetadataCache(path)
    second = GitHubMetadataCache(path)
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return GitHubMetadata({"state": "open", "title": f"value {calls}"})

    initial = await first.resolve("issue:v1:o/r#1", fetch, now=1000.0)
    shared = await second.resolve("issue:v1:o/r#1", fetch, now=1299.0)
    forced = await second.resolve("issue:v1:o/r#1", fetch, now=1300.0, force=True)

    assert initial.value == {"state": "open", "title": "value 1"}
    assert shared.value == initial.value
    assert shared.refreshed is False
    assert forced.value == {"state": "open", "title": "value 2"}
    assert calls == 2


async def test_manual_refresh_boundary_deduplicates_same_key(tmp_path):
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return GitHubMetadata({"title": f"value {calls}"})

    first = await cache.resolve(
        "pr-ref:v1:o/r:1",
        fetch,
        force=True,
        fresh_after=2000.0,
        now=2001.0,
    )
    reused = await cache.resolve(
        "pr-ref:v1:o/r:1",
        fetch,
        force=True,
        fresh_after=2000.0,
        now=2002.0,
    )

    assert first.value == reused.value
    assert calls == 1


async def test_cross_instance_lease_prevents_duplicate_refresh(tmp_path):
    path = tmp_path / "github.sqlite3"
    first = GitHubMetadataCache(path)
    second = GitHubMetadataCache(path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_fetch():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return GitHubMetadata({"title": "one fetch"})

    first_task = asyncio.create_task(
        first.resolve("pr-ref:v1:o/r:2", slow_fetch, now=2000.0)
    )
    await started.wait()
    competing = await second.resolve("pr-ref:v1:o/r:2", slow_fetch, now=2000.0)
    release.set()
    resolved = await first_task

    assert calls == 1
    assert competing.value is None
    assert competing.complete is False
    assert resolved.value == {"title": "one fetch"}


async def test_expired_lease_owner_cannot_overwrite_newer_result(tmp_path):
    path = tmp_path / "github.sqlite3"
    slow = GitHubMetadataCache(path)
    takeover = GitHubMetadataCache(path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_fetch():
        started.set()
        await release.wait()
        return GitHubMetadata({"title": "late"})

    async def current_fetch():
        return GitHubMetadata({"title": "current"})

    slow_task = asyncio.create_task(
        slow.resolve("pr-ref:v1:o/r:5", slow_fetch, now=6000.0)
    )
    await started.wait()
    current = await takeover.resolve(
        "pr-ref:v1:o/r:5", current_fetch, now=6000.0 + slow.LEASE_S + 1
    )
    release.set()
    late = await slow_task

    assert current.value == {"title": "current"}
    assert late.value == current.value
    assert slow.peek("pr-ref:v1:o/r:5").value == current.value


async def test_a_fetch_that_outlived_its_lease_still_returns_its_answer(tmp_path):
    """Its lease was taken over by a fetch still running: nothing newer is stored (#3344).

    The row holds an older answer, which must not replace the one just fetched (#3347 review).
    """
    path = tmp_path / "github.sqlite3"
    slow = GitHubMetadataCache(path)
    takeover = GitHubMetadataCache(path)

    async def old_fetch():
        return GitHubMetadata({"title": "old"})

    await slow.resolve("pr-ref:v1:o/r:6", old_fetch, now=7000.0 - slow.OPEN_TTL_S - 1)
    started = asyncio.Event()
    release = asyncio.Event()
    taken = asyncio.Event()

    async def slow_fetch():
        started.set()
        await release.wait()
        return GitHubMetadata({"title": "late"})

    async def stuck_fetch():
        taken.set()
        await asyncio.Event().wait()

    slow_task = asyncio.create_task(slow.resolve("pr-ref:v1:o/r:6", slow_fetch, now=7000.0))
    await started.wait()
    stuck = asyncio.create_task(
        takeover.resolve("pr-ref:v1:o/r:6", stuck_fetch, now=7000.0 + slow.LEASE_S + 1)
    )
    await taken.wait()
    release.set()
    late = await slow_task
    stuck.cancel()

    assert late.value == {"title": "late"}


async def test_a_key_says_whether_it_is_leased_or_backing_off(tmp_path):
    path = tmp_path / "github.sqlite3"
    cache = GitHubMetadataCache(path)
    other = GitHubMetadataCache(path)
    calls = 0

    async def fails():
        nonlocal calls
        calls += 1
        return None

    async def succeeds():
        nonlocal calls
        calls += 1
        return GitHubMetadata({"title": "fetched"})

    await cache.resolve("issue:v1:o/r#7", fails, now=time.time())
    backed_off = await other.resolve("issue:v1:o/r#7", succeeds)
    assert (backed_off.value, backed_off.reason, calls) == (None, "backoff", 1)
    assert backed_off.failed_s is not None and backed_off.failed_s < 5

    through = await other.resolve("issue:v1:o/r#7", succeeds, ignore_backoff=True)
    assert (through.value, calls) == ({"title": "fetched"}, 2)

    token, _ = cache._claim("issue:v1:o/r#8", force=False, fresh_after=None, now=time.time())
    leased = await other.resolve("issue:v1:o/r#8", succeeds, ignore_backoff=True)
    assert (leased.value, leased.reason, calls) == (None, "leased", 2)


def test_waiting_on_a_lease_serves_the_other_fetch_or_gives_up(tmp_path):
    path = tmp_path / "github.sqlite3"
    cache = GitHubMetadataCache(path)
    other = GitHubMetadataCache(path)
    now = time.time()
    token, _ = cache._claim("issue:v1:o/r#9", force=False, fresh_after=None, now=now)
    ticks = iter(range(100))

    def store_then_sleep(_seconds):
        if next(ticks) == 2:
            cache._store("issue:v1:o/r#9", GitHubMetadata({"title": "theirs"}), now, token)

    served = other.wait_for("issue:v1:o/r#9", 15.0, sleep=store_then_sleep, clock=lambda: 0.0)
    assert (served.value, served.reason) == ({"title": "theirs"}, "")

    cache._claim("issue:v1:o/r#10", force=False, fresh_after=None, now=time.time())
    clock = iter([0.0, 1.0, 20.0])
    gave_up = other.wait_for(
        "issue:v1:o/r#10", 15.0, sleep=lambda _s: None, clock=lambda: next(clock)
    )
    assert (gave_up.value, gave_up.reason) == (None, "leased")


async def test_transient_failure_preserves_trusted_stale_value(tmp_path):
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")

    async def succeeds():
        return GitHubMetadata({"state": "open", "title": "trusted"})

    async def fails():
        return None

    await cache.resolve("issue:v1:o/r#3", succeeds, now=3000.0)
    stale = await cache.resolve("issue:v1:o/r#3", fails, now=3301.0)
    backed_off = await cache.resolve("issue:v1:o/r#3", fails, now=3302.0)

    assert stale.value == {"state": "open", "title": "trusted"}
    assert stale.complete is False
    assert backed_off.value == stale.value
    assert backed_off.complete is False


async def test_terminal_value_uses_long_ttl(tmp_path):
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return GitHubMetadata({"state": "closed"}, terminal=True)

    await cache.resolve("issue:v1:o/r#4", fetch, now=4000.0)
    result = await cache.resolve("issue:v1:o/r#4", fetch, now=4500.0)

    assert result.value == {"state": "closed"}
    assert calls == 1


async def test_terminal_value_is_not_refetched_on_an_hourly_timer(tmp_path):
    """A merged PR is finished, so elapsed hours must not re-read it.

    The deck keeps every pull request and issue it has ever seen, so an hourly
    terminal TTL turns accumulated history into a permanent bill against a
    shared 5000/hour budget (#1394).
    """
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return GitHubMetadata({"state": "closed"}, terminal=True)

    await cache.resolve("pr-ref:v1:o/r:9", fetch, now=1000.0)
    hours_later = 1000.0 + 6 * 3600.0
    result = await cache.resolve("pr-ref:v1:o/r:9", fetch, now=hours_later)

    assert calls == 1
    assert result.value == {"state": "closed"}
    assert result.complete is True
    assert cache.is_fresh("pr-ref:v1:o/r:9", now=hours_later) is True


async def test_a_closed_issue_reopened_outside_the_host_is_seen_after_the_ttl(tmp_path):
    """An external reopen has no local write to invalidate the key (#1395).

    The shim only invalidates state changes this host caused, so a permanently
    fresh terminal value would leave a stale ``closed`` authoritative for ever.
    Here the backing GitHub state flips to open without anything touching
    ``invalidate``, and the bounded terminal TTL is what eventually notices.
    """
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")
    github_state = {"state": "closed"}
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return GitHubMetadata(
            dict(github_state), terminal=github_state["state"] == "closed"
        )

    closed_at = 1000.0
    first = await cache.resolve("issue:v1:o/r#12", fetch, now=closed_at)
    assert first.value == {"state": "closed"}

    # Reopened straight in GitHub: no shim write, so no invalidation.
    github_state["state"] = "open"

    inside_ttl = closed_at + cache.TERMINAL_TTL_S - 1
    held = await cache.resolve("issue:v1:o/r#12", fetch, now=inside_ttl)
    assert held.value == {"state": "closed"}
    assert calls == 1

    past_ttl = closed_at + cache.TERMINAL_TTL_S + 1
    assert cache.is_fresh("issue:v1:o/r#12", now=past_ttl) is False
    observed = await cache.resolve("issue:v1:o/r#12", fetch, now=past_ttl)

    assert calls == 2
    assert observed.value == {"state": "open"}
    assert observed.refreshed is True


async def test_open_value_still_expires_on_its_short_ttl(tmp_path):
    """The day-long terminal TTL must not leak into things that still move."""
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return GitHubMetadata({"state": "open"}, terminal=False)

    await cache.resolve("pr-ref:v1:o/r:10", fetch, now=1000.0)
    await cache.resolve("pr-ref:v1:o/r:10", fetch, now=1000.0 + cache.OPEN_TTL_S + 1)

    assert calls == 2


async def test_a_forced_refresh_still_re_reads_a_terminal_value(tmp_path):
    """A day of freshness is not a day of waiting: a manual refresh reads live.

    A reopen the TTL has not reached yet is exactly the case the operator's
    explicit refresh has to remain a way through.
    """
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return GitHubMetadata({"state": "closed"}, terminal=True)

    await cache.resolve("issue:v1:o/r#11", fetch, now=1000.0)
    await cache.resolve("issue:v1:o/r#11", fetch, now=1100.0, force=True)

    assert calls == 2


async def test_batch_bounds_actual_refreshes_but_reuses_fresh_entries(tmp_path):
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")
    calls = []

    def request(key):
        async def fetch():
            calls.append(key)
            return GitHubMetadata({"key": key})

        return (key, fetch)

    requests = [request(f"issue:v1:o/r#{number}") for number in range(4)]
    first = await cache.resolve_many(requests, now=5000.0, max_refreshes=2)
    second = await cache.resolve_many(requests, now=5001.0, max_refreshes=2)

    assert len([result for result in first.values() if result.refreshed]) == 2
    assert len([result for result in second.values() if result.refreshed]) == 2
    assert calls == [
        "issue:v1:o/r#0",
        "issue:v1:o/r#1",
        "issue:v1:o/r#2",
        "issue:v1:o/r#3",
    ]


async def test_reads_reuse_one_connection_per_thread(tmp_path, monkeypatch):
    """A session card asks for its issue badge on every render (#1088).

    Each of those reads used to open a fresh SQLite connection on the event
    loop; a single scan did several hundred. Reuse is per thread because a
    connection may not cross threads, and the cross-process lease in the file is
    untouched.
    """
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")

    async def fetch():
        return GitHubMetadata({"state": "open", "title": "t"})

    await cache.resolve("issue:v1:o/r#1", fetch, now=1000.0)

    opens = 0
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        nonlocal opens
        opens += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    for _ in range(20):
        assert cache.peek("issue:v1:o/r#1").value == {"state": "open", "title": "t"}

    assert opens == 0

    def in_another_thread() -> int:
        cache.peek("issue:v1:o/r#1")
        return opens

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(in_another_thread).result() == 1


async def test_a_sqlite_failure_throws_the_reused_handle_away(tmp_path):
    """A failed statement can leave the handle mid-transaction.

    Before reuse each caller opened its own connection, so an error could not
    outlive the call. Dropping the handle keeps that true.
    """
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")

    async def fetch():
        return GitHubMetadata({"title": "t"})

    await cache.resolve("issue:v1:o/r#1", fetch, now=1000.0)
    poisoned = cache._connect()
    poisoned.close()  # every later statement on this handle now raises

    assert cache.peek("issue:v1:o/r#1").value is None  # the failed read
    assert cache._connect() is not poisoned
    assert cache.peek("issue:v1:o/r#1").value == {"title": "t"}  # recovered


async def test_a_lookup_carries_how_old_its_value_is(tmp_path):
    """`complete` says "inside the TTL". A caller deciding whether an *absence*
    from the value is evidence needs to know how far outside it fell, because
    minutes past and days past are not the same claim (#1115)."""
    path = tmp_path / "github.sqlite3"
    cache = GitHubMetadataCache(path)

    async def fetch():
        return GitHubMetadata([{"number": 1}])

    refreshed = await cache.resolve("issue-list:v1:o/r", fetch)
    assert refreshed.age_s == 0.0

    peeked = cache.peek("issue-list:v1:o/r")
    assert peeked.complete is True
    assert peeked.age_s is not None and peeked.age_s < 60.0

    # Backdate the row: the value survives, and its age says why it is no longer
    # the answer to "what is open here".
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE github_metadata_v1 SET fetched_at = fetched_at - ?",
            (5 * 24 * 3600,),
        )
    rotten = GitHubMetadataCache(path).peek("issue-list:v1:o/r")
    assert rotten.value == [{"number": 1}]
    assert rotten.complete is False
    assert rotten.age_s is not None and rotten.age_s > 4 * 24 * 3600


async def test_a_missing_key_has_no_age(tmp_path):
    """No value, so no age — never a zero that reads as "just now"."""
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")
    assert cache.peek("issue-list:v1:o/missing").age_s is None
