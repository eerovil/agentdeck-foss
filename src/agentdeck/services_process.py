"""The services process: the Work router, the merge release and the backstop (#4016).

``agentdeck services`` runs this as ``agentdeck-services.service``. It serves one Unix
socket (``services_link.services_socket_path``) with two routes: ``POST /call/<target>/<name>``
for the web's acts (a Merge press, a verdict, a review request) and ``GET /changes``, the held
change feed the web follows (``runtime_feed.ChangeFeed``, kinds in ``services_link.KINDS``).

It builds the three services with ``work_services.build`` over:

- an ``AppState`` of its own on its own ``agentdeck.db`` connection, whose live chats are the
  web's copy (``session_copy``), followed from the web's ``/internal/services/sessions``;
- hooks that call the web's internal routes (``web.routes_services``) for what only the web
  can do, each carrying the key in ``caller.key`` (:func:`caller_key_path`);
- messages written straight into the scheduled-messages table, which the web delivers.

A freeze here delays merges and reviews, never a page; the loop watchdog runs here too, so
Sentry still hears of it, tagged ``services``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import agent_review, runtime_feed, session_copy, work_published, work_services
from .config import AppConfig
from .db import open_db
from .issue_context import IssueResolver
from .providers import PROVIDERS
from .providers.codex.runtime_client import runtime_socket_path
from .scheduled_messages import ScheduledMessage
from .services_link import KINDS, REVIEWS, SCHEDULED, WAKE_ON, WORK_INPUTS
from .state import REVIEWS_TOPIC, AppState
from .webhook_tailer import WebhookWatcher

log = logging.getLogger(__name__)

#: The web's internal routes, and the header that proves the caller is this process.
WEB_PREFIX = "/internal/services"
KEY_HEADER = "X-AgentDeck-Services-Key"
#: How long a start may take: the 30 s worktree claim plus the runtime's accept (#4013).
START_TIMEOUT_S = 90.0
#: Opening a ChatGPT review chat reads and writes GitHub several times.
REVIEW_TIMEOUT_S = 120.0
OTHER_TIMEOUT_S = 30.0
#: Reconnect backoff while the web cannot be reached (it is reloading, usually).
# How long a load of a let-go Work's chats waits for the copy to hold them.
LOAD_WAIT_S = 5.0
RECONNECT_MIN_S = 0.5
#: How long a call from the web waits for a whole chat copy before it is answered "restarting"
#: (#4738). Under the web's ``services_link.CONNECT_WAIT_S``, and long enough for the first
#: read after a start, which took under a second with the web up.
COPY_WAIT_S = 8.0
#: The held read of the chat copy while it is not whole: the web publishes nothing when its
#: first scans end, so a long read would learn of it only at its keepalive (#4738).
COPY_POLL_S = 1.0
#: The calls the web sends without waiting for an answer (``services_link.Later``): a refusal
#: reaches nobody and the call is dropped, so they run on the copy there is, as before #4738.
UNWAITED = frozenset(
    {
        ("router", "note_filed"),
        ("router", "note_registered"),
        ("router", "note_published"),
        ("release", "star"),
    }
)
RECONNECT_MAX_S = 5.0


def web_base_url(config: AppConfig) -> str:
    """This deck's own web, on its configured port: never ``AGENTDECK_URL``, which is the
    agents' address and which the user's service manager sets for every unit, the staging
    twin's included (#4016)."""
    host = config.server.bind if config.server.bind not in ("", "0.0.0.0", "::") else "127.0.0.1"
    return f"http://{f'[{host}]' if ':' in host else host}:{config.server.port}"


def caller_key_path(config: AppConfig) -> Path:
    """The key the web checks on each internal call: written fresh at every start (#4013)."""
    return config.github_webhook.store_path / "services" / "caller.key"


def write_caller_key(path: Path) -> str:
    """A new random key, owner-only, replaced atomically."""
    key = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key)
    temporary.replace(path)
    return key


class WebUnreachable(RuntimeError):
    """The web could not be reached: nothing was sent."""


class WebNoAnswer(RuntimeError):
    """The call reached the web and no answer came back."""


