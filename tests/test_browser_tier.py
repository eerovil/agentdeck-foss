"""Guard the three things that make the parallel suite fast *and* trustworthy:
the `browser` marker (#754), the service-worker default (#754), and the cap on
how many browser tests run at once (#760).

`-m "not browser"` is the tier you run while coding: it drops ~120 Playwright
tests that are ~4% of the suite's count and ~88% of its wall clock. That is only
safe if the marker actually tracks which tests drive a browser — an unmarked
Playwright test makes the fast tier slow, and a wrongly marked one silently
stops running there.

The full suite runs in parallel, which gives every xdist worker a cold Chromium
profile and so multiplies the service-worker activation reload that used to flake
browser tests one at a time. `conftest.pytest_configure` blocks the worker by
default; the tests here prove that default is still in force against a real
browser, because a silently-lapsed default reads as flake, not as a broken guard.

The same is true of the slot cap: it is an autouse fixture keyed on the marker,
so nothing fails loudly if it stops applying — the suite just goes back to
failing a different browser test every run. The last test holds a real browser
open and asserts the cap is wrapping it while it does.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path

import conftest
import pytest
import uvicorn
from conftest import async_playwright
from playwright.async_api import Browser

_TESTS_DIR = Path(__file__).parent
_PLAYWRIGHT_CALLS = ("async_playwright", "chromium.launch")
_MARKER = "browser"


def _marker_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = set()
    for decorator in node.decorator_list:
        # @pytest.mark.browser, and @pytest.mark.parametrize(...) alike
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


@cache
def _test_functions() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    """Every top-level test function in tests/, with the source it can reach.

    A test's own body is not the whole story: several of them drive the browser
    through a module-level helper (`_deployment_pill_metrics`, say, which every
    deploy-pill state shares). So the text each test is judged on is its own body
    plus the bodies of the same-file helpers it reaches, transitively — otherwise a
    perfectly ordinary extraction would read as "marked but never launches one".

    Parsing ~60 files (test_web.py alone is 10k lines) costs a few seconds, so
    the three guards below share one pass.
    """
    found = []
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        source = path.read_text()
        lines = source.splitlines()
        functions = [
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        # Sliced rather than ast.get_source_segment, which rescans the whole file
        # per call and turns this into a quadratic walk of test_web.py.
        bodies = {
            node.name: "\n".join(lines[node.lineno - 1 : node.end_lineno])
            for node in functions
        }
        references = {node.name: _referenced_names(node) for node in functions}
        helpers = {name for name in bodies if not name.startswith("test_")}
        for node in functions:
            if not node.name.startswith("test_"):
                continue
            found.append(
                (path.name, node, _reachable_source(node.name, bodies, references, helpers))
            )
    return found


def _referenced_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every same-file name this function could actually reach.

    Loaded names cover calls and callables passed around; parameter names cover
    fixtures, which pytest resolves by matching the parameter to a function of the
    same name. Read off the AST rather than by substring, so a helper merely named
    in a comment or a docstring does not count: the reverse guard below fails a
    test that carries the marker without launching anything, and a text match would
    let a stray mention satisfy it.
    """
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    return names | {arg.arg for arg in node.args.args + node.args.kwonlyargs}


def _reachable_source(
    name: str, bodies: dict[str, str], references: dict[str, set[str]], helpers: set[str]
) -> str:
    """`name`'s own body plus every same-file helper it reaches, transitively."""
    seen = {name}
    parts = [bodies[name]]
    pending = [name]
    while pending:
        for reached in sorted(references[pending.pop()] & helpers):
            if reached in seen:
                continue
            seen.add(reached)
            parts.append(bodies[reached])
            pending.append(reached)
    return "\n".join(parts)


def _drives_a_browser(body: str) -> bool:
    return any(call in body for call in _PLAYWRIGHT_CALLS)


def test_there_are_test_functions_to_check():
    assert len(_test_functions()) > 500


