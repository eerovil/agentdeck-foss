"""Machine-oriented local API for agents, dispatchers, and dashboard controls."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from .. import error_reporting, gh_cli, issue_context, shutdown
from ..context_delivery import (
    RE_DECIDED_REFUSALS,
    ContextDelivery,
    context_request_sha256,
)
from ..context_handoff import HandoffInput, HandoffMessage, render_context_handoff
from ..context_handoff_collect import collect_workspace_handoff
from ..context_policy import (
    ContextAction,
    ContextCycle,
    ContextDecision,
    ContextPolicyInput,
    MaintenanceResult,
    decide_context_action,
)
from ..models import (
    MUTE_SOURCE_BOSS,
    SNAPSHOT_CAPABILITIES_KEY,
    SNAPSHOT_CONTEXT_MANAGEMENT_V1,
    Capability,
    IssueRef,
    PollerReviewVerdict,
    PullRequestRef,
    Session,
)
from ..providers import PROVIDERS, pending_interaction_for
from ..providers.delivery import DeliveryCorrelation, DeliveryReceipts, delivery_participated
from ..providers.instructions import SessionRole
from ..public_origin import public_origin
from ..restart_nudge import NUDGE_MESSAGE, RestartNudgeRegistry
from ..state import OPERATOR_PRESENCE_IDLE_AFTER_S
from ..titles import normalize_agent_title, title_evidence_signature
from ..wake_on import KIND_DEPLOYMENT, WakeRequestError, parse_request
from ..work import work_id_for
from .chat_successor import reserve_chat_successor
from .deps import (
    delegation_account,
    get_accounts,
    get_config,
    get_control_db,
    get_db,
    get_injector,
    get_state,
    require_access,
    resolve_chat_key,
    resolve_current_leg,
    resolve_session,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(require_access)])


def _presence_payload(request: Request) -> dict:
    presence = get_state(request).operator_presence()
    return {
        "status": presence.status,
        "last_input_at": presence.last_input_at,
        "idle_for_s": presence.idle_for_s,
        "idle_after_s": OPERATOR_PRESENCE_IDLE_AFTER_S,
    }


@router.get("/presence")
async def get_presence(request: Request) -> dict:
    """Expose the single operator's dashboard-input recency."""
    return _presence_payload(request)


@router.post("/presence")
async def report_presence(request: Request) -> dict:
    """Record real browser input; the server clock is authoritative."""
    get_state(request).report_operator_input()
    return _presence_payload(request)


class DelegationRequest(BaseModel):
    cwd: str
    message: str
    account_key: str | None = None
    sandbox: Literal["read-only", "workspace-write"] = "workspace-write"
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] | None = None
    # Codex approval policy. Human-initiated delegations default to "on-request"
    # (Codex asks before commands it deems risky); an autonomous caller (e.g. the
    # kanban worker) passes "never" so it runs unattended without prompts.
    approval_policy: Literal["untrusted", "on-failure", "on-request", "never"] = "on-request"
    # Who started this delegation, so the child nests under it instead of
    # becoming its own top-level chat and its own Deckhand handoff.
    # ``parent_session_id`` is whatever identity the caller could read for
    # itself: a raw provider session id (CLAUDE_CODE_SESSION_ID) or a full
    # session key (AGENTDECK_SESSION_KEY). ``parent_session_token`` is the
    # deck-minted session token, resolved here — it is the only self-identity a
    # Codex chat has, since Codex threads share one process and cannot carry
    # per-thread environment.
    parent_session_id: str | None = None
    parent_session_token: str | None = None


def _delegation_parent_session(
    request: Request, body: DelegationRequest
) -> str | None:
    """The parent identity to record for this delegation, if any.

    An explicit id wins, because only the caller knows whose behalf it acts on;
    otherwise the session token is resolved against the binding the deck made
    when it started that chat. An unresolvable token is logged rather than
    refused: parentage is bookkeeping, and failing the delegation over it would
    be worse than the missing nesting — but it must not pass silently, which is
    exactly how fan-out waves ended up as one Deckhand handoff per child.
    """
    if body.parent_session_id:
        return body.parent_session_id
    if not body.parent_session_token:
        return None
    resolved = get_state(request).session_key_for_token(body.parent_session_token)
    if resolved is None:
        log.warning(
            "delegation named a parent session token that is bound to no session;"
            " the delegated chat will stay top-level in session history and out of"
            " Deckhand"
        )
    return resolved


def _interaction_payload(interaction) -> dict | None:
    return interaction.answer_json() if interaction is not None else None


def _require_transcript_session(request: Request, session_key: str):
    account, session, provider = resolve_session(request, session_key)
    if Capability.TRANSCRIPT not in session.capabilities:
        raise HTTPException(status_code=404, detail="transcript unavailable")
    return account, session, provider


# --- whole-deck state -------------------------------------------------
# These projections are intentionally narrow.  The local control plane is
# unauthenticated, so exposing a read API is not a reason to mirror every
# provider/process field or the latest conversation prose.  The session key
# links callers to the existing per-session surfaces when they need depth.


def _session_summary(session: Session) -> dict:
    # Keep this JSON-native instead of running FastAPI's recursive encoder once
    # per session.  A real deck has around a thousand rows; the direct shape is
    # materially cheaper and stays comfortably in the memory-read cost class.
    return {
        "key": session.key,
        "session_url": session.detail_path,
        "account_key": session.account_key,
        "status": session.status.value,
        "display_state": session.display_state,
        "title": session.display_title,
        "cwd": str(session.cwd) if session.cwd is not None else None,
        "thinking": session.thinking,
        "stalled": session.stalled,
        # Published beside `stalled` because it is the case `stalled` cannot
        # reach: a turn that failed instead of replying leaves nothing running,
        # so no timer fires and an unattended reader sees a resting chat (#778).
        "turn_failed": session.turn_failed,
        # Diagnostics, not depth: descendant work promotes an ancestor to
        # Working, so a watcher that sees a chat stuck on `thinking` cannot
        # otherwise name the descendant responsible for it (#791).
        "subagent_count": session.subagent_count,
        "activity": session.activity,
        "waiting": session.is_waiting,
        "worker_type": session.worker_type,
        "parent_session_key": session.parent_session_key,
        "is_delegated": session.is_delegated,
        "issue_url": session.issue_url,
        "issue_status": session.issue_status,
        "issues": [
            {
                "repository": issue.repository,
                "number": issue.number,
                "url": issue.url,
            }
            for issue in session.issues
        ],
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "last_activity": (
            session.last_activity.isoformat() if session.last_activity else None
        ),
        "last_progress": (
            session.last_progress.isoformat() if session.last_progress else None
        ),
        "capabilities": sorted(capability.value for capability in session.capabilities),
        "pull_requests": [
            {
                "repository": pull.repository,
                "number": pull.number,
                "url": pull.url,
            }
            for pull in session.pull_requests
        ],
    }


@router.get("/sessions")
async def list_sessions(request: Request) -> JSONResponse:
    state = get_state(request)
    return JSONResponse(
        {
            "sessions": [
                _session_summary(session) for session in state.all_sessions()
            ]
        }
    )


class ReviewVerdictBody(BaseModel):
    """One consumed poller merge-review verdict, as the poller host hands it over."""

    repo: str = Field(min_length=1, max_length=200)
    sha: str = Field(min_length=7, max_length=64)
    verdict: str = Field(min_length=1, max_length=40)
    result: str = Field(min_length=1, max_length=40)
    pull_request: str = Field(default="", max_length=200)
    issue_repo: str = Field(default="", max_length=200)
    issue_number: int = 0
    auto: bool = False
    generation: str = Field(default="", max_length=100)
    reviewed_at: str = Field(default="", max_length=64)


@router.post("/poller/review-verdicts")
async def record_poller_review_verdict(request: Request, body: ReviewVerdictBody) -> dict:
    """Keep what a poller merge review decided about one commit (#1122).

    The poller engine decides this but cannot write here — it is the stdlib-only artifact —
    so its host drains one record per reviewed commit into this route. Keyed by repository
    and commit, so a record handed over twice replaces its row instead of doubling it, which
    is what lets the host retry a delivery whose local cleanup failed.
    """
    reviewed_at = None
    if body.reviewed_at:
        try:
            reviewed_at = datetime.fromisoformat(body.reviewed_at)
        except ValueError:
            reviewed_at = None
    # Always true since the store became required (#2601); the field stays because the
    # poller host reads it. `recorded` is only "stop holding this".
    stored = True
    # Through the state store rather than straight at the table, so a session chip
    # already showing this commit as unreviewed learns that it is not (#1127).
    get_state(request).record_review_verdict(
        PollerReviewVerdict(
            repo=body.repo,
            sha=body.sha,
            verdict=body.verdict,
            result=body.result,
            pull_request=body.pull_request,
            issue_repo=body.issue_repo,
            issue_number=body.issue_number,
            auto=body.auto,
            generation=body.generation,
            reviewed_at=reviewed_at,
            recorded_at=datetime.now(UTC),
        )
    )
    return {"recorded": True, "stored": stored, "repo": body.repo, "sha": body.sha}


class SessionReviewVerdictBody(BaseModel):
    """A verdict from a review the deck started for one commit (#1127)."""

    repo: str = Field(min_length=1, max_length=200)
    sha: str = Field(min_length=7, max_length=64)
    verdict: Literal["pass", "changes-needed"]
    pull_request: str = Field(default="", max_length=200)


@router.post("/review-verdicts")
async def record_review_verdict(request: Request, body: SessionReviewVerdictBody) -> dict:
    """Keep what a review the deck started decided about one commit (#1127).

    The same record and the same table as a poller merge review: a verdict is about a
    commit, and where the review came from does not change which commit it read. What
    differs is ``result`` — nothing released or handed back on the strength of this
    one, so it records ``none`` rather than borrowing a release word it did not earn.

    The reviewer is an ordinary agent, so the vocabulary is closed: `pass` or
    `changes-needed`, the two words the poller's own reviewers report.
    """
    state = get_state(request)
    stored = True
    now = datetime.now(UTC)
    state.record_review_verdict(
        PollerReviewVerdict(
            repo=body.repo,
            sha=body.sha,
            verdict=body.verdict,
            result="none",
            pull_request=body.pull_request,
            reviewed_at=now,
            recorded_at=now,
        )
    )
    return {"recorded": True, "stored": stored, "repo": body.repo, "sha": body.sha}


@router.get("/poller/review-verdicts")
async def get_poller_review_verdicts(
    request: Request, limit: int = 200, repo: str = ""
) -> dict:
    """The recorded review history, newest first."""
    verdicts = get_db(request).load_poller_review_verdicts(
        limit=max(1, min(int(limit), 1000)), repo=repo or None
    )
    return {"review_verdicts": jsonable_encoder(verdicts)}


@router.get("/sessions/{session_key}/interaction")
async def get_session_interaction(request: Request, session_key: str) -> dict:
    """This session's actionable interaction, in the shape needed to answer it.

    Deliberately per-session rather than a field on ``/api/sessions``: the read
    is a live provider call, and a real deck lists around a thousand rows.

    An agent speaking through the answer route needs the interaction id and the
    per-question ids, and only the HTML partial used to carry them. Without this
    a reply to a pending question went out as an ordinary injected message, sat
    in the worker's queue, and arrived after the boss answered (#590).
    """
    account, session, provider = resolve_current_leg(request, session_key)
    session_key = session.key
    return {
        "interaction": _interaction_payload(provider.pending_interaction(account, session)),
        "answer_url": f"/sessions/{session.key}/interaction",
    }


@router.get("/usage")
async def get_usage(request: Request) -> dict:
    state = get_state(request)
    return {
        "stale_after_s": state.usage_stale_after_s,
        "usage": [
            jsonable_encoder(state.usage[account_key])
            for account_key in sorted(state.usage)
        ],
    }


@router.get("/host-stats")
async def get_host_stats(request: Request) -> dict:
    return {"host_stats": jsonable_encoder(get_state(request).host_stats)}


@router.get("/git-context")
async def get_git_context(request: Request) -> dict:
    state = get_state(request)
    contexts = request.app.state.assistant.contexts
    return {
        "git_context": {
            session.key: contexts[session.key].as_json()
            for session in state.all_sessions()
            if session.key in contexts
        }
    }


@router.get("/issues")
async def get_issues(request: Request, refresh: bool = False) -> dict:
    """Open issues in every repository the poller manifests declare.

    Read this instead of running ``gh`` — the deck already resolved it through
    the shared, rate-limited GitHub cache. ``complete: false`` means the deck
    could not look, never that nothing is pending; ``unknown`` names which
    repositories that applies to.
    """
    backlog = await request.app.state.issues.resolve(force=refresh)
    return {"issues": backlog.as_json()}


@router.get("/pending")
async def get_pending(request: Request, refresh: bool = False) -> dict:
    """Every pending item, joined across chats, GitHub issues, and board cards.

    A waiting chat, its issue, and its board card are one item here and three
    rows elsewhere; they are joined on repository plus issue number, taken from
    registered issue links and ``agent/issue-<n>`` branch names. Grouping is not
    stored — it is prose a reader produces from this list.
    """
    view = await _pending_view(request, refresh=refresh)
    return {"pending": view.as_json()}


async def _pending_view(request: Request, *, refresh: bool = False):
    state = get_state(request)
    backlog = await request.app.state.issues.resolve(force=refresh)
    return issue_context.join_pending(
        backlog,
        state.all_sessions(),
        request.app.state.assistant.contexts,
        attention_report_for=state.attention_report_for,
    )


@router.get("/sessions/{session_key}/transcript-check")
async def transcript_check(request: Request, session_key: str, expect: str = "") -> dict:
    """Whether the deck's own reader parses this transcript cleanly (#3205).

    The CLI upgrade's check chat asks for one word and reads it back here, through the
    reader the deck renders with, so a CLI that changed its transcript shape fails the
    check. Only counts and a yes/no come back: this API mirrors no conversation prose.
    """
    account, session, provider = _require_transcript_session(request, session_key)
    detail = await provider.load_transcript(account, session)
    replies = [event for event in detail.events if event.role == "assistant"]
    return {
        "session_key": session_key,
        "total_events": detail.total_events,
        "skipped": detail.skipped,
        "replied": any(not event.failed and (event.text or "").strip() for event in replies),
        "failed": any(event.failed for event in replies),
        "found": bool(expect)
        and any(expect in (event.text or "") for event in replies if not event.failed),
    }


