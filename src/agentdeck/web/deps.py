"""Request-scoped accessors + the auth headroom hook.

``require_access`` is a no-op in v0.x (the security model is "bind to a trusted
Tailscale/LAN interface"). Adding token or password auth later means filling in
this one dependency + a login page — no route signatures change.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from ..config import AppConfig
from ..models import Account, Session
from ..providers import PROVIDERS, SessionProvider
from ..state import AppState


async def require_access(request: Request) -> None:
    """Placeholder access gate. No-op in v0.x; the seam for future auth."""
    return None


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_accounts(request: Request) -> list[Account]:
    return request.app.state.accounts


def delegation_account(request: Request, account_key: str | None):
    """The account a machine-started chat runs on, or the refusal explaining why not.

    Shared with the session chip's review launch (#1127), which is a machine-started
    chat by another name: both must land on the deck's one delegation default rather
    than each picking an account by their own rule.
    """
    supported = [
        account
        for account in get_accounts(request)
        if PROVIDERS[account.provider_id].can_start_session(account)
    ]
    if account_key is not None:
        account = next((item for item in supported if item.key == account_key), None)
        if account is None:
            raise HTTPException(status_code=404, detail="unknown delegation account")
        return account
    preferred = [
        account
        for account in supported
        if PROVIDERS[account.provider_id].machine_delegation_default
    ]
    if len(preferred) == 1:
        return preferred[0]
    if len(supported) != 1:
        choices = ", ".join(account.key for account in supported) or "none"
        raise HTTPException(
            status_code=422,
            detail=f"account_key is required; available accounts: {choices}",
        )
    return supported[0]


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def get_db(request: Request):
    return request.app.state.db


def get_control_db(request: Request):
    """Required durable storage for control-plane identity, fences, and actions."""
    return request.app.state.control_db


def get_injector(request: Request):
    return request.app.state.injector


def get_chat_renders(request: Request):
    """The per-worker memo that lets every tab on one chat share its builds (#2865)."""
    from .chat_renders import ChatRenders

    renders = getattr(request.app.state, "chat_renders", None)
    if renders is None:
        renders = request.app.state.chat_renders = ChatRenders()
    return renders


def get_shared_fragments(request: Request):
    """The per-worker memo that keeps one SSE render serving every client.

    Built lazily so a request assembled without the full app wiring still gets a
    working cache rather than an attribute error.
    """
    from .shared_fragments import SharedFragments

    shared = getattr(request.app.state, "shared_fragments", None)
    if shared is None:
        shared = SharedFragments(get_state(request).bus)
        request.app.state.shared_fragments = shared
    return shared


def resolve_session(request: Request, session_key: str) -> tuple[Account, Session, SessionProvider]:
    """Look up a session by key → (account, session, provider), or 404."""
    state = get_state(request)
    session = state.sessions.get(session_key)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    account = next(
        (a for a in get_accounts(request) if a.key == session.account_key),
        None,
    )
    if account is None:
        raise HTTPException(status_code=404, detail="unknown account")
    provider = PROVIDERS[account.provider_id]
    return account, session, provider


def resolve_current_leg(
    request: Request, session_key: str
) -> tuple[Account, Session, SessionProvider]:
    """Resolve a control target: any leg key addresses the chat's *working* leg.

    A published URL outlives the leg it named. Every write and every live
    control therefore lands on the leg of that chat which is actually running,
    so a reply sent to yesterday's account never reaches a dead process — and,
    since a chat can be handed back to an account it used before, not to a
    living process that stopped being the conversation either (#1501). Leg-owned
    routes — per-leg transcript images, pin sequence numbers, the SSE tail,
    desk authorization — keep using ``resolve_session`` on purpose.

    When the chat has moved on and the leg it moved to cannot be reached, the
    refusal says so rather than reporting the named session as unknown: the
    difference matters to whoever is holding a chat URL that used to work. A
    URL naming a session this deck simply does not have is still "unknown
    session", which is what it has always been and what it still means.
    """
    state = get_state(request)
    leg_key = state.chat_reachable_leg_key(session_key)
    if leg_key is None:
        if len(state.chat_session_keys(session_key)) > 1:
            raise HTTPException(
                status_code=404,
                detail="this chat has no leg the deck can reach right now",
            )
        return resolve_session(request, session_key)
    return resolve_session(request, leg_key)


def resolve_chat_key(request: Request, session_key: str) -> tuple[str, Session | None]:
    """The key a route storing per-chat rows should use, and its session when scanned.

    A worker reads its key from ``/api/sessions/by-token/<token>`` before the deck
    has scanned its transcript, so ``resolve_current_leg`` still calls it unknown
    and the worker's first title or issue write used to fail (#2627). Routes that
    only store rows under the key accept such a key while a deck token is bound to
    it; any other unknown key is still refused.
    """
    try:
        _account, session, _provider = resolve_current_leg(request, session_key)
    except HTTPException as exc:
        if exc.detail != "unknown session":
            raise
        if not any(
            session_key.startswith(f"{account.key}:") for account in get_accounts(request)
        ) or not get_state(request).session_key_has_token(session_key):
            raise
        return session_key, None
    return session.key, session
