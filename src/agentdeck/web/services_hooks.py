"""What the moved Work services ask of the web, done by the web (#4016).

The Work router, the merge release and the missed-start backstop run in the services process
(``services_process``), but a few of their acts need what only the web holds: the injector
that spaces starts on one account, the warm spare checkouts, the chat board, the collector
that reads a let-go chat's transcripts, the Work overview the paused-chat hold reads. Each is
one function of ``app`` here. The web's internal routes (``routes_services``) call them for
the services process; the in-process stand-in (``in_process_hooks``) calls them directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import agent_review, live_chats, work_actions
from ..providers import PROVIDERS
from ..work_services import Hooks, held, wake_registry_for

log = logging.getLogger(__name__)

#: The ``work_actions`` kind of a reviewer start the services process asked for (#4013).
REVIEW_START = "review-start"


async def start_work(
    app: Any,
    repository: str,
    number: int,
    *,
    wait: bool = False,
    on_refused: Callable[[], object] | None = None,
    labels=(),
) -> str:
    """``start_work_agent``: the Work's one start, spent by its ``work_actions`` row."""
    from ..work_start import start_work_agent

    return await start_work_agent(
        app, repository, number, wait=wait, on_refused=on_refused, labels=labels
    )


async def review_chat(app: Any, pull: str, work: str, *, note: str = "", again: bool = False):
    """Open (or reuse) the ChatGPT review chat for ``pull`` and note the heads it covers."""
    from .routes_chat_board import note_review_head, request_review

    outcome = await request_review(
        app, pull, work=work, **({"note": note, "again": True} if again else {})
    )
    note_review_head(app, outcome)
    return outcome


def reviewer_hooks(app: Any) -> dict:
    """``AgentReviewer``'s ``start``, ``alive`` and ``checkout_of`` over the web's own parts."""
    return agent_review.deck_hooks(
        app.state.config,
        injector=app.state.injector,
        state=app.state.app_state,
        accounts=app.state.accounts,
        providers=PROVIDERS,
    )


async def start_reviewer(app: Any, kind: str, cwd: str, text: str, token: str) -> tuple[bool, str]:
    """Start one deck reviewer for the services process, once per ``token`` (#4013).

    The row is reserved before the start, so a resent call, after an answer that never
    arrived, answers "already started" rather than starting a second reviewer.
    """
    db = app.state.control_db
    action_id = f"{REVIEW_START}:{token}"
    reserved = db.reserve_work_action(action_id, "", work_actions.NEW_CHAT)
    if reserved is None:
        return False, "the deck could not record the start"
    if not reserved:
        earlier = db.work_action(action_id)
        if earlier is not None and earlier.outcome == "failed":
            return False, earlier.result or "refused"
        return True, "already started"
    started, why = await reviewer_hooks(app)["start"](kind, Path(cwd), text, token)
    db.settle_work_action(action_id, "done" if started else "failed", why)
    return started, why


async def interrupt(app: Any, chat_id: str) -> bool:
    """Stop the chat's turn (#3731): a Work whose issue went to another deck."""
    state = app.state.app_state
    chat = state.chat_by_id(chat_id)
    session = state.current_session_for_chat(chat) if chat is not None else None
    account = next(
        (item for item in app.state.accounts if session and item.key == session.account_key),
        None,
    )
    if session is None or account is None:
        return False
    result = await PROVIDERS[account.provider_id].interrupt(account, session)
    return bool(result.accepted)


async def load_work(app: Any, repository: str, number: int) -> tuple[str, ...]:
    """Read back a finished Work's let-go chats (#2841), so its issue's news reaches them.

    Answers the Work's legs now held, so the services process can wait for its copy of them
    before it routes the news (#4016)."""
    state = app.state.app_state
    path = f"/works/github_issue:{repository.lower()}#{number}"
    legs, missing = live_chats.chat_legs(state, path)
    if missing:
        await live_chats.load(state, app.state.collector, legs)
    return tuple(leg for leg in legs if leg in state.sessions)


async def refresh_overview(app: Any) -> None:
    """Rebuild the Work overview: the injector holds a paused Work's chats by it (#2956)."""
    service = getattr(app.state, "work_overview", None)
    if service is not None:
        await service.refresh()


async def register_pull(app: Any, leg: str, ref: Any) -> bool:
    """Register a pull request with a chat for its Work (#4212): one transaction puts it on
    the working leg alone, takes it off the chat's older legs and leaves every other
    registration alone, answered yes only once the database reads it back that way. A
    failed write changes nothing, in the database or in memory, so the next press writes it
    again; the web's memory then takes, for every leg of the chat, what was committed."""
    state = app.state.app_state
    if state.db is None:
        state.add_pull_request(leg, ref)
        return True
    current = state.chat_current_session_key(leg)
    keys = (*dict.fromkeys((*state.chat_session_keys(leg), leg, current)),)
    if not state.db.register_chat_pull_request(current, keys, ref):
        log.warning("services: %s was not stored for %s", ref.identity, leg)
        return False
    stored = state.db.load_session_pull_requests()
    if not held(stored, keys, ref):
        return False
    for key in keys:
        if stored.get(key):
            state.session_pull_requests[key] = stored[key]
        else:
            state.session_pull_requests.pop(key, None)
    state._republish_chat(leg)
    return True


def in_process_hooks(app: Any, *, scheduled_messages, chat_board, collector) -> Hooks:
    """The hooks of services built inside the web: the switch back, or the tests' stand-in."""

    async def start(repository: str, number: int, *, on_refused: bool = False, **kwargs) -> str:
        from ..work import WorkRef

        work_id = WorkRef(repository.lower(), number).work_id
        refused = (lambda: app.state.work_services._start_refused(work_id)) if on_refused else None
        return await start_work(app, repository, number, on_refused=refused, **kwargs)

    def board_row(number: int):
        board = chat_board.board if chat_board.enabled else None
        return board.rows.get(number) if board is not None else None

    return Hooks(
        start_work=start,
        review_chat=lambda pull, work, **kwargs: review_chat(app, pull, work, **kwargs),
        reviewer=lambda: reviewer_hooks(app),
        interrupt=lambda chat_id: interrupt(app, chat_id),
        load_work=lambda repository, number: load_work(app, repository, number),
        refresh_overview=lambda: refresh_overview(app),
        schedule=scheduled_messages.schedule,
        exists=scheduled_messages.exists,
        board_row=board_row,
        runtime_http=lambda: getattr(app.state, "runtime_http", None),
        scanned=lambda: collector.wait_initial_scans(300.0),
        wake_registry=app.state.wake_on.registry
        if getattr(app.state, "wake_on", None) is not None
        else wake_registry_for(app.state.config),
        register_pull=lambda leg, ref: register_pull(app, leg, ref),
    )