@router.get("/sessions/{session_key}/context-deliveries")
async def list_context_deliveries(request: Request, session_key: str) -> dict:
    """Recent durable context decisions for this logical chat."""
    _account, session, _provider = resolve_current_leg(request, session_key)
    chat_id = get_state(request).chat_identity_key(session.key)
    return {
        "chat_id": chat_id,
        "context_deliveries": jsonable_encoder(
            get_control_db(request).list_chat_context_deliveries(chat_id)
        ),
    }


# --- session self-identification ------------------------------------------
# A Codex thread never learns its own session key from the environment, and a
# freshly spawned Claude worker has none on its first turn. The deck therefore
# mints a token before starting the session, hands it to the agent inside the
# instructions it already injects, and binds it here once the provider returns
# an id. This is a loopback-only identity lookup, not an authorization secret.


def _joined_works(request: Request, session_key: str) -> tuple[str, ...]:
    """The Works this chat joined itself, as ``work_id``s.

    The Work projection first; a chat not scanned in yet has no projection entry, so the
    issues it registered itself answer then.
    """
    from ..work import WorkRef

    state = get_state(request)
    try:
        chat_id = state.chat_identity_key(session_key)
        works = state.work_projection().works_chat_joined(chat_id)
        if not works:
            works = tuple(WorkRef.from_issue(ref) for ref in state.issues_for(session_key))
    except Exception:  # noqa: BLE001 - a lookup that fails names no Work
        return ()
    return tuple(dict.fromkeys(work.work_id for work in works))


@router.get("/sessions/by-token/{token}")
async def session_for_token(request: Request, token: str) -> dict:
    session_key = get_state(request).session_key_for_token(token)
    if session_key is None:
        raise HTTPException(status_code=404, detail="unknown session token")
    session = get_state(request).sessions.get(session_key)
    chat_path = get_state(request).chat_path_for_session(session_key)
    # The same lookup a worker already makes for its key now also tells it this
    # deck's public address, so it can link a file or register its own session
    # URL without guessing a domain (#1838). Empty when no address is known.
    origin = public_origin()
    # The Run this chat most recently joined (#2142), so `agentdeck job submit --verification`
    # attaches its evidence to the deck's own answer rather than to anything the caller types
    # (#2154). Empty for a chat no poller delivery ever reached.
    latest_run = getattr(getattr(get_state(request), "db", None), "latest_session_run", None)
    binding = latest_run(session_key) if latest_run is not None else None
    # A chat in no Run names the one Work it joined (#2895): a Work the deck releases has no
    # Poller card, so its `--verification` job proves that Work's head alone. A chat in
    # several gets them all as ``works``, and `--work` picks one (#3392).
    # A Run-bound chat lists its Run's Work first, then the others it joined itself, which
    # `--work` may name outside the Run (#3392).
    joined = _joined_works(request, session_key)
    work_id = binding.work_id if binding is not None else ""
    if binding is None and len(joined) == 1:
        work_id = joined[0]
    return {
        "session_key": session_key,
        "work_id": work_id,
        "works": list(dict.fromkeys(work for work in (work_id, *joined) if work)),
        "run_id": binding.run_id if binding is not None else "",
        "session_url": chat_path,
        "public_origin": origin,
        "public_session_url": f"{origin}{chat_path}" if origin and chat_path else "",
        # Absent while a just-started session has not been scanned in yet. Until
        # then only the routes that store rows under the key accept it — title,
        # issues, pull-requests, restart-nudge and work-title (#2627); the rest
        # answer "unknown session" until the first scan.
        "display_title": session.display_title if session is not None else None,
    }


# --- registered pull requests --------------------------------------------
# A session's pull requests are exactly the ones registered here, so the chat
# that did the work decides what belongs to it. Nothing is inferred from
# transcript text or from whichever branch a shared checkout sits on.


class PullRequestsRequest(BaseModel):
    pull_requests: list[str] = Field(default_factory=list)
    # The Work the newly listed pull requests are for (#3392); see ``PullRequestRequest``.
    work_id: str = ""


class PullRequestRequest(BaseModel):
    pull_request: str
    # The Work this pull request is for (#3392): a ``work_id`` or ``owner/repo#n`` of an
    # issue this chat registered. Needed only by a chat in several Works.
    work_id: str = ""


class IssuesRequest(BaseModel):
    issues: list[str] = Field(default_factory=list)


class IssueRequest(BaseModel):
    issue: str


def _issue_ref(value: str) -> IssueRef:
    ref = IssueRef.parse(value)
    if ref is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"issue must be a github.com issue URL or owner/repo#number; got {value!r}"
            ),
        )
    return ref


def _issues_payload(session_key: str, refs: tuple[IssueRef, ...]) -> dict:
    return {
        "session_key": session_key,
        "issues": [
            {"repository": ref.repository, "number": ref.number, "url": ref.url}
            for ref in refs
        ],
    }


@router.get("/sessions/{session_key}/issues")
async def list_session_issues(request: Request, session_key: str) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    return _issues_payload(session_key, get_state(request).issues_for(session_key))


class PlannedResetRequest(BaseModel):
    at: str = Field(max_length=64)


def _planned_reset_account(request: Request, account_key: str):
    account = next((item for item in get_accounts(request) if item.key == account_key), None)
    if account is None:
        raise HTTPException(status_code=404, detail="unknown account")
    return account


def _set_planned_reset(request: Request, account_key: str, text: str) -> dict:
    from .. import planned_reset

    account = _planned_reset_account(request, account_key)
    try:
        at = planned_reset.apply(get_db(request), get_state(request), account.key, text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"account": account.key, "at": at.isoformat() if at else None}


@router.get("/accounts/{account_key}/planned-reset")
async def get_planned_reset(request: Request, account_key: str) -> dict:
    """This account's planned usage reset while it is still ahead (#3379)."""
    account = _planned_reset_account(request, account_key)
    at = get_db(request).planned_resets().get(account.key)
    return {"account": account.key, "at": at.isoformat() if at else None}


@router.put("/accounts/{account_key}/planned-reset")
async def put_planned_reset(
    request: Request, account_key: str, body: PlannedResetRequest
) -> dict:
    """Plan a usage reset: ``HH:MM`` on the host's clock or an ISO time (#3379)."""
    if not body.at.strip():
        raise HTTPException(status_code=400, detail="give a time; DELETE clears the plan")
    return _set_planned_reset(request, account_key, body.at)


@router.delete("/accounts/{account_key}/planned-reset")
async def delete_planned_reset(request: Request, account_key: str) -> dict:
    return _set_planned_reset(request, account_key, "")


@router.put("/sessions/{session_key}/issues")
async def put_session_issues(
    request: Request, session_key: str, body: IssuesRequest
) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    refs = [_issue_ref(value) for value in body.issues]
    return _issues_payload(session_key, get_state(request).set_issues(session_key, refs))


@router.post("/sessions/{session_key}/issues")
async def add_session_issue(
    request: Request, session_key: str, body: IssueRequest
) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    ref = _issue_ref(body.issue)
    return _issues_payload(session_key, get_state(request).add_issue(session_key, ref))


@router.delete("/sessions/{session_key}/issues")
async def delete_session_issue(request: Request, session_key: str, issue: str) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    ref = _issue_ref(issue)
    return _issues_payload(session_key, get_state(request).remove_issue(session_key, ref))


class FiledIssueRequest(BaseModel):
    issue: str


#: How recently an issue must have changed on GitHub for ``/api/works/filed`` to start it.
FILED_RECENT_S = 3600.0


@router.post("/works/filed")
async def note_filed_issue(request: Request, body: FiledIssueRequest) -> dict:
    """An issue the deck's ``gh`` shim just filed starts its Work now, not on the webhook
    (#3371).

    Nothing the caller says is trusted but the issue's address: its state and labels are read
    back from GitHub, and only an open issue changed in the last hour that carries its
    project's start label is started. That is no more than GitHub's own delivery, or the
    missed-start backstop, would start, and ``start_work`` still spends a Work's start once.
    """
    ref = _issue_ref(body.issue)
    router_service = getattr(request.app.state, "work_router", None)
    issues = getattr(request.app.state, "issues", None)
    answer = {"issue": ref.url, "starting": False}
    if router_service is None or issues is None or not router_service.switched(ref.repository):
        return answer
    key = (ref.repository, ref.number)
    try:
        read = await issues.refresh_issue_records([key], limit=1)
    except Exception:  # noqa: BLE001 - the webhook and the backstop still start it
        log.warning("filed issue %s could not be read back", ref.url, exc_info=True)
        return answer
    try:
        record = issues.cached_issues([key]).get(key) if read else None
    except Exception:  # noqa: BLE001 - the webhook and the backstop still start it
        log.warning("filed issue %s could not be read from the cache", ref.url, exc_info=True)
        return answer
    labels = (record or {}).get("labels")
    if (
        not record
        or str(record.get("state") or "").lower() != "open"
        or record.get("is_pr")
        or not isinstance(labels, list)
        or not _changed_lately(record.get("updatedAt"))
    ):
        return answer
    answer["starting"] = router_service.note_filed(ref.repository, ref.number, labels)
    return answer


def _changed_lately(stamp: object) -> bool:
    try:
        when = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        return False
    # Either way round, so a clock a little ahead of GitHub's still counts it.
    return abs(datetime.now(UTC).timestamp() - when.timestamp()) <= FILED_RECENT_S


def _pull_request_ref(value: str) -> PullRequestRef:
    ref = PullRequestRef.parse(value)
    if ref is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"pull request must be a github.com pull URL or owner/repo#number; got {value!r}"
            ),
        )
    return ref


def _pull_requests_payload(session_key: str, refs: tuple[PullRequestRef, ...]) -> dict:
    return {
        "session_key": session_key,
        "pull_requests": [
            {"repository": ref.repository, "number": ref.number, "url": ref.url}
            for ref in refs
        ],
    }


@router.get("/sessions/{session_key}/pull-requests")
async def list_session_pull_requests(request: Request, session_key: str) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    return _pull_requests_payload(
        session_key, get_state(request).pull_requests_for(session_key)
    )


@router.put("/sessions/{session_key}/pull-requests")
async def put_session_pull_requests(
    request: Request, session_key: str, body: PullRequestsRequest
) -> dict:
    """Replace the whole list — the session states which PRs are now its own."""
    named, _session = resolve_chat_key(request, session_key)
    refs = [_pull_request_ref(value) for value in body.pull_requests]
    state = get_state(request)
    before = {ref.identity: ref for ref in state.pull_requests_for(named)}
    # A named Work applies to every listed pull request (#3392). Unnamed, a newly listed one
    # gets the chat's own Work and one already registered keeps its Work, or none if it was
    # registered before Works were recorded.
    if body.work_id or any(ref.identity not in before for ref in refs):
        work = _pull_work(request, session_key, named, body.work_id)
        refs = [
            ref
            if ref.identity in before and not body.work_id
            else dataclasses.replace(ref, work_id=work)
            for ref in refs
        ]
    session_key = named
    saved = state.set_pull_requests(session_key, refs)
    _note_registered(
        request,
        [
            ref
            for ref in saved
            if ref.identity not in before or before[ref.identity].work_id != ref.work_id
        ],
    )
    return _pull_requests_payload(session_key, saved)


@router.post("/sessions/{session_key}/pull-requests")
async def add_session_pull_request(
    request: Request, session_key: str, body: PullRequestRequest
) -> dict:
    """Register one more PR, so opening a second one needs no read-modify-write."""
    named, _session = resolve_chat_key(request, session_key)
    ref = _pull_request_ref(body.pull_request)
    known = {item.identity: item for item in get_state(request).pull_requests_for(named)}
    if body.work_id or not known.get(ref.identity, ref).work_id:
        ref = dataclasses.replace(
            ref, work_id=_pull_work(request, session_key, named, body.work_id)
        )
    session_key = named
    saved = get_state(request).add_pull_request(session_key, ref)
    _note_registered(request, [ref])
    return _pull_requests_payload(session_key, saved)


def _pull_work(request: Request, session_key: str, chat_key: str, named: str) -> str:
    """The Work a pull request this chat registers is for (#3392), or 409 saying why not.

    The same answer the Work summary takes: the chat's Run, else the one Work it joined, else
    the one it names among its own. A chat in no Work registers it for none; a chat in
    several is refused here, at registration, rather than later when it asks for a review.
    """
    from ..work import WorkRef

    wanted = named.strip()
    if wanted and WorkRef.from_work_id(wanted) is None and IssueRef.parse(wanted) is None:
        raise HTTPException(
            status_code=422,
            detail=f"work_id must be a Work id or owner/repo#number; got {named!r}",
        )
    binding = _session_run(request, session_key, chat_key, work_id=wanted, required=False)
    return binding.work_id if binding is not None else ""


def _note_registered(request: Request, refs: list[PullRequestRef]) -> None:
    """A registered pull request that already sits ready is reviewed now (#2895): its own
    ready event may have come before this registration, when no Work owned it."""
    router = getattr(request.app.state, "work_router", None)
    if router is None:
        return
    for ref in refs:
        router.note_registered(ref.repository, ref.number)


@router.delete("/sessions/{session_key}/pull-requests")
async def delete_session_pull_request(
    request: Request, session_key: str, pull_request: str
) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    ref = _pull_request_ref(pull_request)
    return _pull_requests_payload(
        session_key, get_state(request).remove_pull_request(session_key, ref)
    )


# --- session title --------------------------------------------------------
# The chat may name itself. Historical Deckhand-generated rows remain readable,
# while new writes are session-authored and persist until explicitly deleted.


class SessionTitleRequest(BaseModel):
    title: str


def _title_payload(request: Request, session_key: str) -> dict:
    state = get_state(request)
    session = state.sessions.get(session_key)
    record = state.generated_title_for(session_key)
    return {
        "session_key": session_key,
        "title": record.title if record is not None else None,
        "display_title": session.display_title if session is not None else None,
        "native_title": session.title if session is not None else None,
        "source": record.source if record is not None else None,
    }


@router.get("/sessions/{session_key}/title")
async def get_session_title(request: Request, session_key: str) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    return _title_payload(request, session_key)


