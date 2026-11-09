"""Name whatever blocks the web process's event loop (#3970).

On 2026-11-01 the web process answered nothing for 67 s and its log could not say
why: something synchronous held the loop. This task beats once a tick on the loop,
and a daemon thread watches the beat. Once the loop has missed it for `BLOCKED_S`,
the thread logs the loop thread's stack, read with `sys._current_frames()`, so the
journal names the blocking call. When the loop runs again, one warning says how long
it was held.

The first version used `faulthandler.dump_traceback_later`, whose C thread reads
every thread's frames without the GIL. On the live deck it crashed a web worker in
`dump_frame` while the loop thread moved on under it. This thread takes the GIL like
any other, which the loop thread gives up every few milliseconds while it runs Python
or waits on SQLite, so a block inside C code that keeps the GIL goes unnamed; the
warning after it still says how long it was.

Beside each stack it writes what the process's other busy threads were doing, since a loop
waiting on a lock names only the wait: five freezes on 2026-11-05 stopped at `Db._lock` and
nothing said who held it (#4114). That is a separate warning, so the error Sentry receives
keeps its grouping and size.

A thread with nothing to do is left out of that list, and so is only one parked exactly where
its pool or Sentry parks it (`_PARKED`): anything else, a plain lock wait included, may be the
holder. About 11 idle AnyIO threads and Sentry's own filled it on 2026-11-05 (#4130).

One stack shows a single moment, so it can blame whatever ran last in a block that many calls
shared. The two freezes #4130 looked at could not say which it was. From the first late tick on,
the thread therefore notes where the loop is, and the warning lists those notes.

Only a served web process runs it (`_ReadyServer` in `web_worker`, the worker
`agentdeck.service` runs, and `_WebServer` in `__main__`), and the Work services process
(`services_process`, #4016), never an app built in a test.

A block that reaches `REPORT_S` is also logged as an error, with its stack taken then, so
error reporting sends it to Sentry (#3993): the person otherwise learns of a minute-long
freeze only from pages that will not open. It is written while the block lasts, since a
person often restarts a frozen deck before the block ends. Shorter blocks stay warnings,
which reach Sentry only as breadcrumbs.
"""

from __future__ import annotations

import asyncio
import linecache
import logging
import os
import sys
import threading
import time
import traceback
from types import FrameType

logger = logging.getLogger(__name__)

# Long enough that a slow but legitimate step (a big Work page build) stays quiet,
# short enough that the stack is taken while the culprit still holds the loop.
BLOCKED_S = 5.0
TICK_S = 1.0
# A freeze a person waiting on a page notices; the deck logged 16 blocks over 20 s in a
# day from one cause (#3993), and most legitimate slow steps stay under this.
REPORT_S = 10.0
# Enough of each other thread to see the call it is in, without a long pool flooding the journal.
OTHER_THREAD_FRAMES = 12
OTHER_THREADS_MAX = 20
# The most notes kept of where a block's loop was, newest kept: one note a tick for a block that
# lasted minutes would flood the journal.
LOOP_NOTES_MAX = 20

_OWN_CODE = os.path.dirname(os.path.abspath(__file__)) + os.sep
# What a thread stands inside while it waits, on its way to the place that parked it.
_WAIT_FILES = ("/threading.py", "/queue.py", "/sentry_sdk/_queue.py")
# Where each kind of idle thread waits for work: file, function, and the words of the line. The
# line tells `self.queue.get()` from the `context.run(func)` a few lines below it, which is a
# real call in C when `func` has no Python frame. A library that rewords its line makes its
# threads show as busy again: noise, never a hidden holder.
_PARKED = (
    ("/concurrent/futures/thread.py", "_worker", "work_queue.get("),
    ("/anyio/_backends/_asyncio.py", "run", "self.queue.get("),
    ("/sentry_sdk/worker.py", "_target", "self._queue.get("),
    ("/sentry_sdk/monitor.py", "_thread", "time.sleep("),
    ("/sentry_sdk/sessions.py", "_thread", ".wait(self.flush_interval)"),
)


async def watch(
    blocked_s: float = BLOCKED_S, tick_s: float = TICK_S, report_s: float = REPORT_S
) -> None:
    loop_thread = threading.get_ident()
    beat = [time.monotonic()]
    stop = threading.Event()
    threading.Thread(
        target=_watch_beat,
        args=(loop_thread, beat, stop, blocked_s, tick_s, report_s),
        name="loop-watchdog",
        daemon=True,
    ).start()
    logger.info("loop watchdog: the loop's stack is logged after a %.0f s block", blocked_s)
    try:
        while True:
            await asyncio.sleep(tick_s)
            now = time.monotonic()
            if now - beat[0] >= blocked_s:
                logger.warning("event loop was blocked for %.1f s", now - beat[0] - tick_s)
            beat[0] = now
    finally:
        stop.set()


