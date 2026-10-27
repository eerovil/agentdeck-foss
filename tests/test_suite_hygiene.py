"""Guards on the test suite itself, in the spirit of `test_browser_tier.py`.

Not product tests: these fail when the suite acquires a habit that costs the
whole run rather than one test.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# `monkeypatch.setattr(asyncio, "sleep", AsyncMock())` and friends.
_NON_YIELDING_SLEEP = re.compile(
    r"setattr\(\s*[\w.]*asyncio\s*,\s*[\"']sleep[\"']\s*,\s*(?:mock\.)?AsyncMock\("
)


def test_no_test_replaces_asyncio_sleep_with_a_bare_async_mock():
    """Awaiting an `AsyncMock()` never suspends, so it is not a fast `sleep`.

    #846: one test patched `asyncio.sleep` this way to skip a watch loop's 1 Hz
    backoff. Nothing then yielded to the event loop — not the task under test,
    not the test's own `await asyncio.sleep(0)` driver loop — so the test spun
    forever, and each spin appended a call record to the mock. A single pytest
    process reached 23 GB of RSS and the kernel OOM-killed the whole run.

    Use a stand-in that still yields, e.g.
    ``real_sleep = asyncio.sleep; monkeypatch.setattr(asyncio, "sleep",
    lambda *a, **k: real_sleep(0))``, and bound the driver loop so a loop that
    stops making progress fails instead of hanging.
    """
    offenders = [
        f"{path.name}:{index}"
        for path in sorted(TESTS_DIR.rglob("test_*.py"))
        if path != Path(__file__).resolve()  # this file spells the pattern out
        for index, line in enumerate(path.read_text().splitlines(), start=1)
        if _NON_YIELDING_SLEEP.search(line)
    ]
    assert not offenders, (
        "these patch asyncio.sleep with a bare AsyncMock, which never yields "
        f"to the event loop and spins the run out of memory: {offenders}"
    )