@router.put("/sessions/{session_key}/title")
async def put_session_title(
    request: Request, session_key: str, body: SessionTitleRequest
) -> dict:
    session_key, session = resolve_chat_key(request, session_key)
    title = normalize_agent_title(session, body.title)
    if title is None:
        raise HTTPException(status_code=422, detail="title must not be empty")
    # Nothing reads the signature back; a chat not scanned yet has no prompts to sign.
    signature = title_evidence_signature(session) if session is not None else ""
    get_state(request).set_generated_title(session_key, title, signature, source="agent")
    return _title_payload(request, session_key)


@router.delete("/sessions/{session_key}/title")
async def delete_session_title(request: Request, session_key: str) -> dict:
    """Drop the stored title; the provider-native one shows again.

    A title the operator typed by hand is theirs to undo: this refuses to clear
    one, exactly as the PUT above refuses to overwrite it.
    """
    session_key, _session = resolve_chat_key(request, session_key)
    get_state(request).clear_generated_title(session_key)
    return _title_payload(request, session_key)


# --- Work resolution --------------------------------------------------------
# The verify worker writes its Work's decision record while the final suite runs (#2328). The
# Work and Run are the deck's own answer for this chat (its latest Run binding, as by-token
# reports them), never something the caller types, and every write replaces the Work's one
# record, so a retried or later write never leaves a duplicate or stale summary behind.


class WorkResolutionRequest(BaseModel):
    problem: str
    solution: str
    rationale: str
    correctness: str
    # Which Work, for a chat with no Run that joined several (#2552).
    work_id: str = ""


def _resolution_payload(work_id: str, resolution: Any) -> dict:
    if resolution is None:
        return {"work_id": work_id, "resolution": None}
    return {
        "work_id": work_id,
        "resolution": {
            "problem": resolution.problem,
            "solution": resolution.solution,
            "rationale": resolution.rationale,
            "correctness": resolution.correctness,
            "updated_at": (
                resolution.updated_at.isoformat() if resolution.updated_at is not None else None
            ),
            "run_id": resolution.run_id or None,
            "session_key": resolution.session_key or None,
        },
    }


def _session_run(
    request: Request, *keys: str, work_id: str = "", required: bool = True
) -> Any:
    """The Run this chat most recently joined, else the one Work it joined itself, else 409.

    ``keys`` are the leg the caller named and the chat's working leg, in that order: the
    binding was made on whichever leg the delivery reached. Not ``required``, a chat in no
    Work at all answers ``None`` rather than 409.
    """
    from ..work import WorkRef

    # Named in a write's body, or as ``?work_id=`` where a route has no body (GET, DELETE).
    wanted = (work_id or request.query_params.get("work_id") or "").strip()
    named = WorkRef.from_work_id(wanted)
    if named is None and (issue := IssueRef.parse(wanted)) is not None:
        named = WorkRef.from_issue(issue)  # ``owner/repo#n`` names its Work too (#3392)
    wanted = named.work_id if named is not None else wanted  # one spelling per issue
    latest_run = getattr(getattr(get_state(request), "db", None), "latest_session_run", None)
    binding = None
    for key in dict.fromkeys(keys):
        binding = latest_run(key) if latest_run is not None else None
        if binding is not None:
            break

    # The same Works by-token lists, so the three asks agree (#3392).
    joined = list(_joined_works(request, keys[-1]))
    if binding is not None and binding.work_id:
        # A Run binding never ends, so a chat that finished its Run and took on another Work
        # by its own act may name that one, outside the Run (#3392); and once it is in
        # several, it must name one rather than fall back on the Run's.
        joined = list(dict.fromkeys([binding.work_id, *joined]))
        if wanted == binding.work_id or (not wanted and len(joined) == 1):
            return binding
        if wanted and wanted not in joined:
            raise HTTPException(
                status_code=409, detail=f"this chat's Run is for {binding.work_id}, not {wanted}"
            )
    # A chat no Run reached, such as a person's rescue chat, may still write for a Work it
    # joined by its own saved act (#2552). Only one: a chat in several names which, and may
    # only name one of its own — never a Work it merely typed.
    if wanted and wanted not in joined:
        raise HTTPException(status_code=409, detail=f"this chat is not part of {wanted}")
    if wanted or len(joined) == 1:
        return _OwnWork(wanted or joined[0])
    if joined:
        raise HTTPException(
            status_code=409,
            detail="this chat is part of several Works; name one as work_id: "
            + ", ".join(sorted(joined)),
        )
    if not required:
        return None
    raise HTTPException(
        status_code=409,
        detail="this chat is not part of any Work's Run; register the Work's issue with "
        "POST /api/sessions/<key>/issues first",
    )


@dataclasses.dataclass(frozen=True)
class _OwnWork:
    """A Work a chat joined without a Run: it writes under no Run id."""

    work_id: str
    run_id: str = ""


@router.get("/sessions/{session_key}/work-resolution")
async def get_work_resolution(request: Request, session_key: str) -> dict:
    _account, session, _provider = resolve_current_leg(request, session_key)
    binding = _session_run(request, session_key, session.key)
    resolution = get_control_db(request).work_resolution(binding.work_id)
    return _resolution_payload(binding.work_id, resolution)


@router.put("/sessions/{session_key}/work-resolution")
async def put_work_resolution(
    request: Request, session_key: str, body: WorkResolutionRequest
) -> dict:
    from ..work_resolution import ResolutionError, WorkResolution, normalize_part

    _account, session, _provider = resolve_current_leg(request, session_key)
    binding = _session_run(request, session_key, session.key, work_id=body.work_id)
    try:
        parts = {
            name: normalize_part(name, getattr(body, name))
            for name in ("problem", "solution", "rationale", "correctness")
        }
    except ResolutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stored = get_control_db(request).set_work_resolution(
        WorkResolution(
            binding.work_id, run_id=binding.run_id, session_key=session.key, **parts
        )
    )
    if stored is None:
        raise HTTPException(status_code=503, detail="the Work resolution could not be stored")
    # An open Work page shows it: announced, so the page's stream reads it (#3228).
    from .work_view import WORK_INPUTS_TOPIC

    get_state(request).bus.publish(WORK_INPUTS_TOPIC)
    return _resolution_payload(binding.work_id, stored)


# --- Work live check ------------------------------------------------------
# What the Work's agent found when it checked its merged change in the live app (#3141), found
# the same way as the resolution. The deck asks for it once the Work's last pull request
# merged; until it is recorded, the Work's closed issue does not ask a person to finish it: it
# reads "Checking it live — waiting on …" while the agent waits on the deploy, and the agent's
# own turn or the ask for a next step otherwise. Once recorded, the Finish line says what the
# check found.


class LiveCheckRequest(BaseModel):
    result: str
    note: str = ""
    work_id: str = ""  # as on the resolution: which Work, for a Run-less chat in several


def _live_check_payload(work_id: str, check: Any) -> dict:
    if check is None:
        return {"work_id": work_id, "live_check": None}
    return {
        "work_id": work_id,
        "live_check": {
            "result": check.result,
            "note": check.note,
            "checked_at": check.checked_at.isoformat() if check.checked_at else None,
            "session_key": check.session_key or None,
        },
    }


@router.get("/sessions/{session_key}/live-check")
async def get_live_check(request: Request, session_key: str) -> dict:
    _account, session, _provider = resolve_current_leg(request, session_key)
    binding = _session_run(request, session_key, session.key)
    return _live_check_payload(
        binding.work_id, get_control_db(request).work_live_check(binding.work_id)
    )


@router.put("/sessions/{session_key}/live-check")
async def put_live_check(request: Request, session_key: str, body: LiveCheckRequest) -> dict:
    from ..work_resolution import ResolutionError, normalize_live_check

    _account, session, _provider = resolve_current_leg(request, session_key)
    binding = _session_run(request, session_key, session.key, work_id=body.work_id)
    try:
        result, note = normalize_live_check(body.result, body.note)
    except ResolutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stored = get_control_db(request).set_work_live_check(
        binding.work_id, result, note, session.key
    )
    if stored is None:
        raise HTTPException(status_code=503, detail="the live check could not be stored")
    await _refresh_works(request)
    return _live_check_payload(binding.work_id, stored)


# --- Work title -----------------------------------------------------------
# The name a worker gives its Work when it files its report (#2474), found the same way as the
# resolution above. It is kept apart from the name the operator typed on the Work page, which
# always wins: a worker can neither replace nor clear it, and the reply's ``source`` says
# which one shows. The GitHub issue is never renamed.


class WorkTitleRequest(BaseModel):
    title: str
    work_id: str = ""  # as on the resolution: which Work, for a Run-less chat in several


def _work_title_payload(request: Request, work_id: str) -> dict:
    db = get_control_db(request)
    custom = db.work_titles().get(work_id, "")
    agent = db.work_agent_titles().get(work_id, "")
    shown = custom or agent
    return {
        "work_id": work_id,
        "title": shown or None,
        "agent_title": agent or None,
        "source": ("user" if custom else "agent") if shown else None,
    }


async def _refresh_works(request: Request) -> None:
    service = getattr(request.app.state, "work_overview", None)
    if service is None:
        return
    try:
        await service.refresh()
    except Exception:  # noqa: BLE001 -- the name is recorded; the next pass shows it
        log.exception("work overview refresh after a worker renamed its Work failed")


@router.get("/sessions/{session_key}/work-title")
async def get_work_title(request: Request, session_key: str) -> dict:
    chat_key, _session = resolve_chat_key(request, session_key)
    binding = _session_run(request, session_key, chat_key)
    return _work_title_payload(request, binding.work_id)


@router.put("/sessions/{session_key}/work-title")
async def put_work_title(request: Request, session_key: str, body: WorkTitleRequest) -> dict:
    from ..work_resolution import normalize_title

    chat_key, _session = resolve_chat_key(request, session_key)
    binding = _session_run(request, session_key, chat_key, work_id=body.work_id)
    title = normalize_title(body.title)
    if not title:
        raise HTTPException(status_code=422, detail="title must not be empty")
    if not get_control_db(request).set_work_agent_title(binding.work_id, title, chat_key):
        raise HTTPException(status_code=503, detail="the Work title could not be stored")
    await _refresh_works(request)
    return _work_title_payload(request, binding.work_id)


@router.delete("/sessions/{session_key}/work-title")
async def delete_work_title(request: Request, session_key: str) -> dict:
    chat_key, _session = resolve_chat_key(request, session_key)
    binding = _session_run(request, session_key, chat_key)
    if not get_control_db(request).set_work_agent_title(binding.work_id, "", chat_key):
        raise HTTPException(status_code=503, detail="the Work title could not be stored")
    await _refresh_works(request)
    return _work_title_payload(request, binding.work_id)


# --- session attention ----------------------------------------------------
# Sessions report only the state; their display title identifies the card and
# the transcript carries the context. AppState scopes the durable report to the
# current user turn.


class SessionAttentionRequest(BaseModel):
    state: Literal["waiting", "finished"]


def _attention_payload(request: Request, session_key: str) -> dict:
    report = get_state(request).attention_report_for(session_key)
    return {
        "session_key": session_key,
        "state": report.state if report is not None else None,
        "updated_at": report.updated_at if report is not None else None,
        "detail": report.detail if report is not None else "",
    }


@router.get("/sessions/{session_key}/attention")
async def get_session_attention(request: Request, session_key: str) -> dict:
    _account, _session, _provider = resolve_current_leg(request, session_key)
    session_key = _session.key
    return _attention_payload(request, session_key)


@router.put("/sessions/{session_key}/attention")
async def put_session_attention(
    request: Request, session_key: str, body: SessionAttentionRequest
) -> dict:
    _account, _session, _provider = resolve_current_leg(request, session_key)
    session_key = _session.key
    get_state(request).set_attention_report(session_key, body.state)
    return _attention_payload(request, session_key)


@router.delete("/sessions/{session_key}/attention")
async def delete_session_attention(request: Request, session_key: str) -> dict:
    _account, _session, _provider = resolve_current_leg(request, session_key)
    session_key = _session.key
    get_state(request).clear_attention_report(session_key)
    return _attention_payload(request, session_key)


# --- mute leases (#823) ---------------------------------------------------
# Silence the ordinary attention pushes for a chat somebody is attending. A
# lease, not a flag — it expires on its own, is renewed only while whatever
# granted it still holds, and every uncertainty resolves to unmuted.


class SessionMuteRequest(BaseModel):
    source: Literal[MUTE_SOURCE_BOSS] = MUTE_SOURCE_BOSS


def _mute_payload(request: Request, session_key: str) -> dict:
    lease = get_state(request).mute_lease(session_key)
    return {
        "session_key": session_key,
        "muted": lease is not None,
        "source": lease.source if lease is not None else None,
        "expires_at": lease.expires_at if lease is not None else None,
        "hard_expires_at": lease.hard_expires_at if lease is not None else None,
    }


@router.get("/sessions/{session_key}/mute")
async def get_session_mute(request: Request, session_key: str) -> dict:
    _account, _session, _provider = resolve_current_leg(request, session_key)
    session_key = _session.key
    return _mute_payload(request, session_key)


@router.put("/sessions/{session_key}/mute")
async def put_session_mute(
    request: Request, session_key: str, body: SessionMuteRequest
) -> dict:
    """Open or renew the lease on one chat somebody is attending.

    A chat blocked on a pending interaction unmutes instead of muting. Only the
    boss can answer one of those, so attending it is a contradiction (#590), and
    silencing it is the shape of the #541 incident.
    """
    account, session, provider = resolve_current_leg(request, session_key)
    session_key = session.key
    state = get_state(request)
    if provider.pending_interaction(account, session) is not None:
        state.release_mute_lease(session_key)
        raise HTTPException(
            status_code=409,
            detail=(
                "this chat is blocked on a pending interaction only the boss can"
                " answer; it cannot be babysat"
            ),
        )
    if state.grant_mute_lease(session_key, source=body.source) is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "this chat's mute cap is spent for now; it can be taken on"
                " again once the boss has engaged with the chat"
            ),
        )
    return _mute_payload(request, session_key)


@router.delete("/sessions/{session_key}/mute")
async def delete_session_mute(request: Request, session_key: str) -> dict:
    """End the mute now."""
    _account, _session, _provider = resolve_current_leg(request, session_key)
    session_key = _session.key
    state = get_state(request)
    state.release_mute_lease(session_key)
    return _mute_payload(request, session_key)


# --- post-restart nudges (#453) -------------------------------------------
# Restarting the persistent runtime replaces every owned agent process, so a chat
# that wants to keep working on the freshly deployed code registers here first.
# The registry is a durable file the runtime consumes on boot, not web state: it
# has to survive the restart it is about.


