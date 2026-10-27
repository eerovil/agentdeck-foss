"""Booted Claude CLIs kept waiting for the next new chat (#3420).

Pressing Start used to wait about two seconds for Claude Code to boot before it could
read the first message. The runtime can keep a CLI booted ahead of a start
(``ClaudeWorkerHost.prepare_spare``), but only one spawned exactly as that start's
chat would be: same worker key, session token, directory, model and effort. This
module decides those ahead of time for the starts worth it -- the New chat form's
preselected account in its remembered directory, the deck chat, and a deck-started Work
while a New work form is open (#3590) -- and hands them to a press that asks for exactly
that. Any other press starts cold, as before.

A Work's spare is booted with the ``work`` role in a worktree made for it
(``.worktrees/work-spare-<hex>``, detached at a freshly fetched base), on the account a
Work start of that repository would pick now. Its agent is handed the person's text the
moment New work is pressed, before the issue exists; the Work's claim adopts the worktree
once GitHub answers (``work_start``). It is kept only while a form asked for it lately.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import meta_chat
from ..providers import PROVIDERS

log = logging.getLogger(__name__)

NEW_CHAT = "new_chat"
DECK_CHAT = "deck_chat"
WORK = "work"
SHAPES = (NEW_CHAT, DECK_CHAT, WORK)
# How long a New work form's ask keeps a Work spare (#3590): opening the form or picking
# another repository asks again.
WORK_WANTED_S = 600.0
# Asked again this often, so a runtime restart (which drops every spare) or a spare
# taken by a start the web did not see is replaced within a minute. Asking for the
# spare already waiting costs the runtime nothing.
REFRESH_S = 60.0


@dataclass(frozen=True)
class Shape:
    """One kind of start, as the runtime spawns it."""

    name: str
    account_key: str
    cwd: str
    model: str | None
    effort: str | None
    # A Work spare's (#3590): its role, and the repository whose main checkout ``cwd`` is.
    role: str | None = None
    repository: str = ""


@dataclass(frozen=True)
class _Offer:
    shape: Shape
    token: str
    key: str
    # Where the spare runs: the shape's own directory, or a Work spare's worktree.
    folder: str = ""

    @property
    def cwd(self) -> str:
        return self.folder or self.shape.cwd


def _resolved(path: str) -> str | None:
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return None


def _saved_defaults(db: Any, account: Any) -> tuple[str | None, str | None]:
    """The account's saved model and effort, as ``saved_new_session_defaults`` reads them."""
    provider = PROVIDERS[account.provider_id]
    stored = db.load_account_new_chat_defaults(account.key) if db is not None else {}
    model, effort = stored.get("model"), stored.get("effort")
    return (
        model if model and provider.is_valid_model(model) else None,
        effort if effort and provider.is_valid_effort(effort) else None,
    )


def desired_shapes(app: Any) -> list[Shape]:
    """The starts that get a spare now: what New chat and the deck chat would send."""
    config = app.state.config
    db = getattr(app.state, "db", None)
    accounts = list(app.state.accounts or [])
    shapes: list[Shape] = []
    if config.inject.enabled:
        offered = [
            account
            for account in accounts
            if PROVIDERS[account.provider_id].offers_new_session(account)
        ]
        manual = db.load_manual_new_chat_cwd() if db is not None else None
        cwd = config.inject.new_chat_default_cwd or manual
        if offered and offered[0].provider_id == "claude_code" and cwd:
            # The form preselects its first account; ``/sessions/new`` resolves the
            # directory and fills a blank model or effort from the saved defaults.
            resolved = _resolved(cwd)
            if resolved:
                model, effort = _saved_defaults(db, offered[0])
                shapes.append(Shape(NEW_CHAT, offered[0].key, resolved, model, effort))
        deck_account = meta_chat.resolve_account(accounts, config.meta_chat.account)
        deck_cwd = _resolved(config.meta_chat.cwd)
        if deck_account is not None and deck_cwd:
            # The first start sends neither; every later leg sends the saved defaults.
            exists = meta_chat.deck_chat_session_key(app.state.app_state, db) is not None
            model, effort = _saved_defaults(db, deck_account) if exists else (None, None)
            shapes.append(Shape(DECK_CHAT, deck_account.key, deck_cwd, model, effort))
    return shapes


