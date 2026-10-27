"""Wake a Poller Instance early when a delivery says to look (#1628, #1649).

The second consumer of the raw webhook streams, and the one that closes the latency
gap the streams were built for: a `WebhookTailer` in the web process that turns a
relevant delivery into `systemctl --user start agentdeck-poller@<instance>`.

Why the web process rather than the poller itself: an instance is not resident. It is
a `Type=oneshot` service a timer refires 30 seconds after the previous run exits, so
nothing inside the poller can hold a watch on anything. Somebody persistent has to
convert a delivery into an early start, and the web process is the persistent thing
already tailing these streams (#1628).

The rules this holds to, all of them from #1628:

* **A wake says "look now", never "this is true".** The started cycle re-reads GitHub
  and decides for itself. Nothing here interprets a payload beyond its event name and
  its `action`, and nothing here writes state a board or a cycle reads.
* **The timer is untouched.** It stays the correctness backstop for every
  delivery GitHub drops, and the cadence composes on its own: `OnUnitInactiveSec=120`
  refires two minutes after any run ends (30 s before #3010), so an early wake postpones
  the timed one instead of doubling it up.
* **Stateless.** The streams are seeked to the end at startup and whatever was unread
  is discarded, so a web restart never replays an hour of old deliveries as a burst of
  wakes. A delivery missed while the process was down costs latency and nothing else —
  the timer covers it.
* **Work and CI completions only.** `issues`, `issue_comment`, `pull_request` and
  `pull_request_review`, plus `check_suite`/`workflow_run` at `action=completed`.
  Pushes and in-progress CI never wake anything, because a wake spends GitHub quota.

There is no UI push here, matching the same amendment made to the pill cache (#1627):
this consumer starts a unit and reports nothing to a browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path

from .webhook_tailer import Delivery, WebhookTailer, WebhookWatcher

log = logging.getLogger(__name__)

CONSUMER = "poller-wake"

#: The watcher's liveness proof (#1631). Touched on every pass of this consumer and on every
#: keep-alive of the shared webhook watch, so the mtime moves at least every `KEEPALIVE_S`
#: while the watch is alive and stops the moment it is not. The poller's quiet skip reads it
#: before trusting a quiet stream: a dead watcher, or a whole dead web process, restores full
#: 30-second polling within `poller_quiet.HEARTBEAT_STALE_S`.
HEARTBEAT_FILENAME = "poller-wake.heartbeat"

#: How often the webhook watch proves itself alive on a quiet stream (#2865). It reads
#: nothing — the streams are read only when they change — and a quarter of the quiet
#: skip's 120-second staleness bound leaves three missed keep-alives of slack.
KEEPALIVE_S = 30.0


def heartbeat_path(streams_root: Path | str) -> Path:
    """Where the watcher's heartbeat lives: beside its tailer state, derived the same way."""
    return Path(streams_root).expanduser().parent / "webhook-tailers" / HEARTBEAT_FILENAME

#: Work events: a card can change because of any of these, so any of them is worth a look.
WORK_EVENTS = frozenset(
    {"issues", "issue_comment", "pull_request", "pull_request_review"}
)

#: CI events, and only once they have finished. An in-progress run tells a cycle nothing
#: it can act on, and a busy repository emits a great many of them.
COMPLETION_EVENTS = frozenset({"check_suite", "workflow_run"})

#: Seconds an instance is left alone after a wake. Long enough that a burst of deliveries
#: from one push-and-comment costs one start, short enough to stay well inside the
#: two-minute timer this only ever races to beat.
COOLDOWN_S = 10.0

#: A `systemctl start --no-block` only enqueues a job, so it returns immediately; this is
#: a bound on a systemd that is not answering at all, not on the poller cycle's own length.
START_TIMEOUT_S = 10.0


def wakes(delivery: Delivery) -> bool:
    """Should this delivery start a cycle? Event name first, payload only if needed."""
    if delivery.event in WORK_EVENTS:
        return True
    if delivery.event not in COMPLETION_EVENTS:
        return False
    return _action(delivery) == "completed"