def _nudge_registry(request: Request) -> RestartNudgeRegistry:
    settings = get_config(request).restart_nudge
    return RestartNudgeRegistry(settings.path, ttl_s=settings.ttl_s)


def _nudge_payload(request: Request, session_key: str) -> dict:
    registry = _nudge_registry(request)
    entry = next(
        (item for item in registry.entries() if item.session_key == session_key), None
    )
    return {
        "session_key": session_key,
        "nudge_on_restart": entry is not None,
        "registered_at": entry.registered_at if entry is not None else None,
        "message": NUDGE_MESSAGE if entry is not None else None,
    }


@router.get("/sessions/{session_key}/restart-nudge")
async def get_session_restart_nudge(request: Request, session_key: str) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    return _nudge_payload(request, session_key)


@router.put("/sessions/{session_key}/restart-nudge")
async def put_session_restart_nudge(request: Request, session_key: str) -> dict:
    """Register this chat for one nudge after the next runtime restart."""
    session_key, _session = resolve_chat_key(request, session_key)
    _nudge_registry(request).register(session_key)
    return _nudge_payload(request, session_key)


@router.delete("/sessions/{session_key}/restart-nudge")
async def delete_session_restart_nudge(request: Request, session_key: str) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    _nudge_registry(request).unregister(session_key)
    return _nudge_payload(request, session_key)


# --- wake on a GitHub event (#2775) ---------------------------------------
# An agent may not poll for a merge, a CI result or a deploy, so it registers the
# wait here and ends its turn; the webhook consumer in ``wake_on`` sends the chat
# one message when the event lands, or when the wait times out. A time wait (#2968)
# has no event: it wakes the chat at the time it names, for long local work.
# A needs-you wait (#3446) wakes the chat once per Work that moves into Needs answer or
# Ready for merge, and stays registered until it is cancelled or its week runs out.


class WakeOnRequest(BaseModel):
    pull_request: str | None = None
    review: str | None = None
    commit: str | None = None
    deployment: str | None = None
    deck_deploy: str | None = None
    at: str | None = None
    after_minutes: int | None = None
    # Works moving into Needs answer or Ready for merge (#3446): ``all`` or ``owner/repo``,
    # and ``only`` (``answer`` or ``merge``) for one of the two.
    needs_you: str | None = None
    only: str | None = None
    waiting_for: str | None = None
    name: str | None = None
    timeout_minutes: int | None = None


def _wake_on_service(request: Request):
    service = getattr(request.app.state, "wake_on", None)
    if service is None or not service.enabled:
        raise HTTPException(
            status_code=409,
            detail="this deck does not receive GitHub webhooks, so it cannot wake a chat on one",
        )
    return service


def _wake_on_payload(request: Request, session_key: str, service) -> dict:
    chat_id = get_state(request).chat_identity_key(session_key)
    return {
        "session_key": session_key,
        "waits": [item.payload() for item in service.registry.for_chat(chat_id)],
    }


@router.get("/sessions/{session_key}/wake-on")
async def get_session_wake_on(request: Request, session_key: str) -> dict:
    session_key, _session = resolve_chat_key(request, session_key)
    return _wake_on_payload(request, session_key, _wake_on_service(request))


@router.post("/sessions/{session_key}/wake-on")
async def post_session_wake_on(
    request: Request, session_key: str, body: WakeOnRequest
) -> dict:
    """Wake this chat once when the named GitHub event lands, its wait times out, or the
    time a time wait names comes."""
    session_key, _session = resolve_chat_key(request, session_key)
    service = _wake_on_service(request)
    try:
        fields = parse_request(**body.model_dump())
    except WakeRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    seen: dict = {}
    if fields.get("kind") == KIND_DEPLOYMENT:
        # The deploys the deck already stored for it (#3584), so the agent never reads the
        # GitHub deployments API, which the App cannot read in every organization. A success
        # for the named commit is already here: there is nothing to wait for.
        latest, done = await asyncio.to_thread(
            service.seen_deployments,
            fields["repository"],
            sha=fields.get("sha", ""),
            name=fields.get("name", ""),
        )
        seen = {"latest": [item.payload() for item in latest]}
        if done is not None:
            return {
                **_wake_on_payload(request, session_key, service),
                **seen,
                "already_happened": True,
                "deployment": done.payload(),
            }
    try:
        registered = service.register(
            session_key, get_state(request).chat_identity_key(session_key), **fields
        )
    except WakeRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        **_wake_on_payload(request, session_key, service),
        **seen,
        "registered": registered.payload(),
    }


@router.delete("/sessions/{session_key}/wake-on")
async def delete_session_wake_on(
    request: Request, session_key: str, id: str | None = None
) -> dict:
    """Cancel one wait by ``id``, or every wait this chat holds when none is given."""
    session_key, _session = resolve_chat_key(request, session_key)
    service = _wake_on_service(request)
    chat_id = get_state(request).chat_identity_key(session_key)
    held = [item.id for item in service.registry.for_chat(chat_id)]
    if id is not None and id not in held:
        raise HTTPException(status_code=404, detail="no such wait on this chat")
    service.registry.remove([id] if id is not None else held)
    return _wake_on_payload(request, session_key, service)


# --- ask for a fresh review of a pull request (#3183) ---------------------
# The deck reviews each ready head once by itself. When a person settled a finding without a
# code change, no push will start the next review, so the Work's agent asks for it here. The
# call also registers the chat's wait on the verdict, so its turn may end on it. With
# ``no_verdict`` it asks again because the last review ended without a verdict for the head
# (#3453): the agent does that without asking the owner, under a cap of its own.


#: The longest reason for asking again: it is quoted in the review request, not a document.
MAX_REVIEW_NOTE = 2000


class ReviewRequestBody(BaseModel):
    pull_request: str
    note: str | None = None
    timeout_minutes: int | None = None
    no_verdict: bool = False


@router.post("/sessions/{session_key}/review-request")
async def post_session_review_request(
    request: Request, session_key: str, body: ReviewRequestBody
) -> dict:
    """Review this chat's pull request at its current head again, and wait on the verdict."""
    from ..work_router import AgainBusy, AgainRefused

    session_key, _session = resolve_chat_key(request, session_key)
    ref = _pull_request_ref(body.pull_request)
    state = get_state(request)
    if ref.identity not in {held.identity for held in state.pull_requests_for(session_key)}:
        raise HTTPException(
            status_code=409,
            detail=f"this chat has not registered {ref.repository}#{ref.number};"
            " register it first (POST …/pull-requests)",
        )
    router_service = getattr(request.app.state, "work_router", None)
    if router_service is None or not router_service.enabled:
        raise HTTPException(
            status_code=409, detail="this deck does not route Work events, so it starts no review"
        )
    service = _wake_on_service(request)
    note = (body.note or "").strip()
    if not note:
        # The note is the only thing that tells the reviewer why it is asked again (#3344);
        # refused before any wait is registered.
        raise HTTPException(
            status_code=422,
            detail="say in the note what changed since the last review (a person's decision,"
            f" a posted walkthrough); at most {MAX_REVIEW_NOTE} characters",
        )
    if len(note) > MAX_REVIEW_NOTE:
        raise HTTPException(status_code=422, detail=f"note is at most {MAX_REVIEW_NOTE} characters")
    try:
        wait = parse_request(
            review=f"{ref.repository}#{ref.number}", timeout_minutes=body.timeout_minutes
        )
    except WakeRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # The wait first: a verdict that lands before it exists would find nothing to end, and the
    # wait would then sit until its timeout. A refused request takes back a wait it added.
    chat_id = state.chat_identity_key(session_key)
    held = {item.id: item for item in service.registry.for_chat(chat_id)}
    try:
        # Stamped with the ask, so a review chat opened before it that closes on its own
        # summary does not end the wait on the review asked for here (#3453).
        registered = service.register(session_key, chat_id, asked=True, **wait)
    except WakeRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        asked = await router_service.ask_again(
            ref.repository, ref.number, note, no_verdict=body.no_verdict
        )
    except AgainBusy as exc:
        # The review already asked for is the one this wait is for: kept, and the chat is
        # told to keep waiting rather than to ask a person (#3453 review).
        return {
            "session_key": session_key,
            "pull_request": f"{ref.repository}#{ref.number}",
            "outcome": "already asked",
            "detail": str(exc),
            "registered": registered.payload(),
        }
    except AgainRefused as exc:
        if registered.id not in held:
            service.registry.remove([registered.id])
        else:
            # The wait it renewed goes back as it was: nothing was asked for (#3453).
            service.registry.update(held[registered.id])
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"session_key": session_key, **asked, "registered": registered.payload()}


# --- deck-owned Claude workers -------------------------------------------
# Thin TCP-surface proxy to the runtime service (which owns worker processes
# over a Unix socket), so external callers — e.g. a board poller — drive
# workers without touching the socket. Keys are opaque and travel in the body.


_RETIRED_DELIVER_FIELDS = frozenset(
    {
        "phase_delivery_id",
        "expected_source_session_id",
        "expected_source_version",
        "dispatch_generation",
    }
)


class WorkerDeliverRequest(BaseModel):
    key: str
    message: str
    cwd: str | None = None
    fresh: bool = False
    # Mirror the runtime's DeliverRequest so the proxy doesn't silently drop
    # images / model / effort / permission_mode an external caller sends.
    images: list[str] = Field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    delivery_id: str | None = None
    # Trusted dispatchers may carry a person's explicit decision to spend past the ceiling.
    allow_over_budget: bool = False
    require_idle: bool = False
    # Active-turn steering must fail closed if the target turn ends before runtime delivery.
    require_active: bool = False
    expected_last_result_at: float | None = None
    context_policy: Literal["off", "auto"] = "off"
    previous_worker_summary: str | None = Field(default=None, max_length=32_000)
    # This public worker surface is for external machine dispatchers.
    delegated: bool = True
    role: SessionRole | None = None
    # Optional: a caller that already minted a token can pass its own. Left unset,
    # one is minted here, because a fresh worker cannot learn its key any other way.
    session_token: str | None = None
    # Optional: the chat this delivery continues, as a URL some earlier turn
    # published. A follow-up that lands on another account is a new leg of that
    # chat rather than a new chat beside it (#1267). Never forwarded to the
    # runtime: it is deck-side identity, not part of the delivery.
    continues_session_url: str | None = None
    # Optional: the card this worker is being sent to work, as `owner/repo#n`. It is
    # registered against the session that takes the delivery, so "which chats worked
    # this card" is answerable from the session itself rather than only from the chat
    # they were grouped into (#2127) — the grouping is the thing that failed in the
    # first place, so the relation must not depend on it.
    issue_ref: str | None = None
    # Frozen poller phase policy. The runtime writes it to the provider hook's
    # private session context before any command can execute.
    command_artifacts: dict[str, Any] | None = None
    # Which Work and Run this delivery belongs to (#2142). Forwarded to the runtime, which
    # freezes them on the delivery receipt; recorded on the ledger row and, once a session
    # takes the delivery, as that session's run binding. Never part of the prompt.
    work_id: str | None = Field(default=None, max_length=512)
    run_id: str | None = Field(default=None, max_length=256)

    @model_validator(mode="before")
    @classmethod
    def _no_retired_fields(cls, data: Any) -> Any:
        """Refuse the fields #2194 retired instead of silently dropping them.

        ``expected_source_*`` made this route refuse a worker that had changed. A caller still
        sending one is relying on a guard that no longer runs, so it is told, not ignored.
        """
        if isinstance(data, dict):
            retired = sorted(_RETIRED_DELIVER_FIELDS & data.keys())
            if retired:
                raise ValueError(f"no longer accepted (#2194): {', '.join(retired)}")
        return data

    @model_validator(mode="after")
    def _correlation_is_explicit(self) -> WorkerDeliverRequest:
        """Refuse a correlation that contradicts the rest of the request, before any I/O.

        Half a correlation, or a Work that is not the named card's canonical id, would bind a
        session to the wrong execution.
        """
        self.work_id = (self.work_id or "").strip() or None
        self.run_id = (self.run_id or "").strip() or None
        if (self.work_id is None) != (self.run_id is None):
            raise ValueError("work_id and run_id must be given together")
        if self.work_id is not None:
            # Only the canonical spelling is a Work id: another spelling of the same issue
            # would bind the same Work under a second id and never match its own receipt.
            kind, _, source = self.work_id.partition(":")
            source_ref = IssueRef.parse(source) if kind == "github_issue" else None
            if source_ref is None or work_id_for(source_ref) != self.work_id:
                raise ValueError("work_id is not a canonical Work id")
        ref = IssueRef.parse(self.issue_ref or "")
        if self.work_id is not None and ref is not None and work_id_for(ref) != self.work_id:
            raise ValueError("work_id is not the canonical id of issue_ref")
        return self

    @property
    def correlation(self) -> DeliveryCorrelation | None:
        return DeliveryCorrelation.of(self.work_id, self.run_id) if self.delivery_id else None


class WorkerKeyRequest(BaseModel):
    key: str


class ArtifactReadRequest(BaseModel):
    card: str = Field(min_length=1, max_length=512)
    stream: Literal["stdout", "stderr"] | None = None
    offset: int = Field(default=0, ge=0)
    max_bytes: int | None = Field(default=None, gt=0, le=1024 * 1024)


def _artifact_session_token(request: Request, session_key: str) -> str:
    token = next(
        (
            value
            for value, bound_session_key in get_state(request).session_tokens.items()
            if bound_session_key == session_key
        ),
        None,
    )
    if token is None:
        raise HTTPException(status_code=404, detail="session artifact access is unavailable")
    return token


class ContextDeliveryResolutionRequest(BaseModel):
    key: str
    delivery_id: str = Field(min_length=1, max_length=512)
    choice: Literal["accepted", "not_written", "abandoned"]
    rationale: str = Field(min_length=1, max_length=4_000)


@router.get("/workers/accounts/{provider}/{label}/workers")
async def keyed_workers(request: Request, provider: str, label: str) -> dict:
    return await _runtime_proxy(request, "GET", f"/workers/accounts/{provider}/{label}/workers")