def _watch_beat(
    loop_thread: int,
    beat: list[float],
    stop: threading.Event,
    blocked_s: float,
    tick_s: float,
    report_s: float,
) -> None:
    # The block (its beat) each message was last written for: each once per block, as the
    # beat stays put until the loop runs again.
    warned = reported = noted = None
    notes: list[str] = []
    while not stop.wait(tick_s):
        last = beat[0]
        held = time.monotonic() - last
        if last != noted:
            noted, notes = last, []
        # A beat on time is at most a tick old, and this look can land just after it is due.
        if held >= 2 * tick_s and (frame := sys._current_frames().get(loop_thread)) is not None:
            notes = [*notes, f"{held:.1f} s: {_where(frame)}"][-LOOP_NOTES_MAX:]
        if held < blocked_s:
            continue
        # The error is written while the block lasts, never after it: a person who
        # restarts a frozen deck ends the block before the loop could say so (#3993).
        level = logging.ERROR if held >= report_s else logging.WARNING
        if last == (reported if level == logging.ERROR else warned):
            continue
        frames = sys._current_frames()
        frame = frames.get(loop_thread)
        if frame is None:
            continue
        warned = last
        if level == logging.ERROR:
            reported = last
        logger.log(
            level,
            "event loop held for over %.0f s; its stack, most recent call last:\n%s",
            report_s if level == logging.ERROR else blocked_s,
            "".join(traceback.format_stack(frame)).rstrip(),
        )
        try:
            logger.warning("%s", _with_notes(notes, _other_threads(loop_thread, frames)))
        except Exception:
            # Never at the cost of the next block's report.
            logger.warning("could not list the other threads", exc_info=True)


def _where(frame: FrameType) -> str:
    """The innermost call of this code in a stack, or of the stack when none is ours."""
    found: FrameType | None = frame
    while found is not None and not found.f_code.co_filename.startswith(_OWN_CODE):
        found = found.f_back
    found = found or frame
    return f"{found.f_code.co_filename}:{found.f_lineno} in {found.f_code.co_name}"


def _with_notes(notes: list[str], threads: str) -> str:
    """The list of threads, after where the loop was during this block."""
    if not notes:
        return threads
    return "\n".join(["where the loop was during this block:", *notes, threads])


def _is_idle(frame: FrameType) -> bool:
    """A thread parked where its pool or Sentry waits for work; any other wait may be the holder."""
    parked: FrameType | None = frame
    while parked is not None and parked.f_code.co_filename.endswith(_WAIT_FILES):
        parked = parked.f_back
    if parked is None:
        return False
    code = parked.f_code
    return any(
        code.co_name == name
        and code.co_filename.endswith(path)
        and words in linecache.getline(code.co_filename, parked.f_lineno)
        for path, name, words in _PARKED
    )


def _runs_our_code(frame: FrameType) -> bool:
    found: FrameType | None = frame
    while found is not None:
        if found.f_code.co_filename.startswith(_OWN_CODE):
            return True
        found = found.f_back
    return False


def _other_threads(loop_thread: int, frames: dict[int, FrameType]) -> str:
    """What every busy thread but the loop and this one was doing, from the same moment.

    Whoever holds a lock of ours is in our code, so those threads go first, and a thread past
    the limit still gets a line: the limit saves the journal, never hides a holder.
    """
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    skip = {loop_thread, threading.get_ident()}
    busy = [
        (names.get(ident, f"thread {ident}"), frame)
        for ident, frame in frames.items()
        if ident not in skip and not _is_idle(frame)
    ]
    if not busy:
        return "no other thread was busy while the loop was held"
    busy.sort(key=lambda thread: not _runs_our_code(thread[1]))
    lines = ["other busy threads while the loop was held:"]
    for name, frame in busy[:OTHER_THREADS_MAX]:
        stack = "".join(traceback.format_stack(frame, limit=OTHER_THREAD_FRAMES)).rstrip()
        lines.append(f"{name}:\n{stack}")
    if len(busy) > OTHER_THREADS_MAX:
        lines.append(f"and {len(busy) - OTHER_THREADS_MAX} more:")
        lines.extend(f"{name}: {_where(frame)}" for name, frame in busy[OTHER_THREADS_MAX:])
    return "\n".join(lines)
