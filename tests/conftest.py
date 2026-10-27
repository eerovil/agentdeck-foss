"""Synthetic fixtures — a fake CLAUDE_CONFIG_DIR + fake /proc tree.

No real Claude data is ever read or copied; everything here is hand-built.

Also home to the two harness-wide defaults that keep the parallel suite honest:
the service worker is blocked in every browser context that does not ask for it
(`pytest_configure`), and no more than `browser_slot_count()` browser tests run
at once anywhere on the host — across xdist workers and across concurrent
pytest processes alike (`browser_slot`).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import os
import re
import tempfile
import time
import uuid
import warnings
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pytest
import pytest_asyncio

from agentdeck.host_slots import (
    HEAVY_SLOT_HELD_ENV,
    QUEUED_JOB_ENV,
    heavy_slot_count,
    heavy_slot_is_held,
    heavy_slot_paths,
    take_a_slot,
)
from agentdeck.test_timings import TIMINGS_ENV, TimingRecorder

# The suite publishes durable engine state thousands of times — a single poller wake
# checkpoints several times, and one test drives several wakes — and every publish waits for
# two files and their directory to reach the disk. That is the guarantee production wants and
# a cost no test can observe: the temp-file-plus-rename is what makes a publish atomic, and
# fsync only decides whether it survives a power cut. Turning it off here takes a state
# publish from about 19ms to about 2ms (#1473).
#
# ``setdefault``, so a run that wants the real thing — proving the durability itself — can
# still ask for it with ``AGENTDECK_FSYNC=1``. Spelled out rather than read from
# ``poller_engine.state.FSYNC_ENV``: importing the engine cost every pytest start,
# the ones tests run as subprocesses included, 0.15 s (#3052).
os.environ.setdefault("AGENTDECK_FSYNC", "0")

# Every test sees the same remote job lanes, never the host's own file (#3623). Assigned,
# not defaulted: a queued suite inherits the runtime's environment.
os.environ["AGENTDECK_JOB_LANES"] = str(Path(__file__).with_name("fixtures") / "job-lanes.toml")



# No test may open a password box on the host's desktop. Tests that add fake GitHub
# remotes reach a real fetch, the credential helper refuses their owner, and git asks
# whatever askpass program the desktop session exported (#3632).
for _name in ("GIT_ASKPASS", "SSH_ASKPASS", "SUDO_ASKPASS"):
    os.environ.pop(_name, None)
os.environ["GIT_TERMINAL_PROMPT"] = "0"

def pytest_configure(config: pytest.Config) -> None:
    """Block the service worker in every browser context that does not ask for it.

    On a fresh Chromium profile `/sw.js` installs, claims the page, and
    `base.html`'s `controllerchange` handler reloads once — which destroys the
    execution context under whatever the test was doing and reads as a product
    failure ("Execution context was destroyed", "Locator.fill timed out"). Every
    Playwright test that loads a deck page on a cold profile is exposed, so the
    repo's rule was "pass service_workers='block' when the worker is not the
    subject" — honoured at 48 of 131 call sites, and running the suite in
    parallel gives each xdist worker its own cold profile and hits the rest.
    Rather than repeat the kwarg 83 more times, invert the default here: a test
    that *is* about the worker passes service_workers="allow" and says so.

    The underlying product bug is #720; this only stops it from being every
    browser test's problem.
    """
    from playwright.async_api import Browser

    for owner, name in ((Browser, "new_page"), (Browser, "new_context")):
        original = getattr(owner, name)
        if getattr(original, "_blocks_service_workers", False):
            continue  # a second pytest_configure (xdist re-imports conftest) is a no-op

        def wrapper(self, *args, __original=original, **kwargs):
            kwargs.setdefault("service_workers", "block")
            return __original(self, *args, **kwargs)

        wrapper._blocks_service_workers = True  # type: ignore[attr-defined]
        wrapper._unwrapped = original  # type: ignore[attr-defined]
        setattr(owner, name, wrapper)

    _forget_operator_error_reporting()
    _forget_operator_mac_runner()
    _pin_host_shape()
    _run_single_process_unless_queued(config)
    _hold_heavy_slot(config)


def _run_single_process_unless_queued(config: pytest.Config) -> None:
    """Run a direct `pytest` on this host as one process (#2666).

    `pyproject.toml` asks xdist for `-n auto`, which on this four-core host turned a
    one-file check into four workers competing with the deck. A queued job, here or in a
    pod, sets `QUEUED_JOB_ENV` and keeps every worker it was given; a direct run gets one
    process unless the caller asked for `-n` themselves. xdist's own `pytest_configure`
    runs last and only starts workers while `dist` is not `"no"`, so turning it off here
    is enough.
    """
    if hasattr(config, "workerinput") or os.environ.get(QUEUED_JOB_ENV):
        return
    if getattr(config.option, "numprocesses", None) is None:
        return  # xdist is not loaded (`-p no:xdist`)
    if _asked_for_workers(
        [*config.invocation_params.args, *os.environ.get("PYTEST_ADDOPTS", "").split()]
    ):
        return
    config.option.numprocesses = 0
    config.option.dist = "no"
    config.option.tx = []


def _asked_for_workers(args) -> bool:
    for arg in args:
        arg = str(arg)
        if arg == "--numprocesses" or arg.startswith("--numprocesses="):
            return True
        if arg.startswith("-n") and not arg.startswith("--"):
            return True
    return False


def _forget_operator_error_reporting() -> None:
    """Never let the host's live Sentry settings reach a test (#1514).

    `initialize_error_reporting` reads `AGENTDECK_SENTRY_DSN` from the environment, and a
    host that has switched reporting on exports it to everything the deck starts — including
    a shell, and therefore pytest. Two things then go wrong. Every process a test spawns
    builds a real client pointed at the operator's own project, so a test's deliberate
    failures are shipped to it as production events. And the client's shutdown flush tries to
    reach that project before the process may exit, which is how
    `test_reload_serves_requests_until_the_replacement_is_ready` failed on this host: the old
    web worker outlived the ten seconds the test allows it to drain, on `master`, with no
    change to the supervisor at all.

    Cleared here rather than in one test, because it is not one test's problem: any test that
    starts a deck process inherits it. Tests that are *about* the reporter set the variable
    themselves with `monkeypatch`, which is unaffected.
    """
    for name in ("AGENTDECK_SENTRY_DSN", "AGENTDECK_SENTRY_ENVIRONMENT",
                 "AGENTDECK_SENTRY_RELEASE"):
        os.environ.pop(name, None)


def _forget_operator_mac_runner() -> None:
    """Never let the host's live Mac runner reach a test (#3056).

    The runtime exports `AGENTDECK_MAC_RUNNER_URL` and its token to everything it
    starts, and a local-lane job is one of those. With them set, every app a test
    builds asks the real Mac how many commands wait and pushes a `mac_runner` event
    onto its deck stream, so a test that reads the next event off that stream gets
    the Mac link instead of the event it caused. Tests that are *about* the Mac
    runner set these themselves with `monkeypatch`, which is unaffected.
    """
    from agentdeck import mac_runner

    for name in (mac_runner.BASE_URL_ENV, mac_runner.TOKEN_ENV,
                 mac_runner.CALLBACK_TOKEN_ENV, mac_runner.STATE_ENV):
        os.environ.pop(name, None)


def _pin_host_shape() -> None:
    """Never let the size or load of the host running the tests decide a lane (#3614).

    The job queue sends the agentdeck checkout's suites here first on a host with enough
    cores, and holds local jobs while it is short of memory. Both read the real machine,
    so the same test would pass on an idle eight-thread host and fail on a busy pod or the
    twelve-thread one. Pinned here to today's order and no memory floor; tests that are
    *about* either set the variable themselves with `monkeypatch`, which is unaffected.
    """
    from agentdeck import host_slots, jobs

    os.environ[jobs.LOCAL_FIRST_MIN_CORES_ENV] = str(1 << 20)
    os.environ[host_slots.LOCAL_JOB_MIN_MEMORY_ENV] = "0"


_heavy_slot_fd: int | None = None


def _hold_heavy_slot(config: pytest.Config) -> None:
    """Take one of the host's heavy slots for this whole run (#1287).

    The backstop under `agentdeck job`: the queue caps the suites it starts, and
    this caps the ones it does not — a plain `pytest` in a fourth worktree is
    exactly how the load-28 pile-up happened, and it never touches the queue.

    Controller only. An xdist worker is a separate process and would take a slot
    of its own, so the env marker set here (which workers inherit) and the
    `workerinput` check both keep them out. The same marker is the escape hatch
    for an ad-hoc run: `AGENTDECK_HEAVY_SLOT_HELD=1 pytest -k foo` skips the wait.

    Held to `pytest_unconfigure` rather than released per test, because the thing
    being capped is the run, and a run that gave its slot back between tests
    would let four suites interleave right back to where this started.
    """
    global _heavy_slot_fd
    if hasattr(config, "workerinput") or heavy_slot_is_held():
        return
    _heavy_slot_fd = take_a_slot(heavy_slot_paths(), _slot_deadline())
    if _heavy_slot_fd is None:
        warnings.warn(
            f"waited {_SLOT_TIMEOUT_S:.0f}s for one of {heavy_slot_count()} heavy "
            "slots and gave up; this run is uncapped and is competing with whatever "
            "else is running on this host",
            stacklevel=2,
        )
        return
    # Inherited by xdist workers and by anything a test spawns, so nothing
    # inside this run queues behind the slot the run already holds.
    os.environ[HEAVY_SLOT_HELD_ENV] = "1"


def _slot_deadline() -> float:
    return time.monotonic() + _SLOT_TIMEOUT_S


def pytest_unconfigure(config: pytest.Config) -> None:
    global _heavy_slot_fd
    if _heavy_slot_fd is not None:
        os.close(_heavy_slot_fd)  # closing releases the flock
        _heavy_slot_fd = None
        os.environ.pop(HEAVY_SLOT_HELD_ENV, None)


# Per-test start/stop times for `agentdeck job tests <id>` (#2297). Set up by the
# process that runs tests — an xdist worker, or a run without xdist — and taken
# out of its environment once read, so a suite a test starts in a subprocess
# does not write its own tests into this run's file. The xdist controller runs
# no tests and keeps it, because that is how the workers it starts get it.
_timings: TimingRecorder | None = None


def _start_test_timings(session: pytest.Session) -> None:
    global _timings
    # From xdist's own handshake rather than PYTEST_XDIST_WORKER, which a suite
    # started by a test inherits from the worker running that test.
    workerinput = getattr(session.config, "workerinput", None)
    worker = str(workerinput.get("workerid", "")) if workerinput else ""
    if not worker and session.config.getoption("numprocesses", None):
        return
    path = os.environ.pop(TIMINGS_ENV, "")
    if path:
        # One id for the whole invocation, so the queue's progress bar can tell
        # this run from an earlier one appending to the same file (#2299).
        run = str(workerinput.get("testrunuid", "")) if workerinput else ""
        _timings = TimingRecorder(path, worker, run or uuid.uuid4().hex)


def pytest_collection_finish(session: pytest.Session) -> None:
    # After deselection, so this is the number of tests the run goes through.
    if _timings is not None:
        _timings.collected(len(session.items))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem):
    if _timings is None:
        yield
        return
    _timings.start(item.nodeid)
    try:
        yield
    finally:
        _timings.stop(item.nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    note_slow_test(report)
    if _timings is not None:
        _timings.report(report.nodeid, report.when, report.outcome)


BROWSER_SLOT_ENV = "AGENTDECK_BROWSER_SLOTS"
_SLOT_DIR = Path(tempfile.gettempdir()) / f"agentdeck-browser-slots-{os.getuid()}"
_SLOT_POLL_S = 0.05
_SLOT_TIMEOUT_S = 900.0
_open_slot_scopes = 0


def _usable_cores() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # pragma: no cover - not Linux
        return os.cpu_count() or 2


def browser_slot_count() -> int:
    """How many browser tests may run at once on this host (#760).

    `-n auto` sizes the pool for the suite as a whole, but a browser test is a
    uvicorn server plus a Chromium browser/renderer/gpu tree, and several of
    those on a four-core host starve each other until the in-page budgets
    (`< 16ms` acknowledgement, 50ms card reconciliation) start missing by a
    millisecond. Half the cores keeps the parallel win for the 2500 cheap tests
    while leaving each browser room to actually run.

    Per-host rather than per-run on purpose. See
    `.claude/codebase/deployment-testing.md` for why, and for the measurements.
    """
    raw = os.environ.get(BROWSER_SLOT_ENV, "").strip()
    if raw:
        try:
            requested = int(raw)
        except ValueError:
            raise pytest.UsageError(
                f"{BROWSER_SLOT_ENV} must be a positive integer, got {raw!r}"
            ) from None
        if requested < 1:
            raise pytest.UsageError(f"{BROWSER_SLOT_ENV} must be >= 1, got {requested}")
        return requested
    return max(1, _usable_cores() // 2)


@contextmanager
def browser_slot(timeout: float = _SLOT_TIMEOUT_S, directory: Path | None = None):
    """Hold one of `browser_slot_count()` slots for the duration of the block.

    The slots are `flock`ed files rather than anything in-process because xdist
    workers are separate processes with no shared memory — and because the
    kernel drops a `flock` when its holder dies, so a killed worker cannot
    strand a slot. The default directory is per-uid and never cleaned: the files
    are lock targets, empty and reused. `directory` is for tests of the
    mechanism itself, which must not contend with the run they are part of.

    Yields True if a slot was acquired. On timeout it warns and yields False,
    running the test uncapped — an oversubscribed browser test is a flake, but a
    suite that hangs forever on a lost slot is worse. The warning matters: a
    silently uncapped test is indistinguishable from the bug this exists to fix.
    """
    global _open_slot_scopes
    directory = _SLOT_DIR if directory is None else directory
    directory.mkdir(mode=0o700, exist_ok=True)
    paths = [directory / f"slot-{i}" for i in range(browser_slot_count())]
    deadline = time.monotonic() + timeout
    _open_slot_scopes += 1
    try:
        fd = take_a_slot(paths, deadline)
        if fd is None:
            warnings.warn(
                f"waited {timeout:.0f}s for one of {len(paths)} browser slots in "
                f"{directory} and gave up; this test ran uncapped and any timing "
                "failure from it is suspect",
                stacklevel=2,
            )
            yield False
            return
        try:
            yield True
        finally:
            # flock is released by the close; closing is the whole teardown.
            os.close(fd)
    finally:
        _open_slot_scopes -= 1


def browser_cap_is_applied() -> bool:
    """Whether this process is inside `browser_slot()` right now.

    Named for the cap rather than the slot because it is true whether or not a
    slot was *won*: what the guard test needs to know is that the fixture still
    wraps browser tests, and a run that timed out waiting is the cap working
    hard, not the cap being gone.
    """
    return _open_slot_scopes > 0


# How long a browser test may wait for the page to finish reacting to something
# the test itself just did. This is a settle budget, not a render budget: the
# work being waited on is a swap, a transition and the layout they trigger, all
# of which land within a frame or two on an idle host. Five seconds is room for
# a starved renderer, not room for a broken one. Waiting on a *server* round
# trip is a different and much larger budget — see QUESTION_RENDER_TIMEOUT_MS in
# tests/test_inject.py.
SETTLE_TIMEOUT_MS = 5_000


async def wait_until(page, step, expression, *, arg=None, timeout=SETTLE_TIMEOUT_MS):
    """Wait for `expression` to be true in `page`; blame `step` if it never is.

    Use this in place of a one-shot `assert await page.evaluate(...)` taken
    straight after a click, a check or a keypress. Such an assert is reading a
    moment: the interaction has been dispatched, but the swap, the transition
    and the layout it causes finish on later frames, so the numbers the test
    gets back are not wrong so much as early. That passes every time on an idle
    laptop and fails in the pod lane, where two browsers share the cores
    (#1826; #1308 and #1456 were the same thing twice before it).

    This is not a bigger timeout. The claim in `expression` is unchanged, so a
    real regression never becomes true and still fails the test — it just takes
    the settle budget to say so. What changes is the failure: `step` names the
    thing that did not happen, instead of `assert False` on a line of geometry.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    try:
        await page.wait_for_function(expression, arg=arg, timeout=timeout)
    except PlaywrightTimeoutError as exc:
        raise AssertionError(
            f"{step} — still not true {timeout / 1000:.0f}s after the interaction"
            f"\nwaited on: {expression.strip()}"
        ) from exc


async def assert_eventually(step, check, *, timeout=SETTLE_TIMEOUT_MS, interval=0.05):
    """Retry `check()` until its assertions hold; blame `step` if they never do.

    The sibling of `wait_until` for the other shape a browser test reads the
    page in: one `evaluate` that returns a dict of measurements, followed by
    assertions on the values. A page predicate cannot express those (a dict is
    always truthy), and rewriting them as one would throw away the actual
    numbers that make such a failure diagnosable. So retry the read and the
    assertions together, and re-raise the last real AssertionError — values and
    all — under a line naming the step.

    `check` is an async callable taking no arguments. Its return value is passed
    through, so a read can still hand its measurements back to the caller.
    """
    deadline = time.monotonic() + timeout / 1000
    while True:
        try:
            return await check()
        except AssertionError as exc:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"{step} — still not true {timeout / 1000:.0f}s after the "
                    f"interaction\nlast read: {exc}"
                ) from exc
        await asyncio.sleep(interval)


async def resized(page, width, height=800):
    """Resize the viewport and wait until the page is actually laid out at it.

    `set_viewport_size` returns once the request has been sent to the browser,
    not once the page has relaid out and its media queries have flipped. A
    measurement taken on the next line can therefore be of the *previous*
    layout, which is how a responsive test at 320px passes on an idle host and
    reports a desktop number under load (#1826).
    """
    await page.set_viewport_size({"width": width, "height": height})
    await wait_until(
        page,
        f"the page relaid out at {width}px wide",
        "(width) => window.innerWidth === width",
        arg=width,
    )


def pytest_report_header() -> str:
    """Say the cap out loud. A run that is slow because it is queueing behind
    another pytest process's browser tests looks exactly like a run that is
    slow, and the slots are host-wide, so the neighbour is often not this run.
    The directory is printed because `TMPDIR` decides how wide "host-wide" is.
    """
    return (
        f"browser slots: {browser_slot_count()} concurrent in {_SLOT_DIR} "
        f"(override with {BROWSER_SLOT_ENV}); "
        f"heavy slots: {heavy_slot_count()} whole "
        f"{'run' if heavy_slot_count() == 1 else 'runs'} at once on this host"
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Print the header ourselves when pytest would swallow it.

    `pytest_report_header` is only displayed at verbosity >= 0, and the command
    AGENTS.md tells everyone to run is `uv run pytest -q` — so the one line that
    explains why a run is queueing behind a neighbouring worktree was invisible
    in exactly the situation it exists for. Measured: a browser tier that takes
    3:35 alone takes 4:22 while sharing the cap with another run, and without
    this line that is indistinguishable from a slow machine.

    `--no-header` suppresses the hook at every verbosity, so it needs printing
    there too — someone silencing pytest's banner did not ask to be lied to
    about why their run is slow. Never both: the two conditions here are the
    exact complement of the reporter's own `showheader and not no_header`.

    Once only: xdist re-runs this in every worker, whose output is discarded
    anyway, so the controller does the printing. At sessionstart rather than
    `pytest_configure` because the terminal reporter does not exist yet then.
    """
    _start_test_timings(session)
    config = session.config
    if hasattr(config, "workerinput"):
        return
    pytest_would_show_it = getattr(config.option, "verbose", 0) >= 0 and not getattr(
        config.option, "no_header", False
    )
    if pytest_would_show_it:
        return
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:  # -p no:terminal, or a non-terminal runner
        return
    reporter.write_line(pytest_report_header())


def _reuse_route_setup_across_apps() -> None:
    """Build FastAPI's per-route setup once per process, not once per app (#3066).

    FastAPI 0.139 resolves an included router's routes lazily, per app: the first
    request an app serves builds every route's dependency analysis and pydantic
    models — 0.3 s to 0.8 s here for the deck's ~300 routes. The deck builds one
    app per process; a test run builds hundreds, and that was most of many page
    tests' time. The setup depends only on the route and how its router was
    included, except for the app it reads dependency overrides from, which each
    copy gets back as its own. Private FastAPI API: if an upgrade renames it, this
    does nothing and the suite is merely slower.
    """
    import copy

    try:
        from fastapi.routing import _EffectiveRouteContext
    except ImportError:
        return
    original = getattr(_EffectiveRouteContext, "from_api_route", None)
    if original is None or getattr(original, "_reused_across_apps", False):
        return
    built: dict[tuple, tuple[object, object]] = {}

    def from_api_route(cls, *, original_route, include_context):
        try:
            key = (
                id(original_route),
                id(include_context.included_router),
                include_context.path_for(original_route),
                tuple(include_context.tags),
                tuple(id(d.dependency) for d in include_context.dependencies),
                include_context.deprecated,
                include_context.include_in_schema,
            )
            provider = include_context.dependency_overrides_provider
        except AttributeError:
            return original(original_route=original_route, include_context=include_context)
        found = built.get(key)
        if found is not None and found[0] is original_route:
            context = copy.copy(found[1])
            context.dependency_overrides_provider = provider
            return context
        context = original(original_route=original_route, include_context=include_context)
        built[key] = (original_route, context)
        return context

    from_api_route._reused_across_apps = True  # type: ignore[attr-defined]
    _EffectiveRouteContext.from_api_route = classmethod(from_api_route)


_reuse_route_setup_across_apps()


#: Numbers every directory `_fresh_dir` makes, so none of them has to be looked up.
_FRESH_DIRS = itertools.count()


def _fresh_dir(factory: pytest.TempPathFactory, group: str, name: str) -> Path:
    """A new empty directory under this run's base temp, made with one `mkdir` (#3059).

    pytest's own `mktemp` finds the next free number by listing every entry already in
    the base temp. The suite made three of them per test, so by the end of a worker's
    share of the run each test was listing thousands of entries three times — about
    20 ms of setup for a test whose body takes well under one. A counter that lives as
    long as the process needs no listing, and the base temp is already per xdist worker.
    """
    path = factory.getbasetemp() / group / f"{name}{next(_FRESH_DIRS)}"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def tmp_path(request, tmp_path_factory) -> Path:
    """pytest's `tmp_path` — unique per test, named after it — without the listing.

    See `_fresh_dir`. This run keeps every directory, as pytest's default retention does.
    """
    return _fresh_dir(tmp_path_factory, "tests", re.sub(r"[\W]", "_", request.node.name)[:30])


@pytest.fixture(autouse=True)
def _no_live_issue_refresh(request, monkeypatch):
    """Never let a test's app run `gh` against every declared repository (#1115).

    ``create_app`` starts the issue-list refresher, and a test that declares its own
    poller board would otherwise reach GitHub. The loop stays wired so
    the app keeps exercising start/stop; only the pass itself is stubbed out.
    A test that means to exercise the refresh marks itself ``issue_refresh``.
    """
    if request.node.get_closest_marker("issue_refresh") is not None:
        return
    from agentdeck.issue_refresh import IssueRefreshService

    async def _skip(self) -> None:
        return None

    monkeypatch.setattr(IssueRefreshService, "refresh_once", _skip)


@pytest.fixture(autouse=True)
def _no_live_guesses(monkeypatch, tmp_path_factory):
    """Never let a page ask the real Claude CLI for a guess (#3367).

    New work's project guess asks Haiku once typing pauses; in a test that would spend
    this host's real account. A test of the guess sets its own runner instead.
    """
    from agentdeck import guess_cli

    async def no_guess(account, prompt):
        return None

    monkeypatch.setattr(guess_cli, "run_claude", no_guess)
    # Voice mode's fast agent (#3444) is a Haiku process too: none starts here.
    from agentdeck import voice_helper

    async def no_agent(account):
        return None

    monkeypatch.setattr(voice_helper, "start_process", no_agent)
    monkeypatch.setattr(voice_helper, "_agents", {})
    monkeypatch.setattr(voice_helper, "LOG_DIR", tmp_path_factory.mktemp("voice-log"))
    # Live prototyping's meeting records (#3569) are files under the home directory too.
    from agentdeck import live_prototype

    monkeypatch.setattr(
        live_prototype, "LIVE_DIR", tmp_path_factory.mktemp("live-prototypes")
    )


@pytest.fixture(autouse=True)
def _forget_cwd_derived_caches():
    """Start every test with the process-wide project cache empty (#1303).

    ``resolve_session_project`` remembers a cwd's answer for a minute so the
    refresh loops stop re-walking the filesystem. Tests build and rebuild
    checkouts far faster than that, and two of them can land on the same path,
    so a memo carried between tests would answer for the wrong directory.
    """
    from agentdeck.models import reset_session_project_cache

    reset_session_project_cache()
    yield
    reset_session_project_cache()


@pytest.fixture(autouse=True)
def _forget_durable_poller_reads():
    """Start every test with the poller board memo empty (#1362).

    ``read_board`` and ``release_pending_cards`` reuse their answer while the
    durable files they decode keep the same size and mtime, so a memo carried
    between tests would answer one test's board read from another's.
    """
    from agentdeck.poller_ops import forget_durable_reads

    forget_durable_reads()
    yield
    forget_durable_reads()


#: Name prefixes of the provider read loops a test can leave running. The
#: worker names each reader task after its account and key when it creates it,
#: which is what makes them findable here without the tests co-operating.
_READER_TASK_PREFIXES = ("claude-worker:",)


def leftover_reader_tasks() -> list[asyncio.Task]:
    """Provider read loops still alive on the running loop.

    Public so the regression in `tests/test_claude_worker.py` can assert on the
    same thing the fixture below acts on, rather than on a second definition of
    "leftover" that could drift from it.
    """
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name().startswith(_READER_TASK_PREFIXES)
    ]


async def collect_leftover_reader_tasks() -> list[asyncio.Task]:
    """Cancel every leftover read loop and wait for it to finish unwinding.

    Awaiting matters as much as cancelling: `Task.cancel` only schedules the
    throw, so a task cancelled and then dropped is destroyed while still
    pending and asyncio complains about it. Returns what it collected so a test
    can check that it found anything at all.
    """
    leftover = leftover_reader_tasks()
    for task in leftover:
        task.cancel()
    if leftover:
        await asyncio.gather(*leftover, return_exceptions=True)
    return leftover


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _collect_leftover_reader_tasks():
    """End read loops the finished test left running, now that loops are shared.

    A test's own event loop used to close at the end of it and take every task
    it had started with it. One loop per xdist worker (see
    `asyncio_default_test_loop_scope` in `pyproject.toml`) means it does not,
    so a worker host that was never stopped keeps reading on the loop the
    *next* test runs on — cross-test interference in waiting, and visible as
    "coroutine ignored GeneratorExit" when the coroutine is finally collected
    mid-await.

    Autouse and in this file rather than in the worker tests, because it is the
    shared loop's consequence and not any one test's bug.
    """
    yield
    await collect_leftover_reader_tasks()


@pytest.fixture(autouse=True)
def _isolate_public_origin(tmp_path, monkeypatch):
    """Keep a test's own address out of this host's real deck (#1838).

    The app records the address each request arrived on so sessions can link a
    file. A test client's requests arrive as `http://testserver`, which is not
    loopback and would otherwise be written to this host's real state file and
    handed to every session the runtime starts next.
    """
    from agentdeck.public_origin import ORIGIN_FILE_ENV, forget_recorded_origin

    monkeypatch.setenv(ORIGIN_FILE_ENV, str(tmp_path / "public-origin.json"))
    forget_recorded_origin()
    yield
    forget_recorded_origin()


@pytest.fixture(scope="session")
def _fake_agent_github_token(tmp_path_factory) -> Path:
    # Outside tmp_path, which several tests assert holds only what they wrote. No test
    # writes to it, so one file serves the whole run.
    token = tmp_path_factory.mktemp("agent-github") / "agent-github.env"
    token.write_text("AGENTDECK_AGENT_GITHUB_TOKEN=test-agent-token\n", encoding="utf-8")
    return token


@pytest.fixture(autouse=True)
def _isolate_agent_github_credential(tmp_path, _fake_agent_github_token, monkeypatch):
    """Keep this host's real GitHub authorities out of the suite (#2105, #2289).

    Every `gh` call and every git credential request is answered by the owner map
    in `github_authority`. This host has a real map, a real agent token and a real
    App key, and the fixtures are full of `eerovil/...` slugs, so without this a
    plain unit test would spend the live agent budget.

    The suite gets the built-in map (`eerovil` -> agent, everything else refused)
    and a fake agent token, so a fake `gh` about an `eerovil` repository runs while
    any other owner is refused exactly as on a real host. A test that means to
    exercise a missing credential or an App writes its own files.
    """
    from agentdeck.github_accounts import POLLER_ROOT_ENV
    from agentdeck.github_identity import (
        ACCOUNTS_FILE_ENV,
        TOKEN_CACHE_ENV,
        TOKEN_FILE_ENV,
    )
    from agentdeck.poller_engine import github_authority

    monkeypatch.setenv(TOKEN_FILE_ENV, str(_fake_agent_github_token))
    monkeypatch.setenv(
        ACCOUNTS_FILE_ENV, str(Path(__file__).with_name("fixtures") / "github-authority.toml")
    )
    monkeypatch.setenv(TOKEN_CACHE_ENV, str(tmp_path / "github-app-tokens"))
    monkeypatch.setenv(POLLER_ROOT_ENV, str(tmp_path / "absent-poller-root"))
    for name in github_authority.TOKEN_VARS + (github_authority.AUTHORITY_ENV,):
        monkeypatch.delenv(name, raising=False)
    github_authority.clear_caches()
    yield
    github_authority.clear_caches()


@pytest.fixture(autouse=True)
def _isolate_github_metadata_cache(tmp_path_factory, monkeypatch):
    """Keep this host's real GitHub metadata cache out of rendered pages (#2424).

    Every deck link to an issue is looked up there for its newest comment, and the real
    cache holds that for the very `eerovil/agentdeck` numbers the fixtures use, so a page
    test asserting an issue href would pass or fail by what this host last saw. Outside
    tmp_path, which several tests assert holds only what they wrote; a test that sets the
    variable itself still wins.
    """
    # The cache makes its own directory, and only when a test actually uses it.
    path = tmp_path_factory.getbasetemp() / "github-cache" / f"{next(_FRESH_DIRS)}.sqlite3"
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(path))


@pytest.fixture(autouse=True)
def _isolate_job_lanes_down(tmp_path, monkeypatch):
    """Keep this host's own list of lanes marked down out of the suite (#2826).

    While desktop is marked down here, a suite that names no lane defaults to this
    host, so every test that expects desktop would pass or fail by the host's file.
    """
    from agentdeck.jobs import LANES_DOWN_ENV

    monkeypatch.setenv(LANES_DOWN_ENV, str(tmp_path / "absent-job-lanes-down"))


@pytest.fixture(autouse=True)
def _isolate_runtime_socket(monkeypatch):
    """Keep this host's live runtime out of every app a test builds (#3066).

    The app's startup asks the runtime socket which workers it owns and records each
    one's session token. Left to default, a test app on this host read the real
    runtime's every live chat into its database — about a minute a test while chats
    were running, and nothing at all on a pod. A path that cannot exist, and short,
    so it stays under the unix-socket length limit; a test that sets the variable
    itself still wins.
    """
    monkeypatch.setenv("AGENTDECK_CODEX_SOCKET", "/nonexistent/agentdeck-test-runtime.sock")


@pytest.fixture(autouse=True)
def _isolate_deck_database(tmp_path_factory, monkeypatch):
    """Keep this host's live deck database out of every default config (#3153).

    `AppConfig()` with no history section names the live deck's database, and opening
    it runs migrations and pruning there. On this host that raced the running deck for
    the write lock (`database is locked`); on a pod there is no such file. Each test
    gets its own file instead, outside tmp_path, which several tests assert holds only
    what they wrote; a test that names a path itself still wins.
    """
    path = tmp_path_factory.getbasetemp() / "deck-db" / f"{next(_FRESH_DIRS)}.db"
    path.parent.mkdir(exist_ok=True)
    monkeypatch.setattr("agentdeck.default_db.DEFAULT_DB_PATH", str(path))


@pytest.fixture(autouse=True)
def _isolate_chatgpt_rate_limit(tmp_path, monkeypatch):
    """Keep this host's real ChatGPT cooldown out of the suite, both ways (#1945).

    The 429 breaker lives in a file beside the browser profile, and the nudge view, the worker
    check and the auto-nudge tick all read it by default. Without this, a host that is genuinely
    being rate limited would fail every test that renders a nudge row — and worse in the other
    direction, a test that records a 429 would stop the real deck's whole ChatGPT fleet for an
    hour. Autouse so a new caller of the breaker cannot forget it.
    """
    try:
        from agentdeck.chatgpt_breaker import ENV_PATH
    except ImportError:  # the public copy leaves ChatGPT out (#3623)
        return
    monkeypatch.setenv(ENV_PATH, str(tmp_path / "chatgpt-rate-limit.json"))


@pytest.fixture(autouse=True)
def _cap_concurrent_browser_tests(request):
    """Serialize browser-marked tests down to `browser_slot_count()` at a time.

    Autouse so the cap cannot be forgotten on a new browser test, and keyed on
    the same `browser` marker `tests/test_browser_tier.py` already keeps honest.
    """
    if request.node.get_closest_marker("browser") is None:
        yield
        return
    with browser_slot():
        yield


# ------------------------------------------------------------------ #1834
# One Playwright driver and one Chromium per xdist worker.
#
# Every browser test used to open `async_playwright()` and `chromium.launch()`
# for itself, and both are paid inside the browser slot the test is holding —
# so the cost is not just per test, it is per test times the whole serialized
# browser tier. Measured on an idle four-core host: 0.45s to start the Node
# driver, 0.08s to launch Chromium, 0.03s to close it, against 0.05s to open a
# fresh *context* on a browser that is already running. Sharing the expensive
# two and keeping the cheap one per test takes ~0.56s off each of ~190 browser
# tests without giving up any isolation that matters: a context is Chromium's
# own boundary for cookies, storage, permissions and service workers, which is
# everything these tests set up.
#
# Sharing across tests needs the tests to share an event loop, because a
# Playwright connection is pinned to the loop that opened it. `_browser_tests_
# share_one_event_loop` below asks pytest-asyncio for a session-scoped loop for
# browser-marked tests only; everything else keeps its own per-test loop.


def _resolve_browser_marker(definition) -> bool:
    try:
        return definition.get_closest_marker("browser") is not None
    except AttributeError:  # pragma: no cover - not a function definition
        return False


#: Set for the life of the session by `_shared_chromium_browser` below.
_shared_chromium: dict[str, object] = {}

#: The fixture browser tests are made to request, so the shared browser is
#: created and destroyed by pytest rather than by whichever test got there
#: first. The name is in one place because `pytest_generate_tests` injects it.
_SHARED_BROWSER_FIXTURE = "_shared_chromium_browser"


#: Tests over the 0.5 s per-test cap, skipped until their fix issue lands (#3046). Browser
#: tests are skipped too, by marker. `AGENTDECK_RUN_TOO_SLOW=1` runs both, and
#: `--gate-skipped` runs only them: the nightly run on master (#3054).
_TOO_SLOW_LIST = Path(__file__).with_name("too_slow.txt")
_GATE_SKIPPED = "--gate-skipped"


def pytest_addoption(parser) -> None:
    # An option rather than the environment variable, because a queued job runs with the
    # runtime's environment, not the submitter's, and a pod gets neither (#3054).
    parser.addoption(
        _GATE_SKIPPED,
        action="store_true",
        help="run only the tests the per-change gate skips: browser tests and tests/too_slow.txt",
    )


def _too_slow_ids() -> set[str]:
    lines = _TOO_SLOW_LIST.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def _skip_too_slow(config, items) -> None:
    too_slow = _too_slow_ids()
    # The tests the gate skips are exempt from the per-test time limits however they come to
    # run — nightly, `--gate-skipped` or `AGENTDECK_RUN_TOO_SLOW=1` (#3053).
    for item in items:
        if item.nodeid in too_slow or _resolve_browser_marker(item):
            item.stash[_CAP_EXEMPT] = True
    if config.getoption(_GATE_SKIPPED, default=False):
        kept, dropped = [], []
        for item in items:
            (kept if item.stash.get(_CAP_EXEMPT, False) else dropped).append(item)
        if dropped:
            config.hook.pytest_deselected(items=dropped)
            items[:] = kept
        return
    if os.environ.get("AGENTDECK_RUN_TOO_SLOW") == "1":
        return
    marker = pytest.mark.skip(reason="over the 0.5 s per-test cap; see tests/too_slow.txt (#3046)")
    # Browser tests are all over the cap; they run nightly on master instead (#3054).
    browser = pytest.mark.skip(reason="browser tests run nightly on master, not per change (#3054)")
    for item in items:
        if item.nodeid in too_slow:
            item.add_marker(marker)
        elif _resolve_browser_marker(item):
            item.add_marker(browser)


#: Seconds a non-browser test may take before it fails the run, and before it is named in the
#: run's summary as close to failing (#3053). Only a pod sets them (`scripts/kube_pytest.sh`),
#: because they were measured on pods and this host's local lane runs the same tests several
#: times slower. Unset or empty, nothing is measured.
#:
#: Two numbers, not one: thousands of tests sit near 0.5 s, so a 0.5 s failure picked a
#: different handful of them in every full run. Failing at 1 s and naming the ones over 0.5 s
#: keeps the run steady and the slow tests visible. Even 1 s was hit by random stalls, so a
#: test over it is timed once more and fails only if both runs are over.
TIME_CAP_ENV = "AGENTDECK_TEST_TIME_CAP"
TIME_WARN_ENV = "AGENTDECK_TEST_TIME_WARN"
_CAP_EXEMPT = pytest.StashKey[bool]()
_LIMITS = pytest.StashKey[tuple[float | None, float | None]]()
_TIMINGS = pytest.StashKey[list[tuple[float, float]]]()
_SLOW_PROPERTY = "agentdeck_over_time_warning_s"
_slow_tests: dict[str, float] = {}


def seconds_from_env(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        raise pytest.UsageError(f"{name} must be a number of seconds, got {raw!r}") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise pytest.UsageError(f"{name} must be above 0, got {raw!r}")
    return seconds


def cpu_wait_s() -> float:
    """Seconds this thread has been ready to run but waiting for a CPU.

    The second field of the kernel's per-thread scheduler statistics. 0.0 where
    the kernel does not keep them, which leaves the cap measuring plain wall time.
    """
    try:
        return int(Path("/proc/thread-self/schedstat").read_text().split()[1]) / 1e9
    except (OSError, IndexError, ValueError):
        return 0.0


def unloaded_s(wall: float, waited: float) -> float:
    """How long a test's body would take with the pod to itself.

    Only the body counts — fixtures set up and torn down around it do not, so a
    shared fixture's first build is not charged to whichever test got it first.
    And the time it sat waiting for a CPU behind the run's other workers is taken
    off, so `-n auto` on a busy pod does not push a test over by itself.
    """
    return wall - max(waited, 0.0)


def over_time_cap(nodeid: str, timings: list[tuple[float, float]], cap: float) -> str | None:
    """Why a test fails the cap, given each run's (wall, waited-for-a-CPU) seconds.

    It fails only when every run was over: a test over the cap is run once more at
    once, because one stall on the pod's shared machine can make a 0.1 s test take
    seconds without it waiting for a CPU at all, and two in a row are rare (#3053).
    """
    took = [unloaded_s(wall, waited) for wall, waited in timings]
    if not took or min(took) <= cap:
        return None
    runs = ", then ".join(f"{t:.2f} s" for t in took)
    return (
        f"{nodeid} took {runs}, over the {cap:g} s per-test cap each time (#3053): "
        f"make it faster, fold it into a test that already exists, or delete it. "
        f"(Wall time {', '.join(f'{w:.2f} s' for w, _ in timings)}, of which "
        f"{', '.join(f'{q:.2f} s' for _, q in timings)} waiting for a CPU.)"
    )


#: What a second run may raise without failing the test: anything a test can, including
#: pytest's own outcomes (`pytest.fail`, `pytest.skip`, "DID NOT RAISE"), which are not
#: `Exception`s. Pluggy re-raises a wrapper's error into the call, so one escaping here
#: would report a test that passed as failed or skipped.
_RERUN_RAISES = (Exception, pytest.fail.Exception, pytest.skip.Exception, pytest.xfail.Exception)


def retimed(run) -> tuple[float, float] | None:
    """Run a test body once more and time it; None when that run raised.

    A second run that raises proves nothing about the test's speed — its fixtures
    were built for one run — so the caller never fails the test on it.
    """
    before, start = cpu_wait_s(), time.perf_counter()
    try:
        run()
    except _RERUN_RAISES:
        return None
    return time.perf_counter() - start, cpu_wait_s() - before


# `trylast`, so this wraps the body inside pytest's own output capture: the second run then
# sees the capture `capsys` and `capfd` read, as the first did, rather than none at all.
@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_call(item: pytest.Item):
    # Read before the body runs, so a test that sets a variable itself does not move its own limit.
    limits = (None, None)
    if not item.stash.get(_CAP_EXEMPT, False):
        limits = (seconds_from_env(TIME_CAP_ENV), seconds_from_env(TIME_WARN_ENV))
    if limits == (None, None):
        yield
        return
    before, start = cpu_wait_s(), time.perf_counter()
    outcome = yield
    timings = [(time.perf_counter() - start, cpu_wait_s() - before)]
    cap = limits[0]
    if outcome.excinfo is None and cap is not None and unloaded_s(*timings[0]) > cap:
        # Once more, with the same fixtures.
        again = retimed(item.runtest)
        if again is None:
            limits = (None, limits[1])
        else:
            timings.append(again)
    item.stash[_LIMITS] = limits
    item.stash[_TIMINGS] = timings


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    if call.when != "call" or not report.passed or _LIMITS not in item.stash:
        return
    cap, warn = item.stash[_LIMITS]
    timings = item.stash[_TIMINGS]
    reason = over_time_cap(item.nodeid, timings, cap) if cap is not None else None
    took = min(unloaded_s(wall, waited) for wall, waited in timings)
    if reason is not None:
        report.outcome = "failed"
        report.longrepr = reason
    elif warn is not None and took > warn:
        # A user property, because it is what xdist carries from a worker to the controller.
        report.user_properties.append((_SLOW_PROPERTY, took))


def note_slow_test(report: pytest.TestReport) -> None:
    for name, value in report.user_properties:
        if name == _SLOW_PROPERTY:
            _slow_tests[report.nodeid] = float(value)


def slow_test_summary(slow: dict[str, float], warn: float, cap: float | None) -> list[str]:
    if not slow:
        return []
    # Not "under the cap": a test over it whose second run raised is listed here, not failed.
    limit = f" without failing the {cap:g} s cap" if cap is not None else ""
    tests = "1 test" if len(slow) == 1 else f"{len(slow)} tests"
    lines = [f"{tests} took over {warn:g} s{limit} (#3053). Make them faster:"]
    slowest_first = sorted(slow.items(), key=lambda kv: -kv[1])
    lines += [f"  {took:.2f} s  {nodeid}" for nodeid, took in slowest_first]
    return lines


def pytest_terminal_summary(terminalreporter) -> None:
    warn = seconds_from_env(TIME_WARN_ENV)
    if warn is None:
        return
    lines = slow_test_summary(_slow_tests, warn, seconds_from_env(TIME_CAP_ENV))
    if lines:
        terminalreporter.section("slow tests")
        for line in lines:
            terminalreporter.write_line(line)


def pytest_collection_modifyitems(config, items) -> None:
    """Make every browser-marked test request the shared browser.

    By name rather than by signature, so ~190 tests keep the parameters they
    have, and by marker rather than autouse, so a run with no browser test in
    it never starts a browser at all. The loop they share is not arranged here
    — `asyncio_default_test_loop_scope` in `pyproject.toml` does that, because
    pytest-asyncio has already read the loop scope by the time any conftest
    hook could set it.
    """
    _skip_too_slow(config, items)
    for item in items:
        if not _resolve_browser_marker(item):
            continue
        if _SHARED_BROWSER_FIXTURE not in item.fixturenames:
            item.fixturenames.append(_SHARED_BROWSER_FIXTURE)


@pytest_asyncio.fixture(loop_scope="session")
async def _shared_chromium_browser(_chromium_for_the_whole_session):
    """The shared browser, swept clean after each test that used it.

    This is the per-test half, and it is not optional bookkeeping. A browser
    that used to die with its test took every context, page and renderer with
    it; a browser that outlives the test does not, so a test that returns
    without closing what it opened — one that fails early, or simply never
    calls `browser.close()` — would leak a context for the rest of the run and
    leave the last tests sharing a browser holding a hundred of them. Sweeping
    here rather than trusting call sites is what keeps the shared browser as
    cheap at the end of the tier as it is at the start.
    """
    browser = _chromium_for_the_whole_session
    try:
        yield browser
    finally:
        for context in list(browser.contexts):
            try:
                await context.close()
            except Exception:  # pragma: no cover - already gone with its test
                pass


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _chromium_for_the_whole_session():
    """Start the worker's driver and Chromium, and shut them down with the run.

    The fixture owns the whole lifetime on purpose. Creating the driver lazily
    from inside whichever test asked for it first and closing it from the
    fixture does not work: pytest-asyncio runs a fixture body in a *copied*
    context and reuses that same context for the finalizer, so a Playwright
    connection opened under the test's context hangs forever when it is closed
    under the fixture's. Owning both ends keeps them in one context.

    Closing explicitly rather than letting the worker process exit take it also
    matters — an orphaned Node driver outliving its pytest run is a documented
    source of protocol errors in the *next* one (see deployment-testing.md).
    """
    from playwright.async_api import async_playwright as _real_async_playwright

    async with _real_async_playwright() as playwright:
        # Every call site in this suite launches with no arguments, and
        # `headless=True` is Playwright's own default, so one browser serves
        # all of them.
        browser = await playwright.chromium.launch()
        _shared_chromium.update(playwright=playwright, browser=browser)
        try:
            yield browser
        finally:
            _shared_chromium.clear()
            await browser.close()


class _SharedBrowser:
    """The shared Chromium, with `close()` narrowed to this test's own contexts.

    Everything else proxies straight through, so a test that reaches past
    `new_page`/`new_context` for something else on the browser still gets the
    real object. `new_page` and `new_context` go through the real methods —
    which `pytest_configure` has already wrapped for the service-worker and
    chat-list defaults — and only remember what they opened, so that a test's
    `browser.close()` disposes of exactly that test's state and leaves the
    browser running for the next one.
    """

    def __init__(self, browser):
        self._browser = browser
        self._contexts: list = []

    def __getattr__(self, name):
        return getattr(self._browser, name)

    async def new_page(self, *args, **kwargs):
        page = await self._browser.new_page(*args, **kwargs)
        self._contexts.append(page.context)
        return page

    async def new_context(self, *args, **kwargs):
        context = await self._browser.new_context(*args, **kwargs)
        self._contexts.append(context)
        return context

    async def close(self, *args, **kwargs):
        contexts, self._contexts = self._contexts, []
        for context in contexts:
            try:
                await context.close()
            except Exception:  # pragma: no cover - already gone with its page
                pass


class _SharedBrowserType:
    """`playwright.chromium`, whose `launch()` hands back the shared browser."""

    def __init__(self, browser_type):
        self._browser_type = browser_type

    def __getattr__(self, name):
        return getattr(self._browser_type, name)

    async def launch(self, **kwargs):
        if kwargs and set(kwargs) != {"headless"}:
            # An unusual launch is nobody else's to share; give it a real one.
            return await self._browser_type.launch(**kwargs)
        return _SharedBrowser(_shared_chromium["browser"])


class _SharedPlaywright:
    def __init__(self, playwright):
        self._playwright = playwright

    def __getattr__(self, name):
        return getattr(self._playwright, name)

    @property
    def chromium(self):
        return _SharedBrowserType(self._playwright.chromium)


@asynccontextmanager
async def async_playwright():
    """A drop-in `playwright.async_api.async_playwright` that reuses the driver.

    Browser tests import this name from `tests.conftest` instead, and otherwise
    read exactly as they did: `async with async_playwright() as playwright`,
    then `await playwright.chromium.launch()`, then `browser.close()`. Keeping
    the call shape is deliberate — `tests/test_browser_tier.py` finds browser
    tests by looking for those very calls in the source, so the hygiene tests
    that keep the `browser` marker honest go on working untouched.
    """
    playwright = _shared_chromium.get("playwright")
    if playwright is None:
        # No shared browser to hand out — an unmarked caller, or a run that
        # reached this outside the fixture. Behave exactly like the real thing
        # rather than failing in a way that looks like a product bug.
        from playwright.async_api import async_playwright as _real_async_playwright

        async with _real_async_playwright() as own:
            yield own
        return
    yield _SharedPlaywright(playwright)


def _proc_stat_line(pid: int, starttime: str) -> str:
    """A /proc/<pid>/stat line whose field 22 (starttime) == ``starttime``.

    registry.proc_starttime splits after the last ')', so index 19 of the
    remainder is field 22. We build 30 placeholder fields and pin index 19.
    """
    fields = [str(i) for i in range(30)]
    fields[0] = "S"  # field 3: state
    fields[19] = starttime  # field 22: starttime
    return f"{pid} (claude) " + " ".join(fields)


@pytest.fixture
def make_proc(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()

    def _make(pid: int, starttime: str) -> None:
        d = root / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(_proc_stat_line(pid, starttime))

    _make.root = root  # type: ignore[attr-defined]
    return _make


@pytest.fixture
def fake_config_dir(tmp_path):
    """Build a minimal synthetic $CLAUDE_CONFIG_DIR and return its path.

    Contains: one live-registered session, one idle transcript-only session,
    a history.jsonl with titles, and a project transcript file.
    """
    cfg = tmp_path / "cfg"
    (cfg / "sessions").mkdir(parents=True)
    (cfg / "projects" / "-tmp-proj").mkdir(parents=True)

    live_sid = "11111111-1111-1111-1111-111111111111"
    idle_sid = "22222222-2222-2222-2222-222222222222"

    # live registry entry (pid 4242, starttime "19")
    (cfg / "sessions" / "4242.json").write_text(
        json.dumps(
            {
                "pid": 4242,
                "sessionId": live_sid,
                "cwd": "/tmp/proj",
                "startedAt": "2026-07-03T08:00:00Z",
                "procStart": "19",
                "version": "2.1.198",
                "kind": "interactive",
                "entrypoint": "sdk-cli",
            }
        )
    )
    # a malformed registry file (must be skipped, never fatal)
    (cfg / "sessions" / "bad.json").write_text("{not json")

    # transcripts for both sessions
    for sid in (live_sid, idle_sid):
        line = {"type": "user", "sessionId": sid, "message": {"role": "user", "content": "hi"}}
        (cfg / "projects" / "-tmp-proj" / f"{sid}.jsonl").write_text(json.dumps(line) + "\n")

    # history.jsonl: title = first prompt, last_prompt = most recent
    lines = [
        {"display": "First prompt", "project": "/tmp/proj", "sessionId": live_sid, "timestamp": 1},
        {"display": "Second prompt", "project": "/tmp/proj", "sessionId": live_sid, "timestamp": 2},
        {"display": "Idle title", "project": "/tmp/proj", "sessionId": idle_sid, "timestamp": 3},
        "{ malformed",
    ]
    (cfg / "history.jsonl").write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines)
    )

    cfg_obj = type("Cfg", (), {})()
    cfg_obj.path = cfg
    cfg_obj.live_sid = live_sid
    cfg_obj.idle_sid = idle_sid
    return cfg_obj


@pytest.fixture
def write_credentials():
    def _write(config_dir: Path, token: str) -> None:
        (config_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": token}})
        )

    return _write