@router.post("/sessions/{session_key}/command-artifacts/{artifact_id}/slice")
async def session_command_artifact_slice(
    request: Request,
    session_key: str,
    artifact_id: str,
    body: ArtifactReadRequest,
) -> dict:
    _account, session, _provider = resolve_session(request, session_key)
    session_token = _artifact_session_token(request, session.key)
    if body.stream is None or body.max_bytes is None:
        raise HTTPException(status_code=422, detail="stream and max_bytes are required")
    return await _runtime_proxy(
        request,
        "POST",
        f"/command-artifacts/{artifact_id}/slice",
        json={
            "session_key": session.key,
            "card": body.card,
            "session_token": session_token,
            "stream": body.stream,
            "offset": body.offset,
            "max_bytes": body.max_bytes,
        },
    )


@router.post("/sessions/{session_key}/command-artifacts/{artifact_id}/full")
async def session_command_artifact_full(
    request: Request,
    session_key: str,
    artifact_id: str,
    body: ArtifactReadRequest,
) -> dict:
    _account, session, _provider = resolve_session(request, session_key)
    session_token = _artifact_session_token(request, session.key)
    return await _runtime_proxy(
        request,
        "POST",
        f"/command-artifacts/{artifact_id}/full",
        json={
            "session_key": session.key,
            "card": body.card,
            "session_token": session_token,
        },
    )


@router.put("/workers/accounts/{provider}/{label}/context-delivery-resolution")
async def resolve_uncertain_context_delivery(
    request: Request,
    provider: str,
    label: str,
    body: ContextDeliveryResolutionRequest,
) -> dict:
    """Apply an exact, deliberate local resolution without writing to a provider."""
    state = get_state(request)
    account_key = f"{provider}:{label}"
    row = state.budget_db.get_context_delivery(
        provider, account_key, body.key, body.delivery_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="context delivery not found")
    runtime_result = await _runtime_proxy(
        request,
        "PUT",
        f"/workers/accounts/{provider}/{label}/context-delivery-resolution",
        json=body.model_dump(),
    )
    if not runtime_result.get("accepted"):
        raise HTTPException(
            status_code=409,
            detail=runtime_result.get("reason", "runtime refused context resolution"),
        )
    resolved = state.budget_db.resolve_context_delivery(
        row.action_id, body.delivery_id, body.choice, body.rationale
    )
    if resolved is None:
        raise HTTPException(
            status_code=409,
            detail="resolution conflicts with the delivery's current state or audit record",
        )
    session_key = resolved.target_session_key or resolved.source_session_key
    report = state.attention_report_for(session_key) if session_key else None
    if report is not None and body.delivery_id in report.detail:
        state.clear_attention_report(session_key)
    return {"context_delivery": jsonable_encoder(resolved)}


@router.get("/jobs")
async def poller_jobs(request: Request) -> dict:
    """Expose the runtime-owned job rows the poller reads verification evidence from (#2154)."""
    return await _runtime_proxy(request, "GET", "/jobs")


@router.get("/jobs/{job_id}")
async def poller_job(request: Request, job_id: str) -> dict:
    """Expose one runtime-owned job without duplicating its configured storage path."""
    return await _runtime_proxy(request, "GET", f"/jobs/{job_id}")


# The Poller no longer delivers here: it calls the runtime's `ensure_delivery` directly
# (#2144), and its phase fields are gone (#2194). This stays the external machine-dispatcher
# surface and the only home of web-side context maintenance until #2209 decides its fate.
@router.post("/workers/accounts/{provider}/{label}/deliver")
async def keyed_deliver(
    request: Request,
    provider: str,
    label: str,
    body: WorkerDeliverRequest,
) -> dict:
    """Keep a frontend deploy from cutting this delivery in half (#1889).

    A delivery persists its ledger fence and then awaits the runtime. Cancelled between
    those two points it can only be recorded `uncertain`, and at-most-once makes that
    verdict permanent: the instruction can never be sent again and the card needs a
    person. The web process's two-second stop grace was chosen for SSE streams, which
    cost nothing when they are cut, and a delivery was silently getting the same deal.

    So a stop that has already been signalled refuses the delivery before a row exists,
    with a reason the poller re-sends under the same delivery id, and a delivery already
    under way is shielded and waited for by `shutdown.drain` instead. Both ends replace
    an unknowable outcome with a known one; neither loosens the ledger.
    """
    if shutdown.is_shutting_down():
        return _context_refusal("host_shutting_down")
    return await shutdown.protect(
        str(body.delivery_id or "unfenced"),
        _keyed_deliver(request, provider, label, body),
    )


def _register_delivered_card(state, session_key: str, issue_ref: str | None) -> None:
    """Record the card this delivery is for against the session that took it (#2127).

    Registration is by chat, and it is additive: a card already held by any leg is not
    re-registered, and anything the worker registered for itself is kept. An
    unparseable reference registers nothing rather than guessing — a missing relation
    is recoverable, a wrong one points a reader at another repository's issue.
    """
    ref = IssueRef.parse(issue_ref or "")
    if ref is None:
        if issue_ref:
            log.warning(
                "worker delivery named card %r, which is not an owner/repo#n"
                " reference; the session keeps no durable card relation",
                issue_ref,
            )
        return
    state.add_issue(session_key, ref)


def _bind_delivered_run(state, session_key: str, body: WorkerDeliverRequest, result: dict) -> None:
    """Record that this session took part in the delivery's Run (#2142).

    Participation is decided once, by ``delivery_participated`` over the runtime's durable
    receipt, and the runtime reports that decision as ``run_participation`` together with the
    receipt's pair — on a replay, the pair first frozen. A runtime too old to report it falls
    back to the request's pair, and only when its answer passes the same rule on its own.
    """
    if "run_participation" in result:
        if result.get("run_participation") is not True:
            return
        correlation = DeliveryCorrelation.of(result.get("work_id"), result.get("run_id"))
    elif delivery_participated(result):
        correlation = body.correlation
    else:
        return
    if correlation is None:
        return
    state.bind_session_run(
        session_key,
        correlation.work_id,
        correlation.run_id,
        delivery_id=body.delivery_id,
    )


def _ledger_correlation(
    db, row: ContextDelivery, body: WorkerDeliverRequest, request_hash: str
) -> ContextDelivery | dict:
    """Hold a frozen ledger row to the correlation it was first given (#2142).

    Checked before anything can reach the runtime. A row naming another Work or Run
    refuses as ``delivery_correlation_conflict``; a row frozen before correlation existed
    adopts the incoming one without resending anything. A row about to be re-armed is left
    alone: it provably wrote nothing, and the re-arm freezes the retry's own correlation.
    """
    incoming = body.correlation
    if incoming is None or _should_rearm(row, request_hash):
        return row
    stored = DeliveryCorrelation.of(row.work_id, row.run_id)
    if stored == incoming:
        return row
    if stored is not None:
        return _context_refusal("delivery_correlation_conflict")
    if not db.adopt_context_correlation(row.action_id, incoming.work_id, incoming.run_id):
        return _context_refusal("context_ledger_failed")
    return dataclasses.replace(row, work_id=incoming.work_id, run_id=incoming.run_id)


async def _keyed_deliver(
    request: Request,
    provider: str,
    label: str,
    body: WorkerDeliverRequest,
) -> dict:
    """Deliver work to a keyed worker, continuing its chat when it moves account.

    A poller follow-up routinely lands on a different account from the turn it
    is continuing, because the account is chosen on headroom. That starts a new
    provider session, and before #1267 it also started a new *chat*: the same
    piece of work read as several unrelated conversations on the deck. When the
    dispatcher names the chat being continued, the successor leg is fenced
    against the current leg of that chat before the runtime is called, so the
    new session joins the chain the moment the provider names it.

    Two cases deliberately do not link. A delivery that resumes the very
    session it continues is not a switch at all, so its reservation is dropped
    rather than turned into a self-link; and a chat whose successor is already
    taken by a start that is still alive keeps it — the work still runs, on a
    chat of its own, because refusing a fork must not mean refusing the turn. A
    reservation whose start died is stepped over rather than honoured, exactly
    as the Continue button does (#1620), because a corpse of a fence would
    otherwise split the card permanently.
    """
    payload = body.model_dump()
    token = payload.get("session_token") or uuid.uuid4().hex
    payload["session_token"] = token
    continues = payload.pop("continues_session_url", None)
    context_policy = payload.pop("context_policy")
    previous_worker_summary = payload.pop("previous_worker_summary")
    state = get_state(request)
    source = state.current_leg_for_published_url(continues or "")
    context_row: ContextDelivery | None = None
    if context_policy == "auto" and body.delivery_id:
        prepared = await _prepare_context_delivery(
            request,
            provider,
            label,
            body,
            token=token,
            continues=continues,
            previous_worker_summary=previous_worker_summary,
        )
        if isinstance(prepared, dict):
            return prepared
        if prepared is not None:
            payload, context_row, source = prepared
            token = str(payload["session_token"])
    reservation = reserve_chat_successor(request, source, token) if source else None
    reserved = reservation in {"new", "reserved", "linked"}
    if source and not reserved:
        # An unlinked session used to leave no trace at all, which is how a card quietly
        # became two chats and stayed that way (#2127). Say so: the turn still runs, but
        # it runs as a chat of its own and a reader deserves to know why.
        log.warning(
            "worker delivery to %s:%s named chat leg %s but could not fence its"
            " successor (%s); this turn runs as its own chat instead of continuing that"
            " one",
            provider,
            label,
            source,
            reservation,
        )
    if context_row is not None and context_row.executed_action == "rollover" and not reserved:
        state.budget_db.mark_clean_failed(
            context_row.action_id,
            final_accepted=False,
            final_action="rejected",
            final_reason="successor_reservation_conflict",
            provider_write_state="not_started",
        )
        return _context_refusal("successor_reservation_conflict")
    try:
        artifact_mode = str((body.command_artifacts or {}).get("mode") or "off")
        runtime_operation = (
            "deliver-command-artifacts-v1"
            if artifact_mode in {"observe", "enforce"}
            else "deliver"
        )
        result = await _runtime_proxy(
            request,
            "POST",
            f"/workers/accounts/{provider}/{label}/{runtime_operation}",
            json=payload,
        )
    except HTTPException as exc:
        if reserved and context_row is None:
            state.release_chat_successor(token)
        if context_row is not None:
            if runtime_operation == "deliver-command-artifacts-v1" and exc.status_code == 404:
                # The runtime predates this route, so it never saw the delivery at
                # all. That is a clean refusal, not an uncertain provider write.
                state.budget_db.mark_clean_failed(
                    context_row.action_id,
                    final_accepted=False,
                    final_action="rejected",
                    final_reason="artifact_runtime_upgrade_required",
                    provider_write_state="not_started",
                )
            else:
                state.budget_db.mark_uncertain(
                    context_row.action_id,
                    provider_write_state="unknown",
                    delivery_stage="write_may_have_started",
                    error="runtime_transport",
                )
                _mark_irreducibly_uncertain(
                    state,
                    context_row,
                    ["runtime transport", "provider outcome unavailable"],
                )
        raise
    session_id = result.get("session_id") if isinstance(result, dict) else None
    if not session_id:
        write_state = str(result.get("provider_write_state") or "not_started")
        successor_created = bool(result.get("successor_created"))
        if reserved and (
            context_row is None
            or (not successor_created and write_state == "not_started")
        ):
            state.release_chat_successor(token)
        if context_row is not None:
            _settle_context_delivery(state.budget_db, context_row, result)
            if result.get("action") == "uncertain":
                _mark_irreducibly_uncertain(
                    state, context_row, ["runtime returned an uncertain provider write"]
                )
        return result
    session_key = f"{provider}:{label}:{session_id}"
    if context_row is not None and result.get("successor_created"):
        state.budget_db.mark_successor_created(context_row.action_id, session_key)
    if reserved and session_key == source:
        state.release_chat_successor(token)
    state.bind_session_token(token, session_key)
    _register_delivered_card(state, session_key, body.issue_ref)
    _bind_delivered_run(state, session_key, body, result)
    if (
        context_row is not None
        and context_row.executed_action == "rollover"
        and source
        and state.chat_identity_key(session_key) != state.chat_identity_key(source)
    ):
        result = {
            **result,
            "accepted": True,
            "action": "uncertain",
            "reason": "context_successor_link_failed",
            "provider_write_state": result.get("provider_write_state") or "unknown",
            "successor_created": True,
            "context_rollover": True,
        }
    if context_row is not None:
        _settle_context_delivery(state.budget_db, context_row, result)
        if result.get("action") == "uncertain":
            _mark_irreducibly_uncertain(
                state, context_row, ["runtime returned an uncertain provider write"]
            )
    return result


