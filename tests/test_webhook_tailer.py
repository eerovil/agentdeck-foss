"""The shared webhook tailer library (#1637), against real streams.

Every fixture here is written through `WebhookLog` — the same appender the ingress route
uses — so these tests read the record shape the host actually stores rather than a
hand-rolled approximation of it.

What is held: a consumer resumes where it committed, an uncommitted pass replays instead
of losing records, a rotated or truncated stream is re-read and the replay is absorbed by
GUID dedupe, a partially written line waits for the rest, two consumers advance
independently, and the last-delivery-seen accessor reports what was read.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from pathlib import Path

import pytest

from agentdeck import webhook_tailer
from agentdeck.github_webhook_log import Repository, WebhookLog
from agentdeck.webhook_tailer import Delivery, WebhookTailer, WebhookWatcher, committing

AGENTDECK = Repository(owner="eerovil", name="agentdeck")
TRAILHEAD = Repository(owner="eerovil", name="trailhead")


def body_for(repository: Repository, *, number: int = 1, updated_at: str = "2026-09-06T12:00:00Z"):
    return json.dumps(
        {
            "action": "opened",
            "issue": {"number": number, "updated_at": updated_at},
            "repository": {
                "full_name": repository.slug,
                "name": repository.name,
                "owner": {"login": repository.owner},
            },
        }
    ).encode()


@pytest.fixture
def root(tmp_path):
    return tmp_path / "github-webhooks"


@pytest.fixture
def log(root) -> WebhookLog:
    return WebhookLog(root)


def deliver(
    log: WebhookLog,
    repository: Repository,
    *,
    delivery: str,
    event: str = "issues",
    number: int = 1,
    received_at: str | None = None,
) -> None:
    log.append(
        body=body_for(repository, number=number),
        headers={
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": event,
            "X-GitHub-Hook-ID": "675283305",
        },
        received_at=received_at,
    )


def tailer_for(root, name: str = "pill-cache", **kwargs) -> WebhookTailer:
    return WebhookTailer(root, consumer=name, **kwargs)


def ids(deliveries: list[Delivery]) -> list[str]:
    return [delivery.delivery_id for delivery in deliveries]


def test_reads_every_stream_and_decodes_the_original_payload(root, log):
    deliver(log, AGENTDECK, delivery="d-1", number=11)
    deliver(log, TRAILHEAD, delivery="d-2", event="pull_request", number=22)

    batch = tailer_for(root).read()

    assert ids(batch) == ["d-1", "d-2"]
    assert [delivery.stream for delivery in batch] == [AGENTDECK.slug, TRAILHEAD.slug]
    assert [delivery.event for delivery in batch] == ["issues", "pull_request"]
    assert batch[0].payload["issue"]["number"] == 11
    assert batch[1].payload["issue"]["number"] == 22
    assert batch[0].repository == AGENTDECK
    assert batch[0].headers["X-GitHub-Hook-ID"] == "675283305"


def test_streams_lists_live_files_and_ignores_archives(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    deliver(log, TRAILHEAD, delivery="d-2")
    log.rotation_path(AGENTDECK, 1).write_bytes(b"")

    assert tailer_for(root).streams() == [AGENTDECK, TRAILHEAD]


def test_no_streams_at_all_is_not_an_error(root):
    tailer = tailer_for(root)
    assert tailer.streams() == []
    assert tailer.read() == []
    tailer.commit()


def test_a_committed_offset_is_not_re_read(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    tailer = tailer_for(root)
    assert ids(tailer.read()) == ["d-1"]
    tailer.commit()

    assert tailer.read() == []

    deliver(log, AGENTDECK, delivery="d-2")
    assert ids(tailer.read()) == ["d-2"]


def test_offsets_survive_a_restart(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    first = tailer_for(root)
    first.read()
    first.commit()

    deliver(log, AGENTDECK, delivery="d-2")

    # A brand-new instance: this is what a consumer looks like after a deploy.
    assert ids(tailer_for(root).read()) == ["d-2"]


def test_an_uncommitted_pass_replays_rather_than_losing_records(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    crashed = tailer_for(root)
    assert ids(crashed.read()) == ["d-1"]
    # No commit — the consumer died between reading and committing.

    assert ids(tailer_for(root).read()) == ["d-1"]


def test_abandoning_the_generator_part_way_commits_nothing_extra(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    deliver(log, AGENTDECK, delivery="d-2")
    tailer = tailer_for(root)

    for delivery in tailer.deliveries():
        assert delivery.delivery_id == "d-1"
        break

    assert ids(tailer_for(root).read()) == ["d-1", "d-2"]


def test_committing_helper_commits_on_a_clean_exit(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    with committing(tailer_for(root)) as tailer:
        assert ids(tailer.deliveries()) == ["d-1"]

    assert tailer_for(root).read() == []


def test_committing_helper_leaves_the_offset_alone_when_the_block_raises(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    with pytest.raises(RuntimeError):
        with committing(tailer_for(root)) as tailer:
            list(tailer.deliveries())
            raise RuntimeError("handler blew up")

    assert ids(tailer_for(root).read()) == ["d-1"]


def test_the_same_delivery_guid_from_two_hooks_is_yielded_once(root, log):
    # Exactly the shape #1625 measured on this host: one event, two hooks pointed at the
    # same endpoint, one shared X-GitHub-Delivery.
    deliver(log, AGENTDECK, delivery="shared-guid")
    deliver(log, AGENTDECK, delivery="shared-guid")

    assert ids(tailer_for(root).read()) == ["shared-guid"]


def test_a_redelivery_after_a_commit_is_absorbed(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()

    deliver(log, AGENTDECK, delivery="d-1")
    assert tailer_for(root).read() == []


def test_truncation_resets_the_offset_and_dedupe_absorbs_the_replay(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    deliver(log, AGENTDECK, delivery="d-2")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()

    # #1622 truncates the live file in place under the appender's lock, so the inode is
    # unchanged and only the contents move backwards.
    log.path_for(AGENTDECK).write_bytes(b"")
    deliver(log, AGENTDECK, delivery="d-3")

    assert ids(tailer_for(root).read()) == ["d-3"]


def test_a_rotation_that_refilled_past_the_old_offset_is_still_detected(root, log):
    for index in range(6):
        deliver(log, AGENTDECK, delivery=f"d-{index}")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()

    # Emptied and refilled between two polls, so the file is *longer* than the committed
    # offset and a size check alone would miss it. The head fingerprint is what catches it.
    log.path_for(AGENTDECK).write_bytes(b"")
    for index in range(6, 20):
        deliver(log, AGENTDECK, delivery=f"d-{index}")

    replayed = ids(tailer_for(root).read())
    assert replayed == [f"d-{index}" for index in range(6, 20)]


def test_a_replay_wider_than_the_dedupe_window_is_yielded_again(root, log):
    # The window is finite on purpose, and past it the consumer's own idempotence — merge
    # by payload updated_at — is what keeps a replay harmless. Hold that this is what
    # happens rather than pretending the window is unbounded.
    for index in range(4):
        deliver(log, AGENTDECK, delivery=f"d-{index}")
    tailer = tailer_for(root, dedupe_window=2)
    tailer.read()
    tailer.commit()

    log.path_for(AGENTDECK).write_bytes(b"")
    for index in range(4):
        deliver(log, AGENTDECK, delivery=f"d-{index}")

    replayed = ids(tailer_for(root, dedupe_window=2).read())
    assert replayed == ["d-0", "d-1"]


def test_a_half_written_final_line_waits_for_the_rest(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    path = log.path_for(AGENTDECK)
    whole = path.read_bytes()
    deliver(log, AGENTDECK, delivery="d-2")
    partial = path.read_bytes()
    path.write_bytes(whole + partial[len(whole) : -20])

    tailer = tailer_for(root)
    assert ids(tailer.read()) == ["d-1"]
    tailer.commit()

    path.write_bytes(partial)
    assert ids(tailer_for(root).read()) == ["d-2"]


def test_an_unparseable_line_is_skipped_not_raised_on(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    path = log.path_for(AGENTDECK)
    path.write_bytes(path.read_bytes() + b"{not json at all\n")
    deliver(log, AGENTDECK, delivery="d-2")

    assert ids(tailer_for(root).read()) == ["d-1", "d-2"]


def test_an_undecodable_body_is_skipped(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    path = log.path_for(AGENTDECK)
    broken = json.dumps({"delivery_id": "d-bad", "raw_body_base64": "!!!not base64!!!"})
    path.write_bytes(path.read_bytes() + broken.encode() + b"\n")

    assert ids(tailer_for(root).read()) == ["d-1"]


def test_a_body_that_is_not_json_still_yields_the_record(root, log):
    # The appender refuses a body it cannot read a repository out of, so this shape can
    # only reach a stream through a crash or a recovered file. It must not stop the read.
    deliver(log, AGENTDECK, delivery="d-1")
    path = log.path_for(AGENTDECK)
    record = json.loads(path.read_text())
    record["delivery_id"] = "d-2"
    record["raw_body_base64"] = base64.b64encode(b"not json at all").decode()
    path.write_bytes(path.read_bytes() + json.dumps(record).encode() + b"\n")

    batch = tailer_for(root).read()
    assert ids(batch) == ["d-1", "d-2"]
    assert batch[1].payload == {}
    assert batch[1].raw_body == b"not json at all"


def test_two_consumers_advance_independently(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    pills = tailer_for(root, "pill-cache")
    pills.read()
    pills.commit()

    poller = tailer_for(root, "poller-wake")
    assert ids(poller.read()) == ["d-1"]
    poller.commit()

    deliver(log, AGENTDECK, delivery="d-2")
    assert ids(tailer_for(root, "pill-cache").read()) == ["d-2"]
    assert ids(tailer_for(root, "poller-wake").read()) == ["d-2"]


def test_reading_can_be_narrowed_to_named_repositories(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    deliver(log, TRAILHEAD, delivery="d-2")

    # What the poller wake needs: only the repositories one instance cares about.
    tailer = tailer_for(root)
    assert ids(tailer.read([AGENTDECK])) == ["d-1"]
    tailer.commit()
    assert ids(tailer_for(root).read()) == ["d-2"]


def test_last_delivery_seen_reports_what_was_read(root, log):
    tailer = tailer_for(root)
    assert tailer.last_delivery_seen(AGENTDECK) is None

    deliver(log, AGENTDECK, delivery="d-1", received_at="2026-09-06T12:00:00+00:00")
    deliver(log, AGENTDECK, delivery="d-2", received_at="2026-09-06T12:05:00+00:00")
    deliver(log, TRAILHEAD, delivery="d-3", received_at="2026-09-06T11:00:00+00:00")
    tailer.read()
    tailer.commit()

    assert tailer.last_delivery_seen(AGENTDECK).isoformat() == "2026-09-06T12:05:00+00:00"
    assert tailer.last_delivery_seen("eerovil/trailhead").isoformat() == "2026-09-06T11:00:00+00:00"
    assert set(tailer.last_deliveries_seen()) == {AGENTDECK.slug, TRAILHEAD.slug}

    # And it survives a restart, so a consumer can show the horizon straight away.
    assert tailer_for(root).last_delivery_seen(AGENTDECK) is not None


def test_last_delivery_seen_ignores_an_out_of_order_arrival(root, log):
    deliver(log, AGENTDECK, delivery="d-1", received_at="2026-09-06T12:05:00+00:00")
    deliver(log, AGENTDECK, delivery="d-2", received_at="2026-09-06T12:00:00+00:00")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()

    assert tailer.last_delivery_seen(AGENTDECK).isoformat() == "2026-09-06T12:05:00+00:00"


def test_a_deduped_delivery_still_moves_the_freshness_horizon(root, log):
    # The horizon is about the stream, not about what this consumer chose to act on.
    deliver(log, AGENTDECK, delivery="shared", received_at="2026-09-06T12:00:00+00:00")
    deliver(log, AGENTDECK, delivery="shared", received_at="2026-09-06T12:05:00+00:00")
    tailer = tailer_for(root)
    assert ids(tailer.read()) == ["shared"]
    tailer.commit()

    assert tailer.last_delivery_seen(AGENTDECK).isoformat() == "2026-09-06T12:05:00+00:00"


def test_a_corrupt_state_file_replays_rather_than_refusing_to_start(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()
    tailer.state_path.write_text("{ this is not json")

    assert ids(tailer_for(root).read()) == ["d-1"]


def test_a_failed_state_write_replays_the_batch(root, log, tmp_path):
    # The moment the guarantee exists for: the durable record of a batch fails to land.
    # Committing to memory first would drop those records — nothing on disk, nothing
    # pending, and the next pass starting from an offset that was never written.
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the state directory would go")
    deliver(log, AGENTDECK, delivery="d-1")
    deliver(log, AGENTDECK, delivery="d-2")

    tailer = tailer_for(root, state_path=blocker / "pill-cache.json")
    assert ids(tailer.read()) == ["d-1", "d-2"]
    with pytest.raises(OSError):
        tailer.commit()

    # The same instance, not a fresh one — the in-memory offsets must not have moved.
    assert ids(tailer.read()) == ["d-1", "d-2"]
    assert ids(tailer_for(root, state_path=blocker / "pill-cache.json").read()) == ["d-1", "d-2"]


def test_a_commit_can_be_retried_after_the_state_write_fails(root, log, monkeypatch):
    deliver(log, AGENTDECK, delivery="d-1")
    tailer = tailer_for(root)
    assert ids(tailer.read()) == ["d-1"]

    replacements: list[Path] = []
    real_replace = Path.replace

    def flaky_replace(self, target):
        replacements.append(Path(target))
        if len(replacements) == 1:
            raise OSError("no space left on device")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    with pytest.raises(OSError):
        tailer.commit()

    # The batch is still pending, so the retry has something to write rather than
    # silently succeeding on an empty commit.
    tailer.commit()
    assert len(replacements) == 2
    assert tailer.read() == []
    assert tailer_for(root).read() == []


def test_two_processes_committing_one_consumer_do_not_collide(root, log, monkeypatch):
    # A web hot reload runs the same tailers in the old and the new worker at once (#3012).
    # With one shared `<name>.tmp`, the second writer renamed the first's staging file
    # away and the first's rename then failed with FileNotFoundError.
    import agentdeck.webhook_tailer as webhook_tailer

    deliver(log, AGENTDECK, delivery="d-1")
    old_worker = tailer_for(root, "work-router")
    new_worker = tailer_for(root, "work-router")
    assert ids(old_worker.read()) == ["d-1"]
    assert ids(new_worker.read()) == ["d-1"]

    real_fsync = webhook_tailer.os.fsync
    interleaved: list[bool] = []

    def fsync_then_let_the_other_worker_commit(fd):
        real_fsync(fd)
        if not interleaved:
            interleaved.append(True)
            new_worker.commit()

    monkeypatch.setattr(webhook_tailer.os, "fsync", fsync_then_let_the_other_worker_commit)
    old_worker.commit()

    assert interleaved == [True]
    assert tailer_for(root, "work-router").read() == []
    assert [path.name for path in old_worker.state_path.parent.iterdir()] == ["work-router.json"]


def test_a_failed_state_write_leaves_no_staging_file_behind(root, log, monkeypatch):
    deliver(log, AGENTDECK, delivery="d-1")
    tailer = tailer_for(root)
    tailer.read()

    def refuse(self, target):
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(OSError):
        tailer.commit()

    assert list(tailer.state_path.parent.iterdir()) == []


def test_state_is_written_where_the_consumer_asked(root, log, tmp_path):
    elsewhere = tmp_path / "somewhere" / "pill-cache.json"
    deliver(log, AGENTDECK, delivery="d-1")
    tailer = tailer_for(root, state_path=elsewhere)
    tailer.read()
    tailer.commit()

    assert json.loads(elsewhere.read_text())["consumer"] == "pill-cache"
    assert ids(tailer_for(root, state_path=elsewhere).read()) == []


def test_a_consumer_name_that_is_not_a_usable_file_name_is_refused(root):
    for name in ("", "..", "poller/wake"):
        with pytest.raises(ValueError):
            tailer_for(root, name)


def test_watch_yields_the_backlog_then_wakes_on_an_append(root, log):
    deliver(log, AGENTDECK, delivery="d-1")

    async def exercise():
        tailer = tailer_for(root)
        stop = asyncio.Event()
        batches = []
        watcher = tailer.watch(stop_event=stop)

        first = await asyncio.wait_for(anext(watcher), timeout=10)
        batches.append(first)
        tailer.commit()

        await asyncio.sleep(0.1)
        await asyncio.to_thread(deliver, log, AGENTDECK, delivery="d-2")

        second = await asyncio.wait_for(anext(watcher), timeout=10)
        batches.append(second)
        tailer.commit()

        stop.set()
        await watcher.aclose()
        return batches

    backlog, live = asyncio.run(exercise())
    assert ids(backlog) == ["d-1"]
    assert ids(live) == ["d-2"]


def test_watch_calls_the_heartbeat_on_the_keepalive_of_a_quiet_stream(root):
    """#1631: the heartbeat is a liveness proof, so it must move on a *quiet* stream —
    the watcher's keep-alives, which read nothing (#2865), are exactly what prove the watch
    alive rather than merely busy."""
    beats = []

    async def exercise():
        tailer = tailer_for(root)
        stop = asyncio.Event()

        async def consume():
            async for _ in tailer.watch(
                stop_event=stop, keepalive_s=0.05, heartbeat=lambda: beats.append(1)
            ):
                pass  # pragma: no cover - nothing is delivered in this test

        task = asyncio.create_task(consume())
        for _ in range(200):
            await asyncio.sleep(0.05)
            if len(beats) >= 3:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    asyncio.run(exercise())
    assert len(beats) >= 3


def test_watch_creates_the_streams_directory_when_nothing_has_arrived_yet(root):
    async def exercise():
        tailer = tailer_for(root)
        stop = asyncio.Event()
        watcher = tailer.watch(stop_event=stop)
        stop.set()
        collected = [batch async for batch in watcher]
        return collected

    assert asyncio.run(exercise()) == []
    assert root.is_dir()


def test_a_reset_is_part_of_the_read_and_an_ordinary_pass_reports_none(root, log):
    """#1807: a consumer holding derived state has to be able to see the replay."""
    deliver(log, AGENTDECK, delivery="d-1")
    tailer = tailer_for(root)
    tailer.read()
    assert tailer.reset_in_last_read == ()
    tailer.commit()

    log.path_for(AGENTDECK).write_bytes(b"")
    deliver(log, AGENTDECK, delivery="d-2")
    tailer.read()
    assert tailer.reset_in_last_read == (AGENTDECK.slug,)

    # Cleared by the next read, so it always belongs to the pass that found it.
    tailer.commit()
    tailer.read()
    assert tailer.reset_in_last_read == ()


