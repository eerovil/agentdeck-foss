"""Recovering a Claude account's login from the deck (#298).

Every route answers with the same fragment, so the panel a phone is looking at is
always the current state of the flow: broken → link + code box → verified. The code
the operator pastes is written straight to the waiting CLI and is never stored,
echoed back, or logged.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..models import Account
from ..providers.claude_code.auth import LoginError
from .deps import get_accounts, get_templates, require_access

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_access)])


def _account(request: Request, account_key: str) -> Account:
    account = next((a for a in get_accounts(request) if a.key == account_key), None)
    if account is None:
        raise HTTPException(status_code=404, detail="unknown account")
    if account.provider_id != "claude_code":
        # Only Claude logins are recoverable this way; saying so beats a flow that
        # silently does nothing for a Codex account.
        raise HTTPException(status_code=400, detail="not a Claude account")
    return account


def _panel(request: Request, account: Account) -> HTMLResponse:
    watch = request.app.state.auth_watch
    html = get_templates(request).get_template("partials/account_auth.html").render(
        account=account,
        auth=watch.status(account.key),
        flow=watch.flow(account.key),
    )
    return HTMLResponse(html)


@router.get("/accounts/{account_key}/auth")
async def account_auth(request: Request, account_key: str) -> HTMLResponse:
    return _panel(request, _account(request, account_key))


@router.post("/accounts/{account_key}/auth/verify")
async def verify_account_auth(request: Request, account_key: str) -> HTMLResponse:
    """Ask the CLI again, now. The retry a human wants after signing in elsewhere."""
    account = _account(request, account_key)
    await request.app.state.auth_watch.refresh_async(account)
    return _panel(request, account)


@router.post("/accounts/{account_key}/auth/login")
async def start_account_login(request: Request, account_key: str) -> HTMLResponse:
    """Start ``claude auth login`` for this account and show the URL it printed."""
    account = _account(request, account_key)
    watch = request.app.state.auth_watch
    await asyncio.to_thread(watch.start_login, account)
    return _panel(request, account)


@router.post("/accounts/{account_key}/auth/code")
async def submit_account_login_code(request: Request, account_key: str) -> HTMLResponse:
    """Hand the browser's code to the waiting CLI, then report what the account says."""
    account = _account(request, account_key)
    form = await request.form()
    code = str(form.get("code") or "")
    watch = request.app.state.auth_watch
    try:
        await asyncio.to_thread(watch.submit_code, account, code)
    except KeyError:
        raise HTTPException(status_code=409, detail="no login is in progress") from None
    except LoginError as exc:
        # The operator's own mistake (an empty box, a stale flow) is not a server
        # error, and the panel already carries the state that explains it.
        log.debug("login code refused for %s: %s", account.key, exc)
    return _panel(request, account)


@router.post("/accounts/{account_key}/auth/cancel")
async def cancel_account_login(request: Request, account_key: str) -> HTMLResponse:
    account = _account(request, account_key)
    request.app.state.auth_watch.forget(account.key)
    return _panel(request, account)
