"""How the web reaches the Work router and the merge release in the services process (#4016).

The web keeps ``app.state.work_router`` and ``app.state.work_release``, so every route reads
them as before, but each is now a stand-in (:class:`RouterLink`, :class:`ReleaseLink`):

- what the poller config alone answers (is a repository's Work run by the deck, which label
  starts one, the merge policy) is worked out in the web, never a call;
- what the release's store and the review history hold is read in the web too: the store is a
  ``SharedFile`` that re-reads when the services process wrote it (#3070), and the history is
  in ``agentdeck.db``;
- every act (a Merge press, a verdict, a review request) is a call to the services process's
  socket (:class:`SocketTransport`). A call that cannot connect waits up to
  :data:`CONNECT_WAIT_S` and then raises :class:`ServicesUnavailable`: nothing was sent, so a
  press may be made again. A call sent with no answer raises :class:`ServicesUncertain`, and is
  never sent again by itself: an uncertain write must not become a second write (#4003).

Changes come back the way the runtime's do (ADR 0007): the services process keeps one change
number and the web holds a ``GET /changes`` open (:class:`ServicesFollower`). Its kinds are
republished on the web's bus, so the cards, the review chips and the message delivery move as
they did when the services ran in the web.

With the switch back on (``[services] in_web``), or under the tests' stand-in, the services
are built inside the web and :class:`InProcessTransport` calls them directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import httpx

from . import review_history, runtime_feed, work_release
from .work_router import SwitchedRepositories
from .work_services import ERRORS, services_enabled, work_reviewer

log = logging.getLogger(__name__)

#: The kinds the services process's change feed names.
WORK_INPUTS = "work_inputs"  # the release store, a re-read issue: the Work cards rebuild
REVIEWS = "reviews"  # a review history event: the review chips
SCHEDULED = "scheduled"  # a message stored for a chat: the delivery loop wakes
WAKE_ON = "wake_on"  # a wake-on wait added or answered: the web's deadlines re-plan
KINDS = frozenset({WORK_INPUTS, REVIEWS, SCHEDULED, WAKE_ON})

#: How long a call waits for the socket to come back before it says the service is restarting.
CONNECT_WAIT_S = 10.0
#: How long a call waits for its answer. A verdict's GitHub reads and merge took 7 to 30 s
#: (#2963), and the services process records it even when the web stops waiting.
CALL_TIMEOUT_S = 60.0
#: How long the web holds a New work filing's starts at most, should it never let go (#3590).
FILING_HOLD_S = 300.0

RESTARTING_TEXT = "The merge service is restarting; try again in a moment."
UNCERTAIN_TEXT = (
    "Not confirmed: the merge service took the request but did not answer. Check the Work"
    " page before trying again."
)

#: The tests' in-process stand-in (``tests/conftest.py`` sets the variable): the services
#: are built inside the app, as with the switch on, so no test needs a second process.
IN_PROCESS = os.environ.get("AGENTDECK_SERVICES_IN_PROCESS") == "1"


class ServicesUnavailable(RuntimeError):
    """The services process could not be reached: nothing was sent."""

    def __init__(self, text: str = RESTARTING_TEXT) -> None:
        super().__init__(text)


class ServicesUncertain(RuntimeError):
    """The call was sent and no answer came: it may have run."""

    def __init__(self, text: str = UNCERTAIN_TEXT) -> None:
        super().__init__(text)


def services_socket_path(config=None) -> Path:
    """The services process's socket: ``$AGENTDECK_SERVICES_SOCKET``, else its own folder.

    The unit's own ``RuntimeDirectory=agentdeck-services``, never the runtime's ``agentdeck``
    folder, which systemd empties on every runtime restart (#4002).
    """
    configured = os.environ.get("AGENTDECK_SERVICES_SOCKET") or (
        config.services.socket if config is not None else ""
    )
    if configured:
        return Path(configured).expanduser()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path("~/.cache").expanduser()
    return base / "agentdeck-services" / "services.sock"


def runs_in_web(config) -> bool:
    """Whether the moved services run inside the web: the switch back, or the tests' stand-in."""
    return bool(config.services.in_web) or IN_PROCESS


# --- transports ---


class SocketTransport:
    """Calls over the services process's Unix socket."""

    def __init__(
        self, path: Callable[[], Path], transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._path = path
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport or httpx.AsyncHTTPTransport(uds=str(self._path())),
                base_url="http://agentdeck-services",
                timeout=httpx.Timeout(CALL_TIMEOUT_S, connect=2.0),
            )
        return self._client

    async def call(self, target: str, method: str, args: Iterable, kwargs: dict) -> Any:
        body = {"args": list(args), "kwargs": dict(kwargs)}
        deadline = time.monotonic() + CONNECT_WAIT_S
        while True:
            try:
                response = await self._http().post(f"/call/{target}/{method}", json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout, FileNotFoundError):
                # Not connected: nothing was sent, so waiting and sending once later is safe.
                if time.monotonic() >= deadline:
                    raise ServicesUnavailable() from None
                await asyncio.sleep(0.5)
                continue
            except httpx.HTTPError as exc:
                log.warning("services call %s.%s got no answer: %s", target, method, exc)
                raise ServicesUncertain() from exc
            break
        if response.status_code == 503:
            raise ServicesUnavailable()
        try:
            answer = response.json()
        except ValueError as exc:
            raise ServicesUncertain() from exc
        error = answer.get("error") if isinstance(answer, dict) else None
        if isinstance(error, dict):
            raise ERRORS.get(str(error.get("type")), RuntimeError)(str(error.get("message") or ""))
        if response.status_code != 200 or not isinstance(answer, dict):
            raise ServicesUncertain()
        return answer.get("result")

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()


class InProcessTransport:
    """Calls the services built inside this process, through the same ``call`` the socket runs."""

    def __init__(self, services) -> None:
        self.services = services

    async def call(self, target: str, method: str, args: Iterable, kwargs: dict) -> Any:
        return await self.services.call(target, method, list(args), dict(kwargs))

    async def aclose(self) -> None:
        return None


class Later:
    """The calls a synchronous caller cannot wait on, sent in order in the background.

    A star, a registration, a filing hold: each is best effort, and something else catches up
    when one is lost (the next join honours a star, the backstop finds a ready head).
    """

    def __init__(self, transport) -> None:
        self._transport = transport
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    def send(self, target: str, method: str, *args: Any, **kwargs: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("services call %s.%s dropped: no event loop", target, method)
            return
        if self._queue is None:
            self._queue = asyncio.Queue()
        self._queue.put_nowait((target, method, args, kwargs))
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._drain(), name="services-later")

    async def _drain(self) -> None:
        assert self._queue is not None
        while not self._queue.empty():
            target, method, args, kwargs = self._queue.get_nowait()
            try:
                await self._transport.call(target, method, args, kwargs)
            except Exception as exc:  # noqa: BLE001 -- best effort; logged
                log.warning("services call %s.%s not made: %s", target, method, exc)

    async def flush(self) -> None:
        task = self._task
        if task is not None:
            with contextlib.suppress(Exception):
                await task


# --- the stand-ins the routes read ---


class RouterLink:
    """``app.state.work_router`` in the web: config reads here, acts over the socket."""

    def __init__(self, config, transport, state, later: Later | None = None) -> None:
        self._config = config
        self._transport = transport
        self._state = state
        self._later = later or Later(transport)
        self._enabled = services_enabled(config)
        self.switched = SwitchedRepositories(config.poller.root)
        # Settable, as a test sets the router's own: the review routes read it (#3849).
        self.reviewer_off = work_reviewer(config) == "off"
        self.pre_review_on = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def starts_on(self, repository: str, labels: Iterable[str]) -> str:
        """``WorkRouterService.starts_on``, from the poller config alone."""
        from .poller_ops import work_start_labels

        repository = repository.lower()
        names = [str(name).strip() for name in labels if str(name).strip()]
        starts = {
            item.lower() for item in work_start_labels(self._config.poller.root).get(repository, ())
        }
        label = next((name for name in names if name.lower() in starts), "")
        if not self._enabled or not self.switched(repository):
            return ""
        return label

    def pre_review_held(self, pull: str) -> bool:
        """Whether ``owner/repo#n``'s review waits on its agent's checklist (#3880)."""
        events = self._state.review_events(pull.lower())
        return self.pre_review_on and review_history.checklist_waits(events)

    @contextlib.contextmanager
    def filing(self, repository: str) -> Iterator[None]:
        """Hold the router's starts in ``repository`` while the deck files a New work (#3590).

        A hold with an end time (``FILING_HOLD_S``), so a web that dies mid-filing cannot
        hold starts for ever."""
        key = uuid.uuid4().hex
        self._later.send("services", "hold_filing", repository, key, FILING_HOLD_S)
        try:
            yield
        finally:
            self._later.send("services", "release_filing", key)

    def note_filed(self, repository: str, number: int, labels: Iterable[str]) -> bool:
        labels = [str(name) for name in labels]
        if not self.starts_on(repository, labels):
            return False
        self._later.send("router", "note_filed", repository, number, labels)
        return True

    def note_registered(self, repository: str, number: int) -> None:
        self._later.send("router", "note_registered", repository, number)

    def wake_sweep(self) -> None:
        self._later.send("services", "wake_sweep")

    async def ask_again(self, *args: Any, **kwargs: Any) -> dict:
        return await self._transport.call("router", "ask_again", args, kwargs)

    async def accept(self, *args: Any, **kwargs: Any) -> dict:
        return await self._transport.call("router", "accept", args, kwargs)

    async def ship_now(self, *args: Any, **kwargs: Any) -> dict:
        return await self._transport.call("router", "ship_now", args, kwargs)

    async def pre_review_done(self, *args: Any, **kwargs: Any) -> dict:
        return await self._transport.call("router", "pre_review_done", args, kwargs)

    async def check_request(self, *args: Any, **kwargs: Any) -> dict | None:
        return await self._transport.call("router", "check_request", args, kwargs)


def _not_in_the_web(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("the release acts only in the services process (#4016)")


class ReleaseLink:
    """``app.state.work_release`` in the web: store and history reads here, acts over the socket.

    The reads go through a ``WorkReleaseService`` whose ports read and never act: its store
    and the review history are the same ones the services process writes.
    """

    def __init__(self, config, transport, state, later: Later | None = None) -> None:
        self._transport = transport
        self._later = later or Later(transport)
        ports = work_release.Ports(
            press_is_review=work_reviewer(config) == "off",
            switched=SwitchedRepositories(config.poller.root),
            paused=lambda work_id: False,
            policy=work_release.Policies(config.poller.root),
            pulls=_not_in_the_web,
            chat=_not_in_the_web,
            read_pull=_not_in_the_web,
            digest=_not_in_the_web,
            green_heads=_not_in_the_web,
            merge=_not_in_the_web,
            update_branch=_not_in_the_web,
            tell=_not_in_the_web,
            reviews=state.review_events,
        )
        self._reader = work_release.WorkReleaseService(
            work_release.ReleaseStore(
                config.github_webhook.store_path / work_release.STORE_FILENAME
            ),
            ports,
            enabled=services_enabled(config),
        )
        self.store = self._reader.store
        self.ports = ports

    # --- reads ---

    def handles(self, work_id: str) -> bool:
        return self._reader.handles(work_id)

    def queue(self, repository: str) -> list[dict]:
        return self._reader.queue(repository)

    def approval(self, work_id: str, pulls=None) -> str:
        return self._reader.approval(work_id, pulls)

    def approval_waits(self, work_id: str) -> str:
        return self._reader.approval_waits(work_id)

    def approval_reason(self, work_id: str, pulls=()) -> str:
        return self._reader.approval_reason(work_id, pulls)

    def approval_cleared(self, work_id: str) -> str:
        return self._reader.approval_cleared(work_id)

    def passed_since(self, work_id: str, pull: str, head: str, since: float) -> bool:
        return self._reader.passed_since(work_id, pull, head, since)

    def review_chat(self, pull: str, head: str) -> str | None:
        return self._reader.review_chat(pull, head)

    # --- acts ---

    async def _call(self, method: str, args: tuple, kwargs: dict) -> Any:
        return await self._transport.call("release", method, args, kwargs)

    async def evaluate(self, *args: Any, **kwargs: Any) -> str:
        return await self._call("evaluate", args, kwargs)

    async def request_merge(self, *args: Any, **kwargs: Any) -> str:
        return await self._call("request_merge", args, kwargs)

    async def withdraw_merge(self, *args: Any, **kwargs: Any) -> str:
        return await self._call("withdraw_merge", args, kwargs)

    async def on_verdict(self, *args: Any, **kwargs: Any) -> str:
        return await self._call("on_verdict", args, kwargs)

    async def on_findings(self, *args: Any, **kwargs: Any) -> bool:
        return await self._call("on_findings", args, kwargs)

    async def stale_findings(self, *args: Any, **kwargs: Any) -> str:
        return await self._call("stale_findings", args, kwargs)

    async def reorder_and_wake(self, repository: str, pulls: list[str]) -> list[dict]:
        return await self._call("reorder_and_wake", (repository, list(pulls)), {})

    def star(self, work_id: str) -> None:
        self._later.send("release", "star", work_id)


# --- the changes the services process announces ---


class ServicesFollower:
    """The web's held ``GET /changes`` on the services process, republished on its bus."""

    def __init__(self, socket_path: Callable[[], Path], *, state, scheduled_messages, wake_on):
        self._state = state
        self._scheduled = scheduled_messages
        self._wake_on = wake_on
        self.feed = runtime_feed.RuntimeFeedClient(
            socket_path=socket_path, kinds=KINDS, name="services-change-feed"
        )
        self._remove: Callable[[], None] | None = None
        self._sub: runtime_feed.FeedSubscription | None = None

    def start(self) -> None:
        self._remove = self.feed.add_listener(self.moved)
        # Held only so the feed runs; the listener does the work.
        self._sub = self.feed.subscribe(lambda item: True)

    def stop(self) -> None:
        if self._sub is not None:
            self._sub.close()
            self._sub = None
        if self._remove is not None:
            self._remove()
            self._remove = None

    def moved(self, kinds: frozenset[str]) -> None:
        """Republish what moved; a reconnect names every kind, which is the catch-up."""
        from .state import REVIEWS_TOPIC
        from .web.work_view import WORK_INPUTS_TOPIC

        if WAKE_ON in kinds and self._wake_on is not None:
            # A stale registry re-reads, which re-plans the web's deadlines.
            self._wake_on.registry.all()
        if SCHEDULED in kinds and self._scheduled is not None:
            self._scheduled.note_stored()
        if REVIEWS in kinds:
            self._state.bus.publish(REVIEWS_TOPIC)
        if kinds & {WORK_INPUTS, WAKE_ON, REVIEWS}:
            self._state.bus.publish(WORK_INPUTS_TOPIC)