def _action(delivery: Delivery) -> str:
    try:
        payload = json.loads(delivery.raw_body)
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    action = payload.get("action")
    return action if isinstance(action, str) else ""


class ManifestRoutes:
    """Repository slug → the instances that declare it, read from the live manifests.

    A Poller Manifest names one `[board].org` and its repositories by bare id, which is
    exactly how the engine spells a slug (`poller_engine.github`), so the mapping is the
    org joined to each `[[repos]].id`. Nothing else in the manifest is read.

    Re-read when a manifest file moves on disk: an instance added, or a repository added
    to one, must route without a web restart.
    """

    def __init__(self, poller_root: Path | str) -> None:
        self.root = Path(poller_root).expanduser() / "instances"
        self._signature: tuple = ()
        self._routes: dict[str, tuple[str, ...]] = {}
        self._read = False

    def instances_for(self, slug: str) -> tuple[str, ...]:
        """Every instance that declares this repository, in stable order.

        More than one is possible and is not an error — two boards may watch the same
        repository — so each of them is woken.
        """
        return self.routes().get(slug.lower(), ())

    def routes(self) -> dict[str, tuple[str, ...]]:
        signature = self._fingerprint()
        if self._read and signature == self._signature:
            return self._routes
        self._routes = self._load()
        self._signature = signature
        self._read = True
        return self._routes

    def _manifests(self) -> list[Path]:
        try:
            children = sorted(child for child in self.root.iterdir() if child.is_dir())
        except OSError:
            return []
        return [child / "poller.toml" for child in children]

    def _fingerprint(self) -> tuple:
        parts: list[tuple] = []
        for path in self._manifests():
            try:
                stat = path.stat()
            except OSError:
                parts.append((path.name, path.parent.name))
            else:
                parts.append((path.name, path.parent.name, stat.st_size, stat.st_mtime_ns))
        return tuple(parts)

    def _load(self) -> dict[str, tuple[str, ...]]:
        routes: dict[str, list[str]] = {}
        for path in self._manifests():
            instance = path.parent.name
            for slug in _declared_repositories(path):
                claimed = routes.setdefault(slug, [])
                if instance not in claimed:
                    claimed.append(instance)
        return {slug: tuple(names) for slug, names in routes.items()}


def _declared_repositories(manifest: Path) -> list[str]:
    """The lowercased slugs one manifest declares, or none if it cannot be read.

    A manifest an operator is mid-edit on, or one this deck's TOML cannot parse, must not
    take the whole watcher down: it routes nothing until it parses again, and the timer
    keeps that instance polling in the meantime.
    """
    try:
        with manifest.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return []
    board = data.get("board")
    org = board.get("org") if isinstance(board, dict) else None
    if not isinstance(org, str) or not org:
        return []
    slugs: list[str] = []
    for entry in data.get("repos") or []:
        if not isinstance(entry, dict):
            continue
        repo_id = entry.get("id")
        if isinstance(repo_id, str) and repo_id:
            slugs.append(f"{org}/{repo_id}".lower())
    return slugs