async def _prepare_context_delivery(
    request: Request,
    provider_id: str,
    label: str,
    body: WorkerDeliverRequest,
    *,
    token: str,
    continues: str | None,
    previous_worker_summary: str | None,
) -> tuple[dict, ContextDelivery, str | None] | dict | None:
    state = get_state(request)
    db = state.budget_db
    if db is None:
        return None
    config = get_config(request).context_management
    account_key = f"{provider_id}:{label}"
    request_hash = context_request_sha256(
        provider_id=provider_id,
        account_key=account_key,
        worker_key=body.key,
        message=body.message,
        cwd=body.cwd,
        fresh=body.fresh,
        image_count=len(body.images),
        model=body.model,
        effort=body.effort,
        permission_mode=body.permission_mode,
        allow_over_budget=body.allow_over_budget,
        require_idle=body.require_idle,
        require_active=body.require_active,
        expected_last_result_at=body.expected_last_result_at,
        role=body.role,
        delegated=body.delegated,
        caller_session_token=body.session_token,
        continues_session_url=continues,
        previous_worker_summary=previous_worker_summary,
        context_policy=body.context_policy,
        command_artifacts=body.command_artifacts,
    )
    existing = db.get_context_delivery(
        provider_id, account_key, body.key, str(body.delivery_id)
    )
    if existing is not None:
        # A refusal that provably wrote nothing is re-armed rather than replayed in two
        # cases. A re-rendered request is a fresh attempt at the same card, not a caller
        # asking the same question twice (#1491). And a refusal in `RE_DECIDED_REFUSALS`
        # is re-armed even when the request is byte-identical (#1560): the engine re-sends
        # exactly that request precisely *because* the answer was about the host and not
        # about the payload, so replaying it makes the card permanently undeliverable.
        # Every other row still keeps its frozen answer.
        rearming = _should_rearm(existing, request_hash)
        if existing.request_sha256 != request_hash and not rearming:
            return _context_refusal("delivery_id_conflict")
        settled = _ledger_correlation(db, existing, body, request_hash)
        if isinstance(settled, dict):
            return settled
        existing = settled
        if not rearming:
            replay = _replay_context_delivery(existing)
            if replay is not None:
                return replay
            if existing.status == "uncertain":
                return await _reconcile_uncertain_context_delivery(
                    request, state, existing
                )
            if existing.status in {
                "reserved",
                "context_pending",
                "successor_created",
                "message_pending",
            } or (existing.status == "clean_failed" and existing.retryable_same_delivery):
                return _resume_context_delivery(
                    db,
                    body,
                    existing,
                    token=token,
                    continued_source=state.current_leg_for_published_url(continues or ""),
                    use_frozen_source=True,
                )
    off_mode = existing is None and config.mode == "off"

    def frozen_off_delivery() -> tuple[dict, ContextDelivery, str | None] | dict | None:
        # A deployment with maintenance turned off still owes the caller a frozen
        # answer for its delivery ID. Without a row, the same ID retried after the
        # mode is turned up would be decided again and could become a compact or a
        # rollover the first attempt never did. Freeze the no-op instead, without
        # any runtime I/O: no snapshot, no handoff, and the ordinary unmodified
        # payload goes to the runtime.
        return _freeze_disabled_context_delivery(
            state,
            db,
            provider_id,
            account_key,
            body,
            request_hash=request_hash,
            token=token,
            continued_source=state.current_leg_for_published_url(continues or ""),
        )

    # Off mode used to return that frozen no-op outright, before asking the runtime
    # anything, so every row it wrote said "disabled" and nothing could say what
    # `enforce` would have done at the same boundary. It now takes the same snapshot
    # as the other modes and decides — a real `proposed_action`, `reason` and
    # occupancy — and executes nothing (#1666). What it must not do is give a
    # delivery a way to fail that it did not have before, so each refusal the decide
    # path can reach falls back to the frozen no-op instead.
    if not off_mode:
        # An unresolved compact or rollover fences the worker. Off mode never starts
        # one, and a row left pending by an earlier mode never fenced it either.
        pending = db.pending_context_delivery(provider_id, account_key, body.key)
        if pending is not None and pending.delivery_id != body.delivery_id:
            return _context_refusal("context_operation_pending")
    try:
        snapshot = await _runtime_proxy(
            request, "GET", f"/workers/accounts/{provider_id}/{label}/workers"
        )
    except HTTPException:
        if not off_mode:
            raise
        return frozen_off_delivery()
    worker = snapshot.get("workers", {}).get(body.key, {})
    runtime_last_result_at = worker.get("last_result_at") if worker else None
    if (
        body.expected_last_result_at is not None
        and runtime_last_result_at != body.expected_last_result_at
    ):
        if off_mode:
            # The runtime checks the same expectation on the delivery itself, so
            # freezing here leaves the outcome where off mode always left it.
            return frozen_off_delivery()
        return _context_refusal("worker_changed")
    source_session_id = worker.get("session_id") if isinstance(worker, dict) else None
    source_key = (
        f"{account_key}:{source_session_id}"
        if isinstance(source_session_id, str) and source_session_id
        else None
    )
    continued_source = state.current_leg_for_published_url(continues or "")
    source = state.sessions.get(source_key or "")
    # Which leg names the chat this delivery belongs to. The live worker's own leg
    # normally does, and the leg being continued stands in when the runtime has no
    # worker to name one — a revival, say. Without that fallback the row files under
    # the worker key and never appears in the chat's own
    # `/sessions/{key}/context-deliveries`, which is what the frozen off-mode row was
    # careful to avoid (#1488).
    #
    # It was provenance only until #1665, which also reads the chat's model preference
    # through it. The maintenance decision still does not: that reads the live worker's
    # session and nothing else.
    ledger_source_key = source_key or continued_source or ""
    ledger_source = state.sessions.get(ledger_source_key) if ledger_source_key else None
    ledger_source_id = str(ledger_source.session_id) if ledger_source is not None else ""
    lineage_supported = bool(source and (not continued_source or continued_source == source_key))
    # The operator's soft model choice for this logical chat. It is spent at a leg
    # boundary, so it is pending only while a *live* process would otherwise swallow
    # it: a dead or absent worker respawns or resumes with the model in the payload
    # below, which costs nothing and keeps the session's own history (#1665).
    #
    # Read through `ledger_source_key`, so a revival with no worker in the snapshot
    # still finds the preference through the leg it continues. `body.key` is the card
    # ref, not a session key, and asking for its chat identity would mint a chat that
    # is not this conversation — losing the preference in exactly the case this branch
    # exists to serve. No leg means no chat and so no preference to find.
    preferred_model = (
        db.chat_model_preference(state.chat_identity_key(ledger_source_key))
        if ledger_source_key
        else ""
    )
    model_change_pending = bool(
        preferred_model
        and worker.get("live")
        and worker.get("model") != preferred_model
    )
    capabilities = snapshot.get(SNAPSHOT_CAPABILITIES_KEY, [])
    runtime_supported = SNAPSHOT_CONTEXT_MANAGEMENT_V1 in capabilities
    previous = (
        db.latest_executed_context_cycle(
            provider_id, account_key, body.key, str(source_session_id)
        )
        if source_session_id
        else None
    )
    previous_delivery = (
        db.latest_context_delivery(
            provider_id, account_key, body.key, str(source_session_id)
        )
        if source_session_id
        else None
    )
    cycle = None
    if previous is not None:
        cycle = ContextCycle(
            ContextAction(previous.executed_action),
            MaintenanceResult(previous.maintenance_result)
            if previous.maintenance_result
            else None,
            previous.after_tokens,
            previous.run_id,
            previous.rearmed,
        )
    decision = (
        ContextDecision(
            ContextAction(existing.proposed_action),
            ContextAction(existing.executed_action),
            existing.reason,
        )
        if existing is not None
        else decide_context_action(
            ContextPolicyInput(
                config=config,
                provider_id=provider_id,
                context_tokens=source.context_tokens if source else None,
                delivery_id=body.delivery_id,
                run_id=body.run_id,
                previous_run_id=previous_delivery.run_id if previous_delivery else None,
                previous_cycle=cycle,
                context_current=state.session_scan_is_current(account_key),
                runtime_supported=runtime_supported,
                lineage_supported=lineage_supported,
                rollover_supported=provider_id in {"claude_code", "codex"},
                compact_supported=provider_id == "codex",
                require_active=body.require_active,
                fresh=body.fresh,
                image_count=len(body.images),
                active_delivery=bool(worker.get("turn_active")),
                pending_interaction=bool(worker.get("pending_interaction")),
                background_work=bool(worker.get("effective_activity"))
                and not bool(worker.get("turn_active")),
                model_change_pending=model_change_pending,
            )
        )
    )
    handoff = ""
    final_message = body.message
    if decision.executed_action is ContextAction.ROLLOVER and source is not None:
        workspace = await collect_workspace_handoff(body.cwd or source.cwd)
        recent: list[HandoffMessage] = []
        account = next((item for item in get_accounts(request) if item.key == account_key), None)
        provider = PROVIDERS.get(provider_id)
        if account is not None and provider is not None:
            try:
                events = await provider.recent_conversation(
                    account, source, limit=config.handoff_recent_messages
                )
                recent = [
                    HandoffMessage(event.role, event.text or "")
                    for event in events
                    if event.text
                ]
            except Exception as exc:  # optional evidence must not block rollover
                log.info("context handoff transcript omitted: %s", exc)
        try:
            handoff = render_context_handoff(
                HandoffInput(
                    why_continued=decision.reason,
                    logical_chat=state.chat_identity_key(source.key),
                    source_leg=source.key,
                    current_work=source.title or body.key,
                    workspace_snapshot=workspace.text
                    or workspace.diagnostic
                    or "workspace unavailable",
                    references=tuple(
                        [
                            *(item.url for item in source.issues),
                            *(item.url for item in source.pull_requests),
                        ]
                    ),
                    previous_summary=previous_worker_summary or "",
                    recent_messages=tuple(recent),
                    next_action=(
                        "Re-check the live checkout, then follow the current dispatched message."
                    ),
                ),
                max_chars=config.handoff_max_chars,
                recent_message_limit=config.handoff_recent_messages,
                previous_summary_limit=config.max_previous_summary_chars,
            )
        except ValueError as exc:
            log.warning("context handoff render failed: %s", exc)
            return _context_refusal("context_handoff_failed")
        # The only other two lines in this path are failures, so a rollover that worked
        # left no trace anywhere a person looks (#1667). Say the chat, the leg being
        # left and why, which is what "did this chat roll over, and when" needs.
        log.info(
            "context rollover: chat=%s source_leg=%s reason=%s handoff_chars=%d",
            state.chat_identity_key(source.key),
            source.key,
            decision.reason,
            len(handoff),
        )
        final_message = f"{handoff}\n\n{body.message}"
    final_hash = hashlib.sha256(final_message.encode()).hexdigest()
    effective_cwd = body.cwd or (str(source.cwd) if source and source.cwd else None)
    # The preference comes first on purpose: `worker.get("model")` is the *outgoing*
    # leg's model, so it would otherwise win every time and the successor would keep
    # the model the operator just asked it to leave (#1665).
    effective_model = (
        preferred_model
        or worker.get("model")
        or body.model
        or (source.model if source else None)
    )
    effective_effort = worker.get("effort") or body.effort
    effective_permission_mode = (
        worker.get("permission_mode")
        if "permission_mode" in worker
        else body.permission_mode
    )
    effective_role = worker.get("role") or body.role
    runtime_fingerprint = DeliveryReceipts.fingerprint(
        final_message,
        cwd=effective_cwd
        if decision.executed_action is ContextAction.ROLLOVER
        else body.cwd,
        fresh=decision.executed_action is ContextAction.ROLLOVER or body.fresh,
        images=body.images,
        model=effective_model
        if decision.executed_action is ContextAction.ROLLOVER
        else body.model,
        effort=effective_effort
        if decision.executed_action is ContextAction.ROLLOVER
        else body.effort,
        permission_mode=(
            effective_permission_mode
            if decision.executed_action is ContextAction.ROLLOVER
            else body.permission_mode
        ),
        allow_over_budget=body.allow_over_budget,
        role=effective_role
        if decision.executed_action is ContextAction.ROLLOVER
        else body.role,
        operation=(
            "context_rollover"
            if decision.executed_action is ContextAction.ROLLOVER
            else "context_maintenance"
            if decision.executed_action is ContextAction.COMPACT
            else None
        ),
        require_idle=(
            True
            if decision.executed_action is not ContextAction.NONE
            else body.require_idle
        ),
        require_active=body.require_active,
    )
    row = ContextDelivery(
        action_id=uuid.uuid4().hex,
        attempt=1,
        provider_id=provider_id,
        account_key=account_key,
        worker_key=body.key,
        delivery_id=str(body.delivery_id),
        request_sha256=request_hash,
        chat_id=state.chat_identity_key(ledger_source_key or body.key),
        source_session_key=ledger_source_key,
        source_session_id=str(source_session_id or ledger_source_id),
        mode=existing.mode if existing is not None else config.mode,
        proposed_action=decision.proposed_action.value,
        executed_action=decision.executed_action.value,
        reason=decision.reason,
        runtime_fingerprint=runtime_fingerprint,
        pre_write_cursor=str(source.last_event_seq) if source else None,
        source_last_event_seq=source.last_event_seq if source else None,
        expected_session_id=str(source_session_id) if source_session_id else None,
        expected_last_result_at=runtime_last_result_at,
        before_tokens=source.context_tokens if source else None,
        handoff_text=handoff or None,
        handoff_sha256=hashlib.sha256(handoff.encode()).hexdigest() if handoff else None,
        handoff_chars=len(handoff),
        effective_cwd=effective_cwd,
        effective_model=effective_model,
        effective_effort=effective_effort,
        effective_permission_mode=effective_permission_mode,
        effective_role=effective_role,
        effective_delegated=bool(worker.get("delegated", body.delegated)),
        preferred_model=preferred_model or None,
        session_token=token,
        final_payload_sha256=final_hash,
        work_id=body.work_id,
        run_id=body.run_id,
    )
    frozen = db.reserve_context_delivery(row)
    if frozen is None:
        return None
    if frozen.request_sha256 != request_hash and not _rearmable_clean_failure(frozen):
        return _context_refusal("delivery_id_conflict")
    settled = _ledger_correlation(db, frozen, body, request_hash)
    if isinstance(settled, dict):
        return settled
    frozen = settled
    if _should_rearm(frozen, request_hash):
        rearmed = _rearm_clean_failure(db, frozen, row)
        if rearmed is None:
            return _context_refusal("context_ledger_failed")
        frozen = rearmed
    if frozen.status == "cancelled" and frozen.error == "worker_changed":
        if not db.rearm_cancelled_context_delivery(frozen.action_id):
            return _context_refusal("worker_changed")
        if not db.refresh_rearmed_context_delivery(frozen.action_id, row):
            return _context_refusal("context_ledger_failed")
        frozen = db.get_context_delivery(
            provider_id, account_key, body.key, str(body.delivery_id)
        )
        if frozen is None:
            return _context_refusal("context_ledger_failed")
    replay = _replay_context_delivery(frozen)
    if replay is not None:
        return replay
    return _resume_context_delivery(
        db,
        body,
        frozen,
        token=token,
        continued_source=continued_source,
    )


def _resume_context_delivery(
    db,
    body: WorkerDeliverRequest,
    frozen: ContextDelivery,
    *,
    token: str,
    continued_source: str | None,
    use_frozen_source: bool = False,
) -> tuple[dict, ContextDelivery, str | None]:
    """Rebuild only the runtime payload frozen before the first provider write."""
    final_message = (
        f"{frozen.handoff_text}\n\n{body.message}" if frozen.handoff_text else body.message
    )
    payload = body.model_dump(
        exclude={
            "continues_session_url",
            "context_policy",
            "previous_worker_summary",
        }
    )
    payload.update(message=final_message, session_token=frozen.session_token or token)
    if frozen.executed_action != "none":
        payload.update(
            expected_session_id=frozen.expected_session_id,
            expected_last_result_at=frozen.expected_last_result_at,
            require_idle=True,
            context_maintenance=frozen.executed_action == "compact",
            context_rollover=frozen.executed_action == "rollover",
            fresh=frozen.executed_action == "rollover",
        )
        db.mark_context_pending(frozen.action_id)
    if frozen.executed_action == "rollover":
        payload.update(
            cwd=frozen.effective_cwd,
            model=frozen.effective_model,
            effort=frozen.effective_effort,
            permission_mode=frozen.effective_permission_mode,
            role=frozen.effective_role,
            delegated=frozen.effective_delegated,
        )
    if frozen.preferred_model:
        # The chat's preference as it stood when this delivery froze. A rollover has
        # already put the same value in `effective_model` above; this line is what
        # carries it on an ordinary respawn or resume, where the payload's model is
        # otherwise whatever the caller sent — for the poller, nothing at all (#1665).
        payload.update(model=frozen.preferred_model)
    successor_source = (
        frozen.source_session_key
        if use_frozen_source or frozen.executed_action == "rollover"
        else continued_source
    )
    return payload, frozen, successor_source