@pytest.mark.asyncio
async def test_watch_hands_over_a_reset_the_dedupe_window_swallowed_whole(root, log):
    """#1807: the pass that carries a reset must reach a consumer that asked for it.

    `watch()` skips a pass whose batch is empty, which is right for an idle poll and
    wrong for a rotation: when every replayed record is already inside the recent-GUID
    window the batch *is* empty, and dropping that pass hides the one signal a consumer
    rebuilding from source needs.
    """
    deliver(log, AGENTDECK, delivery="d-1")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()

    # Rotate and refill with the same delivery: the whole replay is already seen.
    log.path_for(AGENTDECK).write_bytes(b"")
    deliver(log, AGENTDECK, delivery="d-1")

    stop = asyncio.Event()
    handed_over = []

    async def first_pass():
        async for batch in tailer.watch(stop_event=stop, yield_on_reset=True):
            handed_over.append((batch, tailer.reset_in_last_read))
            return

    # Bounded, because the regression this guards is a pass that never arrives: without
    # the yield the loop simply waits forever, and a hang is a far worse test failure
    # than an assertion.
    try:
        await asyncio.wait_for(first_pass(), timeout=5)
    except TimeoutError:  # pragma: no cover - only on a regression
        pytest.fail("watch() never handed over the pass carrying the reset")
    finally:
        stop.set()

    assert handed_over == [([], (AGENTDECK.slug,))]


