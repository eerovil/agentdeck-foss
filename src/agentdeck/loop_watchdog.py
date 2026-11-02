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
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

# Long enough that a slow but legitimate step (a big Work page build) stays quiet,
# short enough that the stack is taken while the culprit still holds the loop.
BLOCKED_S = 5.0
TICK_S = 1.0
# A freeze a person waiting on a page notices; the deck logged 16 blocks over 20 s in a
# day from one cause (#3993), and most legitimate slow steps stay under this.
REPORT_S = 10.0


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
    warned = reported = None
    while not stop.wait(tick_s):
        last = beat[0]
        held = time.monotonic() - last
        if held < blocked_s:
            continue
        # The error is written while the block lasts, never after it: a person who
        # restarts a frozen deck ends the block before the loop could say so (#3993).
        level = logging.ERROR if held >= report_s else logging.WARNING
        if last == (reported if level == logging.ERROR else warned):
            continue
        frame = sys._current_frames().get(loop_thread)
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