def _freeze_disabled_context_delivery(
    state,
    db,
    provider_id: str,
    account_key: str,
    body: WorkerDeliverRequest,
    *,
    request_hash: str,
    token: str,
    continued_source: str | None,
) -> tuple[dict, ContextDelivery, str | None] | dict | None:
    """Freeze one `auto` delivery as a durable no-op while maintenance is off.

    The row carries the same request hash as any other mode, so turning the mode
    up later cannot re-decide a delivery ID that has already been answered.

    Off mode never asks the runtime which session the worker is on, so the leg
    this delivery continues is the only provenance available before any I/O. It
    is enough: the row is filed under that leg's logical chat, which is the key
    `/sessions/{key}/context-deliveries` reads, so a frozen no-op still shows up
    in the chat's own diagnostics. With nothing to continue there is no chat to
    file it under and the worker key stands in, as it does everywhere else.
    """
    source_key = continued_source or ""
    source = state.sessions.get(source_key) if source_key else None
    payload = body.model_dump(
        exclude={
            "continues_session_url",
            "context_policy",
            "previous_worker_summary",
        }
    )
    row = ContextDelivery(
        action_id=uuid.uuid4().hex,
        attempt=1,
        provider_id=provider_id,
        account_key=account_key,
        worker_key=body.key,
        delivery_id=str(body.delivery_id),
        request_sha256=request_hash,
        chat_id=state.chat_identity_key(source_key or body.key),
        source_session_key=source_key,
        source_session_id=str(source.session_id) if source is not None else "",
        mode="off",
        proposed_action=ContextAction.NONE.value,
        executed_action=ContextAction.NONE.value,
        reason="disabled",
        runtime_fingerprint=DeliveryReceipts.fingerprint(
            body.message,
            cwd=body.cwd,
            fresh=body.fresh,
            images=body.images,
            model=body.model,
            effort=body.effort,
            permission_mode=body.permission_mode,
            allow_over_budget=body.allow_over_budget,
            role=body.role,
            require_idle=body.require_idle,
            require_active=body.require_active,
        ),
        pre_write_cursor=str(source.last_event_seq) if source else None,
        effective_cwd=body.cwd,
        effective_model=body.model,
        effective_effort=body.effort,
        effective_permission_mode=body.permission_mode,
        effective_role=body.role,
        effective_delegated=body.delegated,
        session_token=token,
        final_payload_sha256=hashlib.sha256(body.message.encode()).hexdigest(),
        work_id=body.work_id,
        run_id=body.run_id,
    )
    frozen = db.reserve_context_delivery(row)
    if frozen is None:
        return None
    if frozen.request_sha256 != request_hash and not _rearmable_clean_failure(frozen):
        return _context_refusal("delivery_id_conflict")
    settled = _ledger_correlation(db, frozen, body, request_hash)
    if isinstance(settled, dict):
        return settled
    frozen = settled
    if _should_rearm(frozen, request_hash):
        rearmed = _rearm_clean_failure(db, frozen, row)
        if rearmed is None:
            return _context_refusal("context_ledger_failed")
        frozen = rearmed
    replay = _replay_context_delivery(frozen)
    if replay is not None:
        return replay
    if frozen.executed_action != ContextAction.NONE.value:
        return _resume_context_delivery(
            db,
            body,
            frozen,
            token=token,
            continued_source=continued_source,
            use_frozen_source=True,
        )
    payload.update(session_token=frozen.session_token or token)
    return payload, frozen, continued_source


def _rearmable_clean_failure(row: ContextDelivery) -> bool:
    """True when this row's refusal provably wrote nothing, so its ID may be re-rendered.

    A poller retry re-renders its brief from current state and mints a fresh dispatch
    generation, so its request hash legitimately differs from the attempt before it.
    Frozen against a ``clean_failed`` row that never reached the provider, that difference
    used to read as ``delivery_id_conflict`` — and since the engine reuses one delivery ID
    for the whole card, an ``at_capacity`` refusal made that card permanently undeliverable
    and it was parked in *Blocked* (#1491).

    The conditions are the same ones ``rearm_clean_failed_context_delivery`` enforces in
    SQL, and they are what keeps at-most-once: a delivery whose outcome is unknown is
    ``uncertain``, never ``clean_failed``, and still conflicts here.

    ``retryable_same_delivery`` is excluded for a different reason (#1493): that row is a
    clean failure the *provider* asked to be resumed under this exact delivery, because
    context maintenance it began is still standing behind it. Re-rendering the request would
    throw that away, so the resume path keeps it.
    """
    return (
        row.status == "clean_failed"
        and row.provider_write_state == "not_started"
        and not row.successor_created
        and not row.retryable_same_delivery
        and row.target_session_key is None
        and not row.final_accepted
    )


def _should_rearm(row: ContextDelivery, request_hash: str) -> bool:
    """Whether this frozen row is re-armed under the retry's own request, not replayed.

    Two reasons, and the row must provably have written nothing for either. A re-rendered
    request is a genuinely new attempt at the same card (#1491). And a refusal in
    ``RE_DECIDED_REFUSALS`` is re-armed even when the request is byte-identical (#1560),
    because the engine re-sends exactly that request: its answer was about the host, not
    about the payload, so replaying it refuses the card forever on a stale verdict.
    """
    if not _rearmable_clean_failure(row):
        return False
    return row.request_sha256 != request_hash or row.final_reason in RE_DECIDED_REFUSALS


def _rearm_clean_failure(db, frozen: ContextDelivery, row: ContextDelivery):
    """Re-arm a clean failure under the retry's own request, or None if the ledger refused."""
    if not db.rearm_clean_failed_context_delivery(frozen.action_id):
        return None
    if not db.refresh_rearmed_context_delivery(frozen.action_id, row, request_hash=True):
        return None
    return db.get_context_delivery(
        row.provider_id, row.account_key, row.worker_key, row.delivery_id
    )


def _context_refusal(reason: str, *, write_state: str = "not_started") -> dict:
    return {
        "accepted": False,
        "action": "rejected",
        "reason": reason,
        "provider_write_state": write_state,
    }


def _replay_context_delivery(row: ContextDelivery) -> dict | None:
    # Re-deciding a host-transient refusal belongs to `_should_rearm`, which first
    # checks the row provably wrote nothing. Refusing to replay one here as well
    # would also catch the rows that are NOT provably unwritten — and those, with
    # no frozen answer to return, fall through to a fresh delivery (#2136).
    if row.status == "completed" or (
        row.status == "clean_failed" and not row.retryable_same_delivery
    ):
        payload = {
            "accepted": bool(row.final_accepted),
            "action": row.final_action,
            "reason": row.final_reason,
            "session_id": row.final_session_id,
        }
        if row.work_id and row.run_id:
            # A replay answers with the correlation the row was frozen under (#2142).
            payload.update(work_id=row.work_id, run_id=row.run_id)
        if row.executed_action != "none":
            payload.update(
                provider_write_state=row.provider_write_state,
                successor_created=row.successor_created,
                retryable_same_delivery=row.retryable_same_delivery,
                context_maintenance_status=row.maintenance_result,
                context_maintenance_reason=row.error,
                context_rollover=row.executed_action == "rollover",
            )
        return payload
    return None


async def _reconcile_uncertain_context_delivery(
    request: Request, state, row: ContextDelivery
) -> dict:
    query = urlencode({"key": row.worker_key, "delivery_id": row.delivery_id})
    checked = ["runtime receipt"]
    try:
        observed = await _runtime_proxy(
            request,
            "GET",
            f"/workers/accounts/{row.provider_id}/"
            f"{row.account_key.split(':', 1)[-1]}/delivery-receipt?{query}",
        )
    except HTTPException:
        observed = {}
    receipt = observed.get("receipt") if isinstance(observed, dict) else None
    if (
        isinstance(receipt, dict)
        and row.runtime_fingerprint
        and receipt.get("fingerprint")
        and receipt.get("fingerprint") != row.runtime_fingerprint
    ):
        receipt = None
        checked.append("runtime fingerprint mismatch")
    if isinstance(receipt, dict) and not receipt.get("action"):
        stage = str(receipt.get("delivery_stage") or "")
        if stage == "prepared_before_write":
            state.budget_db.mark_clean_failed(
                row.action_id,
                provider_write_state="not_started",
                retryable_same_delivery=True,
                delivery_stage=stage,
                evidence_checked=", ".join(checked),
                uncertainty_reason=None,
                final_accepted=False,
                final_action="rejected",
                final_reason="positive_no_write_proof",
            )
            _clear_context_delivery_attention(state, row)
            return _context_refusal("context_delivery_retryable")
        turn_id = receipt.get("provider_turn_id")
        if stage in {"provider_turn_identified", "provider_turn_completed"} and turn_id:
            result = {
                "accepted": True,
                "action": "accepted",
                "session_id": receipt.get("session_id"),
                "provider_write_state": "started",
                "delivery_stage": stage,
                "provider_turn_id": turn_id,
                "evidence_checked": ", ".join(checked),
            }
            _settle_context_delivery(state.budget_db, row, result)
            _clear_context_delivery_attention(state, row)
            return result
    if not isinstance(receipt, dict) or not receipt.get("action"):
        _mark_irreducibly_uncertain(state, row, checked)
        return _context_refusal("context_delivery_uncertain", write_state="unknown")
    result = {
        "accepted": bool(receipt.get("accepted", True)),
        "action": receipt.get("action"),
        "reason": receipt.get("reason"),
        "session_id": receipt.get("session_id"),
        "provider_write_state": receipt.get("provider_write_state", "started"),
        "successor_created": bool(receipt.get("successor_created")),
        "retryable_same_delivery": bool(receipt.get("retryable_same_delivery")),
        "context_maintenance_status": receipt.get("context_maintenance_status"),
        "context_maintenance_reason": receipt.get("context_maintenance_reason"),
        "context_rollover": bool(receipt.get("context_rollover")),
        "delivery_stage": receipt.get("delivery_stage") or "provider_turn_identified",
        "provider_turn_id": receipt.get("provider_turn_id"),
        "evidence_checked": ", ".join(checked),
    }
    session_id = result.get("session_id")
    if row.executed_action == "rollover" and isinstance(session_id, str) and session_id:
        session_key = f"{row.account_key}:{session_id}"
        state.budget_db.mark_successor_created(row.action_id, session_key)
        if row.session_token:
            reservation = state.reserve_chat_successor(
                row.session_token, row.source_session_key
            )
            if reservation not in {"new", "reserved", "linked"}:
                state.budget_db.mark_uncertain(
                    row.action_id,
                    provider_write_state="unknown",
                    error="context_successor_link_failed",
                )
                return _context_refusal(
                    "context_delivery_uncertain", write_state="unknown"
                )
            state.bind_session_token(row.session_token, session_key)
        if state.chat_identity_key(session_key) != state.chat_identity_key(
            row.source_session_key
        ):
            state.budget_db.mark_uncertain(
                row.action_id,
                provider_write_state="unknown",
                error="context_successor_link_failed",
            )
            return _context_refusal("context_delivery_uncertain", write_state="unknown")
    _settle_context_delivery(state.budget_db, row, result)
    _clear_context_delivery_attention(state, row)
    return result


def _mark_irreducibly_uncertain(
    state, row: ContextDelivery, checked: list[str]
) -> None:
    evidence = ", ".join(checked)
    state.budget_db.record_context_evidence(
        row.action_id,
        delivery_stage="irreducibly_uncertain",
        provider_write_state="unknown",
        evidence_checked=evidence,
        uncertainty_reason="No exact runtime receipt proves the outcome",
    )
    session_key = row.target_session_key or row.source_session_key
    if not session_key:
        return
    detail = (
        f"Context delivery {row.delivery_id} for {row.worker_key} remains fenced. "
        f"Chat {row.chat_id}; source {row.source_session_key or 'unknown'}; "
        f"possible target {row.target_session_key or 'unknown'}; stage irreducibly_uncertain. "
        f"Checked: {evidence}. Ambiguity: provider write may have begun. Safe actions: "
        "confirm accepted, prove not written for same-ID retry, or abandon without resending. "
        # The route that does this has existed since the fence did, and the CLI verb below is
        # its only caller -- but nothing ever said so where a person meets the parked card, so
        # in practice the supported way out was undiscoverable (#1889).
        f"Run: agentdeck resolve-context-delivery --provider {row.provider_id} --account "
        f"{row.account_key.split(':', 1)[-1]} --worker {row.worker_key} --delivery-id "
        f"{row.delivery_id} --choice <accepted|not_written|abandoned> --rationale '<why>'"
    )
    state.set_attention_report(session_key, "waiting", detail)


def _clear_context_delivery_attention(state, row: ContextDelivery) -> None:
    session_key = row.target_session_key or row.source_session_key
    report = state.attention_report_for(session_key) if session_key else None
    if report is not None and row.delivery_id in report.detail:
        state.clear_attention_report(session_key)