@pytest.mark.asyncio
async def test_watch_still_skips_an_idle_pass_for_everyone_else(root, log):
    """The opt-in must not turn a quiet stream into a wake for the other consumers."""
    deliver(log, AGENTDECK, delivery="d-1")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()

    stop = asyncio.Event()
    handed_over = []

    async def drain():
        async for batch in tailer.watch(stop_event=stop, yield_on_reset=True):
            handed_over.append(batch)

    task = asyncio.create_task(drain())
    await asyncio.sleep(0.3)
    stop.set()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(task, timeout=2)
    assert handed_over == []


# --- #2865: one shared watch, no timed re-read -----------------------------------------


def _counting(tailer: WebhookTailer) -> list[int]:
    """Count the tailer's reads, so a test can see a re-read that changed nothing."""
    reads: list[int] = []
    real = tailer.read

    def read(repositories=None):
        reads.append(1)
        return real(repositories)

    tailer.read = read  # type: ignore[method-assign]
    return reads


@pytest.fixture
def quick_watch(monkeypatch):
    """A short watchfiles debounce, so a wake lands in tens of milliseconds."""
    monkeypatch.setattr(webhook_tailer, "_WATCH_DEBOUNCE_MS", 20)
    monkeypatch.setattr(webhook_tailer, "_WATCH_STEP_MS", 10)