def test_every_playwright_test_is_marked_browser():
    unmarked = [
        f"{name}::{node.name}"
        for name, node, body in _test_functions()
        if _drives_a_browser(body) and _MARKER not in _marker_names(node)
    ]
    assert not unmarked, (
        "these tests launch a browser but are missing @pytest.mark.browser, so "
        f"`-m 'not browser'` would run them: {unmarked}"
    )


def test_no_test_claims_the_browser_marker_without_using_one():
    mismarked = [
        f"{name}::{node.name}"
        for name, node, body in _test_functions()
        if _MARKER in _marker_names(node) and not _drives_a_browser(body)
    ]
    assert not mismarked, (
        "these tests are marked @pytest.mark.browser but never launch one, so "
        f"they are skipped by the fast tier for no reason: {mismarked}"
    )


@asynccontextmanager
async def _serving_a_page_that_registers_a_worker():
    """A two-route ASGI app: a page that registers a worker, and a worker that
    claims the page the way `static/sw.js` does — claiming is what fires
    `controllerchange`, and that reload is the flake being prevented."""
    page = (
        b"<!doctype html><body><script>"
        b"navigator.serviceWorker.register('/sw.js')"
        b".finally(() => document.body.dataset.registered = 'settled')"
        b"</script>"
    )
    worker = (
        b"self.addEventListener('install', () => self.skipWaiting());"
        b"self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));"
        b"self.addEventListener('fetch', () => {});"
    )

    async def app(scope, receive, send):
        if scope["type"] != "http":  # pragma: no cover - no websockets here
            return
        if scope["path"] == "/sw.js":
            body, content_type = worker, b"text/javascript"
        else:
            body, content_type = page, b"text/html"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", content_type)],
            }
        )
        await send({"type": "http.response.body", "body": body})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await task
        sock.close()


def test_the_service_worker_default_is_installed_on_the_real_playwright_api():
    for name in ("new_page", "new_context"):
        method = getattr(Browser, name)
        assert getattr(method, "_blocks_service_workers", False), (
            f"Browser.{name} is not wrapped, so cold-profile browser tests are "
            "exposed to the activation reload again"
        )
        # A Playwright upgrade that renames or drops the kwarg would otherwise
        # only show up as the flake coming back.
        assert "service_workers" in inspect.signature(method._unwrapped).parameters


@pytest.mark.browser
@pytest.mark.serviceworker
async def test_no_service_worker_claims_a_page_that_did_not_ask_for_one():
    async with (
        _serving_a_page_that_registers_a_worker() as base,
        async_playwright() as playwright,
    ):
        browser = await playwright.chromium.launch()
        try:
            # Playwright's "block" lets register() resolve and installs nothing,
            # so the registration count — not the promise — is the observable.
            blocked = await browser.new_page()
            await blocked.goto(base)
            await blocked.wait_for_function("document.body.dataset.registered === 'settled'")

            # The same page with the default overridden: proves the emptiness
            # above is the harness talking, not a page that never had a worker.
            allowed = await browser.new_page(service_workers="allow")
            await allowed.goto(base)
            await allowed.wait_for_function("navigator.serviceWorker.controller !== null")

            assert (
                await blocked.evaluate(
                    "navigator.serviceWorker.getRegistrations().then(r => r.length)"
                )
                == 0
            )
            assert await blocked.evaluate("navigator.serviceWorker.controller === null")
        finally:
            await browser.close()


def test_the_browser_slot_cap_leaves_room_on_a_small_host(monkeypatch):
    """The default must cap something and must not cap everything.

    Asserts the two properties rather than the expression: a default equal to
    the xdist pool caps nothing at all, and one below 1 would deadlock the tier.
    Reads the default rather than whatever this run was given, so a run that
    overrides the cap to measure something still guards the default.
    """
    monkeypatch.delenv(conftest.BROWSER_SLOT_ENV, raising=False)
    cap, cores = conftest.browser_slot_count(), conftest._usable_cores()
    assert cap >= 1
    if cores > 1:
        assert cap < cores, (
            f"a cap of {cap} on {cores} cores is the xdist pool size, so it caps nothing"
        )