def _settle_context_delivery(db, row: ContextDelivery, result: dict) -> None:
    values = {
        "final_accepted": bool(result.get("accepted")),
        "final_action": result.get("action"),
        "final_reason": result.get("reason"),
        "final_session_id": result.get("session_id"),
        "provider_write_state": result.get("provider_write_state", "not_started"),
        "successor_created": bool(result.get("successor_created")),
        "retryable_same_delivery": bool(result.get("retryable_same_delivery")),
        "maintenance_result": result.get("context_maintenance_status"),
        "error": result.get("context_maintenance_reason"),
        "delivery_stage": result.get("delivery_stage")
        or (
            "write_may_have_started"
            if result.get("provider_write_state") in {"started", "unknown"}
            else row.delivery_stage
        ),
        "provider_turn_id": result.get("provider_turn_id"),
        "post_write_cursor": result.get("post_write_cursor"),
        "evidence_checked": result.get("evidence_checked"),
        "uncertainty_reason": result.get("uncertainty_reason"),
    }
    if (
        result.get("reason") == "worker_changed"
        and not result.get("successor_created")
        and result.get("provider_write_state", "not_started") == "not_started"
    ):
        db.mark_cancelled(row.action_id, "worker_changed")
    elif result.get("accepted") and result.get("action") != "uncertain":
        db.mark_completed(row.action_id, **values)
    elif result.get("provider_write_state") in {"started", "unknown"}:
        db.mark_uncertain(row.action_id, **values)
    else:
        db.mark_clean_failed(row.action_id, **values)


@router.post("/workers/accounts/{provider}/{label}/release")
async def keyed_release(
    request: Request, provider: str, label: str, body: WorkerKeyRequest
) -> dict:
    return await _runtime_proxy(
        request,
        "POST",
        f"/workers/accounts/{provider}/{label}/release",
        json=body.model_dump(),
    )


async def _runtime_proxy(
    request: Request, method: str, path: str, json: dict | None = None
) -> dict:
    client: httpx.AsyncClient | None = getattr(request.app.state, "runtime_http", None)
    if client is None:
        raise HTTPException(status_code=503, detail="runtime service not configured")
    try:
        response = await client.request(method, path, json=json)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503, detail=f"claude worker runtime unavailable: {exc}"
        ) from None
    if response.status_code >= 400:
        detail: object = response.text
        if response.headers.get("content-type", "").startswith("application/json"):
            detail = response.json().get("detail", detail)
        raise HTTPException(status_code=response.status_code, detail=detail)
    return response.json()


@router.get("/claude/accounts/{label}/workers")
async def claude_workers(request: Request, label: str) -> dict:
    return await _runtime_proxy(request, "GET", f"/claude/accounts/{label}/workers")


@router.post("/claude/accounts/{label}/deliver")
async def claude_deliver(request: Request, label: str, body: WorkerDeliverRequest) -> dict:
    """Deliver work to a Claude worker, and give a freshly spawned one an identity.

    A worker spawned from here has no ``AGENTDECK_SESSION_KEY``: its session id does not
    exist until the provider returns one, and injecting a guess would hand it somebody
    else's identity (#235). So the deck mints a token, which travels into the instructions
    the worker is started with, and binds it to the real key the moment the spawn reports
    that key back — the same exchange ``inject.py`` already performs for a started chat.

    Without this a poller-dispatched worker can never identify itself, and every session
    convention that needs a key — registering the PR it just opened, reporting that it
    needs a human — is unavailable to it for its entire life.
    """
    payload = body.model_dump()
    token = payload.get("session_token") or uuid.uuid4().hex
    payload["session_token"] = token
    # Deck-side identity, never part of a delivery. This legacy Claude-only route
    # predates chat linking and no dispatcher sends it here, but the request model
    # is shared, so drop the field rather than forward one the runtime never asked
    # for.
    payload.pop("continues_session_url", None)

    result = await _runtime_proxy(
        request, "POST", f"/claude/accounts/{label}/deliver", json=payload
    )

    session_id = result.get("session_id") if isinstance(result, dict) else None
    if session_id:
        # Binding a revived worker's token again is harmless and keeps the newest token
        # for a session valid, which is what an agent reading its own instructions holds.
        get_state(request).bind_session_token(token, f"claude_code:{label}:{session_id}")
    return result


@router.post("/claude/accounts/{label}/interrupt")
async def claude_interrupt(request: Request, label: str, body: WorkerKeyRequest) -> dict:
    return await _runtime_proxy(
        request, "POST", f"/claude/accounts/{label}/interrupt", json=body.model_dump()
    )


@router.post("/claude/accounts/{label}/stop")
async def claude_stop(request: Request, label: str, body: WorkerKeyRequest) -> dict:
    return await _runtime_proxy(
        request, "POST", f"/claude/accounts/{label}/stop", json=body.model_dump()
    )


@router.post("/claude/accounts/{label}/park")
async def claude_park(request: Request, label: str, body: WorkerKeyRequest) -> dict:
    return await _runtime_proxy(
        request, "POST", f"/claude/accounts/{label}/park", json=body.model_dump()
    )


@router.post("/claude/accounts/{label}/release")
async def claude_release(request: Request, label: str, body: WorkerKeyRequest) -> dict:
    return await _runtime_proxy(
        request, "POST", f"/claude/accounts/{label}/release", json=body.model_dump()
    )


@router.post("/claude/accounts/{label}/forget")
async def claude_forget(request: Request, label: str, body: WorkerKeyRequest) -> dict:
    return await _runtime_proxy(
        request, "POST", f"/claude/accounts/{label}/forget", json=body.model_dump()
    )


# Bounds for the issue/PR text prepended to a read-only delegate's brief (#787): how many
# referenced items are fetched, how many comments each contributes, and how much of each
# body survives. Deliberately generous — the delegate cannot fetch a truncated remainder.
_READ_ONLY_CONTEXT_ITEMS = 4
_READ_ONLY_CONTEXT_COMMENTS = 30
_READ_ONLY_CONTEXT_BODY_CHARS = 8_000


async def _reference_text(target: str) -> str:
    """One referenced issue or PR as readable text, or empty when it cannot be fetched."""
    gh = shutil.which("gh")
    if gh is None:
        return ""
    kind = "pr" if "/pull/" in target else "issue"
    try:
        code, stdout = await gh_cli.run(
            [gh, kind, "view", target, "--json", "title,body,comments"], timeout=20.0
        )
    except (OSError, TimeoutError):
        return ""
    if code != 0 or not stdout.strip():
        return ""
    try:
        data = json.loads(stdout)
    except ValueError:
        return ""
    lines = [
        f"### {target} — {str(data.get('title') or '').strip()}",
        "",
        str(data.get("body") or "").strip()[:_READ_ONLY_CONTEXT_BODY_CHARS],
    ]
    comments = data.get("comments") or []
    for comment in comments[:_READ_ONLY_CONTEXT_COMMENTS]:
        author = ((comment or {}).get("author") or {}).get("login") or "unknown"
        text = str((comment or {}).get("body") or "").strip()
        lines += ["", f"**@{author} commented:**", text[:_READ_ONLY_CONTEXT_BODY_CHARS]]
    if len(comments) > _READ_ONLY_CONTEXT_COMMENTS:
        lines += ["", f"({len(comments) - _READ_ONLY_CONTEXT_COMMENTS} more comments omitted)"]
    return "\n".join(lines)


async def _read_only_delegation_context(request: Request, parent_session: str | None) -> str:
    """The referenced issue/PR text a read-only Codex delegate cannot fetch itself (#787).

    A ``read-only`` Codex sandbox has no network at all — no ``gh``, no loopback — so the
    documented fallback is to hand the ticket text over in the brief. Doing it here makes
    that happen by default instead of by coordinator discipline: the deck already knows the
    parent session's issue link and registered pull requests, and fetches through the same
    credential fallback every other web-process ``gh`` call uses. Every failure degrades to
    "no context prepended", never to a refused delegation.
    """
    if not parent_session:
        return ""
    state = get_state(request)
    session = state.sessions.get(parent_session)
    targets: list[str] = []
    if session is not None:
        targets.extend(issue.url for issue in session.issues)
    for ref in state.pull_requests_for(parent_session):
        url = f"https://github.com/{ref.repository}/pull/{ref.number}"
        if url not in targets:
            targets.append(url)
    sections = []
    for target in targets[:_READ_ONLY_CONTEXT_ITEMS]:
        text = await _reference_text(target)
        if text:
            sections.append(text)
    if not sections:
        return ""
    return (
        "## Referenced work items (fetched for you — your read-only sandbox has no network,"
        " so do not try `gh` or the web for these)\n\n" + "\n\n---\n\n".join(sections)
    )


@router.post("/delegations", status_code=202)
async def start_delegation(request: Request, body: DelegationRequest) -> dict:
    account = delegation_account(request, body.account_key)
    provider = PROVIDERS[account.provider_id]
    if body.effort is not None and not provider.is_valid_effort(body.effort):
        raise HTTPException(status_code=422, detail="invalid effort")
    try:
        cwd = Path(body.cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=422, detail="invalid working directory") from None
    undeliverable = provider.user_message_refusal(body.message)
    if undeliverable is not None:
        # A brief the delegate's CLI would swallow starts a chat that answers a
        # task it never read, and the parent has no way to tell.
        raise HTTPException(status_code=422, detail=undeliverable)
    parent_session = _delegation_parent_session(request, body)
    message = body.message
    if body.sandbox == "read-only" and account.provider_id == "codex":
        # Claude's read-only mapping (plan mode) keeps its network, so only a Codex
        # delegate needs the referenced text handed over (#787). The caller's message is
        # validated at its own length: the prepended context is trimmed to what fits
        # rather than turning a previously-valid delegation into a refusal.
        context = await _read_only_delegation_context(request, parent_session)
        if context:
            limit = get_injector(request).config.max_message_chars
            room = len(context) if limit is None else limit - len(message) - 2
            if len(context) > room:
                context = context[: max(room - 22, 0)].rstrip()
                context = f"{context}\n\n(context truncated)" if context else ""
            if context:
                message = f"{context}\n\n{message}"
    result, delegation_id = await get_injector(request).start_delegation(
        account,
        provider,
        cwd,
        message,
        sandbox=body.sandbox,
        model=body.model,
        effort=body.effort,
        approval_policy=body.approval_policy,
        parent_session_id=parent_session,
    )
    if not result.accepted or delegation_id is None:
        raise HTTPException(status_code=422, detail=result.reason)
    return {
        "id": delegation_id,
        "state": "starting",
        "status_url": f"/api/delegations/{delegation_id}",
        "timeout_s": get_injector(request).config.delegation_timeout_s,
        # What was actually recorded as the parent, so a caller that named one
        # can tell that it did not stick instead of discovering that its work
        # did not roll up later. Null means this child stays top-level in session
        # history, while the delegated marker still keeps it out of Deckhand.
        "parent_session": parent_session,
    }


@router.get("/delegations/{delegation_id}")
async def delegation_status(request: Request, delegation_id: str) -> dict:
    status = get_injector(request).delegation_status(delegation_id)
    if status is None:
        raise HTTPException(status_code=404, detail="unknown delegation")

    state = status.state
    interaction = None
    session_url = None
    if status.session_key is not None:
        session_url = get_state(request).chat_path_for_session(status.session_key)
        session = get_state(request).sessions.get(status.session_key)
        if session is not None:
            interaction = pending_interaction_for(session, get_accounts(request))
            if interaction is not None and state == "running":
                state = "waiting"

    return {
        "id": status.id,
        "state": state,
        "account_key": status.account_key,
        "session_key": status.session_key,
        "session_url": session_url,
        "reason": status.reason,
        "final_message": status.final_message,
        "interaction": _interaction_payload(interaction),
    }


class HeavySlotRequest(BaseModel):
    """An ask for one of the host's heavy slots.

    Every field is optional: a caller that wants a slot on the default terms
    posts an empty body.
    """

    label: str = ""
    ttl_s: float | None = None
    wait_s: float = 0.0


@router.post("/heavy-slots")
async def acquire_heavy_slot(request: Request, body: HeavySlotRequest) -> dict:
    """Take a host-wide heavy slot, so this caller's heavy work waits its turn.

    For clients the deck did not spawn — a systemd service, a cron script, any
    process with no shim on its PATH and no ``PreToolUse`` hook — which is the
    only kind the command classifier cannot reach. The caller runs its own work
    and releases the lease afterwards; the deck never learns what ran.

    Answers 503 when every slot is busy, which is an ordinary answer and not a
    fault: back off and ask again, or pass ``wait_s`` to have the deck hold the
    request open for you.

    The lease **expires** unless heartbeated. A ``flock`` dies with the process
    holding it, so a killed run cannot strand a slot; an HTTP caller that
    vanishes can, and this host has one heavy slot to strand.
    """
    return await _runtime_proxy(request, "POST", "/heavy-slots", json=body.model_dump())


@router.get("/heavy-slots")
async def list_heavy_slots(request: Request) -> dict:
    """What is holding a heavy slot right now, and how many there are."""
    return await _runtime_proxy(request, "GET", "/heavy-slots")


@router.post("/heavy-slots/{lease_id}/heartbeat")
async def heartbeat_heavy_slot(request: Request, lease_id: str) -> dict:
    """Keep a lease alive. 404 once it is not held — expired, released, or the
    runtime restarted underneath it — so a caller can tell "still mine" from
    "someone else may be running now" and stop rather than carry on regardless.
    """
    return await _runtime_proxy(request, "POST", f"/heavy-slots/{lease_id}/heartbeat")


@router.delete("/heavy-slots/{lease_id}")
async def release_heavy_slot(request: Request, lease_id: str) -> dict:
    """Give the slot back. Releasing one that is already gone is not an error."""
    return await _runtime_proxy(request, "DELETE", f"/heavy-slots/{lease_id}")


class BrowserErrorReport(BaseModel):
    """One uncaught failure a page forwarded back to the deck.

    Everything is optional because a browser gives different shapes for a thrown
    error, a rejected promise, and a cross-origin script it refuses to describe.
    Field lengths are bounded when the report is recorded, not here, so a report
    that is too long is still recorded rather than rejected outright.
    """

    kind: str = "error"
    message: str = ""
    source: str = ""
    line: int | None = None
    column: int | None = None
    stack: str = ""
    page: str = ""


@router.post("/browser-errors", status_code=204)
async def report_browser_error(request: Request, body: BrowserErrorReport) -> Response:
    """Record an uncaught browser failure in the server's log (#1514).

    Without this the page is the one part of the deck whose failures leave no
    trace on the host at all: they land in a console nobody has open, usually on
    a phone. Recording is deliberately unconditional — the journal line is worth
    having whether or not external reporting is configured.
    """

    error_reporting.record_browser_error(
        kind=body.kind,
        message=body.message,
        source=body.source,
        line=body.line,
        column=body.column,
        stack=body.stack,
        page=body.page,
        user_agent=request.headers.get("user-agent", ""),
    )
    return Response(status_code=204)