async def _until(condition, timeout: float = 5.0) -> None:
    async def poll():
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


@pytest.mark.asyncio
async def test_one_shared_watch_wakes_every_consumer_on_an_append(root, log):
    """Each consumer reads from its own offset; the one watch is what wakes them all."""
    deliver(log, AGENTDECK, delivery="d-1")
    watcher = WebhookWatcher(root)
    stop = asyncio.Event()
    seen: dict[str, list[str]] = {"pill-cache": [], "poller-wake": []}

    async def consume(name: str) -> None:
        tailer = tailer_for(root, name)
        async for batch in tailer.watch(stop_event=stop, watcher=watcher):
            seen[name].extend(ids(batch))
            tailer.commit()

    tasks = [asyncio.create_task(consume(name)) for name in seen]
    try:
        for _ in range(200):
            await asyncio.sleep(0.02)
            if all(seen.values()):
                break
        await asyncio.to_thread(deliver, log, AGENTDECK, delivery="d-2")
        for _ in range(250):
            await asyncio.sleep(0.02)
            if all(len(value) == 2 for value in seen.values()):
                break
    finally:
        stop.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        await watcher.stop()
    assert seen == {"pill-cache": ["d-1", "d-2"], "poller-wake": ["d-1", "d-2"]}


@pytest.mark.asyncio
async def test_a_quiet_stream_is_read_once_at_start_and_never_again(root, log, quick_watch):
    """No timer re-reads the streams: with nothing appended, the only read is the start's."""
    deliver(log, AGENTDECK, delivery="d-1")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()
    reads = _counting(tailer)
    stop = asyncio.Event()

    async def drain():
        async for _ in tailer.watch(stop_event=stop):
            pass  # pragma: no cover - nothing new is appended

    task = asyncio.create_task(drain())
    # Not a timing claim: any timed re-read at all would show as a second read here, and the
    # watchfiles debounce alone is a fraction of this.
    await asyncio.sleep(0.08)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_the_first_delivery_for_a_new_owner_is_read_without_another_append(root, log):
    """The owner directory and its stream appear together; nothing else is coming."""
    tailer = tailer_for(root)
    stop = asyncio.Event()
    batches: list[list[str]] = []

    async def consume():
        async for batch in tailer.watch(stop_event=stop):
            batches.append(ids(batch))
            tailer.commit()

    task = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0.3)
        await asyncio.to_thread(deliver, log, TRAILHEAD, delivery="first")
        for _ in range(250):
            await asyncio.sleep(0.02)
            if batches:
                break
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    assert batches == [["first"]]


