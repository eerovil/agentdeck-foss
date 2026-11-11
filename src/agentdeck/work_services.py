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

import inspect
import json
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import agent_review, review_paperwork, review_refs, work_release
from .poller_ops import work_hold_labels, work_start_labels, work_unassigned_elsewhere
from .scheduled_messages import ScheduledMessage
from .wake_on import (
    KIND_PULL_REQUEST,
    WakeOnRegistry,
    parse_request,
    register,
    register_deck_wait,
    registry_path,
)
from .work import WorkRef
from .work_backstop import MissedStartBackstop
from .work_claims import start_issue
from .work_router import (
    REVIEW_POLICY_PATH,
    AgainBusy,
    AgainRefused,
    ShipPorts,
    SwitchedRepositories,
    WorkRouterService,
    base_only_between,
    no_code_between,
    pull_files,
    pulls_lookup,
    work_busy,
    work_pulls,
)
from .work_router import owner_of_issue as work_owner_of_issue
from .work_router import owner_of_pull as work_owner_of_pull
from .work_signals import (
    acknowledge_comment,
    latest_comment,
    read_comment,
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


def work_reviewer(config) -> str:
    """Who reviews a Work's new ready head (#3640): chatgpt, claude, codex or off."""
    return agent_review.effective(
        config.review.reviewer, review_chats=review_refs.reviewer_available()
    )


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

    def start(self) -> None:
        self.router.start()
        self.backstop.start()
        self.release.start(self.state.bus, scanned=self.hooks.scanned)
        if self.check is not None:
            self.check.start()

    async def stop(self) -> None:
        if self.check is not None:
            await self.check.stop()
        await self.backstop.stop()
        await self.router.stop()
        await self.release.stop()

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
        "stale_findings",
        "star",
        "reorder_and_wake",
    ),
    "router": (
        "note_filed",
        "note_registered",
        "ask_again",
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
    agent_reviewer = (
        agent_review.AgentReviewer(
            kind=reviewer,
            max_reviewers=config.review.max_reviewers,
            ledger=config.review.ledger_path,
            digest=review_refs.target_digest,
            **hooks.reviewer(),
        )
        if reviewer in agent_review.PROVIDERS
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

    async def release_pull_files(pull: str) -> set[str] | None:
        repository, _, number = pull.rpartition("#")
        return await pull_files(repository, int(number)) if number.isdigit() else None

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
        merged = {ref.casefold() for ref in refs}
        hooks.wake_registry.remove(
            item.id
            for item in hooks.wake_registry.for_chat(chat_id)
            if item.kind == KIND_PULL_REQUEST
            and not item.pending_text
            and f"{item.repository}#{item.number}".casefold() in merged
        )
        return bool(requests)

    def release_note_evidence(work_id: str, sentence: str) -> bool:
        # Appended to the summary's ``correctness`` part in place of the agent rewriting it
        # (#4253); a Work with no summary yet has nothing to append to, so its agent is asked.
        # Only that part is written, by a compare-and-swap, so a person's or agent's summary
        # write from the web meanwhile is kept (#4253 review).
        from functools import partial

        from .web.work_view import WORK_INPUTS_TOPIC
        from .work_resolution import ResolutionError, evidence_added

        if db is None:
            return False
        try:
            written = db.append_work_correctness(
                work_id, partial(evidence_added, sentence=sentence)
            )
        except ResolutionError:
            return False  # no room in the part: the agent is asked to rewrite it
        if not written:
            return False
        state.bus.publish(WORK_INPUTS_TOPIC)
        return True

    async def release_rerun(work_id: str, repository: str, head: str) -> bool:
        client = hooks.runtime_http()
        if client is None:
            return False
        response = await client.post(
            "/jobs/rerun-verification",
            json={"work_id": work_id, "repository": repository, "head": head},
            timeout=180.0,
        )
        response.raise_for_status()
        return bool(response.json().get("job"))

    release = work_release.WorkReleaseService(
        work_release.ReleaseStore(config.github_webhook.store_path / work_release.STORE_FILENAME),
        work_release.Ports(
            press_is_review=reviewer == agent_review.OFF,
            ship_unfinished=state.ship_unfinished,
            shipped_findings=lambda work_id: [
                (record.pull, record.fix)
                for record in state.shipped_by_work(work_id.removeprefix("github_issue:"))
                if record.shipped
            ],
            switched=switched_repositories,
            paused=release_paused,
            starred=work_starred,
            policy=work_release.Policies(config.poller.root),
            pulls=lambda work_id: work_pulls(state, work_id),
            pulls_lookup=lambda: pulls_lookup(state),
            chat=release_chat,
            read_pull=work_release.read_pull,
            digest=release_digest,
            green_heads=release_green_heads,
            failed_heads=release_failed_heads,
            moved_heads=release_moved_heads,
            unclean_heads=release_unclean_heads,
            merge=work_release.merge,
            update_branch=work_release.update_branch,
            tell=release_tell,
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
            wait_deploy=release_wait_deploy,
            note_evidence=release_note_evidence,
            rerun_verification=release_rerun,
        ),
        enabled=enabled,
    )
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
        return await start_unless_blocked(ask_runtime, repository, number, **kwargs)

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
        split=ship_split,
        register=lambda repository, number, new: register_split(
            state, hooks.register_pull, repository, number, new
        ),
    )

    router = WorkRouterService(
        config.github_webhook.path,
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
        # Review fixes that grew the diff over 15% add a design check to the checklist (#4220).
        diff_sizes=review_refs.diff_sizes,
        # Missing paperwork refuses pre-review-done before any review starts (#4261).
        paperwork=review_paperwork.check,
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
        busy=lambda work_id: work_busy(state, work_id),
        # The reviewer's verdict handed to a chat ends its review waits quietly (#3183).
        on_verdict=hooks.wake_registry.answer_reviews,
        # A head whose diff an earlier review read is not reviewed again (#3314).
        digest=release_digest,
        base_only=base_only_between,
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
    )

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
            hooks.start_work, repository, number, wait=True, **kwargs
        ),
        mine=deck_claims,
        enabled=enabled,
    )
    services = WorkServices(
        router=router, release=release, backstop=backstop, hooks=hooks, state=state, check=check
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
    }


def outcome_from(raw: dict) -> object:
    """``plain_outcome`` read back, as an ``agent_review.Started``."""
    if raw.get("type") != "started":
        return raw.get("value")
    heads = tuple(tuple(pair) for pair in raw.get("heads") or ())
    return agent_review.Started(heads=heads, summary=str(raw.get("summary") or ""))


#: The review-start errors that cross the socket by name.
REVIEW_ERRORS: dict[str, type[Exception]] = {
    "ReviewBusy": review_refs.ReviewBusy,
    "ReviewOff": review_refs.ReviewOff,
    "ReviewError": review_refs.ReviewError,
}
