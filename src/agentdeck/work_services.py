"""The Work router, the merge release and the missed-start backstop, built in one place (#4016).

They used to be wired inside ``create_app`` and ran on the web's event loop, where the
release's walks over every registered pull request froze pages for up to 88 s (#3993,
#3998). Now they run in their own process, ``agentdeck-services.service``
(``services_process``), and the web reaches them through ``services_link``. This module is
what both places build them with: the services process for real, and the web itself only
under the tests' in-process stand-in.

``Hooks`` is everything they need from outside their own files and the database: what only
the web can do (start or interrupt a chat, start a reviewer, open a review chat, load a
let-go Work's chats, refresh the Work overview) and how a message is stored. The web hands
its own objects in; the services process hands in calls to the web's internal routes
(``web.routes_services``).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    agent_review,
    design_review,
    review_paperwork,
    review_refs,
    work_published,
    work_release,
    work_rounds,
)
from .poller_ops import work_hold_labels, work_start_labels, work_unassigned_elsewhere
from .scheduled_messages import ScheduledMessage
from .wake_on import (
    WakeOnRegistry,
    parse_request,
    register,
    register_deck_wait,
    registry_path,
)
from .work import WorkRef
from .work_backstop import MissedStartBackstop
from .work_claims import start_issue
from .work_facts import pull_state
from .work_router import (
    REVIEW_POLICY_PATH,
    AgainBusy,
    AgainRefused,
    ShipPorts,
    ShipRuns,
    SwitchedRepositories,
    WorkRouterService,
    base_only_between,
    behind_base,
    design_asked,
    design_limited,
    late_read,
    no_code_between,
    pending_path,
    pull_files,
    pulls_lookup,
    same_files_merge,
    ship_merge_between,
    work_busy,
    work_pulls,
)
from .work_router import owner_of_issue as work_owner_of_issue
from .work_router import owner_of_pull as work_owner_of_pull
from .work_signals import (
    acknowledge_comment,
    latest_comment,
    newest_comment,
    open_blockers,
    read_comment,
    read_comments,
    read_dependencies,
    read_map,
    read_map_body,
    read_parent,
    start_unless_blocked,
    write_map_body,
)
from .work_start import start_action_id

log = logging.getLogger(__name__)

# How long the release's start sweep waits for the first chat scans, which tell each Work's
# pull requests (#2982); past it the sweep runs anyway and a Work it cannot place waits.
START_SCAN_WAIT_SECONDS = 300.0


@dataclass
class Hooks:
    """What the moved services ask of the web, and where they store a message."""

    #: ``start_work_agent``: start a Work's agent once; its outcome word (#2971).
    start_work: Callable[..., Awaitable[str]]
    #: Open (or reuse) the ChatGPT review chat for a pull request: ``request_review`` plus
    #: ``note_review_head``. Its ``chat_review.ReviewOutcome``, or one of its errors.
    review_chat: Callable[..., Awaitable[object]]
    #: ``AgentReviewer``'s ``start``, ``alive`` and ``checkout_of`` (``agent_review.deck_hooks``).
    reviewer: Callable[[], dict]
    #: Stop a chat's turn (#3731); whether the runtime took it.
    interrupt: Callable[[str], Awaitable[bool]]
    #: Read back a finished Work's let-go chats (#3405).
    load_work: Callable[[str, int], Awaitable[object]]
    #: Rebuild the Work overview, which the injector's paused-chat hold reads (#2956).
    refresh_overview: Callable[[], Awaitable[object]]
    #: ``ScheduledMessageService.schedule`` and ``exists``.
    schedule: Callable[[ScheduledMessage], ScheduledMessage | None]
    exists: Callable[[str], bool]
    #: The chat board's row of a review chat by its number, or ``None`` (#3584).
    board_row: Callable[[int], object | None]
    #: The runtime's HTTP client over its socket, for the verification jobs; ``None`` reads none.
    runtime_http: Callable[[], object | None]
    #: Waits for the first scan of the chats, which tell a Work's pull requests (#2982).
    scanned: Callable[[], Awaitable[object]]
    #: The wake-on waits a review registers and a verdict ends (#3183).
    wake_registry: WakeOnRegistry
    #: Register a pull request with a chat leg, done where the chats live (#4212): the
    #: services process holds only a copy of them, which cannot republish a chat.
    register_pull: Callable[[str, Any], Awaitable[bool]]


def make_deck_claims(config) -> Callable[[str, str], bool]:
    """Whether an issue assigned to a login ("" for nobody) is this deck's to work (#3731).

    Two decks on one repository: the issue's assignee says which one works it. The Work list
    asks once per issue, so the overlays are read at most every 30 s.
    """
    unassigned_elsewhere: list = [0.0, frozenset()]

    def deck_claims(repository: str, assignee: str) -> bool:
        from . import work_claims
        from .poller_engine.github_authority import deck_person

        person = deck_person()
        if person and not assignee and time.monotonic() - unassigned_elsewhere[0] > 30.0:
            unassigned_elsewhere[:] = [
                time.monotonic(),
                work_unassigned_elsewhere(config.poller.root),
            ]
        return work_claims.mine(
            assignee,
            person=person,
            takes_unassigned=repository.lower() not in unassigned_elsewhere[1],
        )

    return deck_claims


#: How often the reviewers' ledger is swept when no review starts (#4458).
REVIEWER_SWEEP_S = 300.0


async def tell_design_dropped(
    release,
    verdicts,
    line: dict,
    running: list[dict],
    newest_stopped: str = "",
    closed: Callable[[str], Awaitable[bool]] | None = None,
) -> bool | None:
    """Tell the Work its design reviewer of ``line`` stopped without a verdict (#4458).

    ``None``, nothing to tell, when that design comment is no longer the one to act on
    (``_still_actionable``), or its issue is ``closed`` (#4675). Otherwise whether the Work was told
    now: ``False`` when its chat could not be reached, or was told already."""
    from .work import IssueRef, WorkRef

    key = str(line.get("pull") or "")
    issue = key.removeprefix("design:")
    comment = _comment_of(line)
    token = str(line.get("token") or "")
    ref = IssueRef.parse(issue)
    if not key.startswith("design:") or ref is None or not comment or not token:
        return None
    if not _still_actionable(issue, comment, verdicts, running, newest_stopped):
        return None
    if closed is not None and await closed(issue):
        return None
    return await release.on_design_dropped(WorkRef.from_issue(ref).work_id, comment, token)


def _still_actionable(
    issue: str, comment: str, verdicts, running: list[dict], newest_stopped: str
) -> bool:
    """Whether design ``comment`` of ``issue`` is still the one to act on (#4458 review): the
    newest design comment the deck knows of, from its recorded verdicts, its running reviewers
    and the newest one it saw stop (``DesignDropped``'s file), and one with neither a verdict
    nor a reviewer still running on it. Another reviewer that stopped on the same comment hides
    nothing: each start is told."""
    key = f"design:{issue}"
    mine = {_comment_of(other) for other in running if other.get("pull") == key}
    known = set(verdicts.of(issue)) | mine
    if comment in known:
        return False
    return not any(_newer(other, comment) for other in known | {newest_stopped})


def _comment_of(line: dict) -> str:
    return str(line.get("head") or "").removeprefix("comment-")


def _newer(other: str, comment: str) -> bool:
    try:
        return int(other) > int(comment)
    except ValueError:
        return False


#: How long a stopped design reviewer's notice is tried again while the Work's chat cannot be
#: reached, say mid-move to another account (#4458 helper checks).
RETELL_S = 3600.0


class DesignDropped:
    """Tells Works their design reviewer stopped with no verdict (#4458): the sweep's
    ``on_stopped``.

    The sweep removes a stopped reviewer's ledger line, so what is still needed of it is kept
    in one file beside the ledger (``design-dropped.json``), surviving a services restart
    (review round 5): per issue the newest design comment whose reviewer was seen stopped,
    which makes an older design's notice obsolete, and the notices the Work's chat could not
    take yet, tried again on each tick for ``RETELL_S``. Its calls run one at a time."""

    def __init__(
        self,
        release,
        verdicts,
        path: Path,
        clock: Callable[[], float] = time.time,
        closed: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._closed = closed
        self.release = release
        self._verdicts = verdicts
        self._path = path
        self._clock = clock
        self._lock = asyncio.Lock()

    @classmethod
    def beside(cls, release, verdicts, ledger: Path, closed=None) -> DesignDropped:
        return cls(release, verdicts, ledger.with_name("design-dropped.json"), closed=closed)

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        for part in ("newest", "unheard"):
            if not isinstance(data.get(part), dict):
                data[part] = {}
        return data

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=1))
        temporary.replace(self._path)

    @property
    def unheard(self) -> dict[str, dict]:
        """The notices waiting for the Work's chat, by reviewer start token."""
        return self._read()["unheard"]

    async def __call__(self, line: dict, running: list[dict], stopped: list[dict]) -> None:
        async with self._lock:
            data = self._read()
            for other in [*stopped, line]:
                key = str(other.get("pull") or "")
                comment = _comment_of(other)
                if not key.startswith("design:") or not comment:
                    continue
                issue = key.removeprefix("design:")
                if not _newer(data["newest"].get(issue, ""), comment):
                    data["newest"][issue] = comment
            self._write(data)
            await self._tell(data, line, running, self._clock())

    async def retell(self, running: list[dict]) -> None:
        async with self._lock:
            data = self._read()
            for held in list(data["unheard"].values()):
                if isinstance(held, dict) and isinstance(held.get("line"), dict):
                    await self._tell(data, held["line"], running, float(held.get("since") or 0))

    async def _tell(self, data: dict, line: dict, running: list[dict], since: float) -> None:
        token = str(line.get("token") or "")
        issue = str(line.get("pull") or "").removeprefix("design:")
        newest = str(data["newest"].get(issue) or "")
        try:
            told = await tell_design_dropped(
                self.release, self._verdicts, line, running, newest, self._closed
            )
        except Exception:  # noqa: BLE001 - the store or chat failing is tried again (review)
            log.exception("work services: telling %s its reviewer stopped failed", line.get("pull"))
            told = False
        if told is False and self._clock() - since < RETELL_S:
            data["unheard"][token] = {"line": line, "since": since}
        else:
            data["unheard"].pop(token, None)
        self._write(data)


def work_reviewer(config) -> str:
    """Who reviews a Work's new ready head (#3640): chatgpt, claude, codex or off."""
    return agent_review.effective(
        config.review.reviewer, review_chats=review_refs.reviewer_available()
    )


def design_chat_route(config) -> design_review.ChatRoute:
    """ChatGPT as the design reviewer (#4722), over the code review's chat route: the chat
    repository's issues, and one worker started per chat."""
    from . import chat_review, review_fallback

    board = config.chat_board

    def wanted() -> bool:
        switch = review_fallback.switch_path(board)
        return work_reviewer(config) == agent_review.CHATGPT and not review_fallback.active(switch)

    async def open_chat(title: str, body: str) -> tuple[str, bool]:
        outcome = await chat_review.open_chat(board, title, body)
        return f"{outcome.repository}#{outcome.issue_number}".lower(), bool(outcome.task)

    async def close_chat(chat: str, comment: str) -> bool:
        repository, _, number = chat.partition("#")
        return number.isdigit() and await chat_review.close_issue(repository, int(number), comment)

    return design_review.ChatRoute(wanted, open_chat, close_chat)


def services_enabled(config) -> bool:
    """Whether the moved services run at all: ``[services] enabled``, else the webhook streams'.

    Staging turns the streams off (its wake watcher would start production's poller units)
    but may run these in its services twin over a copied stream (#4018).
    """
    chosen = config.services.enabled
    return config.github_webhook.enabled if chosen is None else bool(chosen)


def held(stored: dict, keys, ref: Any) -> bool:
    """Whether ``stored`` (``load_session_pull_requests``) registers ``ref`` with exactly one
    of the chat's legs ``keys``, for its Work when it names one (#4212): a second leg holding
    it could place it in another Work."""
    found = [
        item
        for key in dict.fromkeys(keys)
        for item in stored.get(key, ())
        if item.identity == ref.identity
    ]
    return len(found) == 1 and (not ref.work_id or found[0].work_id == ref.work_id)


async def register_split(
    state: Any,
    add: Callable[[str, Any], Awaitable[bool]],
    repository: str,
    number: int,
    new: int,
) -> bool:
    """Register pull request ``new`` with every chat that registered ``number``, for the same
    Work: the deck opened it to ship ``number``'s reviewed commit (#4212 review). Whether any
    chat had and every one took it. ``add`` is ``Hooks.register_pull``: the web registers it,
    since the services process's copy of a chat cannot be republished."""
    from .models import PullRequestRef

    wanted = (repository.lower(), number)
    legs = [
        (leg, ref.work_id)
        for leg, refs in state.session_pull_requests.items()
        for ref in refs
        if ref.identity == wanted
    ]
    added = [
        await add(leg, PullRequestRef(repository.lower(), new, work_id=work_id))
        for leg, work_id in legs
    ]
    return bool(legs) and all(added)


def check_on(config) -> bool:
    """Whether pull requests get the GitHub check (#4152): switched on, with the services
    running, and in an edition with a reviewer."""
    return (
        config.review_check.enabled
        and services_enabled(config)
        and review_refs.reviewer_available()
    )


@dataclass
class WorkServices:
    """The three moved services and what answers a call to them (``call``)."""

    router: WorkRouterService
    release: work_release.WorkReleaseService
    backstop: MissedStartBackstop
    hooks: Hooks
    state: Any
    #: Starts in flight whose later runtime refusal the web reports back, by Work id.
    refused: dict[str, Callable[[], object]] = field(default_factory=dict)
    #: The New work filings the web holds (#3590): key -> ``(repository, until)``.
    filings: dict[str, tuple[str, float]] = field(default_factory=dict)
    #: Run before each router call: the services process reads the registrations again, so a
    #: pull request a chat registered a moment ago is known to the call it caused (#4016).
    before_router_call: Callable[[], object] | None = None
    #: The GitHub check's publisher (#4152), ``None`` when it is off.
    check: Any = None
    #: Lighter review (#4399, ``light_review.LightReview``).
    light: Any = None
    #: The reviewer whose ledger the sweep tick reads (#4458), ``None`` with reviews off.
    reviewer: Any = None
    #: Its ``on_stopped`` (``DesignDropped``), whose unheard notices each tick tries again.
    design_dropped: Any = None
    _sweeping: asyncio.Task | None = None

    def start(self) -> None:
        self.router.start()
        self.backstop.start()
        self.release.start(self.state.bus, scanned=self.hooks.scanned)
        if self.light is not None:
            self.light.start()
        if self.check is not None:
            self.check.start()
        if self.reviewer is not None and self._sweeping is None:
            self._sweeping = asyncio.create_task(
                self._sweep_reviewers(), name="work-reviewer-sweep"
            )

    async def _sweep_reviewers(self) -> None:
        """Sweep the reviewers' ledger every ``REVIEWER_SWEEP_S`` (#4458), after the chats are
        copied: a design reviewer that stopped is dropped and its Work told even while no other
        review starts, which was the only sweep before."""
        await self.hooks.scanned()
        while True:
            try:
                running = await self.reviewer.sweep_now()
                if self.design_dropped is not None:
                    await self.design_dropped.retell(running)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("work services: reviewer sweep failed")
            await asyncio.sleep(REVIEWER_SWEEP_S)

    async def stop(self) -> None:
        sweeping, self._sweeping = self._sweeping, None
        if sweeping is not None:
            sweeping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeping
        if self.check is not None:
            await self.check.stop()
        await self.backstop.stop()
        await self.router.stop()
        await self.release.stop()
        if self.light is not None:
            await self.light.stop()

    # --- the calls the web makes (``services_link``) ---

    async def call(self, target: str, method: str, args: list, kwargs: dict) -> object:
        """Run one whitelisted call and answer what it returned, made plain for JSON."""
        allowed = CALLS.get(target, ())
        if method not in allowed:
            raise LookupError(f"no call {target}.{method}")
        if target == "services":
            fn = getattr(self, f"_{method}")
        else:
            fn = getattr(self.router if target == "router" else self.release, method)
        if target == "router" and self.before_router_call is not None:
            self.before_router_call()
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return json.loads(json.dumps(result, default=list))

    def _hold_filing(self, repository: str, key: str, seconds: float) -> None:
        self.filings[key] = (repository.lower(), time.time() + float(seconds))

    def _release_filing(self, key: str) -> None:
        self.filings.pop(key, None)

    def _filing_now(self, repository: str) -> bool:
        now = time.time()
        for key, (held, until) in list(self.filings.items()):
            if until <= now:
                self.filings.pop(key, None)
            elif held == repository:
                return True
        return False

    def _start_refused(self, work_id: str) -> None:
        callback = self.refused.pop(work_id, None)
        if callback is not None:
            callback()

    def _wake_sweep(self) -> None:
        self.router.wake_sweep()


#: Every call the web may make, by target. Anything else is refused, so the socket runs
#: nothing a page did not mean to.
CALLS: dict[str, tuple[str, ...]] = {
    "release": (
        "evaluate",
        "request_merge",
        "withdraw_merge",
        "on_verdict",
        "on_findings",
        "on_design_review",
        "stale_findings",
        "star",
        "reorder_and_wake",
    ),
    "router": (
        "design_review",
        "design_accept",
        "design_record",
        "note_filed",
        "note_registered",
        "note_published",
        "note_deployed",
        "ask_again",
        "recover_review",
        "accept",
        "ship_now",
        "pre_review_done",
        "check_request",
        "ready_press",
        "ready_received",
    ),
    "services": (
        "hold_filing",
        "release_filing",
        "start_refused",
        "wake_sweep",
    ),
}

#: The errors a call may raise on purpose, raised again by name on the web's side.
ERRORS: dict[str, type[Exception]] = {
    "AgainRefused": AgainRefused,
    "AgainBusy": AgainBusy,
    "PressRefused": work_release.PressRefused,
    "ValueError": ValueError,
}


def build(
    config,
    *,
    state,
    db,
    issues,
    hooks: Hooks,
    watcher=None,
) -> WorkServices:
    """The router, the release and the backstop over ``state``, ``db`` and ``hooks``."""
    enabled = services_enabled(config)
    reviewer = work_reviewer(config)
    verdicts = design_review.Verdicts.beside(config.review.ledger_path)

    # Late-bound: the release is made below, and a sweep runs only once the services start.
    design_stopped = DesignDropped.beside(
        None, verdicts, config.review.ledger_path, design_review.read_closed
    )

    def make_reviewer(kind: str) -> agent_review.AgentReviewer:
        return agent_review.AgentReviewer(
            kind=kind,
            max_reviewers=config.review.max_reviewers,
            ledger=config.review.ledger_path,
            digest=review_refs.target_digest,
            on_stopped=design_stopped,
            read_issue=agent_review.read_issue,
            **hooks.reviewer(),
        )

    agent_reviewer = make_reviewer(reviewer) if reviewer in agent_review.PROVIDERS else None
    # The design review (#4332) runs on the code reviewer, sharing its places and ledger. When
    # ChatGPT reviews code, one Claude reviewer on that ledger is the only one writing it. It is
    # made at start (#4458), so the sweep tick finds a ledger an earlier run left.
    design_reviewer = (
        agent_reviewer
        if agent_reviewer is not None
        else make_reviewer(agent_review.CLAUDE)
        if reviewer != agent_review.OFF
        else None
    )

    async def start_design(
        issue: str, design_url: str, *, checkout: str = "", earlier: str = ""
    ) -> object:
        if design_reviewer is None:
            raise review_refs.ReviewOff(review_refs.REVIEWER_OFF_TEXT)
        return await design_reviewer.start_design(
            issue, design_url, checkout=checkout, earlier=earlier
        )

    design = (
        design_review.DesignRound(
            start_design,
            verdicts,
            reviews=lambda token, issue, comment: agent_review.reviews_design(
                config.review.ledger_path, token, issue, comment
            ),
            closed=design_review.read_closed,
            reviewing=lambda issue, comment: (
                agent_review.design_reviewer(config.review.ledger_path, issue, comment) is not None
            ),
        )
        if reviewer != agent_review.OFF
        else None
    )

    async def start_work_review(
        pull: str, work: str, *, note: str = "", again: bool = False
    ) -> object:
        if agent_reviewer is not None:
            return await agent_reviewer.start(pull, work, note=note, again=again)
        if reviewer == agent_review.OFF:
            raise review_refs.ReviewOff(review_refs.REVIEWER_OFF_TEXT)
        return await hooks.review_chat(pull, work, note=note, again=again)

    async def ready_pull_head(repository: str, number: int) -> str:
        from .review_refs import pull_facts

        facts = await pull_facts(repository, number) or {}
        if str(facts.get("state") or "").upper() != "OPEN" or facts.get("isDraft"):
            return ""
        return str(facts.get("headRefOid") or "")

    async def open_pull_head(repository: str, number: int) -> str:
        from .review_refs import pull_facts

        facts = await pull_facts(repository, number) or {}
        if str(facts.get("state") or "").upper() != "OPEN":
            return ""
        return str(facts.get("headRefOid") or "")

    def work_event_refusal(chat_id: str, text: str) -> str | None:
        from .work_message import message_refusal

        return message_refusal(state, chat_id, text)

    # The deck's own release (#2895 step 2): a switched Work merges here,
    # from its verdicts, pull request and label events, finished jobs and idle chats.
    switched_repositories = SwitchedRepositories(config.poller.root)

    def release_paused(work_id: str) -> bool:
        # Read live from the deck's own pause table (#2559), never the Work overview's
        # snapshot, which nothing rebuilds while no dashboard watches. A label pause is the
        # issue's label, which the release reads itself.
        return bool(db is not None and work_id in db.paused_works())

    def work_finished(work_id: str) -> bool:
        # Someone pressed Finish (#2510). Unreadable is unfinished.
        if db is None:
            return False
        wanted = work_id.casefold()
        return any(
            key.casefold() == wanted and finished is not None
            for key, (_closed, finished) in db.work_signoffs().items()
        )

    def work_starred(work_id: str) -> bool:
        # A person's star (#3116), read live like the pause above. Unreadable is unstarred.
        # Case-blind: a webhook may spell the repository otherwise than the Work page did.
        if db is None:
            return False
        return work_id.casefold() in {starred.casefold() for starred in db.starred_works()}

    from .work_overview import carries_label

    def paused_label_for(work_id: str) -> str:
        """The paused label the Work's project manifest names, "" for none."""
        repository = work_id.partition(":")[2].rpartition("#")[0]
        return str(release.ports.policy(repository).paused_label or "")

    def cached_record(repository: str, number: int) -> dict | None:
        """The issue's cached record; None when it has none or the cache cannot be read."""
        try:
            return issues.cached_issues([(repository, number)]).get((repository, number))
        except Exception:  # noqa: BLE001 -- unreadable reads as no record, as it did before
            log.debug("issue cache unreadable for %s#%s", repository, number, exc_info=True)
            return None

    def cached_issue(work_id: str) -> dict | None:
        ref = WorkRef.from_work_id(work_id)
        if ref is None:
            return None
        return cached_record(ref.repository, ref.number)

    def router_paused(work_id: str) -> bool:
        # Paused on this deck, or by the label on the Work's cached issue (#2956): the way the
        # Work card reads it, so a pull request event pauses no less than a comment does.
        if release_paused(work_id):
            return True
        return carries_label(cached_issue(work_id), paused_label_for(work_id))

    async def reread_work_issue(repository: str, number: int) -> None:
        from .web.work_view import WORK_INPUTS_TOPIC

        if await issues.refresh_issue_records([(repository, number)], limit=1):
            state.bus.publish(WORK_INPUTS_TOPIC)

    async def router_resume(work_id: str) -> bool:
        """Resume a paused Work an automatic event reached (#2956): both of its pauses."""
        from . import gh_cli

        if db is not None and work_id in db.paused_works() and not db.resume_work(work_id):
            return False
        wanted = paused_label_for(work_id)
        ref = WorkRef.from_work_id(work_id)
        record = cached_issue(work_id)
        if ref is not None and carries_label(record, wanted):
            # Spelled as the issue carries it, which may differ in case from the manifest's.
            label = next(
                str(name).strip()
                for name in record["labels"]
                if str(name).strip().casefold() == wanted.strip().casefold()
            )
            # As the repository owner's non-human authority, like the Work page's Resume.
            argv = [
                "gh",
                "issue",
                "edit",
                str(ref.number),
                "--repo",
                ref.repository,
                "--remove-label",
                label,
            ]
            try:
                code, _out, err = await gh_cli.run_once(argv, timeout=30, repo=ref.repository)
            except (OSError, TimeoutError):
                log.warning(
                    "work-router: removing %s from %s failed", label, work_id, exc_info=True
                )
                return False
            if code != 0:
                log.warning(
                    "work-router: removing %s from %s: %s",
                    label,
                    work_id,
                    err.decode("utf-8", "replace").strip(),
                )
                return False
            await reread_work_issue(ref.repository, ref.number)
        try:
            # The injector holds a paused Work's chats by the overview's snapshot
            # (``paused_hold``): it is fresh before any message to the Work goes out.
            await hooks.refresh_overview()
        except Exception:  # noqa: BLE001 -- resumed; the next pass shows it
            log.warning("work overview refresh after resuming %s failed", work_id, exc_info=True)
        if release.handles(work_id):
            try:
                # A verdict the release held as "paused" before this resume merges now, as
                # the Work page's Resume makes it look again (``_wake_release``, #2956 review).
                await release.evaluate(work_id)
            except Exception:  # noqa: BLE001 -- resumed; the release catches up later
                log.warning("work release of %s after resuming failed", work_id, exc_info=True)
        return True

    def release_chat(work_id: str) -> str | None:
        ref = work_id.partition(":")[2]
        repository, _, number = ref.rpartition("#")
        owner = work_owner_of_issue(state, repository, int(number)) if number.isdigit() else None
        return owner[0] if owner else None

    async def release_same_files(ref: str, old: str, new: str) -> bool:
        repository, _, number = ref.partition("#")
        return await same_files_merge(repository, int(number), old, new)

    async def release_digest(ref: str, head: str) -> str:
        repository, _, number = ref.partition("#")
        digest, _why = await review_refs.target_digest(repository, int(number), head)
        return digest

    class _Runtime:
        """``runtime_jobs``' ``app``: only ``state.runtime_http`` is read."""

        class state:  # noqa: N801 -- stands in for ``app.state``
            pass

    def runtime_app() -> object:
        _Runtime.state.runtime_http = hooks.runtime_http()
        return _Runtime

    async def heads_of(work_id: str, pick) -> set[str]:
        from .web.work_view import runtime_jobs

        rows = await runtime_jobs(runtime_app(), [work_id])
        heads = {pick(row, rows) for row in rows}
        return {head for head in heads if head}

    async def release_green_heads(work_id: str) -> set[str]:
        from .poller_engine.verification import credited_head_for_work_alone

        return await heads_of(
            work_id, lambda row, rows: credited_head_for_work_alone(row, work_id=work_id, jobs=rows)
        )

    async def release_script_green_heads(work_id: str) -> set[str]:
        """Heads a green verification of exactly ``bin/test.sh`` proves (#4399)."""
        from .poller_engine.verification import script_green_head_for_work_alone

        return await heads_of(
            work_id, lambda row, rows: script_green_head_for_work_alone(row, work_id=work_id)
        )

    async def release_script_failed_heads(work_id: str) -> set[str]:
        """Heads a verification of exactly ``bin/test.sh`` failed at (#4401 review)."""
        from .poller_engine.verification import script_failed_head_for_work_alone

        return await heads_of(
            work_id,
            lambda row, rows: script_failed_head_for_work_alone(row, work_id=work_id, jobs=rows),
        )

    async def release_script_verifying(work_id: str) -> bool:
        from .web.work_view import runtime_queue

        queue = await runtime_queue(runtime_app(), work_id=work_id)
        return work_release.verifies(queue, work_id, ["bin/test.sh"])

    async def release_failed_heads(work_id: str) -> set[str]:
        from .poller_engine.verification import failed_head_for_work_alone

        return await heads_of(
            work_id, lambda row, rows: failed_head_for_work_alone(row, work_id=work_id, jobs=rows)
        )

    async def release_moved_heads(work_id: str) -> set[str]:
        from .poller_engine.verification import moved_head_for_work_alone

        return await heads_of(
            work_id, lambda row, rows: moved_head_for_work_alone(row, work_id=work_id)
        )

    async def release_unclean_heads(work_id: str) -> set[str]:
        from .poller_engine.verification import unclean_head_for_work_alone

        return await heads_of(
            work_id, lambda row, rows: unclean_head_for_work_alone(row, work_id=work_id)
        )

    async def work_verification_rows(work_id: str) -> list[dict]:
        from .web.work_view import runtime_work_jobs

        # Raises when unread, so the check holds nothing back (#4326 review).
        return await runtime_work_jobs(runtime_app(), work_id)

    async def release_verifying(work_id: str) -> bool:
        from .web.work_view import runtime_queue

        return work_release.verifies(await runtime_queue(runtime_app(), work_id=work_id), work_id)

    def release_tell(chat_id: str, message_id: str, text: str) -> bool:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        scheduled = hooks.schedule(
            ScheduledMessage(id=message_id, chat_id=chat_id, text=text, due_at=now, created_at=now)
        )
        # ``None`` is a replay the store absorbed or a row it refused; only it can say which.
        return scheduled is not None or hooks.exists(message_id)

    def release_note(chat_id: str, note_id: str, text: str) -> bool:
        add = getattr(db, "add_pending_event", None)
        return bool(add(note_id, chat_id, text)) if add is not None else False

    async def release_late_read(pull: str, old: str, live: str) -> tuple[bool, set[str] | None]:
        repository, _, number = pull.rpartition("#")
        if not number.isdigit():
            return False, None
        return await late_read(repository, int(number), old, live)

    async def release_pull_files(pull: str) -> set[str] | None:
        repository, _, number = pull.rpartition("#")
        return await pull_files(repository, int(number)) if number.isdigit() else None

    async def release_loading(merges: list[tuple[str, set[str]]]) -> frozenset[str] | None:
        # Which of this deck's processes load a merge, from its merge commit's tree (#4500).
        from .deployment import deploy_repo_dir
        from .live_check_scope import loading_processes

        return await loading_processes(deploy_repo_dir(), merges)

    def release_wait_deploy(chat_id: str, refs: list[str], bodies: list[dict], then: str) -> bool:
        # The deploy waits the live-check request asked the agent to register (#4253), on the
        # chat's working leg as its own would be, carrying that request for when they end.
        chat = state.chat_by_id(chat_id)
        session = state.current_session_for_chat(chat) if chat is not None else None
        session_key = session.key if session is not None else chat_id
        requests = [parse_request(**body) for body in bodies]
        for fields in requests:
            register_deck_wait(
                hooks.wake_registry,
                config.github_webhook.path,
                session_key,
                chat_id,
                then=then,
                **fields,
            )
        # The chat's own waits on these merges are answered by the deploy wait, which says
        # they merged: one wake, not two.
        hooks.wake_registry.end_merge_waits(chat_id, refs)
        return bool(requests)

    def release_merge_told(chat_id: str, refs: list[str]) -> None:
        # The release told the chat these merged (#4753): its own waits on them end.
        hooks.wake_registry.end_merge_waits(chat_id, refs)

    def release_note_evidence(work_id: str, sentence: str) -> bool:
        # Appended to the summary's ``correctness`` part in place of the agent rewriting it
        # (#4253); a Work with no summary yet has nothing to append to, so its agent is asked.
        # Only that part is written, by a compare-and-swap, so a person's or agent's summary
        # write from the web meanwhile is kept (#4253 review).
        from functools import partial

        from .web.work_view import WORK_INPUTS_TOPIC
        from .work_resolution import AGENT_WRITES, ResolutionError, evidence_added

        if db is None:
            return False
        try:
            written = db.append_work_correctness(
                work_id, partial(evidence_added, sentence=sentence)
            )
        except ResolutionError:
            written = False  # no room in the part: the agent is asked to rewrite it
        if not written:
            # The rewrite the deck now asks for changes only this part, so it is let past the
            # limit on summary writes (#4726).
            db.allow_work_resolution_write(work_id, AGENT_WRITES)
            return False
        state.bus.publish(WORK_INPUTS_TOPIC)
        return True

    async def release_rerun(
        work_id: str, repository: str, head: str, command: list[str] | None = None
    ) -> bool:
        client = hooks.runtime_http()
        if client is None:
            return False
        body: dict = {"work_id": work_id, "repository": repository, "head": head}
        if command is not None:
            body["command"] = command  # a light pull request's bin/test.sh (#4399)
        response = await client.post(
            "/jobs/rerun-verification",
            json=body,
            timeout=180.0,
        )
        response.raise_for_status()
        return bool(response.json().get("job"))

    # Rounds of a reopened Work (#4585): the release counts only the current round's pull
    # requests, and the router starts a round on a reopen and skips an earlier one's merge.
    def round_of(work_id: str) -> Any:
        return work_rounds.load(db, [work_id]).get(work_id)

    def round_pulls(work_id: str) -> tuple[str, ...]:
        return work_rounds.current(work_pulls(state, work_id), round_of(work_id))

    def round_pulls_lookup() -> Callable[[str], tuple[str, ...]]:
        lookup = pulls_lookup(state)
        rounds = work_rounds.load(db)
        return lambda work_id: work_rounds.current(lookup(work_id), rounds.get(work_id))

    def start_reopened_round(work_id: str) -> bool:
        earlier = work_rounds.settled(work_pulls(state, work_id), issues.cached_pulls, pull_state)
        return work_rounds.start(db, work_id, earlier, work_rounds.REOPENED)

    def earlier_pulls(work_id: str) -> frozenset[str]:
        return getattr(round_of(work_id), "earlier", None) or frozenset()

    def release_design_waits(work_id: str) -> bool:
        # A design review asked for and not answered (#4781): the deck's own reviewer in the
        # ledger, or the router's waiting entry (a reviewer place, or ChatGPT's chat).
        issue = work_id.removeprefix("github_issue:")
        if design is None or "#" not in issue:
            return False
        # Or the series limit stopped it until the owner answers (#4800): the gate refuses
        # ``pre-review-done`` then too.
        return design_asked(
            config.github_webhook.path, config.review.ledger_path, issue, time.time()
        ) or design_limited(config.review.ledger_path, issue)

    # The Ship now presses running here (#4558): the router runs them, the release's hold
    # line reads them.
    ship_runs = ShipRuns()
    release = work_release.WorkReleaseService(
        work_release.ReleaseStore(config.github_webhook.store_path / work_release.STORE_FILENAME),
        work_release.Ports(
            press_is_review=reviewer == agent_review.OFF,
            ship_unfinished=state.ship_unfinished,
            ship_running_head=ship_runs.head_of,
            shipped_findings=lambda work_id: [
                (record.pull, record.fix)
                for record in state.shipped_by_work(work_id.removeprefix("github_issue:"))
                if record.shipped
            ],
            switched=switched_repositories,
            paused=release_paused,
            starred=work_starred,
            policy=work_release.Policies(config.poller.root),
            pulls=round_pulls,
            pulls_lookup=round_pulls_lookup,
            round_of=round_of,
            chat=release_chat,
            read_pull=work_release.read_pull,
            digest=release_digest,
            green_heads=release_green_heads,
            failed_heads=release_failed_heads,
            replaced_checks=work_release.replaced_checks,
            same_files=release_same_files,
            moved_heads=release_moved_heads,
            unclean_heads=release_unclean_heads,
            merge=work_release.merge,
            update_branch=work_release.update_branch,
            tell=release_tell,
            # A note for the chat's next message, waking nothing (#4303); in the control
            # database the web's send paths read it from.
            note=release_note,
            # A chat of the Work mid-turn may still push (#2481): read from the live sessions.
            busy=lambda work_id: work_busy(state, work_id),
            issue_labels=work_release.issue_labels,
            permission=work_release.permission,
            verifying=release_verifying,
            refusal=work_event_refusal,
            review_event=state.record_review_event,
            reviews=state.review_events,
            forget_reviews=state.forget_review_events,
            pull_files=release_pull_files,
            loading_processes=release_loading,
            late_read=release_late_read,
            wait_deploy=release_wait_deploy,
            merge_told=release_merge_told,
            note_evidence=release_note_evidence,
            rerun_verification=release_rerun,
            design_waits=release_design_waits,
        ),
        enabled=enabled,
    )
    design_stopped.release = release
    # The release's own record feeds the cards (Ready for merge, #2960): announced, so a merge
    # or a recorded pass rebuilds them.
    from .web.work_view import WORK_INPUTS_TOPIC

    release.store.on_change = lambda: state.bus.publish(WORK_INPUTS_TOPIC)
    # A verdict the release takes ends its chat's review waits without a second message (#3183).
    release.on_review_answer = hooks.wake_registry.answer_reviews

    def router_review_chat(pull: str, head: str):
        # The newest review chat about this head and its board row (#3584): whether a ready
        # press at a head under review waits on that review or asks for it again.
        chat = release.review_chat(pull, head)
        if chat is None:
            return None
        number = chat.rpartition("#")[2]
        return chat, hooks.board_row(int(number)) if number.isdigit() else None

    def router_ready_pulls() -> list[tuple[str, int, str]]:
        # The backstop's candidates (#3903): every pull request a chat registered in a
        # switched repository whose cached row is open and not a draft. One local cache read;
        # a pull request the deck never resolved has no row and waits for its first one.
        refs = {
            ref.identity for refs in tuple(state.session_pull_requests.values()) for ref in refs
        }
        switched = {repository for repository, _ in refs if switched_repositories(repository)}
        refs = {ref for ref in refs if ref[0] in switched}
        found = []
        for (repository, number), row in issues.cached_pulls(refs).items():
            if (
                str(row.get("state") or "").lower() == "open"
                and not row.get("mergedAt")
                and not row.get("isDraft")
            ):
                found.append((repository, number, str(row.get("headRefOid") or "")))
        return found

    def router_wait_review(chat_id: str, pull: str, asked: bool) -> None:
        # The same wait ``review-request`` registers, on the chat's working leg.
        chat = state.chat_by_id(chat_id)
        session = state.current_session_for_chat(chat) if chat is not None else None
        session_key = session.key if session is not None else chat_id
        fields = parse_request(review=pull)
        register(hooks.wake_registry, session_key, chat_id, asked=asked, **fields)

    deck_claims = make_deck_claims(config)

    async def start_unblocked_ticket(repository: str, number: int) -> tuple[str, str]:
        # The Start button's own write and label (#3764): the first ownership label.
        from .poller_ops import read_board

        gh = shutil.which("gh")
        labels = read_board(config.poller.root).ownership_labels(repository)
        if gh is None or not labels:
            return "failed", "no gh or no start label"
        return await start_issue(gh, repository, number, labels[0], deck_claims)

    services: WorkServices

    def deployed() -> frozenset[str]:
        # The Works shown as Waiting for a try (#4384), or with a registered pull request
        # merged (#4398): a blocker among them counts as done.
        load = getattr(db, "load_done_blockers", None)
        return load() if load is not None else frozenset()

    def blockers(repository: str, number: int):
        return open_blockers(repository, number, deployed=deployed)

    async def ask_runtime(repository: str, number: int, *, on_refused=None, **kwargs) -> str:
        # A start the runtime refuses later is reported back by the web (``start_refused``).
        work_id = WorkRef(repository.lower(), number).work_id
        if on_refused is not None:
            services.refused[work_id] = on_refused
        return await hooks.start_work(
            repository, number, on_refused=on_refused is not None, **kwargs
        )

    async def start_work(repository: str, number: int, **kwargs) -> str:
        # Held while a blocker is open or unread (#4216), on every router start and retry.
        return await start_unless_blocked(
            ask_runtime, repository, number, blockers=blockers, **kwargs
        )

    check = None
    if check_on(config) and db is not None:
        try:
            from .review_check import ReviewCheckPublisher
        except ImportError:  # the public copy has no reviewer and leaves the file out
            ReviewCheckPublisher = None
        if ReviewCheckPublisher is not None:
            check = ReviewCheckPublisher(
                state=state,
                db=db,
                owned=lambda repository, number: (
                    work_owner_of_pull(state, repository, number) is not None
                ),
            )

    async def ship_open_head(repository: str, number: int) -> tuple[str, bool]:
        from .review_refs import pull_facts

        facts = await pull_facts(repository, number) or {}
        if str(facts.get("state") or "").upper() != "OPEN":
            return "", False
        return str(facts.get("headRefOid") or ""), bool(facts.get("isDraft"))

    async def ship_mark_ready(repository: str, number: int) -> bool:
        from .work_claims import run_gh_write

        argv = ["gh", "pr", "ready", str(number), "--repo", repository]
        outcome, _why = await run_gh_write(argv, repository, "Mark ready")
        return outcome == "done"

    async def ship_split(repository: str, number: int, head: str, issue: int) -> int:
        from .ship_split import open_ship_pull

        return await open_ship_pull(repository, number, head, issue)

    ship = ShipPorts(
        open_head=ship_open_head,
        mark_ready=ship_mark_ready,
        claim=state.claim_shipped,
        note_shipped=state.note_shipped,
        shipped=state.shipped,
        shipped_by_work=state.shipped_by_work,
        split=ship_split,
        behind=behind_base,
        register=lambda repository, number, new: register_split(
            state, hooks.register_pull, repository, number, new
        ),
    )

    def tests_checked(repository: str) -> bool:
        """Whether ``repository``'s manifest entry says ``check_tests = true`` (#4326)."""
        return repository.lower() in release.ports.policy(repository).tests_checked

    from . import light_review

    # Lighter review of robot pull requests outside their guarded code (#4399): on only where
    # a manifest entry says ``light_review``.
    light = light_review.LightReview(
        light_review.LightPorts(
            policy=lambda repository: release.ports.policy(repository).light(repository),
            store=db,
            reviews=state.review_events,
            live_head=light_review.gh_live_head,
            base_branch=light_review.gh_base_branch,
            digest=release_digest,
            chat=release_chat,
            tell=release_tell,
            look=release.queue_bench_look,
            fetch=light_review.git_fetch,
            git=light_review.git_read,
            bench=light_review.run_bench,
            find_issue=light_review.gh_find_issue,
            file_issue=light_review.gh_file_issue,
            merged=light_review.gh_merged,
        )
    )
    release.ports.light = light
    release.ports.script_green_heads = release_script_green_heads
    release.ports.script_failed_heads = release_script_failed_heads
    release.ports.script_verifying = release_script_verifying

    async def close_review_chat(
        work: str, pull: str, head: str, settled: Callable[[str, str], bool], reason: str
    ) -> list[int]:
        from .chat_review import close_settled_review

        return await close_settled_review(config.chat_board, work, pull, head, settled, reason)

    # The published label on a Work's issue (#4794): registrations and rounds from the
    # database at sync time, so every process decides from the same set.
    registrations = work_published.Registrations(db)
    published = work_published.PublishedLabels(
        work_published.Ports(
            labels=release.ports.policy.published_labels,
            registered=registrations.registered,
            registered_issues=registrations.registered_issues,
            pull_state=work_published.gh_pull_state,
            issue_labels=work_published.gh_issue_labels,
            edit_label=work_published.gh_edit_label,
            create_label=work_published.gh_create_label,
            open_issues=work_published.gh_open_issues,
        ),
        pending_path(config.github_webhook.path),
        time.time,
    )
    router = WorkRouterService(
        config.github_webhook.path,
        published=published,
        works_of_pull=registrations.works_of_pull,
        chat_repository=config.chat_board.repository,
        newest_comment=newest_comment,
        switched=switched_repositories,
        release=release,
        owner_of_issue=lambda repository, number: work_owner_of_issue(state, repository, number),
        owner_of_pull=lambda repository, number: work_owner_of_pull(state, repository, number),
        schedule=hooks.schedule,
        exists=hooks.exists,
        start_review=start_work_review,
        reviewer_off=reviewer == agent_review.OFF,
        # The agent goes through a short checklist before each review starts (#3880).
        pre_review=True,
        review_policy=(
            f"https://github.com/{config.chat_board.repository}/{REVIEW_POLICY_PATH}"
            if config.chat_board.repository
            else ""
        ),
        refusal=work_event_refusal,
        ready_head=ready_pull_head,
        # A verdict on a head its agent turned back to draft still counts (#4352).
        open_head=open_pull_head,
        # Review fixes that grew the diff over 15% add a design check to the checklist (#4220).
        diff_sizes=review_refs.diff_sizes,
        # Missing paperwork refuses pre-review-done before any review starts (#4261).
        # Where the manifest says ``check_tests = true``, Python code changed with no test, and
        # a head with no test run, refuse it too (#4326).
        # Before the first review, the helper checks' comment too (#4437).
        paperwork=lambda repository, number, work, first=False: review_paperwork.check(
            repository,
            number,
            work,
            tests=tests_checked(repository),
            helpers=first,
        ),
        design=design,
        design_chat=design_chat_route(config) if design is not None else None,
        verification_rows=work_verification_rows,
        tests_checked=tests_checked,
        light=light,
        enabled=enabled,
        watcher=watcher,
        # The ownership label on an issue no chat owns starts its Work's agent (step 5b).
        start_work=start_work,
        start_labels=lambda repository: work_start_labels(config.poller.root).get(
            repository.lower(), ()
        ),
        # Removing it from a Work's issue approves the spec its agent wrote (#3134).
        spec_only_label=lambda repository: release.ports.policy(repository).spec_only_label,
        # A paused Work is resumed by its own automatic news (#2956): paused on this deck, or
        # by the label its cached issue carries.
        paused=router_paused,
        starred=work_starred,
        resume=router_resume,
        # A person's comment reaches a Work's agent only from a Work-tier login (#2895 review).
        authorized=release.holds_work,
        # A merge re-reads its Work's issue, in case the close delivery was dropped (#2947).
        reread_issue=reread_work_issue,
        # A person's comment passed on to the Work's chat gets a 👀 on GitHub (#3109).
        acknowledge=acknowledge_comment,
        # Someone else closing a Work's issue is news to its chat unless the Work is finished
        # or the chat is mid-turn, as it is when it closes its own issue (#3188).
        finished=work_finished,
        # The closing comment a restart kept the router from seeing is read from GitHub.
        latest_comment=latest_comment,
        # A findings comment a review chat's closing line links is read back (#4279).
        read_comment=read_comment,
        # A ``/reviewed`` verdict whose webhook never came is read back (#4478).
        read_comments=read_comments,
        busy=lambda work_id: work_busy(state, work_id),
        # The reviewer's verdict handed to a chat ends its review waits quietly (#3183).
        on_verdict=hooks.wake_registry.answer_reviews,
        # A pass carried over a base merge is a pending note for the chat (#4306).
        pending_note=release_note,
        # A head whose diff an earlier review read is not reviewed again (#3314).
        digest=release_digest,
        base_only=base_only_between,
        ship_merge=ship_merge_between,
        # A no-code review's fix that changed only doc files is not reviewed again (#3322).
        no_code=no_code_between,
        # A person's comment on a finished Work's issue reaches its let-go chat (#3405).
        load_work=hooks.load_work,
        # A ready press at a head under review says where that review is (#3584).
        review_chat=router_review_chat,
        wait_review=router_wait_review,
        # A wayfinder map's chat wakes when its last ticket closes (#3627).
        read_issue=read_map,
        read_parent=read_parent,
        # ... and an issue starts once its last blocker closes (#3764, #4216).
        read_dependencies=read_dependencies,
        start_ticket=start_unblocked_ticket,
        # ... or once it is deployed and waits only for a person's try (#4384).
        deployed=deployed,
        # ... or once its Work's registered pull request merged into the default branch: the
        # router records the merge, and a reopen of the Work's issue drops it (#4398).
        note_merged=getattr(db, "note_merged_pull", None),
        forget_merged=getattr(db, "forget_merged_pulls", None),
        # ... and a reopen starts a new round, whose earlier merges count for nothing (#4585).
        start_round=start_reopened_round,
        earlier_pulls=earlier_pulls,
        hold_labels=lambda repository: work_hold_labels(config.poller.root).get(
            repository.lower(), ()
        ),
        # ... and the deck keeps its lists from its tickets' answers (#3695).
        read_body=read_map_body,
        write_body=write_map_body,
        mine=deck_claims,
        interrupt=hooks.interrupt,
        # A ready head nobody started a review of is caught, and shown stuck (#3903).
        ready_pulls=router_ready_pulls,
        # Each head's review history is the router's own record too (#3955).
        review_event=state.record_review_event,
        reviews=state.review_events,
        check=check,
        forget_reviews=state.forget_review_events,
        # Ship now, fix later (#4212).
        ship=ship,
        ship_runs=ship_runs,
        # A review chat whose one pull request passed after a late minor verdict (#4474).
        # Only ChatGPT's chats: a deck reviewer already running ends with its own verdict.
        close_review=(
            close_review_chat if agent_reviewer is None and reviewer != agent_review.OFF else None
        ),
    )
    # A late minor or no-code verdict's pushed fix is checked by the router (#4474).
    release.on_late = router.late_fixed

    def work_start_spent(work_id: str) -> bool:
        # A start refused before anything ran is settled ``failed`` and is not spent (#2971).
        press = db.work_action(start_action_id(work_id)) if db is not None else None
        return press is not None and press.outcome != "failed"

    # A labelled Work whose "label added" delivery was lost is started every 15 minutes
    # from the cached issue lists, the same way the router would have (#2951).
    backstop = MissedStartBackstop(
        start_labels=lambda: work_start_labels(config.poller.root),
        hold_labels=lambda: work_hold_labels(config.poller.root),
        backlog=issues.cached,
        reread=lambda repository, number: issues.refresh_issue_records(
            [(repository, number)], limit=1
        ),
        record=cached_record,
        owned=lambda repository, number: work_owner_of_issue(state, repository, number) is not None,
        paused=release_paused,
        spent=work_start_spent,
        # Held while a blocker is open or unread (#4216), the same check as the router's.
        start_work=lambda repository, number, **kwargs: start_unless_blocked(
            hooks.start_work, repository, number, blockers=blockers, wait=True, **kwargs
        ),
        mine=deck_claims,
        enabled=enabled,
    )
    services = WorkServices(
        router=router,
        release=release,
        backstop=backstop,
        hooks=hooks,
        state=state,
        check=check,
        light=light,
        # Swept only where the services run: a deck that runs none starts no reviewer either.
        reviewer=design_reviewer if enabled else None,
        design_dropped=design_stopped,
    )
    router.filing_holds = services._filing_now
    return services


def wake_registry_for(config) -> WakeOnRegistry:
    """The wake-on registry file the web's ``WakeOnService`` keeps, opened on its own."""
    return WakeOnRegistry(registry_path(config.github_webhook.path))


# --- an outcome crossing the socket ---


def plain_outcome(outcome: object) -> dict:
    """A review start's answer, as JSON: what the router reads of it (``work_router.covers``)
    and its summary for the log. A ``chat_review.ReviewOutcome`` and an
    ``agent_review.Started`` both carry ``heads``."""
    heads = getattr(outcome, "heads", None)
    if heads is None:
        return {"type": "other", "value": None if outcome is None else str(outcome)}
    return {
        "type": "started",
        "heads": [list(pair) for pair in heads],
        "summary": str(getattr(outcome, "summary", "") or ""),
        "chat": str(getattr(outcome, "chat", "") or ""),
    }


def outcome_from(raw: dict) -> object:
    """``plain_outcome`` read back, as an ``agent_review.Started``."""
    if raw.get("type") != "started":
        return raw.get("value")
    heads = tuple(tuple(pair) for pair in raw.get("heads") or ())
    return agent_review.Started(
        heads=heads, summary=str(raw.get("summary") or ""), chat=str(raw.get("chat") or "")
    )


#: The review-start errors that cross the socket by name.
REVIEW_ERRORS: dict[str, type[Exception]] = {
    "ReviewBusy": review_refs.ReviewBusy,
    "ReviewOff": review_refs.ReviewOff,
    "ReviewError": review_refs.ReviewError,
}