@pytest.mark.asyncio
async def test_a_missed_watch_event_is_caught_up_by_the_next_append(root, log, quick_watch):
    """The committed offset is the catch-up: an append nobody was woken for is read with
    the next one, not lost and not left for a timer."""
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()
    reads = _counting(tailer)
    watcher = WebhookWatcher(root)
    stop = asyncio.Event()
    batches: list[list[str]] = []

    async def consume():
        async for batch in tailer.watch(stop_event=stop, watcher=watcher):
            batches.append(ids(batch))
            tailer.commit()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(watcher.settled.wait(), timeout=5)
        await _until(lambda: reads)  # the consumer's start-up read is done
        # Written while the consumer is not subscribed to anything: its event is missed.
        wakes = list(watcher._wakes)
        watcher._wakes.clear()
        woken = watcher.wakes
        await asyncio.to_thread(deliver, log, AGENTDECK, delivery="missed")
        await _until(lambda: watcher.wakes > woken)  # the watch fired, to nobody
        await asyncio.sleep(0)
        assert batches == []
        watcher._wakes.extend(wakes)
        await asyncio.to_thread(deliver, log, AGENTDECK, delivery="next")
        for _ in range(250):
            await asyncio.sleep(0.02)
            if batches:
                break
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        await watcher.stop()
    assert [item for batch in batches for item in batch] == ["missed", "next"]


