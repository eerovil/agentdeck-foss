"""Private Unix-socket client for bounded command artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class ArtifactClientError(RuntimeError):
    pass


def request_artifact(
    socket_path: Path, artifact_id: str, operation: str, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket_path)),
            base_url="http://agentdeck-runtime",
            timeout=30.0,
        ) as client:
            response = client.post(
                f"/command-artifacts/{artifact_id}/{operation}", json=payload
            )
    except httpx.HTTPError as exc:
        raise ArtifactClientError("the AgentDeck runtime is not reachable") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise ArtifactClientError(str(detail or f"request refused ({response.status_code})"))
    try:
        value = response.json()
    except ValueError as exc:
        raise ArtifactClientError("the runtime returned an unreadable response") from exc
    if not isinstance(value, dict):
        raise ArtifactClientError("the runtime returned an unreadable response")
    return value