class PollerWakeService:
    """Tail the streams and start the instance a relevant delivery belongs to.

    Web-process owned, and deliberately without durable state of its own: what it holds
    between deliveries is a per-instance cooldown in memory, which a restart is welcome
    to forget.
    """

    def __init__(
        self,
        streams_root: Path | str,
        poller_root: Path | str,
        *,
        enabled: bool = True,
        cooldown_s: float = COOLDOWN_S,
        routes: ManifestRoutes | None = None,
        start_unit: Callable[[str], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        watcher: WebhookWatcher | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._tailer = WebhookTailer(streams_root, consumer=CONSUMER)
        # The web process's one shared watch (#2865); None opens a private one.
        self._watcher = watcher
        self._heartbeat_path = heartbeat_path(streams_root)
        self._routes = routes or ManifestRoutes(poller_root)
        self._cooldown_s = max(0.0, cooldown_s)
        self._start_unit = start_unit or _systemctl_start
        self._clock = clock
        self._last_wake: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="poller-wake")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def seek_to_end(self) -> None:
        """Drop whatever is unread, so only deliveries from now on can wake anything.

        Read and committed rather than merely offset-set, so the tailer's rotation and
        dedupe bookkeeping ends up exactly where a normal pass would have left it.
        """
        for _ in self._tailer.deliveries():
            pass
        self._tailer.commit()

    def _touch_heartbeat(self) -> None:
        """Move the heartbeat's mtime, and never let bookkeeping take the watcher down."""
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._heartbeat_path.touch()
        except OSError:
            # A heartbeat that cannot be written costs skipped skips — the pollers it
            # gates simply keep their full 30-second cadence — never a lost wake.
            log.debug("could not touch %s", self._heartbeat_path, exc_info=True)

    async def _run(self) -> None:
        first = True
        while True:
            try:
                if first:
                    self.seek_to_end()
                    first = False
                async for batch in self._tailer.watch(
                    stop_event=self._stop,
                    watcher=self._watcher,
                    heartbeat=self._touch_heartbeat,
                    keepalive_s=KEEPALIVE_S,
                ):
                    await self.apply(batch)
                    self._tailer.commit()
                return  # the stop event ended the watch
            except asyncio.CancelledError:
                raise
            except Exception:
                # A wake is a latency optimisation over a poller that is already correct
                # without it, so nothing here is worth losing the loop over. After a
                # failure the uncommitted pass replays: a start that actually returned is
                # cooldown-absorbed, while a start that raised rolls its reservation back;
                # systemd itself also joins a repeat start to a running job.
                log.exception("poller wake pass failed; retrying")
                await asyncio.sleep(5)

    async def apply(self, batch: Iterable[Delivery]) -> None:
        """Wake every instance this batch of deliveries concerns, each at most once."""
        wanted: list[str] = []
        for delivery in batch:
            if not wakes(delivery):
                continue
            instances = self._routes.instances_for(delivery.stream)
            if not instances:
                # A repository with a stream but no manifest claiming it. Ordinary on a
                # host whose webhook is configured more widely than its boards are.
                log.debug(
                    "webhook delivery for %s matches no poller instance", delivery.stream
                )
                continue
            for instance in instances:
                if instance not in wanted:
                    wanted.append(instance)
        for instance in wanted:
            await self.wake(instance)

    async def wake(self, instance: str) -> bool:
        """Start one instance's unit unless it was started moments ago."""
        now = self._clock()
        last = self._last_wake.get(instance)
        if last is not None and now - last < self._cooldown_s:
            return False
        # Reserve the cooldown before awaiting so concurrent deliveries cannot
        # start the same unit twice. If the start itself raises, undo only this
        # reservation so the uncommitted webhook replay can retry immediately.
        self._last_wake[instance] = now
        try:
            log.info("webhook delivery wakes poller instance %s", instance)
            result = self._start_unit(instance)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            if self._last_wake.get(instance) == now:
                self._last_wake.pop(instance, None)
            raise
        return True


async def _systemctl_start(instance: str) -> None:
    """Enqueue a start of the instance's unit, without waiting for the cycle.

    `--no-block` is not a nicety: the unit is a `Type=oneshot`, so a blocking start would
    not return until the whole poller cycle had finished. Starting one that is already
    running joins the running job rather than queueing a second cycle.
    """
    unit = f"agentdeck-poller@{instance}.service"
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "start",
            "--no-block",
            unit,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        log.warning("could not run systemctl to wake %s", unit)
        return
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=START_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        log.warning("systemctl start %s did not return; gave up on this wake", unit)
        return
    if process.returncode != 0:
        log.warning(
            "systemctl start %s failed (%s): %s",
            unit,
            process.returncode,
            (stderr or b"").decode("utf-8", "replace").strip(),
        )
