"""The public edition's stand-in for the Mac command-approval tool (#3623).

The tool runs a command on the author's Mac after a tap there; its Mac half is private,
so the public copy leaves it out and reports it as not set up.
"""

from __future__ import annotations

PUBLIC_STUB = True
BASE_URL_ENV = "AGENTDECK_MAC_RUNNER_URL"
TOKEN_ENV = "AGENTDECK_MAC_RUNNER_TOKEN"
CALLBACK_TOKEN_ENV = "AGENTDECK_MAC_RUNNER_CALLBACK_TOKEN"
STATE_ENV = "AGENTDECK_MAC_RUNNER_STATE"


def configured() -> bool:
    return False


def approval_url() -> str:
    return ""


def approval_link(text: str) -> str:
    return text


def note_handover(control_db, _session_key: str, action_id: str) -> None:
    return None


def session_instructions(session_token: str | None) -> str:
    return ""


async def waiting_count() -> int:
    return 0