# --- #2754: a rotation must not swallow the deliveries just before it ------------------


@pytest.fixture
def rotating(root) -> WebhookLog:
    return WebhookLog(root, rotate_bytes=4_000, keep_rotations=3)


def fill_until_rotated(log: WebhookLog, repository: Repository, prefix: str) -> list[str]:
    """Deliver until the stream rotates; the ids written, the rotating one last."""
    archive = log.rotation_path(repository, 1)
    before = archive.read_bytes() if archive.exists() else b""
    written = []
    while True:
        written.append(f"{prefix}-{len(written)}")
        deliver(log, repository, delivery=written[-1])
        if archive.exists() and archive.read_bytes() != before:
            return written


def test_the_delivery_that_triggers_a_rotation_is_still_read(root, rotating):
    deliver(rotating, AGENTDECK, delivery="seed")
    tailer = tailer_for(root)
    assert ids(tailer.read()) == ["seed"]
    tailer.commit()

    written = fill_until_rotated(rotating, AGENTDECK, "d")
    # The live stream is empty: everything since the last read is only in the archive.
    assert rotating.path_for(AGENTDECK).stat().st_size == 0

    assert ids(tailer.read()) == written
    assert tailer.reset_in_last_read == (AGENTDECK.slug,)
    tailer.commit()
    assert tailer.read() == []


