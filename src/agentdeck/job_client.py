"""Talking to the runtime's job queue from a shell (``agentdeck job``).

Synchronous and over the runtime's private Unix socket, like
``session_secrets.claim_runtime_secret``: the caller is a command line inside an
agent's turn, not part of the web process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

_TIMEOUT_S = 30.0


class JobClientError(RuntimeError):
    """The runtime's queue could not be reached, or refused the request."""


def _client(socket_path: Path) -> httpx.Client:
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=str(socket_path)),
        base_url="http://agentdeck-runtime",
        timeout=_TIMEOUT_S,
    )


def _call(socket_path: Path, method: str, path: str, **kwargs: Any) -> Any:
    try:
        with _client(socket_path) as client:
            response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise JobClientError(
            "the agentdeck runtime is not reachable; is agentdeck-codex.service running?"
        ) from exc
    if response.status_code == 404 and path == "/jobs":
        # The queue is either turned off or older than this CLI — a live runtime
        # keeps running across deploys, so "route not found" is a real state.
        raise JobClientError(
            "this runtime has no job queue: it is disabled, or it has not been "
            "restarted since the queue was deployed"
        )
    if response.status_code >= 400:
        raise JobClientError(_detail(response))
    try:
        return response.json()
    except ValueError as exc:
        raise JobClientError("the runtime returned an unreadable response") from exc


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"the runtime refused the request ({response.status_code})"
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return str(detail) if detail else f"the runtime refused the request ({response.status_code})"


def submit_job(
    socket_path: Path,
    *,
    command: list[str],
    cwd: str,
    lane: str = "local",
    lane_defaulted: bool = False,
    session_key: str | None = None,
    label: str = "",
    purpose: str = "",
    work_id: str = "",
    run_id: str = "",
    workspaces: list[str] | None = None,
    max_minutes: int | None = None,
) -> dict[str, Any]:
    return _call(
        socket_path,
        "POST",
        "/jobs",
        json={
            "command": command,
            "cwd": cwd,
            "lane": lane,
            "lane_defaulted": lane_defaulted,
            "session_key": session_key,
            "label": label,
            # Sent only when set, so a submission from no Run's chat is byte-for-byte what it
            # was before verification jobs existed (#2154). A Run's ordinary job carries its
            # Run too, so the poller knows its worker is waiting on it (#2622).
            **(
                {"purpose": purpose, "work_id": work_id, "run_id": run_id}
                if purpose or run_id
                else {}
            ),
            # Any job from a Work's chat names its Work, so a starred Work's jobs go first
            # (#3116); the queue keeps it as evidence only for the jobs above.
            **({"work_id": work_id} if work_id and not (purpose or run_id) else {}),
            **({"workspaces": workspaces} if workspaces else {}),
            **({"max_minutes": max_minutes} if max_minutes is not None else {}),
        },
    )


def get_job(socket_path: Path, job_id: str) -> dict[str, Any]:
    return _call(socket_path, "GET", f"/jobs/{job_id}")


def list_jobs(socket_path: Path) -> list[dict[str, Any]]:
    payload = _call(socket_path, "GET", "/jobs")
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    return jobs if isinstance(jobs, list) else []


def records_other_workspaces(socket_path: Path) -> bool:
    """Whether this runtime records the other workspaces a verification job names (#2148).

    A runtime keeps running across deploys, so the CLI can be newer than it. The evidence
    route shipped with the feature, so its presence is the answer; asked before submitting,
    so a runtime that would silently drop ``workspaces`` never gets the job at all.
    """
    try:
        with _client(socket_path) as client:
            response = client.get("/job-evidence", params={"work_id": "-"})
    except httpx.HTTPError as exc:
        raise JobClientError(
            "the agentdeck runtime is not reachable; is agentdeck-codex.service running?"
        ) from exc
    if response.status_code == 404:
        return False
    if response.status_code >= 400:
        raise JobClientError(_detail(response))
    return True


def cancel_job(socket_path: Path, job_id: str) -> dict[str, Any]:
    return _call(socket_path, "POST", f"/jobs/{job_id}/cancel")