class WebCalls:
    """The services process's calls to the web's internal routes."""

    def __init__(self, base_url: str, key: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={KEY_HEADER: key},
            transport=transport,
            timeout=httpx.Timeout(OTHER_TIMEOUT_S, connect=5.0),
        )

    async def post(self, act: str, body: dict, *, timeout: float = OTHER_TIMEOUT_S) -> Any:
        """One call; raises :class:`WebUnreachable` or :class:`WebNoAnswer`."""
        try:
            response = await self._client.post(f"{WEB_PREFIX}/{act}", json=body, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise WebUnreachable(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise WebNoAnswer(str(exc)) from exc
        if response.status_code == 503:
            raise WebUnreachable("the web answered 503")
        if response.status_code != 200:
            raise WebNoAnswer(f"the web answered {response.status_code}")
        return response.json()

    async def get(self, path: str, params: dict, *, timeout: float) -> Any:
        response = await self._client.get(f"{WEB_PREFIX}/{path}", params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


def remote_hooks(
    config: AppConfig,
    *,
    web: WebCalls,
    state: AppState,
    db,
    feed: runtime_feed.ChangeFeed,
    scanned: Callable[[], Any],
    runtime_http: Callable[[], Any],
    wake_registry,
    copied: Callable[[tuple[str, ...], float], Any],
    complete: Callable[[], bool] = lambda: True,
) -> work_services.Hooks:
    """``Hooks`` over the web's internal routes and this process's own database.

    ``complete()`` says whether the chat copy is a whole one (``SessionFollower.complete``)."""

    async def start_work(
        repository: str, number: int, *, wait: bool = False, on_refused: bool = False, labels=()
    ) -> str:
        body = {
            "repository": repository,
            "number": number,
            "wait": wait,
            "labels": list(labels),
            "refused": bool(on_refused),
        }
        # Keyed by the Work's start row (``start_action_id``): a resend after a lost answer
        # gets "already started", never a second chat.
        for attempt in range(2):
            try:
                answer = await web.post("start_work", body, timeout=START_TIMEOUT_S)
            except WebUnreachable:
                return "the web is not reachable"
            except WebNoAnswer:
                if attempt == 0:
                    continue
                return "uncertain"
            return str(answer.get("outcome") or "uncertain")
        return "uncertain"

    async def review_chat(pull: str, work: str, *, note: str = "", again: bool = False):
        body = {"pull": pull, "work": work, "note": note, "again": again}
        try:
            answer = await web.post("review_chat", body, timeout=REVIEW_TIMEOUT_S)
        except WebUnreachable as exc:
            # Nothing was sent: asked again after the router's back-off.
            raise work_services.REVIEW_ERRORS["ReviewError"](
                f"the deck's web is not reachable ({exc})"
            ) from exc
        error = answer.get("error")
        if isinstance(error, dict):
            raise work_services.REVIEW_ERRORS.get(str(error.get("type")), RuntimeError)(
                str(error.get("message") or "")
            )
        return work_services.outcome_from(answer)

    def reviewer() -> dict:
        checkout_of = agent_review.deck_hooks(
            config,
            injector=None,
            state=state,
            accounts=config.build_accounts(),
            providers=PROVIDERS,
        )["checkout_of"]

        async def start(kind: str, cwd: Path, text: str, token: str) -> tuple[bool, str]:
            body = {"kind": kind, "cwd": str(cwd), "text": text, "token": token}
            for attempt in range(2):
                try:
                    answer = await web.post("start_reviewer", body, timeout=START_TIMEOUT_S)
                except WebUnreachable as exc:
                    return False, f"the deck's web is not reachable ({exc})"
                except WebNoAnswer:
                    if attempt == 0:
                        continue
                    # Sent with no answer: counted as started, never a second kind (#4013).
                    return True, "not confirmed"
                return bool(answer.get("started")), str(answer.get("why") or "")
            return True, "not confirmed"

        def alive(token: str) -> bool | None:
            key = state.session_key_for_token(token)
            session = state.sessions.get(key) if key else None
            if session is None:
                # A copy refilled after a web restart, before the web's first scan, lacks chats
                # that run: missing from it is not stopped (#4458).
                return None if complete() else True
            if session.shown_thinking is None:
                # The web sends ``shown_thinking`` only when it is built to (#4458): without
                # it, the chat's own ``thinking`` says whether its turn is still open.
                return bool(session.thinking)
            return bool(session.shown_thinking)

        return {"start": start, "alive": alive, "checkout_of": checkout_of}

    async def resent(act: str, body: dict) -> Any:
        for attempt in range(2):
            try:
                return await web.post(act, body)
            except WebNoAnswer:
                if attempt:
                    raise
        return None

    async def interrupt(chat_id: str) -> bool:
        try:
            answer = await resent("interrupt", {"chat_id": chat_id})
        except (WebUnreachable, WebNoAnswer):
            log.warning("services: interrupting %s did not reach the web", chat_id)
            return False
        return bool((answer or {}).get("accepted"))

    async def load_work(repository: str, number: int) -> None:
        answer = await resent("load_work", {"repository": repository, "number": number})
        # The router reads the Work's owner right after this: wait until the copy holds the
        # chats the web just loaded (#4016), as the web's own memory did at once.
        await copied(tuple((answer or {}).get("legs") or ()), LOAD_WAIT_S)

    async def refresh_overview() -> None:
        await resent("refresh_overview", {})

    async def register_pull(leg: str, ref) -> bool:
        body = {
            "leg": leg,
            "repository": ref.repository,
            "number": ref.number,
            "work_id": ref.work_id,
        }
        try:
            answer = await resent("register_pull", body)
        except (WebUnreachable, WebNoAnswer):
            log.warning("services: registering %s did not reach the web", ref.identity)
            return False
        if not (answer or {}).get("ok"):
            return False
        # Read at once what the web just wrote, as its own memory would hold it; the copy's
        # next ``registrations`` then finds nothing new to act on, as in the web. A read that
        # failed answers no rows: it is not taken, and the split stops for another press.
        stored = db.load_session_pull_requests()
        if not work_services.held(stored, (*state.chat_session_keys(leg), leg), ref):
            return False
        state.session_pull_requests = stored
        return True

    def schedule(message: ScheduledMessage) -> ScheduledMessage | None:
        recorded = db.record_scheduled_message(message)
        if recorded is not None:
            feed.bump(f"{SCHEDULED}:{message.chat_id}")
        return recorded

    from . import chat_board

    # The public copy's stand-in has no board (#3623): no review chats, no rows.
    make_board = getattr(chat_board, "ChatBoard", None)
    board = (
        make_board(config.chat_board.repository, store_path=config.chat_board.store_path)
        if make_board is not None and config.chat_board.enabled
        else None
    )
    seen: list = [None]

    def board_row(number: int):
        if board is None:
            return None
        try:
            stamp = config.chat_board.store_path.stat().st_mtime_ns
        except OSError:
            return None
        if stamp != seen[0]:
            board.rows.clear()
            board.load()
            seen[0] = stamp
        return board.rows.get(number)

    return work_services.Hooks(
        start_work=start_work,
        review_chat=review_chat,
        reviewer=reviewer,
        interrupt=interrupt,
        load_work=load_work,
        refresh_overview=refresh_overview,
        schedule=schedule,
        exists=db.has_scheduled_message,
        board_row=board_row,
        runtime_http=runtime_http,
        scanned=scanned,
        wake_registry=wake_registry,
        register_pull=register_pull,
    )


class SessionFollower:
    """Follow the web's copy of its live chats into this process's ``state`` (#4001)."""

    def __init__(self, web: WebCalls, state: AppState, db, *, on_registered) -> None:
        self._web = web
        self._state = state
        self._db = db
        self._on_registered = on_registered
        self.copied = asyncio.Event()
        #: Whether the copy now held is a whole one (#4458): cleared by a ``reset`` answer,
        #: which refills the copy from a web that may not have scanned yet; ``copied`` is set
        #: once, for the first.
        self.complete = False
        #: When this copy was last started over. A first scan that never ends must not refuse
        #: every call for good: after ``START_SCAN_WAIT_SECONDS`` the copy there is counts,
        #: as it does for ``scanned``.
        self._began = time.monotonic()
        self._applied = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="services-session-copy")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def scanned(self) -> None:
        """Wait for the web's first complete copy, at most ``START_SCAN_WAIT_SECONDS``."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.copied.wait(), work_services.START_SCAN_WAIT_SECONDS)

    async def holds(self, keys: tuple[str, ...], timeout: float) -> bool:
        """Wait until the copy holds every one of ``keys``; whether it does by ``timeout``."""
        deadline = time.monotonic() + timeout
        while not all(key in self._state.sessions for key in keys):
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self._applied.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._applied.wait(), left)
        return True

    async def whole(self, timeout: float) -> bool:
        """Wait until the copy is a whole one; whether it is by ``timeout`` (#4738).

        Not yet read after a start, or refilled from a web that has not scanned, the copy
        holds no chat or only some: a call placing a pull request in its Work would refuse
        one that is registered.
        """
        deadline = time.monotonic() + timeout
        while not self.complete:
            if time.monotonic() - self._began >= work_services.START_SCAN_WAIT_SECONDS:
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self._applied.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._applied.wait(), left)
        return True

    def apply(self, answer: dict) -> None:
        try:
            changed, registrations = session_copy.apply(self._state, answer)
            if registrations:
                self.reload_registrations()
            if answer.get("reset"):
                self.complete = False
                self._began = time.monotonic()
            if answer.get("scanned"):
                self.complete = True
                self.copied.set()
        finally:
            # Last, so a waiter reads the new flags; set even when an answer was half applied.
            self._applied.set()
        if changed or registrations:
            # What ``sessions_changed`` announces in the web; the release looks again.
            self._state.bus.publish("sessions", changed)

    def reload_registrations(self) -> None:
        """Read the registrations again; a pull request new here is acted on as registered."""
        state, db = self._state, self._db
        before = {ref.identity for refs in state.session_pull_requests.values() for ref in refs}
        state.session_pull_requests = db.load_session_pull_requests()
        state.session_issues = db.load_session_issues()
        new = {
            ref.identity: ref
            for refs in state.session_pull_requests.values()
            for ref in refs
            if ref.identity not in before
        }
        if before:
            for ref in new.values():
                self._on_registered(ref.repository, ref.number)

    async def _run(self) -> None:
        since: int | None = None
        epoch: str | None = None
        backoff = RECONNECT_MIN_S
        while True:
            params: dict[str, Any] = {
                "wait": session_copy.KEEPALIVE_S if self.complete else COPY_POLL_S
            }
            if since is not None and epoch is not None:
                params.update(since=since, epoch=epoch)
            try:
                answer = await self._web.get(
                    "sessions", params, timeout=session_copy.KEEPALIVE_S + 15.0
                )
                self.apply(answer)
                since, epoch = int(answer["revision"]), str(answer["epoch"])
                backoff = RECONNECT_MIN_S
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- the web reloads; read the copy again
                log.info("services: the web's session copy is not readable now: %s", exc)
                since = epoch = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_S)


def create_services_app(
    config: AppConfig,
    *,
    web_transport: httpx.AsyncBaseTransport | None = None,
    watchdog: bool = True,
) -> FastAPI:
    """The services process's app; ``web_transport`` is a test's way to the web."""
    feed = runtime_feed.ChangeFeed()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = open_db(
            config.history.db_path,
            config.history.usage_retention_days,
            config.context_management.retention_days,
        )
        state = AppState(db=db, budget_db=db)
        key = write_caller_key(caller_key_path(config))
        web = WebCalls(web_base_url(config), key, transport=web_transport)
        runtime_http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(runtime_socket_path())),
            base_url="http://agentdeck-runtime",
            timeout=httpx.Timeout(30.0, read=None),
        )
        wake_registry = work_services.wake_registry_for(config)
        wake_registry.on_change = lambda: feed.bump(WAKE_ON)
        services: work_services.WorkServices | None = None

        def registered(repository: str, number: int) -> None:
            if services is not None:
                services.router.note_registered(repository, number)

        follower = SessionFollower(web, state, db, on_registered=registered)
        issues = IssueResolver(poller_root=config.poller.root)
        watcher = WebhookWatcher(config.github_webhook.path)
        hooks = remote_hooks(
            config,
            web=web,
            state=state,
            db=db,
            feed=feed,
            scanned=follower.scanned,
            runtime_http=lambda: runtime_http,
            wake_registry=wake_registry,
            copied=follower.holds,
            complete=lambda: follower.complete,
        )
        services = work_services.build(
            config, state=state, db=db, issues=issues, hooks=hooks, watcher=watcher
        )
        services.before_router_call = follower.reload_registrations
        # Registrations and rounds changed here record their label syncs too (#4794).
        work_published.configure(services.router.pending, nudge=services.router.note_published)
        # A cached pull request row the hourly refresh rewrote wakes the backstop (#3903).
        issues.on_pull_rows = services.router.wake_sweep
        app.state.services = services
        app.state.state = state
        app.state.whole_copy = follower.whole
        tasks = [asyncio.create_task(forward(state, feed), name="services-forward")]
        if watchdog:
            from . import loop_watchdog

            tasks.append(asyncio.create_task(loop_watchdog.watch(), name="loop-watchdog"))
        jobs = runtime_feed.RuntimeFeedClient()
        remove_jobs = jobs.add_listener(
            lambda kinds: state.bus.publish("jobs") if runtime_feed.JOBS in kinds else None
        )
        jobs_sub = jobs.subscribe(runtime_feed.JOBS)
        follower.start()
        services.start()
        log.info("services: the Work router, the merge release and the backstop are running")
        try:
            yield
        finally:
            jobs_sub.close()
            remove_jobs()
            await services.stop()
            await follower.stop()
            await watcher.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            feed.close()
            await web.aclose()
            await runtime_http.aclose()
            db.close()

    async def forward(state: AppState, feed: runtime_feed.ChangeFeed) -> None:
        """This process's bus, told to the web as change-feed kinds."""
        from .web.work_view import WORK_INPUTS_TOPIC

        topics = {WORK_INPUTS_TOPIC: WORK_INPUTS, REVIEWS_TOPIC: REVIEWS}
        with state.bus.subscribe(*topics) as subscription:
            while True:
                topic, _data = await subscription.get()
                moved = {topics[topic]}
                while (more := subscription.get_nowait()) is not None:
                    moved.add(topics[more[0]])
                feed.bump(*sorted(moved))

    app = FastAPI(title="agentdeck-services", lifespan=lifespan, openapi_url=None)
    app.state.change_feed = feed

    @app.post("/call/{target}/{method}")
    async def call(target: str, method: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        args = body.get("args") if isinstance(body, dict) else None
        kwargs = body.get("kwargs") if isinstance(body, dict) else None
        services = app.state.services
        whole = getattr(app.state, "whole_copy", None)
        gated = target != "services" and (target, method) not in UNWAITED
        if gated and whole is not None and not await whole(COPY_WAIT_S):
            # Nothing was done, so the web says "restarting; try again" (#4738): answered
            # from a copy still empty after a start, ``pre-review-done`` refused a
            # registered pull request as one no Work's chat registered.
            log.info("services: %s.%s refused: the chat copy is not whole yet", target, method)
            return JSONResponse(
                {"error": {"type": "Starting", "message": "the chat copy is not whole yet"}}, 503
            )

        async def run() -> Any:
            return await services.call(target, method, list(args or ()), dict(kwargs or {}))

        try:
            # Finished even when the web stops waiting: a verdict's merge is recorded all the
            # same (``shutdown.protect`` in the web before #4016).
            result = await asyncio.shield(asyncio.ensure_future(run()))
        except LookupError as exc:
            return JSONResponse({"error": {"type": "LookupError", "message": str(exc)}}, 404)
        except tuple(work_services.ERRORS.values()) as exc:
            return JSONResponse({"error": {"type": type(exc).__name__, "message": str(exc)}})
        return JSONResponse({"result": result})

    @app.get("/changes")
    async def changes(
        since: int | None = None, epoch: str | None = None, wait: float = 0.0
    ) -> JSONResponse:
        return JSONResponse(
            await feed.wait(since, epoch, max(0.0, min(wait, runtime_feed.KEEPALIVE_S)))
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "kinds": sorted(KINDS), "at": time.time()}

    return app


def _hold_lock(socket: Path) -> int | None:
    """Take ``<socket>.lock`` for this process's life; None when another process holds it.

    The kernel lets go of it when the process ends, however it ends, so a crash leaves no
    stale lock. The descriptor is returned so nothing closes it before then.
    """
    import fcntl

    fd = os.open(f"{socket}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _answering(path: Path) -> bool:
    """Whether a process accepts connections on this Unix socket; a stale file does not."""
    import socket as sockets

    probe = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def main(argv: list[str]) -> None:
    """``agentdeck services``: serve the moved services on their socket until stopped."""
    import argparse

    import uvicorn

    from .config import config_path, load_config
    from .error_reporting import initialize_error_reporting
    from .services_link import services_socket_path

    parser = argparse.ArgumentParser(
        prog="agentdeck services",
        description="Run the Work router, the merge release and the missed-start backstop.",
    )
    parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    initialize_error_reporting("services")
    config = load_config(config_path())
    socket = services_socket_path(config)
    socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A second process would unlink the first one's socket and both would route, merge and run
    # the backstop (#4454: a hand-run one beside a launchd job). The lock, held until this
    # process ends, decides between two starting together; the probe catches one from before
    # the lock existed. Either way this one exits, and a supervisor starting it tries again and
    # takes over once the first has stopped.
    lock = _hold_lock(socket)
    if lock is None or _answering(socket):
        logging.getLogger(__name__).error(
            "another agentdeck services is already serving %s; not starting a second", socket
        )
        raise SystemExit(4)
    socket.unlink(missing_ok=True)
    os.umask(0o077)
    app = create_services_app(config)
    server = uvicorn.Server(uvicorn.Config(app, uds=str(socket), log_level="info"))
    handle_exit = server.handle_exit

    def stop(sig, frame) -> None:
        # A held change-feed request would otherwise keep the server waiting (#2865).
        app.state.change_feed.shutdown_soon()
        handle_exit(sig, frame)

    server.handle_exit = stop  # type: ignore[method-assign]
    server.run()
    if not server.started:
        raise SystemExit(3)