def test_a_rotation_reads_the_archive_tail_then_the_refilled_live_stream(root, rotating):
    deliver(rotating, AGENTDECK, delivery="seed")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()

    written = fill_until_rotated(rotating, AGENTDECK, "d")
    deliver(rotating, AGENTDECK, delivery="after")

    assert ids(tailer_for(root).read()) == [*written, "after"]


def test_two_rotations_between_reads_read_every_archive_once(root, rotating):
    deliver(rotating, AGENTDECK, delivery="seed")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()

    first = fill_until_rotated(rotating, AGENTDECK, "a")
    second = fill_until_rotated(rotating, AGENTDECK, "b")
    deliver(rotating, AGENTDECK, delivery="live")

    assert ids(tailer_for(root).read()) == [*first, *second, "live"]


def test_more_rotations_than_are_kept_replays_the_live_stream_alone(root):
    log = WebhookLog(root, rotate_bytes=4_000, keep_rotations=1)
    deliver(log, AGENTDECK, delivery="seed")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()

    fill_until_rotated(log, AGENTDECK, "a")
    second = fill_until_rotated(log, AGENTDECK, "b")
    deliver(log, AGENTDECK, delivery="live")

    # The archive holding the committed offset is gone, so nothing anchors a catch-up:
    # the newest archive is not guessed at, and the live stream is replayed as before.
    assert "seed" not in second
    assert ids(tailer_for(root).read()) == ["live"]


def test_a_corrupt_archive_does_not_stop_the_live_read(root, rotating):
    deliver(rotating, AGENTDECK, delivery="seed")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()

    fill_until_rotated(rotating, AGENTDECK, "d")
    archive = rotating.rotation_path(AGENTDECK, 1)
    # Keep the gzip header and head intact so the fingerprint still matches, then cut
    # the stream short: decompressing past the head fails.
    archive.write_bytes(archive.read_bytes()[:600])
    deliver(rotating, AGENTDECK, delivery="live")

    assert "live" in ids(tailer_for(root).read())


def test_an_uncommitted_catch_up_is_replayed_on_the_next_pass(root, rotating):
    deliver(rotating, AGENTDECK, delivery="seed")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()

    written = fill_until_rotated(rotating, AGENTDECK, "d")
    assert ids(tailer_for(root).read()) == written
    # Never committed, so a fresh consumer process sees the same catch-up again.
    assert ids(tailer_for(root).read()) == written


def test_a_stream_shorter_than_the_fingerprint_that_grows_is_not_a_rotation(root, log):
    # A stream whose first record is shorter than the fingerprint window used to change
    # its fingerprint on the very next append. Compared over the same span, it does not.
    log.path_for(AGENTDECK).parent.mkdir(parents=True, exist_ok=True)
    log.path_for(AGENTDECK).write_bytes(b'{"delivery_id": "tiny"}\n')
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()

    deliver(log, AGENTDECK, delivery="d-1")
    assert ids(tailer.read()) == ["d-1"]
    assert tailer.reset_in_last_read == ()


def test_state_written_before_head_size_existed_still_resumes(root, log):
    deliver(log, AGENTDECK, delivery="d-1")
    tailer = tailer_for(root)
    tailer.read()
    tailer.commit()
    state = json.loads(tailer.state_path.read_text())
    for stream in state["streams"].values():
        del stream["head_size"]
    tailer.state_path.write_text(json.dumps(state))

    deliver(log, AGENTDECK, delivery="d-2")
    resumed = tailer_for(root)
    assert ids(resumed.read()) == ["d-2"]
    assert resumed.reset_in_last_read == ()
