"""Say when the poller board moved, so nothing re-reads it on a clock (#2865).

The board is what every installed poller instance publishes: its manifest, its state files
and its reviewer's state file (`poller_ops.read_board`). The engines republish those on
their own wakes, from another process, so no in-process write can announce them. Each open
chat page used to re-read the board every five seconds and the Work overview rebuilt every
ten to catch a card move. This watches the files instead and publishes ``board`` when
their fingerprint — the same one `read_board` reuses its decode on — actually moved.

A file watch can miss a write. The catch-up is the fingerprint itself (ADR 0007): every
event compares the whole fingerprint, not just the file it named, and a page reads the
board once when it connects.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from watchfiles import awatch

from . import poller_ops, watch_retry
from .events import EventBus

log = logging.getLogger(__name__)

BOARD_TOPIC = "board"


def _watch_paths(root: Path) -> tuple[str, ...]:
    """The instances tree, and the directory of each instance's reviewer state file."""
    instances = root / "instances"
    paths = {str(instances)} if instances.is_dir() else set()
    for instance in poller_ops._instance_names(root):
        try:
            from . import poller_nudge  # imports poller_ops: resolved at call time

            path = poller_nudge.external_review_state_file(root, instance)[1]
        except Exception:  # noqa: BLE001 -- a bad instance must not stop the watch
            continue
        if path is not None and path.parent.is_dir() and not path.is_relative_to(instances):
            paths.add(str(path.parent))
    return tuple(sorted(paths))


class BoardWatch:
    """Publishes ``BOARD_TOPIC`` on ``bus`` whenever the poller board's files move."""

    def __init__(self, root: str | Path, bus: EventBus) -> None:
        self._root = Path(root).expanduser()
        self._bus = bus
        self._task: asyncio.Task | None = None
        self._signature: tuple | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="board-watch")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def check(self) -> bool:
        """Publish if the fingerprint moved since the last look; whether it did."""
        signature = poller_ops._durable_signature(self._root)
        if signature == self._signature:
            return False
        first = self._signature is None
        self._signature = signature
        if not first:
            self._bus.publish(BOARD_TOPIC)
        return not first

    async def _run(self) -> None:
        self.check()
        # A failed watch is retried, never abandoned: nothing else would ever publish
        # ``board`` again. The catch-up after a failure is the fingerprint itself.
        await watch_retry.retry_forever(self._watch, name="poller board", caught_up=self.check)

    async def _watch(self) -> None:
        """Watch the current set of paths until it changes; raise if the watch fails."""
        paths = _watch_paths(self._root)
        # A new instance or reviewer path means a new set to watch: restart on it.
        changed_paths = asyncio.Event()
        if not paths:
            # Nothing installed yet. Watch the nearest directory that exists, not
            # recursively, until an instance appears: returning for good here left a
            # board installed later unwatched until the next restart.
            await self._await_paths(changed_paths)
            return
        # Observe before catching up (ADR 0007 rule 4): `awatch` opens its watch
        # synchronously before its first suspension, so this check runs once the watch
        # exists and a write made before it opened is still seen.
        caught_up = asyncio.get_running_loop().call_soon(self.check)
        try:
            async for _changes in awatch(*paths, debounce=500, step=100, stop_event=changed_paths):
                self.check()
                if _watch_paths(self._root) != paths:
                    changed_paths.set()
        finally:
            caught_up.cancel()

    def _anchor(self) -> Path:
        """The nearest existing directory on the way to the instances tree."""
        anchor = self._root / "instances"
        while not anchor.is_dir() and anchor != anchor.parent:
            anchor = anchor.parent
        return anchor

    async def _await_paths(self, stop: asyncio.Event) -> None:
        """Return once there is something to watch, or a nearer directory to wait on."""
        anchor = self._anchor()
        log.info("no poller instances under %s; waiting in %s", self._root, anchor)

        def look() -> None:
            # Once the watch exists: an instance made before it opened is still noticed.
            self.check()
            if _watch_paths(self._root) or self._anchor() != anchor:
                stop.set()

        caught_up = asyncio.get_running_loop().call_soon(look)
        try:
            async for _changes in awatch(
                anchor, debounce=500, step=100, recursive=False, stop_event=stop
            ):
                look()
        finally:
            caught_up.cancel()