def test_the_browser_slot_cap_is_overridable(monkeypatch):
    monkeypatch.setenv(conftest.BROWSER_SLOT_ENV, "3")
    assert conftest.browser_slot_count() == 3
    for bad in ("0", "-1", "two", "1.5"):
        monkeypatch.setenv(conftest.BROWSER_SLOT_ENV, bad)
        with pytest.raises(pytest.UsageError):
            conftest.browser_slot_count()


def test_a_browser_slot_excludes_the_next_taker(monkeypatch, tmp_path):
    """With one slot, the second caller waits — which is the whole mechanism.

    Its own slot directory, so this never contends with the browser tests of the
    run it is part of.
    """
    monkeypatch.setenv(conftest.BROWSER_SLOT_ENV, "1")
    with conftest.browser_slot(directory=tmp_path) as first:
        assert first is True
        assert conftest.browser_cap_is_applied()
        # Same process, but a flock belongs to the open file description, so a
        # second os.open is a genuine second taker and not a re-entrant no-op.
        # Giving up says so out loud rather than quietly running uncapped.
        with pytest.warns(UserWarning, match="browser slots"):
            with conftest.browser_slot(timeout=0.2, directory=tmp_path) as second:
                assert second is False, "a second caller took a slot that was already held"
    assert not conftest.browser_cap_is_applied()
    with conftest.browser_slot(timeout=0.2, directory=tmp_path) as after:
        assert after is True, "the slot was not released"


# Any test file will do for the header tests below; this one imports nothing heavy.
_LIGHT_TEST_FILE = "test_kube_watchdog.py"


def test_the_browser_slot_cap_announces_itself_under_q():
    """The cap must say itself out loud under `-q`, not just at full verbosity.

    `-q` is the command AGENTS.md tells everyone to run, and pytest hides
    `pytest_report_header` below verbosity 0 — so the plain hook alone was
    invisible in exactly the situation it exists for: a run queueing behind a
    neighbouring worktree's suite looks identical to a slow machine.

    A real subprocess because the subject is pytest's own header suppression,
    which cannot be observed from inside an already-running session.
    `--collect-only -n0` starts no browser, and it collects a small file with no
    browser imports of its own, since the header is printed before collection and
    this file's Playwright import would only double the cost of each run.

    `PYTEST_ADDOPTS` is cleared rather than inherited: anything in it that
    raises verbosity back to 0 (a stray `-v`) would make pytest's own header
    print the line, and the assertion would then pass with `pytest_sessionstart`
    deleted — which is the one regression this test exists to catch.
    """
    quiet = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n0",
            "--collect-only",
            "-p",
            "no:cacheprovider",
            _LIGHT_TEST_FILE,
        ],
        cwd=_TESTS_DIR,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert "browser slots:" in quiet.stdout, (
        f"the cap did not announce itself under -q:\n{quiet.stdout}\n{quiet.stderr}"
    )
    assert quiet.stdout.count("browser slots:") == 1, quiet.stdout
    # ...and still exactly once where pytest shows the hook itself. The two
    # printers must stay complementary; a doubled banner is the failure mode of
    # getting that condition wrong, and nothing else would notice it.
    loud = subprocess.run(
        [sys.executable, "-m", "pytest", "-n0", "--collect-only", _LIGHT_TEST_FILE],
        cwd=_TESTS_DIR,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert loud.stdout.count("browser slots:") == 1, loud.stdout


@pytest.mark.browser
async def test_a_browser_test_runs_while_holding_a_slot():
    """Evidence that the autouse fixture really wraps browser-marked tests.

    This test is marked and does launch a browser, so it is subject to exactly
    the fixture every other browser test is subject to. It asks whether the
    fixture *ran*, not whether it won a slot: giving up after the timeout is the
    cap working hard on a busy host, and asserting on the win would turn that
    into a false report that the cap had been removed.
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            assert conftest.browser_cap_is_applied(), (
                "this browser-marked test is not inside browser_slot(), so the "
                "autouse cap in tests/conftest.py has stopped applying"
            )
        finally:
            await browser.close()