class StartSpares:
    """The spare each kind of start may take, and the runtime asked to keep it booted."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._offers: dict[str, _Offer] = {}
        # Shapes whose spare a press took and whose start has not answered yet. Asking
        # for the next spare first would make the runtime stop the taken one before
        # the start reached it.
        self._taken: set[str] = set()
        self._followers: set[asyncio.Task] = set()
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        # The repository a New work form asked a Work spare for, and until when (#3590).
        self._work_wanted: tuple[str, float] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="start-spares")

    async def stop(self) -> None:
        for follower in list(self._followers):
            follower.cancel()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def nudge(self) -> None:
        """Ask again now: a spare was taken, or what New chat would send changed."""
        self._wake.set()

    def want_work(self, repository: str) -> None:
        """A New work form is open on ``repository``: keep a Work spare for it a while."""
        self._work_wanted = (repository, time.monotonic() + WORK_WANTED_S)
        self.nudge()

    async def _work_shape(self) -> Shape | None:
        """The Work spare a New work form asked for lately, or None (#3590)."""
        from .. import work_start
        from ..work_side_chat import local_repository_checkout

        wanted = self._work_wanted
        if wanted is None or time.monotonic() > wanted[1]:
            self._work_wanted = None
            return None
        repository = wanted[0]
        account = work_start.start_account(self._app, repository)
        if account is None:
            return None
        main = await asyncio.to_thread(
            local_repository_checkout, repository, work_start.checkout_candidates(self._app)
        )
        if not main:
            return None
        return Shape(WORK, account.key, main, None, None, role="work", repository=repository)

    async def _forget(self, offer: _Offer) -> None:
        """An offer nothing will take: a Work spare's worktree goes with it."""
        from .. import work_start

        if offer.folder:
            await asyncio.to_thread(work_start.remove_spare_worktree, offer.folder)

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 -- a start without a spare is only cold
                log.exception("could not refresh the start spares")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), REFRESH_S)

    async def refresh(self) -> None:
        from .. import work_start

        accounts = {account.key: account for account in self._app.state.accounts or []}
        shapes = desired_shapes(self._app)
        work = await self._work_shape()
        if work is not None:
            shapes.append(work)
        for name in set(self._offers) - {shape.name for shape in shapes}:
            await self._forget(self._offers.pop(name))
        # A spare nothing asks for any more -- its start gone, or moved to another
        # account -- is stopped, or the runtime would keep it booted forever. Asked of
        # every account each time, so one a previous web process left is stopped too.
        wanted = {(shape.account_key, shape.name) for shape in shapes}
        for account in accounts.values():
            drop = getattr(PROVIDERS[account.provider_id], "drop_spare", None)
            if drop is None:
                continue
            for name in SHAPES:
                if (account.key, name) not in wanted:
                    await drop(account, name)
        for shape in shapes:
            if shape.name in self._taken:
                continue
            offer = self._offers.get(shape.name)
            if offer is None or offer.shape != shape:
                if offer is not None:
                    await self._forget(offer)
                    del self._offers[shape.name]
                folder = ""
                if shape.name == WORK:
                    folder = await asyncio.to_thread(work_start.make_spare_worktree, shape.cwd)
                    if not folder:
                        continue
                offer = _Offer(shape, uuid.uuid4().hex, f"chat-{uuid.uuid4().hex[:12]}", folder)
                self._offers[shape.name] = offer
            account = accounts.get(shape.account_key)
            if account is None:
                continue
            prepare = getattr(PROVIDERS[account.provider_id], "prepare_spare", None)
            if prepare is None:
                continue
            await prepare(
                account,
                shape.name,
                offer.key,
                session_token=offer.token,
                cwd=offer.cwd,
                model=shape.model,
                effort=shape.effort,
                **({"role": shape.role} if shape.role else {}),
            )

    def claim(
        self,
        name: str,
        *,
        account_key: str,
        cwd: str,
        model: str | None,
        effort: str | None,
        action_id: str | None,
    ) -> tuple[str, str] | None:
        """The ``(session token, worker key)`` this press starts under, or None for cold.

        A resend of a press that took a spare gets that spare's token and key again, so
        the runtime answers it from its receipt instead of starting a second chat. A
        caller passes no ``action_id`` only when its own start refuses a resend.
        """
        db = getattr(self._app.state, "db", None)
        if action_id and db is not None:
            earlier = db.start_spare_claim(action_id)
            if earlier is not None:
                return earlier
        offer = self._offers.get(name)
        if offer is None or offer.shape != Shape(
            name, account_key, cwd, model or None, effort or None
        ):
            return None
        if action_id:
            if db is None:
                return None
            claimed = db.claim_start_spare(action_id, offer.token, offer.key)
            if claimed is None:
                return None
            if claimed != (offer.token, offer.key):
                return claimed
        del self._offers[name]
        self._taken.add(name)
        return offer.token, offer.key

    def claim_work(self, repository: str, *, account_key: str) -> tuple[str, str, str] | None:
        """The ``(session token, worker key, worktree)`` of the Work spare a New work press
        takes (#3590), or None for a cold start.

        No resend can reach it: the press is reserved before it starts anything, and a
        replay is answered from that reservation.
        """
        offer = self._offers.get(WORK)
        if (
            offer is None
            or offer.shape.account_key != account_key
            or offer.shape.repository.lower() != repository.lower()
        ):
            return None
        del self._offers[WORK]
        self._taken.add(WORK)
        return offer.token, offer.key, offer.cwd

    def release(self, name: str) -> None:
        """The press that took ``name``'s spare started nothing: ask for one again now."""
        self._taken.discard(name)
        self.nudge()

    def replenish_after(self, name: str, answer: Awaitable[Any]) -> None:
        """Ask for the next spare of ``name`` once the start that took one has answered."""

        async def follow() -> None:
            try:
                await answer
            finally:
                self._taken.discard(name)
                self.nudge()

        task = asyncio.create_task(follow(), name=f"start-spare-follow:{name}")
        self._followers.add(task)
        task.add_done_callback(self._followers.discard)
